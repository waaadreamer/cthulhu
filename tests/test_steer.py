"""主人消息插入 Master 当前回合（turn/steer）与中断重投的不变量。用协议夹具与手工租约，不调用模型。"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cthulhu_v4.center import Center
from cthulhu_v4.runtime import make_runtime
from cthulhu_v4.util import LOCAL
from tests.test_runtime import FakeSink


class SteerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.c = Center(self.tmp.name, clock=lambda: self.now)
        self.set_features(['steer'])

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def set_features(self, features):
        n = self.c.store.get('node', LOCAL); n['features'] = features; self.c.store.put('node', n)

    def start_turn(self, sender='system'):
        """模拟中心已向本机节点派发一个 Master 回合。"""
        with self.c.store.atomic():
            first = self.c.enqueue(sender, 'master', '执行者进度', 8 if sender == 'system' else 1)
            batch = self.c.next_batch('master')
            j = {'id': 'turn_a', 'node': LOCAL, 'agent': 'master', 'message': first['id'], 'messages': [m['id'] for m in batch],
                 'state': 'running', 'until': self.now + 70, 'started': self.now}
            self.c.store.put('lease', j)
            self.c.set_messages(j['messages'], 'running')
        return j

    def lease(self):
        return self.c.store.get('lease', 'turn_a')

    def msg(self, mid):
        return self.c.store.get('message', mid)

    def sync_jobs(self):
        return self.c.hub.sync(LOCAL, {'instance': 'i', 'events': [], 'backends': ['demo'], 'have': ['turn_a']})['jobs']

    def report(self, kind, data):
        n = self.c.store.get('node', LOCAL)
        with self.c.store.atomic():
            self.c.hub.ingest(LOCAL, {'seq': n['ack'] + 1, 'job': 'turn_a', 'kind': kind, 'data': data})

    def test_owner_message_is_steered_into_running_turn_not_queued(self):
        self.start_turn()
        m = self.c.enqueue('owner', 'master', '先回答我', 9)
        self.assertEqual(self.msg(m['id'])['state'], 'running')
        self.assertIn(m['id'], self.lease()['messages'])
        [job] = self.sync_jobs()
        self.assertEqual([s['id'] for s in job['steer']], [m['id']])
        self.assertIn('先回答我', job['steer'][0]['text'])
        self.report('steer', {'message': m['id'], 'ok': True})
        self.assertEqual(self.lease()['steer'], [])
        self.assertNotIn('steer', self.sync_jobs()[0])                       # 确认后不再下发
        self.report('finished', {'outcome': 'completed', 'result': '答复'})
        self.assertEqual(self.msg(m['id'])['state'], 'handled')

    def test_failed_or_unconfirmed_steer_returns_message_to_queue(self):
        self.start_turn()
        a = self.c.enqueue('owner', 'master', '第一条', 9)
        self.report('steer', {'message': a['id'], 'ok': False, 'error': 'no active turn'})
        self.assertEqual(self.msg(a['id'])['state'], 'queued')
        self.assertNotIn(a['id'], self.lease()['messages'])
        b = self.c.enqueue('owner', 'master', '第二条', 9)                    # 回合在插入确认前就结束
        self.report('finished', {'outcome': 'completed', 'result': ''})
        self.assertEqual((self.msg(b['id'])['state'], self.msg(b['id'])['interrupted']), ('queued', True))
        self.assertEqual(self.c.next_message('master')['id'], a['id'])     # 主人的消息排在最前

    def test_no_steer_into_non_owner_feishu_turn_or_old_node(self):
        self.start_turn(sender='feishu')
        m = self.c.enqueue('owner', 'master', '主人的话', 9)
        self.assertEqual(self.msg(m['id'])['state'], 'queued')           # 不能让别人的回合变成“主人回合”
        self.report('finished', {'outcome': 'completed', 'result': ''})
        self.set_features([])
        self.c.store.put('lease', self.lease() | {'state': 'running', 'messages': [], 'id': 'turn_a'})
        m2 = self.c.enqueue('owner', 'master', '旧节点', 9)
        self.assertEqual(self.msg(m2['id'])['state'], 'queued')

    def test_interrupted_master_turn_requeues_owner_messages_with_bounded_retries(self):
        self.start_turn()
        m = self.c.enqueue('owner', 'master', '别丢我', 9)
        self.report('steer', {'message': m['id'], 'ok': True})
        for i in range(3):
            self.report('finished', {'outcome': 'unknown', 'error': '失联'})
            got = self.msg(m['id'])
            self.assertEqual((got['state'], got['interrupted'], got['retries']), ('queued', True, i + 1))
            self.assertIn('"interrupted": true', self.c.prompt('master', [got]))
            j = self.lease() | {'state': 'running', 'messages': [m['id']], 'steer': []}
            self.c.store.put('lease', j); self.c.set_message(m['id'], 'running')
            a = self.c.store.get('agent', 'master'); a['paused'] = False; self.c.store.put('agent', a)
        self.report('finished', {'outcome': 'unknown', 'error': '失联'})
        self.assertEqual(self.msg(m['id'])['state'], 'unknown')             # 三次之后不再循环重投

    def test_worker_messages_keep_old_unknown_semantics(self):
        j = self.start_turn()
        self.report('finished', {'outcome': 'unknown', 'error': '失联'})
        self.assertEqual(self.msg(j['message'])['state'], 'unknown')


class RuntimeSteerTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_steer_uses_active_turn_and_master_has_no_sleep_tool(self):
        sink = FakeSink(); launches = []
        agent = {'id': 'master', 'backend': 'codex', 'cwd': str(Path.cwd()), 'session': None, 'model': None, 'reasoning_effort': None}
        original = asyncio.create_subprocess_exec
        async def spawn(*argv, **kwargs):
            launches.append(argv)
            return await original(sys.executable, str(Path(__file__).with_name('fake_native.py')), 'codex', **kwargs)
        with patch('asyncio.create_subprocess_exec', spawn):
            rt = make_runtime(sink, agent)
            try:
                run = asyncio.create_task(rt.run('wait-steer'))
                await asyncio.wait_for(rt.steer('主人新消息'), 3)
                self.assertEqual(await asyncio.wait_for(run, 3), 'steered:主人新消息')
                with self.assertRaises(RuntimeError):                       # 回合已结束：拒绝插入，交由中心退回队列
                    await asyncio.wait_for(rt.steer('迟到'), 3)
            finally:
                await rt.close()
        self.assertIn('features.sleep_tool=false', launches[0])
        worker = make_runtime(sink, agent | {'id': 'old_x'})
        with patch('asyncio.create_subprocess_exec', spawn):
            try: await asyncio.wait_for(worker.run('one'), 3)
            finally: await worker.close()
        self.assertNotIn('features.sleep_tool=false', launches[1])        # 执行者可以等外部作业


if __name__ == '__main__':
    unittest.main()
