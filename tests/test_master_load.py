"""Master 负载与主聊天去重的不变量：等待到期不进 Master 队列、执行者进度合并、最终回复不重复进主聊天。"""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cthulhu_v4.center import Center
from tests.helpers import Harness, task_args


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.c = Center(self.tmp.name, clock=lambda: self.now)

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def create(self):
        t = self.c.tool('master', 'create_task', dict(title='测试', description='只读验证', acceptance='给出证据',
            deadline=datetime.fromtimestamp(99999, timezone.utc).isoformat(), cwd=self.tmp.name))
        for m in self.c.store.all('message'):
            m['state'] = 'handled'; self.c.store.put('message', m)
        return t

    def queued(self, target='master'):
        return [m for m in self.c.store.all('message') if m['target'] == target and m['state'] == 'queued']

    def test_wait_expiry_wakes_worker_without_queueing_master(self):
        t = self.create()
        self.c.tool(t['agent'], 'wait_until', dict(reason='作业运行中', operation='job-1', seconds=60))
        self.now += 120; self.c.tick()
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.queued(t['agent'])), 1)                 # 执行者照常被唤醒
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'running')
        self.assertTrue(any(e['kind'] == 'wait_expired' and e['task'] == t['id'] for e in self.c.store.events(limit=500)))

    def test_queued_progress_from_one_worker_is_appended_not_duplicated(self):
        t = self.create(); aid = t['agent']
        a = self.c.tool(aid, 'send_message', dict(to='master', text='进度一'))
        b = self.c.tool(aid, 'send_message', dict(to='master', text='进度二', priority=7))
        self.assertEqual(a['id'], b['id'])
        [m] = self.queued()
        self.assertIn('进度一', m['text']); self.assertIn('进度二', m['text'])
        self.assertLess(m['text'].index('进度一'), m['text'].index('进度二'))
        self.assertEqual(m['priority'], 7)
        # 每段原文仍各有一条事件，消息导出与侧栏不丢
        texts = [e['text'] for e in self.c.store.events(limit=500) if e['kind'] == 'message' and e['actor'] == aid]
        self.assertEqual(texts, ['进度一', '进度二'])

    def test_progress_not_merged_once_dispatched_or_for_questions(self):
        t = self.create(); aid = t['agent']
        a = self.c.tool(aid, 'send_message', dict(to='master', text='进度一'))
        self.c.set_message(a['id'], 'running')                             # 已交给模型，不能再改
        b = self.c.tool(aid, 'send_message', dict(to='master', text='进度二'))
        self.assertNotEqual(a['id'], b['id'])
        self.c.tool(aid, 'ask_master', dict(text='选哪个', recommendation='A', blocking=False))
        self.assertEqual(len(self.queued()), 2)                            # 提问副本单独一条
        self.assertEqual(self.c.store.get('message', b['id'])['text'], '进度二')

    def test_owner_messages_are_never_merged(self):
        self.create()
        self.c.enqueue('owner', 'master', '指令一', 9)
        self.c.enqueue('owner', 'master', '指令二', 9)
        self.assertEqual(len(self.queued()), 2)


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name))
        self.c = self.h.c

    async def asyncTearDown(self):
        await self.h.close(); self.tmp.cleanup()

    def chats(self):
        return [e for e in self.c.store.events(limit=2000) if e['kind'] == 'chat']

    def asides(self):
        return [e for e in self.c.store.events(limit=2000) if e['kind'] == 'reply' and e['actor'] == 'master']

    async def test_owner_turn_reply_reaches_chat(self):
        self.c.enqueue('owner', 'master', '你好', 9)
        await self.h.settle()
        self.assertEqual(len(self.chats()), 1)
        self.assertEqual(self.asides(), [])

    async def test_worker_only_turn_reply_stays_aside(self):
        t = self.c.tool('master', 'create_task', task_args(self.tmp.name, backend='demo'))
        await self.h.settle()
        before = len(self.chats())
        self.c.enqueue('system', 'master', '某个系统通知', 8)
        await self.h.settle()
        self.assertEqual(len(self.chats()), before)
        self.assertTrue(self.asides())
        dispatch = [e for e in self.c.store.events(limit=2000) if e['kind'] == 'dispatch' and e['target'] == 'master'][-1]
        self.assertIs(dispatch['data']['owner'], False)

    def test_reply_after_tell_owner_is_not_repeated(self):
        m = self.c.enqueue('owner', 'master', '查一下', 9)
        seq = self.c.store.emit('dispatch', '', target='master')
        job = {'id': 'turn_x', 'messages': [m['id']], 'message': m['id'], 'seq': seq}
        self.assertTrue(self.c.reply_is_public(job))
        self.c.tool('master', 'tell_owner', {'text': '已安排'})
        self.assertFalse(self.c.reply_is_public(job))
        self.assertTrue(self.c.reply_is_public({k: v for k, v in job.items() if k != 'seq'}))  # 升级前的回合保持原样


class HistoryTests(unittest.TestCase):
    def test_history_is_conversation_only_newest_first_and_searchable(self):
        with tempfile.TemporaryDirectory() as d:
            c = Center(d); s = c.store
            c.enqueue('owner', 'master', '训练先不动', 9)
            s.emit('chat', '收到，100% 静态排查', actor='master', visible=True)
            s.emit('reply', '已收到进度', actor='master')
            s.emit('reply', '执行者自己的回复', actor='old_x')
            s.emit('native:item/completed', '静态排查', actor='master')
            kinds = [e['kind'] for e in s.history()]
            self.assertEqual(kinds, ['reply', 'chat', 'message'])
            self.assertEqual([e['text'] for e in s.history('静态 100%')], ['收到，100% 静态排查'])
            self.assertEqual([e['kind'] for e in s.history(aside=False)], ['chat', 'message'])
            self.assertEqual(s.history('99%'), [])                         # % 按字面匹配
            top = s.history(limit=1)[0]
            self.assertEqual([e['kind'] for e in s.history(before=top['seq'])], ['chat', 'message'])
            s.close()


if __name__ == '__main__':
    unittest.main()
