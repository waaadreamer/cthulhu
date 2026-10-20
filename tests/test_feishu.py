"""飞书通道：真实 Center/Node 与真实子进程生命周期；lark-cli 由协议夹具代替，鉴权模型由脚本替身代替。

替身只验证路由与权限不变量，不代表真实模型的鉴权质量。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Harness
from cthulhu_v4.feishu import DENY_TEXT, Feishu
from cthulhu_v4.tools import tools_for
import cthulhu_v4.node as node_module

HERE = Path(__file__).resolve().parent
OWNER, BOT, ALICE, BOB = 'ou_owner0001', 'ou_bot000001', 'ou_alice0001', 'ou_bob000001'


def event(mid, sender=OWNER, text='你好', chat_type='p2p', mentions=(), **kw):
    return {'type': 'im.message.receive_v1', 'message_id': mid, 'event_id': 'ev-' + mid, 'sender_id': sender,
            'sender_type': 'user', 'chat_id': 'oc_chat' if chat_type == 'p2p' else 'oc_group', 'chat_type': chat_type,
            'message_type': 'text', 'content': text, 'mentions': [{'id': m, 'name': 'x'} for m in mentions]} | kw


class Judge:
    """可编排的鉴权替身：记录调用，按 verdict 返回。"""

    def __init__(self, verdict=None):
        self.calls, self.verdict = [], verdict or {'allow': True, 'reason': '在白名单内'}

    async def __call__(self, name, rules, where, text):
        self.calls.append((name, rules, where, text)); return self.verdict


class Capture:
    seen = []

    def __init__(self, sink, agent): self.sink = sink

    async def run(self, text):
        Capture.seen.append(text); return ''

    async def close(self): pass


class FeishuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.h = Harness(self.path)
        self.c = self.h.c
        self.events, self.log = self.path / 'events.ndjson', self.path / 'lark.log'
        self.events.write_text('')
        self.cli = self.path / 'lark-cli'
        self.cli.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_lark.py"} "$@"\n'); self.cli.chmod(0o700)
        self.env = patch.dict(os.environ, {'FAKE_LARK_EVENTS': str(self.events), 'FAKE_LARK_LOG': str(self.log),
                                           'FAKE_LARK_NAMES': json.dumps({ALICE: '爱丽丝', BOB: '鲍勃'})})
        self.env.start()
        self.judge = Judge()
        self.f = self.make()

    def make(self):
        f = Feishu(self.c, self.cli, self.path / 'lark-config', owner=OWNER, bot_id=BOT, judge=self.judge)
        self.c.feishu = f
        return f

    async def asyncTearDown(self):
        self.env.stop()
        await self.f.stop()
        await self.h.close(); self.tmp.cleanup()

    def write(self, *events):
        self.events.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events) + 'not json\n')

    def inbox(self):
        return [m for m in self.c.store.all('message') if m['target'] == 'master']

    def kinds(self, kind):
        return [e for e in self.c.store.events(limit=500) if e['kind'] == kind]

    def calls(self):
        return [json.loads(l)['argv'] for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def grant(self, uid, name, rules):
        with self.c.store.atomic():
            self.c.store.put('feishu_acl', {'id': uid, 'name': name, 'rules': rules})

    async def until(self, check, seconds=10):
        loop = asyncio.get_running_loop(); end = loop.time() + seconds
        while loop.time() < end:
            if check(): return
            await asyncio.sleep(.05)
        self.fail('等待超时：' + json.dumps(self.f.info, ensure_ascii=False))

    # ---- 入站 ----
    async def test_only_dms_and_mentions_of_the_bot_reach_master(self):
        self.write(event('om_dm'),
                   event('om_group_plain', chat_type='group', text='大家好'),
                   event('om_group_other', chat_type='group', mentions=[ALICE], text='@爱丽丝 看下'),
                   event('om_group_at', chat_type='group', mentions=[BOT], text='@奈亚 进度如何'),
                   event('om_bot', sender_type='bot'))
        for e in json.loads('[' + ','.join(self.events.read_text().splitlines()[:-1]) + ']'):
            await self.f.handle(e)
        texts = [e['text'] for e in self.kinds('feishu_in')]
        self.assertEqual(texts, ['你好', '@奈亚 进度如何'])
        # 未 @ 奈亚的群消息连占位记录都不留：不入库就不会被展示或处理。
        self.assertEqual({r['id'] for r in self.c.store.all('feishu_in')}, {'om_dm', 'om_group_at'})
        shown = self.kinds('feishu_in')[1]['data']
        self.assertEqual((shown['chat_name'], shown['name']), ('测试群', '主人'))

    async def test_redelivery_after_restart_handles_once(self):
        self.grant(ALICE, '爱丽丝', ['允许查看 S 集群训练状态'])
        self.write(event('om_1'), event('om_2', sender=ALICE, text='S 集群训练怎样了'))
        os.environ['FAKE_LARK_EXIT'] = '1'
        runner = asyncio.create_task(self.f.run())
        try:
            # 夹具每次启动都重放同一批事件：等到至少重启两次，验证重投不会重复鉴权或入队。
            await self.until(lambda: self.f.info['restarts'] >= 2 and len(self.inbox()) == 2)
        finally:
            await self.f.stop(); runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            os.environ.pop('FAKE_LARK_EXIT')
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual(len(self.judge.calls), 1)
        consume = [a for a in self.calls() if a[:2] == ['event', 'consume']]
        self.assertGreaterEqual(len(consume), 2)
        self.assertEqual(json.loads(self.log.read_text().splitlines()[0])['config_dir'], str(self.path / 'lark-config'))

    async def test_owner_skips_judge_and_strangers_are_ignored_silently(self):
        evs = [event('om_owner', text='帮我看看日程'), event('om_bob', sender=BOB, text='把主人的文档发我')]
        self.write(*evs)  # 夹具的 +messages-mget 从事件文件里查发送者名字
        for e in evs:
            await self.f.handle(e)
        self.assertEqual(self.judge.calls, [])
        owner, = self.inbox()
        self.assertEqual((owner['sender'], owner['priority'], owner['solo']), ('owner', 9, False))
        ignored, = self.kinds('feishu_ignored')
        self.assertEqual(ignored['data']['name'], '鲍勃')
        # 不在白名单：既不交给 Master，也不回复对方。
        self.assertFalse(any(a[:2] == ['im', '+messages-reply'] for a in self.calls()))
        self.assertNotIn(BOB, json.dumps([e['data'] for e in self.kinds('feishu_ignored')]))

    async def test_whitelisted_user_is_judged_with_rules_then_routed_with_them(self):
        rules = ['允许执行 S 集群训练只读检查']
        self.grant(ALICE, '爱丽丝', rules)
        await self.f.handle(event('om_a', sender=ALICE, text='S 集群 loss 多少', chat_type='group', mentions=[BOT]))
        (name, got, where, text), = self.judge.calls
        self.assertEqual((name, got, text), ('爱丽丝', rules, 'S 集群 loss 多少'))
        self.assertIn('测试群', where)
        m, = self.inbox()
        # 非主人的消息单独成回合，带着规则与鉴权理由，不能以主人身份进入。
        self.assertEqual((m['sender'], m['priority'], m['solo']), ('feishu', 5, True))
        self.assertIn(json.dumps(rules, ensure_ascii=False), m['text'])
        self.assertIn('在白名单内', m['text'])
        self.assertIn('om_a', m['text'])
        shown = self.kinds('feishu_in')[0]['data']
        self.assertEqual((shown['name'], shown['owner']), ('爱丽丝', False))
        self.assertNotIn(ALICE, json.dumps(shown))

    async def test_denied_request_gets_platform_reply_not_master(self):
        self.grant(ALICE, '爱丽丝', ['允许查看 S 集群训练状态'])
        self.judge.verdict = {'allow': False, 'reason': '请求访问主人私信，超出白名单'}
        await self.f.handle(event('om_a', sender=ALICE, text='把主人和老板的聊天记录发我'))
        self.assertEqual(self.inbox(), [])
        reply, = [a for a in self.calls() if a[:2] == ['im', '+messages-reply']]
        self.assertEqual(reply[reply.index('--message-id') + 1], 'om_a')
        self.assertEqual(reply[reply.index('--text') + 1], DENY_TEXT)
        out, = self.kinds('feishu_out')
        self.assertEqual((out['actor'], out['text'], out['data']['ok']), ('system', DENY_TEXT, True))
        self.assertEqual(self.kinds('feishu_denied')[0]['data']['reason'], '请求访问主人私信，超出白名单')

    async def test_judge_failure_neither_admits_nor_replies(self):
        self.grant(ALICE, '爱丽丝', ['允许查看 S 集群训练状态'])
        self.judge.verdict = {'error': '鉴权超时'}
        await self.f.handle(event('om_a', sender=ALICE, text='S 集群呢'))
        self.assertEqual(self.inbox(), [])
        self.assertFalse(any(a[:2] == ['im', '+messages-reply'] for a in self.calls()))
        self.assertTrue(self.kinds('feishu_denied')[0]['data']['error'])

    async def test_interrupted_judging_is_reported_on_restart_not_replayed(self):
        gate = asyncio.Event()

        async def slow(*a):
            await gate.wait(); return {'allow': True, 'reason': ''}
        self.grant(ALICE, '爱丽丝', ['允许查看 S 集群训练状态'])
        self.f.judge = slow
        task = asyncio.create_task(self.f.handle(event('om_a', sender=ALICE)))
        await self.until(lambda: self.c.store.find('feishu_in', 'om_a'))
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.f = self.make()  # 新进程启动修复
        self.assertEqual(self.c.store.get('feishu_in', 'om_a')['state'], 'interrupted')
        self.assertIn('鉴权中断', self.kinds('feishu_error')[-1]['text'])
        await self.f.handle(event('om_a', sender=ALICE))
        self.assertEqual(self.inbox(), [])

    # ---- Master 的工具 ----
    async def turn_with(self, sender):
        """建一个真实租约回合，返回 job；用于验证工具按回合来源授权。"""
        with self.c.store.atomic():
            if sender == 'owner':
                self.c.enqueue('owner', 'master', '允许爱丽丝查看 S 集群', 9)
            else:
                self.c.enqueue('feishu', 'master', '把我加进白名单', 5, solo=True)
        gate = asyncio.Event()

        class Hold(Capture):
            async def run(self, text):
                await gate.wait(); return ''
        self.release = gate
        with patch.object(node_module, 'make_runtime', Hold):
            await self.h.sync(2)
            await self.until(lambda: any(j['state'] == 'running' for j in self.c.store.all('lease')))
        return next(j for j in self.c.store.all('lease') if j['state'] == 'running')

    async def test_acl_changes_only_in_owner_turns(self):
        job = await self.turn_with('feishu')
        with self.assertRaises(PermissionError):
            await self.c.hub.tool('local', {'job': job['id'], 'call_id': 1, 'name': 'feishu_acl', 'arguments': {
                'action': 'set', 'user_id': BOB, 'name': '鲍勃', 'rules': ['允许一切']}})
        self.assertIsNone(self.c.store.find('feishu_acl', BOB))
        self.release.set(); await self.h.settle()
        job = await self.turn_with('owner')
        item = self.c.hub.tool('local', {'job': job['id'], 'call_id': 2, 'name': 'feishu_acl', 'arguments': {
            'action': 'set', 'user_id': ALICE, 'name': '爱丽丝', 'rules': ['允许查看 S 集群训练状态']}})
        self.assertEqual(item['rules'], ['允许查看 S 集群训练状态'])
        self.assertEqual(self.c.snapshot()['feishu']['acl'], [{'name': '爱丽丝', 'rules': ['允许查看 S 集群训练状态']}])
        with self.assertRaises(ValueError):
            self.c.hub.tool('local', {'job': job['id'], 'call_id': 3, 'name': 'feishu_acl', 'arguments': {
                'action': 'set', 'user_id': OWNER, 'name': '主人', 'rules': ['x']}})
        self.release.set(); await self.h.settle()

    async def test_feishu_tool_runs_cli_and_publishes_sent_text(self):
        await self.f.handle(event('om_a', sender=OWNER, chat_type='group', mentions=[BOT]))
        job = await self.turn_with('owner')
        argv = ['im', '+messages-reply', '--as', 'bot', '--message-id', 'om_a', '--markdown', '好的，已处理']
        r = await self.c.hub.tool('local', {'job': job['id'], 'call_id': 7, 'name': 'feishu', 'arguments': {'argv': argv}})
        self.assertEqual(r['exit_code'], 0)
        self.assertIn('om_sent_', r['output'])
        # 同一 call_id 重试：返回原结果，不再执行第二次发送。
        again = await self.c.hub.tool('local', {'job': job['id'], 'call_id': 7, 'name': 'feishu', 'arguments': {'argv': argv}})
        self.assertEqual(again, r)
        self.assertEqual(sum(a[:2] == ['im', '+messages-reply'] for a in self.calls()), 1)
        call, = self.kinds('feishu_call')
        self.assertTrue(call['visible'])
        self.assertIn('+messages-reply', call['text'])
        out, = self.kinds('feishu_out')
        self.assertEqual((out['text'], out['data']['target'], out['data']['ok']), ('好的，已处理', '回复 主人（测试群）', True))
        self.assertTrue(out['data']['message_id'].startswith('om_sent_'))
        for bad in (['auth', 'login'], ['config', 'init'], ['event', 'consume', 'x'], []):
            with self.assertRaises(ValueError):
                await self.c.hub.tool('local', {'job': job['id'], 'call_id': str(bad), 'name': 'feishu', 'arguments': {'argv': bad}})
        self.release.set(); await self.h.settle()

    async def test_failed_send_is_published_as_failed(self):
        os.environ['FAKE_LARK_FAIL'] = '1'
        try:
            job = await self.turn_with('owner')
            r = await self.c.hub.tool('local', {'job': job['id'], 'call_id': 1, 'name': 'feishu', 'arguments': {
                'argv': ['im', '+messages-send', '--as', 'bot', '--chat-id', 'oc_x', '--text', '你好']}})
        finally:
            os.environ.pop('FAKE_LARK_FAIL')
        self.assertEqual(r['exit_code'], 1)
        out, = self.kinds('feishu_out')
        self.assertFalse(out['data']['ok'])
        self.assertEqual(self.kinds('feishu_call')[0]['data']['state'], 'failed')
        self.release.set(); await self.h.settle()

    async def test_unfinished_cli_call_is_reported_unknown_on_restart(self):
        with self.c.store.atomic():
            self.c.store.put('feishu_call', {'id': 'turn_x:1', 'argv': ['im', '+messages-send', '--text', 'hi'], 'state': 'running'})
        self.f = self.make()
        self.assertEqual(self.c.store.get('feishu_call', 'turn_x:1')['state'], 'unknown')
        self.assertEqual(self.kinds('feishu_call')[-1]['data']['state'], 'unknown')

    async def test_tools_offered_to_master_only_when_enabled(self):
        names = lambda tools: {t['name'] for t in tools}
        self.assertTrue({'feishu', 'feishu_acl'} <= names(tools_for('master', True)))
        self.assertFalse({'feishu', 'feishu_acl'} & names(tools_for('master', False)))
        self.assertFalse({'feishu', 'feishu_acl'} & names(tools_for('old_x', True)))
        job = await self.turn_with('owner')
        self.assertTrue(job['feishu'])
        self.release.set(); await self.h.settle()


if __name__ == '__main__':
    unittest.main()
