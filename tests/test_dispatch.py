"""中心 → local 节点的统一派发路径：真实 Node、真实租约，不调用模型。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Harness, task_args
import cthulhu_v4.node as node_module


class Scripted:
    """可编排的运行时替身：只验证调度协议，不代表模型行为。"""
    seen, gates, closed = [], {}, []

    def __init__(self, sink, agent): self.sink = sink

    async def run(self, text):
        value = json.loads(text.split('\n', 1)[1])['message']['text']
        Scripted.seen.append(value)
        if value in Scripted.gates: await Scripted.gates[value].wait()
        if value == 'boom': raise RuntimeError('broken')
        return 'done:' + value

    async def close(self): Scripted.closed.append(True)


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.h = Harness(self.path); self.c = self.h.c
        Scripted.seen, Scripted.gates, Scripted.closed = [], {}, []

    async def asyncTearDown(self):
        await self.h.close(); self.tmp.cleanup()

    def scripted(self):
        return patch.object(node_module, 'make_runtime', lambda sink, a: Scripted(sink, a))

    async def test_demo_full_question_answer_complete_flow(self):
        t = self.c.tool('master', 'create_task', task_args(self.path))
        await self.h.settle(lambda: self.c.store.all('question'))
        await self.h.settle()
        q = self.c.store.all('question')[0]
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'waiting')
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=q['version'], answer='继续'))
        await self.h.settle(lambda: self.c.store.get('task', t['id'])['state'] == 'completed')
        self.assertTrue(self.c.store.get('agent', t['agent'])['session'].startswith('demo-'))
        # 工具调用经节点 → hub → center.tool，与远端同一路径并记录在事件里。
        self.assertTrue(any(e['kind'] == 'harness_tool' and e['text'] == 'finish_task' for e in self.c.store.events(limit=500)))

    async def test_one_active_turn_per_agent_and_priority_order(self):
        Scripted.gates['high'] = asyncio.Event()
        with self.scripted():
            with self.c.store.atomic():
                self.c.enqueue('system', 'master', 'low', 1)
                self.c.enqueue('owner', 'master', 'high', 9)
            await self.h.sync(3)
            self.assertEqual(Scripted.seen, ['high'])
            self.assertEqual(len(self.h.node.active), 1)
            Scripted.gates['high'].set()
            await self.h.settle()
        self.assertEqual(Scripted.seen, ['high', 'low'])
        self.assertTrue(any(e['kind'] == 'chat' and e['text'] == 'done:high' for e in self.c.store.events()))

    async def test_runtime_failure_marks_failed_and_pauses_master(self):
        with self.scripted():
            m = self.c.enqueue('owner', 'master', 'boom')
            await self.h.settle()
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'failed')
        self.assertTrue(self.c.store.get('agent', 'master')['paused'])

    async def test_review_covered_while_queued_never_dispatches(self):
        with self.scripted():
            m = self.c.enqueue('system', 'master', '排队审核', key='review')
            await self.h.sync(2)
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'cancelled')
        self.assertEqual(Scripted.seen, [])

    async def test_priority10_waits_for_stop_confirmation_then_runs(self):
        Scripted.gates['slow'] = asyncio.Event()
        with self.scripted():
            old = self.c.enqueue('owner', 'master', 'slow', 9)
            await self.h.sync(2)
            job = next(j for j in self.c.store.all('lease') if j['state'] == 'running')
            urgent = self.c.enqueue('owner', 'master', 'urgent', 10)
            # 停止确认之前旧回合的写权限已撤销，新消息尚未派发。
            with self.assertRaises(PermissionError): self.c.hub.valid_job('local', job['id'])
            self.assertEqual(self.c.store.get('message', urgent['id'])['state'], 'queued')
            await self.h.settle(lambda: self.c.store.get('message', urgent['id'])['state'] == 'handled')
        self.assertEqual(Scripted.seen, ['slow', 'urgent'])
        self.assertTrue(Scripted.closed)
        self.assertEqual(self.c.store.get('lease', job['id'])['state'], 'interrupted')
        self.assertEqual(self.c.store.get('message', old['id'])['state'], 'unknown')
        self.assertFalse(self.c.store.get('agent', 'master')['paused'])

    async def test_pause_stops_process_and_resume_keeps_session(self):
        Scripted.gates['work'] = asyncio.Event()
        with self.scripted():
            t = self.c.tool('master', 'create_task', task_args(self.path))
            self.c.save_session(t['agent'], 'native-1')
            await self.h.sync(2)
            self.c.tool(t['agent'], 'send_message', {'to': 'master', 'text': 'x'})
            self.c.tool('master', 'control_task', dict(task_id=t['id'], action='pause', reason='用户暂停'))
            await self.h.settle(lambda: not self.c.hub.busy(t['agent']))
            self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
            self.c.tool('master', 'control_task', dict(task_id=t['id'], action='resume', reason='继续'))
            Scripted.gates.clear()
            await self.h.settle(lambda: not self.c.has_queued(t['agent']) and not self.c.hub.busy(t['agent']))
        self.assertEqual(self.c.store.get('agent', t['agent'])['session'], 'native-1')

    async def test_lease_expiry_marks_unknown_and_pauses_without_replay(self):
        Scripted.gates['hang'] = asyncio.Event()
        with self.scripted():
            t = self.c.tool('master', 'create_task', task_args(self.path))
            m = self.c.enqueue('master', t['agent'], 'hang', 9)
            await self.h.sync(3)
            job = next(j for j in self.c.store.all('lease') if j['agent'] == t['agent'] and j['state'] == 'running')
            now = self.c.clock
            self.c.clock = lambda: job['until'] + 1
            self.c.hub.expire()
            self.c.clock = now
        self.assertEqual(self.c.store.get('lease', job['id'])['state'], 'unknown')
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.assertTrue(any(e['kind'] == 'node_attention' for e in self.c.store.events(limit=500)))
        with self.assertRaises(PermissionError): self.c.hub.valid_job('local', job['id'])

    async def test_node_restart_reports_unknown_without_replay(self):
        import sqlite3
        from cthulhu_v4.node import Node
        from cthulhu_v4.transport import LocalLink
        Scripted.gates['hang'] = asyncio.Event()
        with self.scripted():
            m = self.c.enqueue('owner', 'master', 'hang')
            await self.h.sync(2)
            # 崩溃快照：回合仍为 running 时的节点磁盘。
            crashed = self.path / 'crashed'; crashed.mkdir()
            src = sqlite3.connect(self.path / 'local-node' / 'node.db'); dst = sqlite3.connect(crashed / 'node.db')
            src.backup(dst); src.close(); dst.close()
            old = self.h.node
            await old.close(); self.c.hub.detach('local', old.link); old.store.close()
            node = Node(crashed, backends=['demo']); node.link = LocalLink(self.c.hub, node)
            self.c.hub.attach('local', node.link); self.h.node = node
            await self.h.sync(2)
        self.assertEqual(Scripted.seen, ['hang'])  # 没有重放
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'unknown')
        self.assertTrue(self.c.store.get('agent', 'master')['paused'])
