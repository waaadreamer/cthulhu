"""Completion-driven supervision on the real isolated temple/galaxy test server."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from cthulhu.gods import Yog
from test_universe import HarnessServer


class CompletionServer(HarnessServer):
    patrol_interval = 300

    def test_completion_wakes_yog_before_periodic_deadline(self):
        # An ordinary agent finishing must not wake Yog.
        self.chat("az", "REPLY ordinary completion")
        time.sleep(1.2)
        self.assertEqual(self.api("patrols"), [])
        marker = "completion-trigger-verified"
        started = time.time()
        self.chat("gh", "REPLY " + marker)
        msg = self.wait(lambda: next(m for m in self.api("messages?agent_id=az")
                                    if m["sender"] == "yo" and marker in m["text"]), 10)
        self.assertIn("Ghroth finished a turn", msg["text"])
        patrol = self.wait(lambda: self.api("patrols"), 10)
        latency = msg["created_at"] - started
        self.assertLess(latency, 10)
        time.sleep(2)
        self.assertEqual(len(self.api("patrols")), len(patrol))
        self.assertEqual(len([m for m in self.api("messages?agent_id=az")
                              if m["sender"] == "yo" and marker in m["text"]]), 1)
        print("COMPLETION_SERVER " + json.dumps({"port": self.port, "state": str(self.dir),
              "periodic_interval": self.patrol_interval, "completion_to_report_seconds": latency,
              "report_id": msg["id"], "patrol_count": len(patrol)}), flush=True)


class PatrolScheduling(unittest.IsolatedAsyncioTestCase):
    def agent(self, interval=300):
        yog = object.__new__(Yog)
        yog.patrol_interval = interval
        yog.patrol_wake = asyncio.Event()
        yog.patrol_pending = False
        yog.draining = False
        yog.patrol = AsyncMock()
        return yog

    async def run_scheduler(self, yog):
        task = asyncio.create_task(yog.background())
        self.addAsyncCleanup(self.stop_scheduler, task)

    async def stop_scheduler(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_bursts_and_completion_during_patrol_are_serial(self):
        yog = self.agent()
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = calls = 0

        async def patrol():
            nonlocal active, maximum, calls
            active += 1
            calls += 1
            maximum = max(maximum, active)
            entered.set()
            await release.wait()
            active -= 1

        yog.patrol.side_effect = patrol
        await self.run_scheduler(yog)
        for _ in range(20):
            await yog._on_frame({"type": "patrol_requested"})
        await self.until(entered.is_set)
        for _ in range(20):
            await yog._on_frame({"type": "patrol_requested"})
        self.assertEqual(calls, 1)
        release.set()
        await self.until(lambda: calls == 2)
        await asyncio.sleep(.1)
        self.assertEqual(calls, 2)
        self.assertEqual(maximum, 1)

    async def test_drain_retains_trigger_until_resume(self):
        yog = self.agent()
        yog.draining = True
        yog.wake = asyncio.Event()
        yog.drain_status = lambda: {"draining": yog.draining}
        await self.run_scheduler(yog)
        await yog._on_frame({"type": "patrol_requested"})
        await asyncio.sleep(.05)
        yog.patrol.assert_not_awaited()
        self.assertTrue(yog.patrol_pending)
        await yog._on_frame({"type": "rpc", "method": "drain", "params": {"enabled": False}})
        await self.until(lambda: yog.patrol.await_count == 1)
        self.assertFalse(yog.patrol_pending)

    async def test_periodic_patrol_continues_after_failure(self):
        yog = self.agent(interval=.02)
        yog.patrol.side_effect = [RuntimeError("test failure"), None, None]
        with self.assertLogs("cthulhu.gods", level="ERROR"):
            await self.run_scheduler(yog)
            await self.until(lambda: yog.patrol.await_count >= 2)
        self.assertFalse(yog.patrol_busy)
