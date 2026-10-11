"""End-to-end tests: real temple + galaxy + agent processes, fake codex."""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cthulhu.protocol import Link, agent_token  # noqa: E402
from cthulhu.store import Store  # noqa: E402
from cthulhu.temple import password_hash  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_codex.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Universe(unittest.TestCase):
    runtimes: dict = {}

    @classmethod
    def setUpClass(cls):
        os.chmod(FAKE, 0o755)
        cls.dir = Path(tempfile.mkdtemp())
        cls.port = free_port()
        cls.secret = secrets.token_hex(16)
        gtoken = secrets.token_hex(16)
        cls.ids = {"azathoth": "az", "shub": "sh", "yog": "yo", "ghroth": "gh"}
        temple = {"secret": cls.secret, "db": str(cls.dir / "temple.db"), "listen": ["127.0.0.1", cls.port],
                  "galaxies": [{"id": "g1", "name": "test", "token": gtoken}],
                  "gods": [{"id": v, "role": k, "galaxy": "g1"} for k, v in cls.ids.items()]}
        galaxy = {"id": "g1", "token": gtoken, "temple_ws": f"ws://127.0.0.1:{cls.port}/ws",
                  "state": str(cls.dir / "galaxy"), "codex": FAKE, "cwd": str(cls.dir), "patrol_interval": 2, "runtimes": cls.runtimes}
        (cls.dir / "temple.json").write_text(json.dumps(temple))
        (cls.dir / "galaxy.json").write_text(json.dumps(galaxy))
        salt = secrets.token_hex(16)
        Store(cls.dir / "temple.db").set_account("owner", salt, password_hash("pw", salt))
        py = str(ROOT / "venv" / "bin" / "python")
        env = os.environ | {"PYTHONPATH": str(ROOT), "CTHULHU_FAKE_CLAUDE": "1"}
        cls.temple = subprocess.Popen([py, "-m", "cthulhu", "temple", "--config", str(cls.dir / "temple.json")], env=env)
        time.sleep(1.5)
        cls.galaxy = subprocess.Popen([py, "-m", "cthulhu", "galaxy", "--config", str(cls.dir / "galaxy.json")], env=env)
        cls.cookie = cls.login()
        cls.wait(lambda: all(a["online"] for a in cls.api("directory")["agents"]), 30)

    @classmethod
    def tearDownClass(cls):
        cls.galaxy.terminate()
        cls.galaxy.wait(15)
        cls.temple.terminate()
        cls.temple.wait(5)

    @classmethod
    def login(cls):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/login", method="POST",
                                     data=json.dumps({"username": "owner", "password": "pw"}).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.headers["set-cookie"].split(";")[0]

    @classmethod
    def api(cls, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"cookie": cls.cookie, "x-cthulhu": "1", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    @staticmethod
    def wait(cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = cond()
                if v:
                    return v
            except Exception:
                pass
            time.sleep(0.2)
        raise AssertionError("timed out")

    def chat(self, to, text):
        msg = self.api("send", {"to": to, "text": text})
        return self.wait(lambda: next(t for t in self.api(f"chat?agent_id={to}") if t["id"] == msg["id"] and t["status"] == "done"))

    def test_owner_chat_and_memory(self):
        turn = self.chat("az", "你好阿撒托斯")
        self.assertIn("你好阿撒托斯", turn["reply"])
        mem = self.wait(lambda: self.api("memory?agent_id=az"))
        self.assertTrue(mem[0]["text"].startswith("记住"))

    def test_owner_cannot_reach_shub(self):
        r = self.api("send", {"to": "sh", "text": "hi"})
        self.assertEqual(r["status"], 403)

    def test_azathoth_messages_shub_and_shub_memory_only_from_azathoth(self):
        self.chat("az", 'CALL temple_send {"to": "sh", "text": "去建一个 old_one"}')
        msgs = self.wait(lambda: [m for m in self.api("messages?agent_id=sh") if m["sender"] == "az" and m["state"] == "handled"])
        self.assertEqual(msgs[0]["receiver"], "sh")
        self.wait(lambda: self.api("memory?agent_id=sh"))

    def test_matrix_denies_via_tool(self):
        # ghroth holds no tools; azathoth may not reach old_ones directly
        turn = self.chat("az", 'CALL temple_spawn {"name": "x", "galaxy_id": "g1", "task": "t", "skill": "s"}')
        self.assertIn("error", turn["reply"])

    def test_spawn_and_patrol(self):
        self.chat("az", 'CALL temple_send {"to": "sh", "text": "CALL temple_spawn {\\"name\\": \\"研究员\\", \\"galaxy_id\\": \\"g1\\", \\"task\\": \\"调研\\", \\"skill\\": \\"你是研究员\\"}"}')
        old = self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["role"] == "old_one" and a["online"]), 30)
        # The old_one starts on its task by itself.
        self.wait(lambda: self.api(f"latest?agent_id={old['id']}").get("last_status") == "completed")
        # yog's patrol eventually logs a summary
        self.wait(lambda: self.api("patrols"), 30)

    def test_impersonation_rejected(self):
        async def go():
            link = Link(f"ws://127.0.0.1:{self.port}/ws", {"kind": "agent", "id": "sh", "token": agent_token("wrong", "sh", 1)},
                        lambda w: asyncio.sleep(0), lambda f: asyncio.sleep(0))
            await asyncio.wait_for(link.run(), 10)
            return link.fatal
        self.assertIn("credential", asyncio.run(go()))

    def test_owner_event_stream(self):
        import websockets

        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{self.port}/api/events",
                                          additional_headers={"Cookie": self.cookie}, proxy=None) as ws:
                welcome = json.loads(await asyncio.wait_for(ws.recv(), 5))
                self.api("send", {"to": "gh", "text": "维护一下"})
                kinds = set()
                while "stream" not in kinds or "chat" not in kinds:
                    kinds.add(json.loads(await asyncio.wait_for(ws.recv(), 10))["type"])
                return welcome
        self.assertEqual(asyncio.run(go())["type"], "welcome")

    def test_unauthenticated_api(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/directory")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)


class RuntimeSwitching(Universe):
    """Fallback, live backend switching from settings, and cross-backend handoff."""
    runtimes = {"azathoth": {"backend": "claude", "model": "claude-broken",
                             "fallback": {"backend": "codex", "model": "gpt-6.1-sol"}}}

    def runtime_of(self, aid):
        return next(a for a in self.api("directory")["agents"] if a["id"] == aid)["runtime"]

    def test_fallback_then_switch_then_handoff(self):
        # Primary is broken: the turn is answered by the codex fallback, and the UI sees it.
        turn = self.chat("az", "第一句：我的代号是青蛙")
        self.assertIn("echo[codex]", turn["reply"])
        self.assertTrue(self.runtime_of("az")["fallback_active"])
        self.assertTrue(turn["model"].startswith("codex:"))
        # Owner switches azathoth to a healthy claude model in settings.
        r = self.api("runtimes", {"key": "azathoth", "config": {"backend": "claude", "model": "claude-opus-5-5"}})
        self.assertEqual(r["overrides"]["azathoth"]["model"], "claude-opus-5-5")
        turn = self.chat("az", "第二句")
        self.assertIn("echo[claude-opus-5-5]", turn["reply"])
        # The new claude session got the earlier codex exchange bridged in.
        self.assertIn("我的代号是青蛙", turn["reply"])
        self.assertEqual(self.runtime_of("az")["backend"], "claude")
        # Invalid configs are rejected.
        self.assertEqual(self.api("runtimes", {"key": "azathoth", "config": {"backend": "nope", "model": "x"}})["status"], 400)
        self.api("runtimes", {"key": "azathoth", "config": None})

    # The base suite's assertions don't depend on which backend answers.


class MixedUniverse(Universe):
    """Same suite with azathoth and ghroth on the Claude runtime."""
    runtimes = {"azathoth": {"backend": "claude", "model": "claude-opus-5-5", "memory_model": "claude-sonnet-5-5"},
                "ghroth": {"backend": "claude", "model": "claude-opus-5-5"}}


if __name__ == "__main__":
    unittest.main()
