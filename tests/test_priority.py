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
        gods = [{"id": r.value, "role": r.value, "galaxy": "g1"} for r in (Role.AZATHOTH, Role.SHUB, Role.GHROTH)]
        self.t = Temple({"secret": "s", "db": str(Path(self.tmp.name) / "temple.db"),
                         "galaxies": [{"id": "g1", "name": "g", "token": "x"}], "gods": gods})
        self.t.store.upsert_agent("old_a", "a", Role.OLD_ONE, "g1", "shub", spec={})
        self.t.store.upsert_agent("old_b", "b", Role.OLD_ONE, "g1", "shub", spec={})

    def tearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    def route(self, *a, **kw):
        return asyncio.run(self.t.route(*a, **kw))

    def test_old_one_cannot_send_urgent(self):
        with self.assertRaisesRegex(Denied, "may not send priority 10"):
            self.route("old_a", "shub", "x", priority=10)
        with self.assertRaisesRegex(Denied, "may not send priority 10"):
            self.route("old_a", "old_b", "x", priority=10)
        self.assertEqual(self.route("old_a", "shub", "x", priority=9)["priority"], 9)
        self.assertEqual(self.route("shub", "old_a", "x", priority=10)["priority"], 10)
        with self.assertRaises(Denied):
            self.route("shub", "old_a", "x", priority=11)

    def test_owner_default_and_urgent(self):
        self.assertEqual(OWNER_PRIORITY, 9)
        self.assertEqual(asyncio.run(self.t.owner_send("azathoth", "hi"))["priority"], 9)
        self.assertEqual(asyncio.run(self.t.owner_send("azathoth", "now", 10))["priority"], 10)

    def test_pending_ordered_by_priority_then_time(self):
        for text, p in [("a0", 0), ("b5", 5), ("c0", 0), ("d10", 10), ("e5", 5)]:
            self.route("azathoth", "shub", text, priority=p)
            time.sleep(0.002)
        self.assertEqual([m["text"] for m in self.t.store.pending_for("shub")], ["d10", "b5", "e5", "a0", "c0"])


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
        # Azathoth hands ghroth a long job (priority 0); the owner's urgent message cuts in.
        # (Yog's reports quote "SLEEP", which can keep fake azathoth asleep for a while; hence the long wait.)
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
    """Same, with ghroth on the Claude runtime."""
    runtimes = {"ghroth": {"backend": "claude", "model": "claude-opus-5-5"}}


if __name__ == "__main__":
    unittest.main()
