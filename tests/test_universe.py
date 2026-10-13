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
import urllib.parse
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


class HarnessServer(unittest.TestCase):
    runtimes: dict = {}
    patrol_interval = 2

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
                  "state": str(cls.dir / "galaxy"), "codex": FAKE, "cwd": str(cls.dir), "patrol_interval": cls.patrol_interval, "runtimes": cls.runtimes}
        (cls.dir / "temple.json").write_text(json.dumps(temple))
        (cls.dir / "galaxy.json").write_text(json.dumps(galaxy))
        salt = secrets.token_hex(16)
        Store(cls.dir / "temple.db").set_account("owner", salt, password_hash("pw", salt))
        py = str(ROOT / "venv" / "bin" / "python")
        env = os.environ | {"PYTHONPATH": str(ROOT), "CTHULHU_FAKE_CLAUDE": "1"}
        cls.temple = subprocess.Popen([py, "-m", "cthulhu", "temple", "--config", str(cls.dir / "temple.json")], env=env)
        cls.galaxy = subprocess.Popen([py, "-m", "cthulhu", "galaxy", "--config", str(cls.dir / "galaxy.json")], env=env)
        cls.cookie = cls.wait(cls.login, 30)
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

class Universe(HarnessServer):
    def test_owner_chat_and_memory(self):
        turn = self.chat("az", "你好阿撒托斯")
        self.assertIn("你好阿撒托斯", turn["reply"])
        mem = self.wait(lambda: self.api("memory?agent_id=az"))
        self.assertTrue(any(m["text"].startswith("记住") for m in mem))

    def test_owner_cannot_reach_shub(self):
        r = self.api("send", {"to": "sh", "text": "hi"})
        self.assertEqual(r["status"], 403)

    def test_azathoth_messages_shub_and_shub_memory_only_from_azathoth(self):
        self.chat("az", 'CALL temple_send {"to": "sh", "title": "建 old_one", "text": "去建一个 old_one"}')
        msgs = self.wait(lambda: [m for m in self.api("messages?agent_id=sh") if m["sender"] == "az" and m["state"] == "handled"])
        self.assertEqual(msgs[0]["receiver"], "sh")
        self.wait(lambda: self.api("memory?agent_id=sh"))

    def test_ghroth_memory_from_azathoth(self):
        self.chat("az", 'CALL temple_send {"to": "gh", "text": "长期规则：只读任务不做改动"}')
        self.wait(lambda: [m for m in self.api("messages?agent_id=gh") if m["sender"] == "az" and m["state"] == "handled"])
        self.wait(lambda: self.api("memory?agent_id=gh"))

    def test_message_arriving_mid_turn_is_steered_into_it(self):
        self.chat("az", 'CALL temple_send {"to": "sh", "text": "WAIT_STEER_FIXTURE first"}')
        first = self.wait(lambda: next((m for m in self.api("messages?agent_id=sh")
                                       if m['text'] == 'WAIT_STEER_FIXTURE first'), None))
        self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["id"] == "sh")["state"] == "working")
        self.chat("az", 'CALL temple_send ' + json.dumps({'to': 'sh', 'task_id': first['task_id'],
                                                        'priority': 5, 'text': 'STEER_SECOND'}))
        msgs = self.wait(lambda: [m for m in self.api("messages?agent_id=sh") if m["sender"] == "az" and m["receiver"] == "sh"
                                  and m["state"] == "handled" and ("WAIT_STEER_FIXTURE" in m["text"] or "STEER_SECOND" in m["text"])])
        latest = self.wait(lambda: (r := self.api("latest?agent_id=sh")).get("last_reply", "").startswith("steered:") and r)
        self.assertIn("STEER_SECOND", latest["last_reply"])      # the running turn received it
        self.wait(lambda: len([m for m in self.api("messages?agent_id=sh") if m["sender"] == "az" and m["state"] == "handled"
                               and ("WAIT_STEER_FIXTURE" in m["text"] or "STEER_SECOND" in m["text"])]) == 2)

    @staticmethod
    def via_shub(report: dict) -> str:
        """An owner prompt that makes azathoth send shub a task, and shub answer with `report` (tagged with that task)."""
        inner = "CALL temple_send " + json.dumps({"to": "az", "task_id": "$TASK", **report}, ensure_ascii=False)
        return "CALL temple_send " + json.dumps({"to": "sh", "title": "查磁盘", "text": inner}, ensure_ascii=False)

    def notes(self):
        return [t for t in self.api("chat?agent_id=az&limit=200") if not t["user_text"]]

    def test_task_progress_and_done_reports(self):
        tell = lambda s: "CALL tell_owner " + json.dumps({"text": s}, ensure_ascii=False)
        self.chat("az", self.via_shub({"status": "progress", "text": tell("步骤：已创建执行者")}))
        step = self.wait(lambda: next(t for t in self.notes() if t["reply"] == "步骤：已创建执行者"))
        self.assertEqual(step["kind"], "progress")
        self.assertEqual(step["task_title"], "查磁盘")
        self.assertEqual(step["task_state"], "open")
        # Follow-up on the same task; shub marks it done, so azathoth's note is the summary.
        follow = "CALL temple_send " + json.dumps({"to": "sh", "task_id": step["task_id"], "text": "CALL temple_send " + json.dumps(
            {"to": "az", "task_id": "$TASK", "status": "done", "text": tell("总结：磁盘剩 4.9G")}, ensure_ascii=False)}, ensure_ascii=False)
        self.chat("az", follow)
        final = self.wait(lambda: next(t for t in self.notes() if t["reply"] == "总结：磁盘剩 4.9G"))
        self.assertEqual((final["kind"], final["task_id"], final["task_state"]), ("summary", step["task_id"], "done"))
        # Azathoth forgets tell_owner: the program reminds it, then delivers the reply itself.
        self.chat("az", self.via_shub({"status": "done", "text": "磁盘查完了"}))
        self.wait(lambda: next(t for t in self.notes() if "tell_owner" in t["reply"] and t["kind"] == "summary"), 30)

    def test_shub_report_needs_task_status(self):
        turn = self.chat("az", "CALL temple_send " + json.dumps({"to": "sh", "title": "无状态", "text": "CALL temple_send " + json.dumps(
            {"to": "az", "task_id": "$TASK", "text": "没写状态"}, ensure_ascii=False)}, ensure_ascii=False))
        self.wait(lambda: next(m for m in self.api("messages?agent_id=sh") if "没写状态" in m["text"] and m["state"] == "handled"))
        self.assertFalse([m for m in self.api("messages?agent_id=az") if m["text"] == "没写状态"])

    def test_memory_revised_in_place_and_pushed_to_live_thread(self):
        self.chat("az", "记一下这个")
        mem = self.wait(lambda: self.api("memory?agent_id=az"))
        first = mem[0]
        time.sleep(1.1)
        mem = self.api("memory", {"agent_id": "az", "id": first["id"], "text": "S 集群已连接（截至今天）"})
        edited = next(m for m in mem if m["id"] == first["id"])
        self.assertEqual(edited["text"], "S 集群已连接（截至今天）")
        self.assertGreater(edited["updated_at"], edited["created_at"])
        turn = self.chat("az", "现在呢")
        self.assertIn("<memory_update>", turn["reply"])
        self.assertIn("S 集群已连接", turn["reply"])
        # Already delivered: the next turn carries no delta for it.
        turn = self.chat("az", "再问一次")
        self.assertNotIn("S 集群已连接", turn["reply"])

    def test_file_preview(self):
        f = self.dir / "work" / "报告.md"
        f.parent.mkdir(exist_ok=True)
        f.write_text("# 结果\n$E=mc^2$\n")
        r = self.api("file?path=" + urllib.parse.quote(str(f)))
        self.assertEqual((r["kind"], r["galaxy"]), ("markdown", "g1"))
        self.assertIn("E=mc^2", r["text"])
        d = self.api("file?path=" + urllib.parse.quote(str(f.parent)))
        self.assertEqual(d["kind"], "dir")
        self.assertIn("报告.md", [e["name"] for e in d["entries"]])
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/raw?path=" + urllib.parse.quote(str(f)),
                                     headers={"cookie": self.cookie})
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.read().decode(), f.read_text())
            self.assertEqual(resp.headers["content-security-policy"], "sandbox")
        self.assertEqual(self.api("file?path=/nonexistent/x.md")["status"], 404)
        self.assertEqual(self.api("file?path=relative.md")["status"], 404)

    def test_matrix_denies_via_tool(self):
        # ghroth holds no tools; azathoth may not reach old_ones directly
        turn = self.chat("az", 'CALL temple_spawn {"name": "x", "galaxy_id": "g1", "task": "t", "skill": "s"}')
        self.assertIn("error", turn["reply"])

    def test_temple_agents_lists_galaxies(self):
        turn = self.chat("az", "CALL temple_agents {}")
        self.assertIn('"galaxies"', turn["reply"])
        self.assertIn('"g1"', turn["reply"])

    def test_spawn_and_patrol(self):
        self.chat("az", 'CALL temple_send {"to": "sh", "title": "建研究员", "text": "CALL temple_spawn {\\"name\\": \\"研究员\\", \\"galaxy_id\\": \\"g1\\", \\"task\\": \\"调研\\", \\"skill\\": \\"你是研究员\\"}"}')
        old = self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["role"] == "old_one" and a["online"]), 30)
        # The old_one starts on its task by itself.
        self.wait(lambda: self.api(f"latest?agent_id={old['id']}").get("last_status") == "completed")
        # yog's patrol eventually logs a summary
        self.wait(lambda: self.api("patrols"), 30)

    def test_yog_always_reports_working_ghroth(self):
        # Ghroth answers no one; while it works, every patrol must reach azathoth (program-enforced).
        self.api("send", {"to": "gh", "text": "SLEEP 8"})
        msg = self.wait(lambda: next(m for m in self.api("messages?agent_id=az") if m["sender"] == "yo"
                                     and "Ghroth is working" in m["text"]), 20)
        self.assertIn("SLEEP 8", msg["text"])

    def test_latest_result_survives_restart(self):
        # Yog reads the last finished turn from the temple log, so a restart right after finishing loses nothing.
        self.chat("gh", "REPLY 部署完成")
        r = self.api("latest?agent_id=gh")
        self.assertEqual((r["last_reply"], r["last_status"]), ("部署完成", "completed"))
        self.assertTrue(r["finished_at"])

    def test_drain_with_queued_work_is_safe(self):
        # Regression: an agent that finished a batch with more work queued showed "working" forever while
        # draining, so the deploy never got a safe drain and every message waited behind it.
        async def go():
            from cthulhu.protocol import maintenance_token
            link = Link(f"ws://127.0.0.1:{self.port}/ws", {"kind": "maintenance", "id": "t",
                        "token": maintenance_token(self.secret)}, lambda w: asyncio.sleep(0), lambda f: asyncio.sleep(0))
            task = asyncio.create_task(link.run())
            try:
                await link.call("maintenance_drain", galaxy="g1", enabled=True, timeout=10)
                self.api("send", {"to": "gh", "text": "排空时到达的消息"})
                for _ in range(40):
                    st = await link.call("maintenance_drain", galaxy="g1", timeout=10)
                    if st["safe_to_restart"]:
                        return st
                    await asyncio.sleep(0.5)
                return st
            finally:
                await link.call("maintenance_drain", galaxy="g1", enabled=False, timeout=10)
                task.cancel()
        st = asyncio.run(go())
        self.assertTrue(st["safe_to_restart"], st)
        # Undrained: the queued message is handled.
        self.wait(lambda: any(t["user_text"] == "排空时到达的消息" and t["status"] == "done" for t in self.api("chat?agent_id=gh")))

    def test_owner_message_not_held_by_drain(self):
        async def go():
            from cthulhu.protocol import maintenance_token
            link = Link(f"ws://127.0.0.1:{self.port}/ws", {"kind": "maintenance", "id": "t2",
                        "token": maintenance_token(self.secret)}, lambda w: asyncio.sleep(0), lambda f: asyncio.sleep(0))
            task = asyncio.create_task(link.run())
            try:
                await link.call("maintenance_drain", galaxy="g1", enabled=True, timeout=10)
                turn = await asyncio.to_thread(self.chat, "gh", "排空中也要马上回答")
                return turn
            finally:
                await link.call("maintenance_drain", galaxy="g1", enabled=False, timeout=10)
                task.cancel()
        self.assertIn("排空中也要马上回答", asyncio.run(go())["reply"])

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
        for path in ("directory", "file?path=/etc/passwd", "raw?path=/etc/passwd"):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 401)


class RuntimeSwitching(Universe):
    """Fallback, live backend switching from settings, and cross-backend handoff."""
    runtimes = {"azathoth": {"backend": "claude", "model": "claude-broken",
                             "fallback": {"backend": "codex", "model": "gpt-6.1-sol"}}}

    def runtime_of(self, aid):
        return next(a for a in self.api("directory")["agents"] if a["id"] == aid)["runtime"]

    def test_runtime_change_applies_while_idle(self):
        # Shub is idle; a settings change shows up without waiting for its next message.
        self.api("runtimes", {"key": "shub", "config": {"backend": "codex", "model": "gpt-6-luna"}})
        self.wait(lambda: self.runtime_of("sh")["model"] == "gpt-6-luna", 20)
        self.api("runtimes", {"key": "shub", "config": None})
        self.wait(lambda: self.runtime_of("sh")["model"] != "gpt-6-luna", 20)

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
