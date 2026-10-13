"""Force-stop / resume of any agent (gods included): owner and local maintenance only."""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from test_universe import ROOT, HarnessServer
from cthulhu.roles import Denied, Role
from cthulhu.temple import Temple


class Access(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gods = [{"id": r.value, "role": r.value, "galaxy": "g1"} for r in (Role.AZATHOTH, Role.SHUB, Role.YOG, Role.GHROTH)]
        self.t = Temple({"secret": "s", "db": str(Path(self.tmp.name) / "db"),
                         "galaxies": [{"id": "g1", "name": "g1", "token": "t"}], "gods": gods})
        self.t.store.upsert_agent("old_a", "a", Role.OLD_ONE, "g1", "shub", {"task": "t"})

    async def asyncTearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    def conn(self, agent_id):
        from cthulhu.temple import Conn
        c = Conn(None, "agent", agent_id)
        self.t.agents[agent_id] = c
        return c

    async def test_no_agent_can_force_stop_or_resume(self):
        for who in ("azathoth", "shub", "yog", "ghroth"):
            for method in ("force_stop", "resume", "halt", "relaunch", "maintenance_stop", "maintenance_resume"):
                with self.assertRaises(Denied, msg=(who, method)):
                    await self.t.handle_agent_call(self.conn(who), method, {"agent_id": "ghroth"})
        # Shub's old_one tool still cannot reach management.
        for target in ("azathoth", "yog", "ghroth"):
            with self.assertRaisesRegex(Denied, "only its own old_ones"):
                await self.t.handle_agent_call(self.conn("shub"), "stop", {"agent_id": target})
        self.assertTrue(all(a["desired"] == "running" for a in self.t.store.agents()))

    async def test_shub_keeps_old_one_control_but_not_over_a_force_stop(self):
        shub = self.conn("shub")
        await self.t.handle_agent_call(shub, "stop", {"agent_id": "old_a"})
        await self.t.handle_agent_call(shub, "restart", {"agent_id": "old_a"})
        self.assertEqual(self.t.store.agent("old_a")["desired"], "running")
        await self.t.halt("old_a", "主人", force=True)
        with self.assertRaisesRegex(Denied, "force-stopped"):
            await self.t.handle_agent_call(shub, "restart", {"agent_id": "old_a"})
        await self.t.relaunch("old_a", "主人")
        self.assertEqual(self.t.store.agent("old_a")["desired"], "running")

    async def test_stopped_agent_rejects_and_cannot_act(self):
        self.t.store.add_message("azathoth", "ghroth", "queued before the stop")
        await self.t.halt("ghroth", "maintenance", force=True)
        self.assertEqual(self.t.store.pending_for("ghroth"), [])
        with self.assertRaisesRegex(Denied, "stopped"):
            await self.t.route("azathoth", "ghroth", "hi")
        with self.assertRaisesRegex(Denied, "stopped"):
            await self.t.owner_send("ghroth", "hi")
        with self.assertRaisesRegex(Denied, "stopped"):
            await self.t.handle_agent_call(self.conn("ghroth"), "send", {"to": "nyarlathotep", "text": "x"})
        self.assertEqual(self.t.public_agent(self.t.store.agent("ghroth"))["stopped_by"], "maintenance")
        # Persisted: a new temple on the same db keeps it stopped and never launches it.
        t2 = Temple(self.t.config)
        self.assertEqual(t2.store.agent("ghroth")["desired"], "stopped")
        self.assertNotIn("ghroth", [a["id"] for a in t2.store.agents("g1", include_stopped=False)])
        t2.store.db.close()


def agent_pids(scope: str, marker: str) -> set[int]:
    """Processes of this test universe whose command line contains marker, plus all their descendants."""
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True).stdout
    rows = [(int(p), int(pp), a) for p, pp, a in (l.strip().split(None, 2) for l in out.splitlines() if l.strip())]
    found = {p for p, _, a in rows if marker in a and scope in a}
    while True:
        more = {p for p, pp, _ in rows if pp in found} - found
        if not more:
            return found
        found |= more


def alive(pids):
    return {p for p in pids if os.path.exists(f"/proc/{p}") and "zombie" not in Path(f"/proc/{p}/status").read_text().lower()}


