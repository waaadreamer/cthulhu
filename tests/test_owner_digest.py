import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cthulhu.gods import Azathoth
from cthulhu.runtime import TurnResult
from cthulhu.store import Store
from cthulhu.temple import Temple
from cthulhu.task_display import short_name


class OwnerDigestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {"secret": "test", "db": str(Path(self.tmp.name) / "temple.db"),
            "galaxies": [{"id": "test", "name": "测试", "token": "test"}],
            "gods": [{"id": "az", "role": "azathoth", "galaxy": "test"}]}
        self.temple = Temple(self.config)
        self.addCleanup(lambda: self.temple.store.db.close())
        self.clock = patch("cthulhu.temple.time.time", return_value=12000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)

    def task(self, tid, name="部署界面修复", state="running"):
        self.temple.store.index_task(tid, "test", "完整任务描述，不应出现在侧边栏" * 30, name=name)
        self.temple.store.set_task(tid, state=state)

    def checkpoint(self, tid, summary="已完成30项验证", pace="ok", event="event1"):
        self.temple.store.add_digest(tid, "原始工具流水：不公开", pace != "ok", event,
            {"progress": {"summary": summary, "pace": pace, "at": self.now.return_value}})

    def turns(self):
        return self.temple.store.turns("az")

    async def test_healthy_and_stalled_are_separate_task_rows(self):
        self.task("t_ok")
        self.task("t_stuck", "训练性能")
        self.checkpoint("t_ok")
        self.checkpoint("t_stuck", "等待有效产物", "none", "event2")
        await self.temple.send_digest()
        rows = {t["task_id"]: t for t in self.turns()}
        self.assertEqual(len(rows), 2)
        self.assertIn("30项验证", rows["t_ok"]["reply"])
        self.assertIn("无增长", rows["t_stuck"]["reply"])
        self.assertNotIn("训练性能", rows["t_ok"]["reply"])
        self.assertNotIn("原始工具流水", str(self.turns()))
        self.assertFalse(self.temple.store.unsent_digest())
        self.assertEqual({m["task_id"] for m in self.temple.store.pending_for("az")}, {"t_ok", "t_stuck"})

    async def test_silent_task_each_period_but_not_each_retry(self):
        self.task("t")
        await asyncio.gather(*(self.temple.send_digest() for _ in range(5)))
        self.assertEqual(len(self.turns()), 1)
        self.now.return_value += 600
        await self.temple.send_digest()
        self.assertEqual(len(self.turns()), 2)
        self.assertTrue(all("未收到" in r["reply"] for r in self.turns()))

    async def test_late_management_fills_own_period_idempotently(self):
        self.task("t")
        await self.temple.send_digest()
        old = self.turns()[0]
        self.now.return_value += 600
        await self.temple.send_digest()
        for _ in range(2):
            await self.temple.note_owner("继续验证", task_id="t", source_message_id=old["id"])
        rows = self.turns()
        self.assertEqual(len(rows), 2)
        self.assertIn("继续验证", rows[0]["reply"])
        self.assertNotIn("继续验证", rows[1]["reply"])
        with self.assertRaises(Exception):
            await self.temple.note_owner("串任务", task_id="wrong", source_message_id=old["id"])

    async def test_late_event_after_snapshot_is_retained_for_next_period(self):
        self.task("t")
        await self.temple.send_digest()
        self.checkpoint("t")
        await self.temple.send_digest()
        self.assertEqual(len(self.temple.store.unsent_digest()), 1)
        self.now.return_value += 600
        await self.temple.send_digest()
        self.assertFalse(self.temple.store.unsent_digest())
        self.assertEqual(len(self.turns()), 2)

    async def test_terminal_state_and_reused_agent_keep_historical_identity(self):
        self.task("old", "训练性能", "cancelled")
        self.task("new", "部署界面修复")
        self.temple.store.set_roster("test", [{"id": "worker", "name": "旧日支配者1", "task_id": "new"}])
        self.checkpoint("old", "旧503与0步", "none")
        await self.temple.send_digest()
        old = next(t for t in self.turns() if t["task_id"] == "old")
        self.assertEqual(old["task_title"], "训练性能")
        self.assertIn("已结束", old["reply"])
        self.assertNotIn("旧503", old["reply"])
        self.assertEqual(self.temple.directory()["agents"][-1]["name"], "部署界面修复")
        self.temple.store.add_message("worker", "az", "历史消息", task_id="old")
        self.assertEqual(self.temple.store.messages()[-1]["task_title"], "训练性能")

    async def test_global_does_not_get_a_task(self):
        self.task("t")
        self.temple.store.add_digest(None, "全局告警", True)
        await self.temple.send_digest()
        self.assertEqual(sum(t["task_id"] is None for t in self.turns()), 1)
        self.assertNotIn("全局", next(t["reply"] for t in self.turns() if t["task_id"]))

    async def test_ai_failure_is_visible_without_raw_prompt_or_historical_reply(self):
        self.task("t")
        self.temple.store.set_task("t", summary="旧403，0步")
        self.checkpoint("t", "AI 总结不可用；probe 1→2")
        await self.temple.send_digest()
        text = self.turns()[0]["reply"]
        self.assertIn("AI 总结不可用", text)
        self.assertNotIn("旧403", text)

    async def test_queue_and_turn_survive_offline_restart_and_retry(self):
        self.task("t")
        self.checkpoint("t")
        self.temple.deliver = AsyncMock(side_effect=ConnectionError("offline"))
        with self.assertRaises(ConnectionError):
            await self.temple.send_digest()
        self.temple.store.db.close()
        self.temple = Temple(self.config)
        await self.temple.send_digest()
        self.assertEqual(len(self.turns()), 1)
        self.assertEqual(len(self.temple.store.pending_for("az")), 1)
        self.assertFalse(self.temple.store.unsent_digest())

    async def test_hub_event_replay_deduplicates_durable_digest(self):
        self.task("t")
        payload = {"task_id": "t", "text": "检查点", "event_id": "same-event", "kind": "checkpoint"}
        await self.temple.hub_report("test", payload)
        await self.temple.hub_report("test", payload)
        self.assertEqual(len(self.temple.store.unsent_digest()), 1)

    async def test_management_actual_success_and_failure_are_recorded_in_source_row(self):
        self.task("t")
        await self.temple.send_digest()
        source = self.turns()[0]["id"]
        conn = SimpleNamespace(id="az")
        self.temple.task_command = AsyncMock(return_value={"task_id": "t", "state": "running"})
        await self.temple.handle_agent_call(conn, "continue_task", {"task_id": "t", "instruction": "推进验证", "report_source": source})
        self.temple.task_command.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await self.temple.handle_agent_call(conn, "cancel_task", {"task_id": "t", "report_source": source})
        await self.temple.note_owner("下一步核实产物", task_id="t", source_message_id=source)
        self.assertEqual(len(self.turns()), 1)
        row = self.turns()[0]
        self.assertEqual(len(row["report"]["actions"]), 2)
        self.assertIn("状态 running", row["reply"])
        self.assertIn("取消任务：失败", row["reply"])
        self.assertEqual(row["report"]["actions"][0]["params"]["instruction"], "推进验证")

    async def test_cross_task_action_result_is_not_attached_to_source_task(self):
        self.task("a")
        self.task("b", "训练性能")
        await self.temple.send_digest()
        source = next(t["id"] for t in self.turns() if t["task_id"] == "a")
        self.temple.task_command = AsyncMock(return_value={"state": "cancelled"})
        await self.temple.handle_agent_call(SimpleNamespace(id="az"), "cancel_task", {"task_id": "b", "report_source": source})
        self.assertFalse(self.temple.store.turn(source)["report"]["actions"])
        self.assertTrue(any(t["task_id"] == "b" and t["report"]["actions"] for t in self.turns()))

    def test_name_is_immutable_and_backfills_existing_tasks(self):
        self.task("t", "部署界面修复")
        self.temple.store.set_task("t", short_name="错名")
        self.assertEqual(self.temple.store.task("t")["short_name"], "部署界面修复")
        self.temple.store.x("UPDATE task_index SET short_name=NULL,description='请修复部署界面按钮' WHERE id='t'")
        self.temple.store.db.close()
        self.temple = Temple(self.config)
        self.assertIn("部署界面", self.temple.store.task("t")["short_name"])
        self.assertEqual(short_name("完整任务" * 100, "28节点训练性能"), "28节点训练性能")


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
                self.assertIn("总结不可用", self.agent.link.call.call_args.kwargs["text"])
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
