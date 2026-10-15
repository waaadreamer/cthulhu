"""Message priority: queue order, who may send 10, owner default, and preemption by priority 10."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from cthulhu.agent import Agent
from cthulhu.roles import Denied, Role
from cthulhu.temple import OWNER_PRIORITY, Temple

try:
    from test_universe import HarnessServer
except ImportError:
    from tests.test_universe import HarnessServer


class PriorityRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gods = [{"id": r.value, "role": r.value, "galaxy": "g1"} for r in (Role.AZATHOTH, Role.NYARLATHOTEP)]
        self.t = Temple({"secret": "s", "db": str(Path(self.tmp.name) / "temple.db"),
                         "galaxies": [{"id": "g1", "name": "g", "token": "x"}], "gods": gods})

    def tearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    def route(self, *a, **kw):
        return asyncio.run(self.t.route(*a, **kw))

    def test_old_one_priority_cap_and_matrix(self):
        from cthulhu import roles
        with self.assertRaisesRegex(Denied, "at most priority 8"):
            roles.require_priority(Role.OLD_ONE, 9)
        roles.require_priority(Role.OLD_ONE, 8)
        roles.require_priority(Role.AZATHOTH, 10)
        with self.assertRaises(Denied):
            roles.require_priority(Role.AZATHOTH, 11)
        # Old_ones reach only old_ones; azathoth only through report/finish_task on the hub.
        self.assertFalse(roles.can_send(Role.OLD_ONE, Role.AZATHOTH))
        self.assertTrue(roles.can_send(Role.OLD_ONE, Role.OLD_ONE))
        self.assertTrue(roles.can_send(Role.AZATHOTH, Role.OLD_ONE))
        self.assertTrue(roles.can_send("owner", Role.OLD_ONE))

    def test_owner_default_and_urgent(self):
        self.assertEqual(OWNER_PRIORITY, 9)
        self.assertEqual(asyncio.run(self.t.owner_send("azathoth", "hi"))["priority"], 9)
        self.assertEqual(asyncio.run(self.t.owner_send("azathoth", "now", 10))["priority"], 10)

    def test_pending_ordered_by_priority_then_time(self):
        for text, p in [("a0", 0), ("b5", 5), ("c0", 0), ("d10", 10), ("e5", 5)]:
            self.route("azathoth", "nyarlathotep", text, priority=p)
            time.sleep(0.002)
        self.assertEqual([m["text"] for m in self.t.store.pending_for("nyarlathotep")], ["d10", "b5", "e5", "a0", "c0"])


class TakeBatch(unittest.TestCase):
    def agent(self, queue):
        a = Agent.__new__(Agent)
        a.queue = list(queue)
        return a

    def test_order_and_urgent_alone(self):
        q = [{"id": i, "priority": p, "created_at": t} for i, p, t in
             [("a", 0, 1), ("b", 5, 2), ("c", 5, 1.5), ("d", 9, 3)]]
        a = self.agent(q)
        self.assertEqual([m["id"] for m in a.take_batch()], ["d", "c", "b", "a"])
        a = self.agent(q + [{"id": "u", "priority": 10, "created_at": 4}])
        self.assertEqual([m["id"] for m in a.take_batch()], ["u"])
        self.assertEqual([m["id"] for m in a.take_batch()], ["d", "c", "b", "a"])


class Preemption(HarnessServer):
    """A priority-10 message interrupts a long turn at once; the interrupted message runs afterwards."""

    def turn(self, to, mid):
        return next(t for t in self.api(f"chat?agent_id={to}") if t["id"] == mid)

    def test_owner_urgent_interrupts_chat_and_requeues_it(self):
        slow = self.api("send", {"to": "gh", "text": "SLEEP 20"})
        self.assertEqual(slow["priority"], 9)
        self.wait(lambda: self.turn("gh", slow["id"])["status"] == "running")
        start = time.time()
        urgent = self.api("send", {"to": "gh", "text": "REPLY 加急已处理", "urgent": True})
        self.assertEqual(urgent["priority"], 10)
        done = self.wait(lambda: self.turn("gh", urgent["id"])["status"] == "done" and self.turn("gh", urgent["id"]), 10)
        self.assertLess(time.time() - start, 8)
        self.assertEqual(done["reply"], "加急已处理")
        # The interrupted message was not lost: it runs again after the urgent one and completes.
        self.assertIn(self.turn("gh", slow["id"])["status"], ("queued", "running"))
        again = self.wait(lambda: self.turn("gh", slow["id"])["status"] == "done" and self.turn("gh", slow["id"]), 40)
        self.assertEqual(again["reply"], "slept")
        self.wait(lambda: next(m for m in self.api("messages?agent_id=gh") if m["id"] == slow["id"])["state"] == "handled")

    def test_urgent_interrupts_agent_message_batch(self):
        # Azathoth hands nyarl a long job (priority 0); the owner's urgent message cuts in.
        self.api("send", {"to": "az", "text": 'CALL temple_send {"to": "gh", "text": "SLEEP 20"}'})
        job = self.wait(lambda: next(m for m in self.api("messages?agent_id=gh") if m["sender"] == "az"
                                     and m["text"] == "SLEEP 20" and m["state"] != "handled"), 60)
        self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["id"] == "gh")["state"] == "working")
        time.sleep(0.5)
        start = time.time()
        urgent = self.api("send", {"to": "gh", "text": "REPLY 先处理这个", "urgent": True})
        self.wait(lambda: self.turn("gh", urgent["id"])["status"] == "done", 10)
        self.assertLess(time.time() - start, 8)
        self.assertNotEqual(next(m for m in self.api("messages?agent_id=gh") if m["id"] == job["id"])["state"], "handled")
        self.wait(lambda: next(m for m in self.api("messages?agent_id=gh") if m["id"] == job["id"])["state"] == "handled", 40)
        self.assertEqual(self.api("latest?agent_id=gh")["last_reply"], "slept")


class ClaudePreemption(Preemption):
    """Same, with nyarl on the Claude runtime."""
    runtimes = {"nyarlathotep": {"backend": "claude", "model": "claude-opus-5-5"}}


if __name__ == "__main__":
    unittest.main()


class OldOneRuntimeDefaults(unittest.TestCase):
    """An old_one runs its own setting, else its galaxy's default, else the all-old_ones default, else temple config."""

    def test_official_default_is_only_for_old_ones_without_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            t = Temple({"secret": "s", "db": str(Path(root) / "db")})
            try:
                expected = {"backend": "galaxy_api", "provider": "ali", "model": "claude-opus-5-5-official"}
                for aid in ("new", "existing"):
                    self.assertEqual(t.runtime_for({"id": aid, "role": Role.OLD_ONE, "galaxy_id": "g"}), expected)
                self.assertIsNone(t.runtime_for({"id": "azathoth", "role": Role.AZATHOTH}))
                self.assertIsNone(t.runtime_for({"id": "nyarlathotep", "role": Role.NYARLATHOTEP}))
            finally:
                t.store.db.close()

    def test_precedence(self):
        tmp = tempfile.TemporaryDirectory()
        cfg = {"backend": "codex", "model": "cfg-model"}
        t = Temple({"secret": "s", "db": str(Path(tmp.name) / "t.db"), "old_one_runtime": cfg,
                    "galaxies": [{"id": "g1", "name": "a", "token": "x"}, {"id": "g2", "name": "b", "token": "y"}],
                    "gods": [{"id": "azathoth", "role": "azathoth", "galaxy": "g1"}]})
        try:
            t.store.set_roster("g1", [{"id": "o1", "name": "o1"}])
            t.store.set_roster("g2", [{"id": "o2", "name": "o2"}])
            run = lambda aid: t.runtime_for({"id": aid, "role": Role.OLD_ONE})["model"]
            self.assertEqual((run("o1"), run("o2")), ("cfg-model", "cfg-model"))
            asyncio.run(t.set_runtime("old_one", {"backend": "codex", "model": "gpt-6-luna"}))
            asyncio.run(t.set_runtime("galaxy:g1", {"backend": "codex", "model": "gpt-6.1-sol"}))
            self.assertEqual((run("o1"), run("o2")), ("gpt-6.1-sol", "gpt-6-luna"))
            asyncio.run(t.set_runtime("o1", {"backend": "codex", "model": "gpt-6-astra"}))
            self.assertEqual(run("o1"), "gpt-6-astra")
            # A new old_one (no roster row yet) gets its galaxy's default.
            self.assertEqual(t.runtime_for({"id": "new", "role": Role.OLD_ONE, "galaxy_id": "g1"})["model"], "gpt-6.1-sol")
        finally:
            t.store.db.close()
            tmp.cleanup()
