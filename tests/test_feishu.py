"""飞书入站：真实 Center/Node 与真实子进程生命周期，lark-cli 由协议夹具代替，不访问飞书。"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import Harness
from cthulhu_v4.feishu import Feishu
import cthulhu_v4.node as node_module

HERE = Path(__file__).resolve().parent
OWNER = 'ou_owner'


def event(mid, sender=OWNER, text='你好', **kw):
    return {'type': 'im.message.receive_v1', 'message_id': mid, 'event_id': 'ev-' + mid, 'sender_id': sender,
            'sender_type': 'user', 'chat_id': 'oc_chat', 'chat_type': 'p2p', 'message_type': 'text',
            'content': text} | kw


class Capture:
    """记录派发给运行时的 agent 规格与输入，不代表模型行为。"""
    seen = []

    def __init__(self, sink, agent): self.agent = agent

    async def run(self, text):
        Capture.seen.append((self.agent, text)); return ''

    async def close(self): pass


class FeishuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.h = Harness(self.path)
        self.c = self.h.c
        self.events, self.log = self.path / 'events.ndjson', self.path / 'lark.log'
        self.cli = self.path / 'lark-cli'
        self.cli.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_lark.py"} "$@"\n'); self.cli.chmod(0o700)
        self.f = Feishu(self.c, self.cli, self.path / 'lark-config', owner=OWNER)
        self.c.feishu = self.f
        self.env = patch.dict(os.environ, {'FAKE_LARK_EVENTS': str(self.events), 'FAKE_LARK_LOG': str(self.log)})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        await self.f.stop()
        await self.h.close(); self.tmp.cleanup()

    def write(self, *events):
        self.events.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events) + 'not json\n')

    def inbox(self):
        return [m for m in self.c.store.all('message') if m['target'] == 'master']

    async def until(self, check, seconds=10):
        loop = asyncio.get_running_loop(); end = loop.time() + seconds
        while loop.time() < end:
            if check(): return
            await asyncio.sleep(.05)
        self.fail('等待超时：' + json.dumps(self.f.info, ensure_ascii=False))

    async def test_redelivery_after_restart_enqueues_once(self):
        self.write(event('om_1'), event('om_2', sender='ou_other', text='帮我查一下'),
                   event('om_bot', sender_type='bot'))
        os.environ['FAKE_LARK_EXIT'] = '1'
        runner = asyncio.create_task(self.f.run())
        try:
            # 夹具每次启动都重放同一批事件：等到至少重启两次，验证重投不会重复入队。
            await self.until(lambda: self.f.info['restarts'] >= 2)
        finally:
            await self.f.stop(); runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        msgs = self.inbox()
        self.assertEqual(len(msgs), 2)
        owner, other = sorted(msgs, key=lambda m: m['priority'], reverse=True)
        self.assertEqual((owner['sender'], owner['priority']), ('owner', 9))
        self.assertEqual((other['sender'], other['priority']), ('feishu', 5))
        self.assertIn('非主人 ou_other', other['text'])
        self.assertIn('--message-id om_2', other['text'])
        self.assertIn('不会自动回复', owner['text'])
        # 路由文本是副本：主聊天展示的是 feishu_in 原文，不重复显示包装后的提示。
        self.assertTrue(all(m['copy'] for m in msgs))
        shown = [e for e in self.c.store.events(limit=500) if e['kind'] == 'feishu_in']
        self.assertEqual([e['text'] for e in shown], ['你好', '帮我查一下'])
        self.assertEqual([e['data']['owner'] for e in shown], [True, False])
        runs = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertGreaterEqual(len(runs), 2)
        self.assertEqual(runs[0]['argv'][:4], ['event', 'consume', 'im.message.receive_v1', '--as'])
        self.assertEqual(runs[0]['config_dir'], str(self.path / 'lark-config'))

    async def test_stop_closes_stdin_and_exits_cleanly(self):
        self.write(event('om_1'))
        os.environ.pop('FAKE_LARK_EXIT', None)
        runner = asyncio.create_task(self.f.run())
        await self.until(lambda: len(self.inbox()) == 1)
        self.assertEqual(self.f.info['state'], 'running')
        await self.f.stop()
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.f.info['state'], 'stopped')
        self.assertEqual(self.f.proc.returncode, 0)

    async def test_owner_requires_configured_open_id(self):
        # 没有配置主人时，任何飞书发送者都不能以 owner 身份进入，也就拿不到打断级优先级。
        self.f.owner = None
        with self.c.store.atomic():
            m = self.f.ingest(event('om_1'))
        self.assertEqual((m['sender'], m['priority']), ('feishu', 5))

    async def test_long_text_is_truncated_with_pointer(self):
        with self.c.store.atomic():
            m = self.f.ingest(event('om_1', text='x' * 20000))
        self.assertIn('已截断，原文 20000 字', m['text'])
        self.assertLess(len(m['text']), 17000)

    async def test_cli_env_reaches_master_runtime_only(self):
        Capture.seen = []
        with self.c.store.atomic():
            self.f.ingest(event('om_1'))
        with patch.object(node_module, 'make_runtime', Capture):
            await self.h.settle(lambda: Capture.seen)
        agent, text = Capture.seen[0]
        self.assertEqual(agent['id'], 'master')
        self.assertEqual(agent['env'], {'LARKSUITE_CLI_CONFIG_DIR': str(self.path / 'lark-config'),
                                        'LARK_CLI': str(self.cli)})
        self.assertIn('om_1', text)
        self.assertEqual(self.c.agent_env('task_x'), {})
        self.assertEqual(self.c.snapshot()['feishu']['owner'], OWNER)


if __name__ == '__main__':
    unittest.main()
