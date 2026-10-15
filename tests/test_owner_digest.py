import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cthulhu.gods import Azathoth
from cthulhu.runtime import TurnResult
from cthulhu.temple import Temple


class OwnerDigestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.temple = Temple({
            "secret": "test", "db": str(Path(self.tmp.name) / "temple.db"),
            "galaxies": [{"id": "test", "name": "测试", "token": "test"}],
            "gods": [{"id": "az", "role": "azathoth", "galaxy": "test"}],
        })
        self.addCleanup(self.temple.store.db.close)
        self.temple.route = AsyncMock(return_value={"id": "digest-test"})

    def task(self, task_id, state="running"):
        self.temple.store.index_task(task_id, "test", f"检查任务 {task_id}")
        self.temple.store.set_task(task_id, state=state)

    async def test_healthy_and_stalled_tasks_reach_owner_grouped_without_azathoth(self):
        self.task("t_ok")
        self.task("t_stuck")
        self.temple.store.add_digest("t_ok", "已完成 30 个有效步骤", False)
        self.temple.store.add_digest("t_stuck", "无增长：尚未提交作业", True)
        await self.temple.send_digest()
        turns = self.temple.store.turns("az")
        self.assertEqual(len(turns), 1)
        text = turns[0]["reply"]
        self.assertIn("### t_ok", text)
        self.assertIn("### t_stuck", text)
        self.assertIn("30 个有效步骤", text)
        self.assertIn("无增长", text)
        self.assertEqual(text.count("判断与操作：待阿撒托斯核实后补充。"), 2)
        self.assertFalse(self.temple.store.unsent_digest())
        self.assertIn("正常进展也必须汇报", self.temple.route.call_args.args[2])

    async def test_open_task_without_new_events_still_gets_each_periodic_report(self):
        self.task("t_silent")
        for _ in range(2):
            await self.temple.send_digest()
        turns = self.temple.store.turns("az")
        self.assertEqual(len(turns), 2)
        self.assertTrue(all("本轮未收到" in t["reply"] for t in turns))

    async def test_terminal_task_keeps_current_state_above_delayed_event(self):
        self.task("t_old", state="cancelled")
        self.temple.store.add_digest("t_old", "此前无增长", True)
        await self.temple.send_digest()
        text = self.temple.store.turns("az")[0]["reply"]
        self.assertIn("当前状态：cancelled", text)
        self.assertIn("历史事件，以当前状态为准", text)
        await self.temple.send_digest()
        self.assertEqual(len(self.temple.store.turns("az")), 1)

    async def test_global_alerts_and_no_work(self):
        await self.temple.send_digest()
        self.assertFalse(self.temple.store.turns("az"))
        self.temple.store.add_digest(None, "星系离线", True)
        await self.temple.send_digest()
        self.assertIn("### 全局事件", self.temple.store.turns("az")[0]["reply"])


class DigestResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = object.__new__(Azathoth)
        self.agent.reporting = None
        self.agent.latest = {}
        self.agent.last_turn = None
        self.agent.link = SimpleNamespace(call=AsyncMock())
        self.agent.render = Mock(return_value="按任务分类的事实汇总")
        self.agent.complete = Mock()
        self.agent.run_main = AsyncMock(return_value=TurnResult("completed", ""))
        self.batch = [{"id": "d1", "kind": "digest", "sender": "system", "text": "汇总"}]

    async def test_completed_reply_is_forwarded_when_tool_was_not_called(self):
        self.agent.run_main.return_value = TurnResult("completed", "任务 A：进展正常；判断：继续；操作：未调整。")
        await self.agent.internal(self.batch)
        call = self.agent.link.call.call_args
        self.assertEqual(call.args[0], "notify_owner")
        self.assertIn("操作：未调整", call.kwargs["text"])
        self.assertIsNone(self.agent.reporting)

    async def test_empty_and_failed_replies_never_silently_drop_digest(self):
        for result in (TurnResult("completed", ""), TurnResult("failed", "API error")):
            with self.subTest(status=result.status):
                self.agent.link.call.reset_mock()
                self.agent.run_main.return_value = result
                await self.agent.internal(self.batch)
                self.agent.link.call.assert_awaited_once()
                self.assertIn("判断与操作尚未回传", self.agent.link.call.call_args.kwargs["text"])
                self.assertNotIn("API error", self.agent.link.call.call_args.kwargs["text"])

    async def test_explicit_owner_report_is_not_sent_twice(self):
        async def run(_):
            await self.agent.tell_owner("任务 A：已核验，继续执行。")
            return TurnResult("completed", "")
        self.agent.run_main.side_effect = run
        await self.agent.internal(self.batch)
        self.agent.link.call.assert_awaited_once()

    async def test_failed_delivery_does_not_mark_report_as_delivered(self):
        self.agent.reporting = {"kind": "progress", "told": False}
        self.agent.link.call.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.agent.tell_owner("任务 A：操作失败。")
        self.assertFalse(self.agent.reporting["told"])

    async def test_non_digest_internal_message_does_not_force_duplicate_report(self):
        await self.agent.internal([{"id": "r1", "kind": "report", "sender": "system", "text": "状态"}])
        self.agent.link.call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
