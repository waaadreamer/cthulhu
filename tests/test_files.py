"""文件链接：节点读取、凭证目录保护、经 hub 转发、中心缓存与离线兜底。不调用模型。"""
import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path

from helpers import Harness
from cthulhu_v4.files import parse, read_path


class ReadPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_dir_unchanged_and_errors(self):
        f = self.root / 'report.md'; f.write_text('# 结论\n内容')
        r = read_path(str(f))
        self.assertEqual((r['kind'], base64.b64decode(r['content']).decode()), ('file', '# 结论\n内容'))
        self.assertEqual(read_path(str(f), {'mtime': r['mtime'], 'size': r['size']}), {'unchanged': True})
        (self.root / 'sub').mkdir()
        d = read_path(str(self.root))
        self.assertEqual([e['name'] for e in d['entries']], ['sub', 'report.md'])  # 目录在前
        self.assertIn('不存在', read_path(str(self.root / 'nope'))['error'])
        self.assertIn('绝对路径', read_path('relative/path')['error'])
        big = self.root / 'big.bin'; big.write_bytes(b'x' * 100)
        r = read_path(str(big), max_bytes=10)
        self.assertTrue(r['truncated']); self.assertEqual(len(base64.b64decode(r['content'])), 10)

    def test_protected_dirs_cannot_be_read_even_via_symlink(self):
        secret = self.root / 'state'; secret.mkdir(); (secret / 'node.json').write_text('{"token": "t"}')
        link = self.root / 'innocent'; os.symlink(secret / 'node.json', link)
        for target in (secret / 'node.json', secret, link):
            self.assertIn('凭证', read_path(str(target), protected=[secret.resolve()])['error'])

    def test_parse_ref(self):
        self.assertEqual(parse('S::/home/u/a b.md'), ('S', '/home/u/a b.md'))
        for bad in ('S:/x', '::/x', 'S::relative', '/abs/only'):
            with self.assertRaises(ValueError): parse(bad)


class FileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.h = Harness(self.path); self.c = self.h.c
        self.runner = asyncio.create_task(self.h.node.run())
        self.f = self.path / 'work' / 'report.md'; self.f.parent.mkdir(); self.f.write_text('v1')
        self.ref = 'local::' + str(self.f)
        for _ in range(50):
            if 'files' in (self.c.store.get('node', 'local').get('features') or []): break
            await asyncio.sleep(.05)

    async def asyncTearDown(self):
        self.runner.cancel(); await asyncio.gather(self.runner, return_exceptions=True)
        await self.h.close(); self.tmp.cleanup()

    async def test_read_through_node_cache_and_offline_fallback(self):
        r = await self.c.files.read(self.ref)
        self.assertEqual((base64.b64decode(r['content']), r['cached']), (b'v1', False))
        r = await self.c.files.read(self.ref)
        self.assertTrue(r['cached'])                            # 15 秒内直接用缓存
        r = await self.c.files.read(self.ref, refresh=True)
        self.assertTrue(r['cached'])                            # 节点确认未变化，不重传内容
        self.f.write_text('v2 更长')
        r = await self.c.files.read(self.ref, refresh=True)
        self.assertEqual((base64.b64decode(r['content']).decode(), r['cached']), ('v2 更长', False))
        # 节点离线：返回中心缓存并明确标注是旧副本。
        self.c.hub.detach('local', self.h.node.link)
        r = await self.c.files.read(self.ref, refresh=True)
        self.assertIn('离线', r['stale'])
        with self.assertRaisesRegex(ValueError, '离线'):
            await self.c.files.read('local::' + str(self.path / 'never-read.txt'))

    async def test_center_state_is_protected_and_unknown_node_rejected(self):
        with self.assertRaisesRegex(ValueError, '凭证'):
            await self.c.files.read('local::' + str(self.h.node.state_dir / 'node.db'))
        with self.assertRaisesRegex(ValueError, '不存在'):
            await self.c.files.read('nowhere::/etc/hostname')

    async def test_nodes_without_feature_are_told_to_upgrade(self):
        with self.c.store.atomic(): self.c.hub.register('old')
        class Link:
            def wake(self): pass
        self.c.hub.attach('old', Link())
        with self.assertRaisesRegex(ValueError, '升级'):
            await self.c.files.read('old::/etc/hostname')