class ForceStop(HarnessServer):
    def turn(self, to, mid):
        return next(t for t in self.api(f"chat?agent_id={to}") if t["id"] == mid)

    def pids(self, marker):
        return agent_pids(str(self.dir), marker)

    def agent(self, aid):
        return next(a for a in self.api("directory")["agents"] if a["id"] == aid)

    def cli(self, *args):
        env = os.environ | {"PYTHONPATH": str(ROOT)}
        r = subprocess.run([str(ROOT / "venv" / "bin" / "python"), "-m", "cthulhu", *args, "--config", str(self.dir / "temple.json")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_stop_mid_turn_reject_persist_resume(self):
        # Ghroth is in a long turn with more work queued behind it.
        slow = self.api("send", {"to": "gh", "text": "SLEEP 120"})
        self.wait(lambda: self.turn("gh", slow["id"])["status"] == "running")
        queued = self.api("send", {"to": "gh", "text": "REPLY 不该在恢复后执行"})
        procs = self.pids('"id": "gh"')
        self.assertGreaterEqual(len(procs), 2, "agent and its model process")

        start = time.time()
        r = self.api("force_stop", {"agent_id": "gh"})
        self.assertEqual((r["desired"], r["stopped_by"]), ("stopped", "主人"))
        self.wait(lambda: not alive(procs), 15)
        self.assertLess(time.time() - start, 15)
        self.assertEqual(self.turn("gh", slow["id"])["status"], "interrupted")
        self.assertEqual(self.turn("gh", queued["id"])["status"], "interrupted")
        self.assertEqual({m["state"] for m in self.api("messages?agent_id=gh") if m["id"] in (slow["id"], queued["id"])}, {"cancelled"})

        # Rejected while stopped: the owner gets 403, an agent's send fails with a clear error.
        self.assertEqual(self.api("send", {"to": "gh", "text": "hi"})["status"], 403)
        reply = self.chat("az", 'CALL temple_send {"to": "gh", "text": "x"}')["reply"]
        self.assertIn("stopped", reply)
        self.assertFalse(self.agent("gh")["online"])

        # Maintenance CLI stops yog too.
        self.assertIn("stopped", self.cli("stop", "yo"))
        self.wait(lambda: not alive(self.pids('"id": "yo"')), 15)

        # The temple restarts: both stay stopped and the galaxy does not relaunch them.
        self.restart_temple()
        time.sleep(3)
        self.assertFalse(self.agent("gh")["online"] or self.agent("yo")["online"])
        self.assertFalse(self.pids('"id": "gh"') or self.pids('"id": "yo"'))
        self.assertEqual(self.agent("gh")["stopped_by"], "主人")

        # Resume: fresh start, the cancelled backlog does not run, new messages work.
        self.assertIn("running", self.cli("resume", "yo"))
        r = self.api("resume", {"agent_id": "gh"})
        self.assertEqual((r["desired"], r["stopped_by"]), ("running", None))
        self.wait(lambda: self.agent("gh")["online"] and self.agent("yo")["online"], 30)
        self.assertEqual(self.chat("gh", "REPLY 已恢复")["reply"], "已恢复")
        self.assertEqual(self.turn("gh", queued["id"])["status"], "interrupted")
        self.assertEqual(next(m for m in self.api("messages?agent_id=gh") if m["id"] == queued["id"])["state"], "cancelled")

    def test_force_stop_azathoth(self):
        self.api("send", {"to": "az", "text": "SLEEP 120"})
        self.wait(lambda: self.agent("az")["state"] == "working")
        procs = self.pids('"id": "az"')
        self.assertIn("stopped", self.cli("stop", "az"))
        self.wait(lambda: not alive(procs), 15)
        self.assertEqual(self.api("send", {"to": "az", "text": "hi"})["status"], 403)
        self.assertIn("running", self.cli("resume", "az"))
        self.wait(lambda: self.agent("az")["online"], 30)
        self.assertEqual(self.chat("az", "REPLY 回来了")["reply"], "回来了")

    @classmethod
    def restart_temple(cls):
        cls.temple.terminate()
        cls.temple.wait(10)
        env = os.environ | {"PYTHONPATH": str(ROOT), "CTHULHU_FAKE_CLAUDE": "1"}
        cls.temple = subprocess.Popen([str(ROOT / "venv" / "bin" / "python"), "-m", "cthulhu", "temple",
                                       "--config", str(cls.dir / "temple.json")], env=env)
        cls.cookie = cls.wait(cls.login, 30)
        cls.wait(lambda: next(a for a in cls.api("directory")["agents"] if a["id"] == "az")["online"], 30)


if __name__ == "__main__":
    unittest.main()
