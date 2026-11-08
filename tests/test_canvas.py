"""Canvas 状态/调度不变量。命令票据使用真实 ExecutionService，不启动模型。"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cthulhu_v4.canvas import Canvases, WORKER_GRACE
from cthulhu_v4.center import Center
from cthulhu_v4.execution import FEATURE


class CanvasTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.c = Center(Path(self.tmp.name) / 'center', clock=lambda: self.now)
        self.api = Canvases(self.c)
        node = self.c.store.get('node', 'local'); node['features'] = [FEATURE]
        self.c.store.put('node', node)
        self.c.hub.links['local'] = SimpleNamespace(wake=lambda: None)

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def spec(self, **kw):
        return dict(name='汇总', html_template='<p>Canvas</p>', node_id='local', cwd=self.tmp.name,
                    script='printf sample', interval=10, timeout=3, task_ids=[]) | kw

    async def create(self, **kw):
        return await self.api.route('POST', '/api/canvas', {}, self.spec(**kw))

    async def tick(self, worker='one'):
        return await self.api.route('POST', '/api/canvas/tick', {}, {'worker': worker})

    def board(self, ident):
        return self.c.store.get('canvas', ident)

    def finish(self, ident, state='completed', stdout='{"experiments":[{"name":"a"},{"name":"b"}]}', **kw):
        board = self.board(ident)
        command = self.c.executions.get(board['inflight']['command_id'])
        command.update(state=state, stdout=stdout, stderr='', exit_code=0 if state == 'completed' else 1,
                       finished_at=self.now)
        command.update(kw)
        self.c.store.put('command', command)

    async def test_independent_creation_multi_task_and_validation(self):
        a = await self.create()
        b = await self.create(task_ids=['t_a', 't_b', 't_a'])
        self.assertEqual(a['task_ids'], [])
        self.assertEqual(b['task_ids'], ['t_a', 't_b'])
        self.assertEqual(self.c.store.all('task'), [])
        value = await self.api.route('GET', '/api/canvas', {}, {})
        self.assertEqual(len(value['canvases']), 2)
        self.assertNotIn('script', value['canvases'][0])
        for bad in ({'interval': True}, {'timeout': float('nan')}, {'cwd': 'relative'}, {'task_ids': 't_a'}):
            with self.assertRaises(ValueError): await self.create(**bad)

    async def test_concurrent_workers_and_response_retry_never_overlap_or_catch_up(self):
        board = await self.create()
        results = await asyncio.gather(self.tick('one'), self.tick('two'), self.tick('one'))
        self.assertEqual(sum(len(x['started']) for x in results), 1)
        first = self.board(board['id'])['inflight']['command_id']
        self.now += 100
        self.assertEqual((await self.tick())['started'], [])
        self.assertEqual(self.board(board['id'])['inflight']['command_id'], first)
        self.finish(board['id'])
        self.assertEqual((await self.tick())['started'], [])
        self.assertEqual(self.board(board['id'])['next_at'], self.now + 10)
        self.now += 9
        self.assertEqual((await self.tick())['started'], [])
        self.now += 1
        self.assertEqual(len((await self.tick())['started']), 1)
        self.assertEqual(len(self.c.store.all('command')), 2)

    async def test_last_success_survives_failure_text_and_offline_and_expiry_are_explicit(self):
        board = await self.create()
        self.assertEqual(board['status'], 'collector_offline')
        await self.tick(); self.finish(board['id']); await self.tick()
        data = self.api.data(self.board(board['id']))
        self.assertEqual(len(data['data']['experiments']), 2)
        self.assertTrue(data['fresh'])
        self.now += 10; await self.tick(); self.finish(board['id'], 'failed', stderr='boom'); await self.tick()
        data = self.api.data(self.board(board['id']))
        self.assertEqual(data['attempt']['stderr'], 'boom')
        self.assertEqual(data['status'], 'failed'); self.assertFalse(data['fresh'])
        self.assertEqual(len(data['data']['experiments']), 2)
        self.now += WORKER_GRACE + 10
        self.assertTrue(self.api.data(self.board(board['id']))['stale'])
        self.assertEqual(self.api.data(self.board(board['id']))['status'], 'collector_offline')
        self.c.hub.links.clear()
        self.assertEqual((await self.tick())['started'], [])
        self.assertEqual(self.api.data(self.board(board['id']))['status'], 'node_offline')
        self.c.hub.links['local'] = SimpleNamespace(wake=lambda: None)
        await self.tick(); self.finish(board['id'], stdout='<b>literal</b>'); await self.tick()
        data = self.api.data(self.board(board['id']))
        self.assertEqual(data['format'], 'text'); self.assertEqual(data['data'], '<b>literal</b>')

    async def test_edit_and_close_fence_late_results_without_stopping_external_work(self):
        board = await self.create()
        await self.tick()
        command_id = self.board(board['id'])['inflight']['command_id']
        edited = await self.api.route('POST', '/api/canvas/' + board['id'], {}, {'revision': 1, 'name': '新配置'})
        self.assertEqual(edited['revision'], 2)
        self.assertEqual((await self.tick())['started'], [])
        self.finish(board['id']); await self.tick()
        self.assertIsNone(self.board(board['id'])['last_success'])
        self.assertTrue(self.board(board['id'])['last_attempt']['discarded'])
        self.now += 10; await self.tick()
        second = self.board(board['id'])['inflight']['command_id']
        await self.api.route('POST', '/api/canvas/' + board['id'] + '/close', {}, {})
        self.assertEqual(self.c.executions.get(second)['state'], 'queued')
        self.finish(board['id']); await self.tick()
        self.assertIsNone(self.board(board['id'])['last_success'])
        self.assertTrue(self.board(board['id'])['last_attempt']['discarded'])
        self.now += 100; self.assertEqual((await self.tick())['started'], [])
        self.assertEqual(self.c.executions.get(command_id)['state'], 'completed')

    async def test_pause_resume_and_unknown_require_explicit_verification(self):
        board = await self.create()
        path = '/api/canvas/' + board['id']
        await self.api.route('POST', path + '/pause', {}, {})
        self.assertEqual((await self.tick())['started'], [])
        await self.api.route('POST', path + '/resume', {}, {})
        await self.tick(); self.finish(board['id'], 'unknown'); await self.tick()
        self.assertEqual(self.board(board['id'])['state'], 'paused')
        self.now += 100; self.assertEqual((await self.tick('restarted-worker'))['started'], [])
        with self.assertRaises(ValueError): await self.api.route('POST', path + '/resume', {}, {})
        await self.api.route('POST', path + '/resume', {}, {'acknowledge_unknown': True})
        self.assertEqual(len((await self.tick())['started']), 1)

    async def test_truncated_output_is_not_promoted_to_fresh_success(self):
        board = await self.create()
        await self.tick(); self.finish(board['id'], stdout='kept'); await self.tick()
        self.now += 10; await self.tick()
        self.finish(board['id'], stdout='partial', truncated=True); await self.tick()
        data = self.api.data(self.board(board['id']))
        self.assertEqual(data['data'], 'kept')
        self.assertTrue(data['attempt']['truncated'])
        self.assertEqual(data['status'], 'failed'); self.assertFalse(data['fresh'])

    async def test_worker_delayed_result_and_future_node_clock_do_not_refresh_old_data(self):
        board = await self.create()
        await self.tick()
        self.now += 100
        self.finish(board['id'], finished_at=self.now + 99999, received_at=self.now)
        await self.tick('worker-recovered')
        data = self.api.data(self.board(board['id']))
        self.assertEqual(data['sampled_at'], self.now)
        self.assertTrue(data['stale']); self.assertFalse(data['fresh'])

    async def test_nonstandard_json_numbers_remain_text_and_do_not_break_browser_json(self):
        board = await self.create()
        for stdout in ('{"value":NaN}', '{"value":Infinity}', '{"value":1e999}', '{"value":"\\ud800"}'):
            await self.tick(); self.finish(board['id'], stdout=stdout); await self.tick()
            data = self.api.data(self.board(board['id']))
            self.assertEqual(data['format'], 'text')
            self.assertEqual(data['data'], stdout)
            self.now += 10

    async def test_center_restart_keeps_inflight_and_never_replays_unknown_command(self):
        board = await self.create()
        await self.tick()
        original = self.board(board['id'])['inflight']['command_id']
        self.c.store.close()
        self.c = Center(Path(self.tmp.name) / 'center', clock=lambda: self.now)
        self.api = Canvases(self.c)
        self.c.hub.links['local'] = SimpleNamespace(wake=lambda: None)
        self.assertEqual(self.c.executions.get(original)['state'], 'unknown')
        self.assertEqual((await self.tick('new-worker'))['started'], [])
        self.assertTrue(self.board(board['id'])['blocked_unknown'])
        self.assertEqual(len(self.c.store.all('command')), 1)

    async def test_full_node_does_not_block_result_reconciliation_of_other_panels(self):
        boards = [await self.create(name=str(i)) for i in range(5)]
        self.assertEqual(len((await self.tick())['started']), 4)
        self.assertIn('4', self.board(boards[4]['id'])['last_attempt']['error'])
        self.finish(boards[0]['id']); await self.tick()
        self.assertIsNotNone(self.board(boards[0]['id'])['last_success'])

    async def test_failed_creation_rolls_back_canvas_and_execution_as_one_unit(self):
        board = await self.create()
        original = self.c.store
        put = original.put
        def fail(kind, obj):
            if kind == 'canvas' and obj.get('inflight'): raise RuntimeError('simulate commit boundary failure')
            return put(kind, obj)
        original.put = fail
        try:
            with self.assertRaises(RuntimeError): await self.tick()
        finally:
            original.put = put
        self.assertEqual(self.c.store.all('command'), [])
        self.assertIsNone(self.board(board['id'])['inflight'])
        self.assertEqual(len((await self.tick())['started']), 1)
