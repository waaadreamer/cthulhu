import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cthulhu_v4 import models
from cthulhu_v4.models import clean, discover

from helpers import Harness

FAKE = Path(__file__).with_name('fake_model_cli.py')


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(self.tmp.name) / 'bin'; bin_dir.mkdir()
        # argv[0] 决定夹具扮演哪个 CLI，所以用同名链接。夹具必须可执行且有 shebang：
        # 否则 exec 失败后会继续沿 PATH 找到真正的 codex/claude。
        FAKE.chmod(0o755)
        for cli in ('codex', 'claude'):
            (bin_dir / cli).symlink_to(FAKE)
        self.log = Path(self.tmp.name) / 'calls.jsonl'
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_MODELS_LOG=str(self.log), FAKE_MODELS_MODE='')

    def tearDown(self):
        self.tmp.cleanup()

    def run_discover(self, previous=None, mode=''):
        self.env['FAKE_MODELS_MODE'] = mode
        return asyncio.run(discover(['demo', 'codex', 'claude'], self.env, previous, cwd=Path(self.tmp.name) / 'probe'))

    def test_codex_lists_all_visible_models_with_efforts(self):
        out = self.run_discover()
        self.assertTrue(all(c['cli'] in ('codex', 'claude') for c in map(json.loads, self.log.read_text().splitlines())))
        self.assertEqual([m['id'] for m in out['codex']['list']], ['gpt-a', 'gpt-b'])   # 翻页、跳过隐藏模型
        self.assertEqual(out['codex']['list'][0]['efforts'], ['low', 'medium', 'ultra'])
        self.assertTrue(out['codex']['list'][0]['default'])
        self.assertIsNone(out['codex']['error'])
        self.assertNotIn('demo', out)

    def test_claude_resolves_aliases_without_waiting_for_reply(self):
        started = time.monotonic()
        out = self.run_discover()
        self.assertLess(time.monotonic() - started, 20)        # 夹具在 init 后睡 30 秒：必须在 init 处结束
        self.assertEqual({m['alias']: m['id'] for m in out['claude']['list']},
                         {a: f'claude-{a}-9-9' for a in ('fable', 'opus', 'sonnet', 'haiku')})
        cwds = {c['cwd'] for c in map(json.loads, self.log.read_text().splitlines()) if c['cli'] == 'claude'}
        self.assertEqual(cwds, {str(Path(self.tmp.name) / 'probe')})   # 固定目录：不在 ~/.claude/projects 留下一堆空目录

    def test_partial_and_failed_discovery(self):
        out = self.run_discover(mode='claude-partial')
        self.assertEqual([m['alias'] for m in out['claude']['list']], ['fable', 'opus', 'sonnet'])
        good = self.run_discover()
        failed = self.run_discover(previous=good, mode='codex-fail')
        # 发现失败时保留上次成功的目录，并带上失败原因；不能因为一次失败把可选模型清空。
        self.assertEqual(failed['codex']['list'], good['codex']['list'])
        self.assertTrue(failed['codex']['error'])

    def test_clean_bounds_node_reports(self):
        raw = {'codex': {'list': [{'id': 'x' * 200}, {'id': 'ok', 'name': 'n' * 500, 'efforts': ['low', 5, 'y' * 40]},
                                  'junk'] + [{'id': f'm{i}'} for i in range(100)], 'at': 'bad', 'error': 'e' * 999},
               'evil': {'list': [{'id': 'a'}]}}
        out = clean(raw)
        self.assertEqual(set(out), {'codex'})
        self.assertEqual(out['codex']['list'][0], {'id': 'ok', 'name': 'n' * 120, 'efforts': ['low']})
        self.assertLessEqual(len(out['codex']['list']), 64)
        self.assertIsNone(out['codex']['at'])
        self.assertEqual(len(out['codex']['error']), 300)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self.tmp.name))

    async def asyncTearDown(self):
        self.h.bridge.stop(); self.h.c.store.close(); self.h.node.store.close(); self.tmp.cleanup()

    def stored(self):
        return self.h.c.store.get('node', 'local').get('models')

    async def test_node_reports_catalog_once_and_center_keeps_it(self):
        catalog = {'codex': {'list': [{'id': 'gpt-a', 'name': 'GPT-A', 'efforts': ['low']}], 'at': 1.0, 'error': None}}
        self.h.node.models, self.h.node.models_dirty = catalog, True
        sent = []
        original = self.h.node.link.request
        async def spy(body):
            sent.append('models' in body); return await original(body)
        self.h.node.link.request = spy
        await self.h.sync(3)
        self.assertEqual(sent, [True, False, False])            # 只在变化时上报一次
        self.assertEqual(self.stored(), catalog)
        self.assertIn('models', next(n for n in self.h.c.hub.nodes() if n['id'] == 'local'))

    async def test_failed_sync_reports_again(self):
        self.h.node.models, self.h.node.models_dirty = {'codex': {'list': [{'id': 'gpt-a'}], 'at': 1.0, 'error': None}}, True
        original = self.h.node.link.request
        async def drop(body):
            raise ConnectionError('断线')
        self.h.node.link.request = drop
        with self.assertRaises(ConnectionError):
            await self.h.node.sync_once()
        self.assertTrue(self.h.node.models_dirty)                # 中心没确认：下次重报
        self.h.node.link.request = original
        await self.h.sync()
        self.assertEqual(self.stored()['codex']['list'][0]['id'], 'gpt-a')
        self.assertFalse(self.h.node.models_dirty)


if __name__ == '__main__':
    unittest.main()
