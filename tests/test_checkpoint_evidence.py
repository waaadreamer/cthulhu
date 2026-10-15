"""Recovery evidence must reach the existing checkpoint AI without crossing task bindings."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from cthulhu.hub import Hub


class CheckpointEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('cthulhu.hub.time.time', return_value=1200)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.hub = Hub(SimpleNamespace(state=self.tmp.name, config={}, link=SimpleNamespace(connected=asyncio.Event())))
        self.addCleanup(self.hub.store.db.close)
        self.hub.push_index = AsyncMock()
        self.worker = self.hub.store.add_old_one('旧日支配者1', {})['id']
        self.hub.store.add_task('t', '修复任务', '', 'running', self.worker, {
            'bound_at': 1200, 'events': [], 'samples': [{'stage': 1, 'value': 1}], 'probe_history': [],
            'probe': {'stage': 1, 'command': 'git rev-list --count baseline..HEAD', 'rationale': '新增提交', 'baseline': 1, 'verdict': 'approved'},
            'checkpoint': {'n': 1, 'probe_fail_streak': 0, 'stall_streak': 0}})

    async def test_failed_turn_then_new_report_live_and_commit_growth_reach_ai(self):
        self.now.return_value = 1210
        self.hub.store.log_turn(self.worker, 'galaxy_api', 'gpt-6-astra', 'thread', 'old prompt', '旧403且0步，没有实现', 'failed', 't')
        self.hub.store.log_turn(self.worker, 'galaxy_api', 'gpt-6-astra', 'thread', 'other task', '其他任务内容', 'completed', 'other')
        self.now.return_value = 1240
        await self.hub.report_up(self.worker, '已提交 f1baa00；17项专项验证通过', False)
        self.now.return_value = 1300
        self.hub.conns[self.worker] = object()
        self.hub.agent_rpc = AsyncMock(return_value={'current_task': 't', 'activity_task': 't', 'last_activity_at': 1280,
            'live': ['git commit f1baa00', '检查通过：17项'], 'last_reply': '旧403且0步', 'last_task': 't'})
        self.hub.run_probe = AsyncMock(return_value={'value': 2, 'error': None})
        self.hub.ai.judge_progress = AsyncMock(return_value={'pace': 'ok', 'summary': '新增提交且专项验证通过', 'probe_stale': False, 'note_to_azathoth': ''})
        await self.hub.checkpoint_running(self.hub.task('t'))
        payload = self.hub.ai.judge_progress.call_args.args[0]
        self.assertTrue(payload['grew_since_last'])
        self.assertEqual(payload['samples'], [1, 2])
        self.assertIn('已提交 f1baa00', payload['recent_work'])
        self.assertIn('git commit f1baa00', payload['recent_work'])
        self.assertNotIn('旧403', payload['recent_work'])
        self.assertNotIn('其他任务内容', payload['recent_work'])
        self.assertIn('失败/中断', payload['recent_work'])
        self.assertEqual(self.hub.task('t')['data']['evidence_at'], 1300)

    async def test_rpc_snapshot_time_is_included_without_widening_the_next_period(self):
        self.now.return_value = 1300
        self.hub.conns[self.worker] = object()
        async def latest(*args, **kwargs):
            self.now.return_value = 1301
            return {'current_task': 't', 'activity_task': 't', 'last_activity_at': 1301,
                    'live': ['RPC期间新增活动']}
        self.hub.agent_rpc = AsyncMock(side_effect=latest)
        self.hub.run_probe = AsyncMock(return_value={'value': 2, 'error': None})
        self.hub.ai.judge_progress = AsyncMock(return_value={'pace': 'ok', 'summary': '提交指标增长，继续核验', 'probe_stale': False, 'note_to_azathoth': ''})
        await self.hub.checkpoint_running(self.hub.task('t'))
        self.assertIn('RPC期间新增活动', self.hub.ai.judge_progress.call_args.args[0]['recent_work'])
        task = self.hub.task('t')
        self.assertEqual(task['data']['evidence_at'], 1301)
        text = self.hub.turn_digest(self.worker, task, {'current_task': 't', 'activity_task': 't',
            'last_activity_at': 1302, 'live': ['截止之后活动']}, until=1301)
        self.assertNotIn('截止之后活动', text)

    def test_reused_executor_stale_live_and_unattributed_old_turn_are_excluded(self):
        self.now.return_value = 1260
        self.hub.store.log_turn(self.worker, 'codex', 'fake', 'thread', 'old', '旧任务未完成', 'completed')
        self.hub.store.log_turn(self.worker, 'codex', 'fake', 'thread', 'other', '跨任务泄漏', 'completed', 'other')
        text = self.hub.turn_digest(self.worker, self.hub.task('t'), {
            'current_task': 'other', 'activity_task': 'other', 'last_activity_at': 1260, 'live': ['旧任务实时活动']})
        self.assertNotIn('旧任务', text)
        self.assertNotIn('跨任务', text)
        self.assertIn('没有可归属', text)

    async def test_checkpoint_ai_failure_preserves_probe_without_prompt_fallback(self):
        self.now.return_value = 1300
        self.hub.status[self.worker] = {'current': '原始系统prompt绝不能当总结'}
        self.hub.run_probe = AsyncMock(return_value={'value': 2, 'error': None})
        self.hub.ai.judge_progress = AsyncMock(side_effect=RuntimeError('AI down'))
        await self.hub.checkpoint_running(self.hub.task('t'))
        summary = self.hub.task('t')['data']['last_summary']
        self.assertIn('AI 总结不可用', summary)
        self.assertIn('1→2', summary)
        self.assertNotIn('prompt', summary)


if __name__ == '__main__':
    unittest.main()
