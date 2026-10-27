import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from cthulhu_v4 import memory
from cthulhu_v4.center import Center
from cthulhu_v4.memory import Memory, chunks, read_path

FAKE = (sys.executable, str(Path(__file__).with_name('fake_codex_exec.py')))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 10, 28, 3, 30).timestamp()
        self.c = Center(self.tmp.name, backend='codex', clock=lambda: self.now)
        a = self.c.store.get('agent', 'master'); a['session'] = 'thread-old'; self.c.store.put('agent', a)
        self.log = Path(self.tmp.name) / 'calls.jsonl'
        self.env = mock.patch.dict(os.environ, {'FAKE_CODEX_LOG': str(self.log), 'FAKE_CODEX_MODE': ''})
        self.env.start()
        self.m = Memory(self.c, codex=FAKE)

    def tearDown(self):
        self.env.stop(); self.c.store.close(); self.tmp.cleanup()

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def say(self, text):
        self.c.store.emit('chat', text, actor='master')

    def test_chunks_never_cross_days_or_size(self):
        s = self.c.store
        with mock.patch.object(memory, 'CHUNK_BYTES', 200):
            for i in range(6):
                s.emit('chat', '甲' * 30 + str(i), actor='master')
            s.db.execute("UPDATE events SET at = at + 86400 WHERE seq = (SELECT MAX(seq) FROM events)")
            parts = list(chunks(s, 0, s.highwater()))
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(p['bytes'] <= 200 for p in parts))
        self.assertEqual(len({p['day'] for p in parts[-1:]}), 1)
        self.assertNotEqual(parts[-1]['day'], parts[0]['day'])
        # 每条消息恰好出现一次
        self.assertEqual(sum(len(p['parts']) for p in parts), 6)

    def test_digest_builds_codex_layout_and_is_incremental(self):
        self.say('第一天的决定：R600x 一律用 envs/R6000.yaml')
        asyncio.run(self.m.digest())
        root = self.m.root
        self.assertEqual((root / 'memory_summary.md').read_text().split('\n')[0], 'v1')
        self.assertIn('R6000.yaml', (root / 'raw_memories.md').read_text())
        self.assertEqual(len(list((root / 'rollout_summaries').glob('*.md'))), 1)
        self.assertFalse((root / 'phase2_workspace_diff.md').exists())   # 与原生一致：整合后删掉 diff
        first = len(self.calls())
        self.assertEqual(first, 2)                                       # 一段阶段 1 + 一次阶段 2
        asyncio.run(self.m.digest())
        self.assertEqual(len(self.calls()), first)                       # 没有新消息：不调用模型
        self.say('第二条')
        asyncio.run(self.m.digest())
        stage1 = [c for c in self.calls() if '--output-schema' in c['args']]
        self.assertEqual(len(stage1), 2)
        self.assertIn('第二条', stage1[-1]['prompt'])
        self.assertNotIn('R6000.yaml', stage1[-1]['prompt'])              # 只提炼新增部分

    def test_stage1_failure_keeps_cursor_at_contiguous_prefix(self):
        os.environ['FAKE_CODEX_MODE'] = 'stage1-fail'
        with mock.patch.object(memory, 'CHUNK_BYTES', 60):
            self.say('正常段' * 5)
            self.say('失败段' * 5)
            self.say('之后段' * 5)
            with self.assertRaises(RuntimeError):
                asyncio.run(self.m.digest())
            ok = self.m.outputs()
            self.assertEqual(len(ok), 1)                                 # 失败段之后的成功结果不落盘
            os.environ['FAKE_CODEX_MODE'] = ''
            asyncio.run(self.m.digest())
        retried = [c['prompt'] for c in self.calls() if '--output-schema' in c['args']][-2:]
        self.assertTrue(any('失败段' in p for p in retried) and any('之后段' in p for p in retried))
        self.assertEqual(len(self.m.outputs()), 3)

    def test_bad_consolidation_does_not_advance_baseline(self):
        self.say('要记住的事')
        os.environ['FAKE_CODEX_MODE'] = 'phase2-bad'
        with self.assertRaises(RuntimeError):
            asyncio.run(self.m.digest())
        self.assertTrue(self.m.dirty())
        os.environ['FAKE_CODEX_MODE'] = ''
        asyncio.run(self.m.digest())                                     # 没有新消息也要重跑阶段 2
        self.assertFalse(self.m.dirty())
        self.assertEqual(self.m.root.joinpath('memory_summary.md').read_text()[:2], 'v1')

    def test_rotation_waits_for_idle_master(self):
        self.c.enqueue('owner', 'master', '还没处理', 5)
        self.assertFalse(self.c.rotate_master('测试'))
        self.assertEqual(self.c.store.get('agent', 'master')['session'], 'thread-old')
        self.c.cancel_queued('master')
        self.assertTrue(self.c.rotate_master('测试'))
        a = self.c.store.get('agent', 'master')
        self.assertIsNone(a['session'])
        self.assertEqual(a['epoch'], 1)
        self.assertEqual(a['previous_sessions'][-1]['session'], 'thread-old')
        self.assertFalse(self.c.rotate_master('测试'))                   # 已经是新会话：不重复换

    def test_step_runs_once_per_night_inside_window(self):
        self.say('今晚要整理')
        self.now = datetime(2026, 10, 28, 14, 0).timestamp()
        asyncio.run(self.m.step())
        self.assertEqual(self.calls(), [])                               # 白天不动
        self.now = datetime(2026, 10, 28, 3, 30).timestamp()
        asyncio.run(self.m.step())
        self.assertIsNone(self.c.store.get('agent', 'master')['session'])
        n = len(self.calls())
        a = self.c.store.get('agent', 'master'); a['session'] = 'thread-new'; self.c.store.put('agent', a)
        asyncio.run(self.m.step())
        self.assertEqual(len(self.calls()), n)                           # 同一晚不重复整理
        self.assertEqual(self.c.store.get('agent', 'master')['session'], 'thread-new')

    def test_rotation_retries_until_master_idle(self):
        self.c.enqueue('owner', 'master', '凌晨还在忙', 5)
        asyncio.run(self.m.step())
        self.assertEqual(self.c.store.get('agent', 'master')['session'], 'thread-old')
        self.c.cancel_queued('master')
        asyncio.run(self.m.step())                                       # 同一晚稍后空闲时再换
        self.assertIsNone(self.c.store.get('agent', 'master')['session'])

    def test_seed_keeps_old_memories_and_only_digests_newer(self):
        src = Path(self.tmp.name) / 'preview'
        (src / 'rollout_summaries').mkdir(parents=True)
        (src / 'MEMORY.md').write_text('# Task Group: old\nrollout_path: /old/master-history-preview/2026-10-19.md\n')
        (src / 'memory_summary.md').write_text('v1\n旧偏好\n')
        (src / 'raw_memories.md').write_text('# Raw Memories\n\n头\n\n## Thread `old`\n旧记忆内容\n\n')
        self.say('导入前的消息')
        through = self.c.store.highwater()
        self.m.seed(src, through, '/old/master-history-preview')
        self.assertIn(str(Path(self.tmp.name) / 'master-history'), (self.m.root / 'MEMORY.md').read_text())
        self.assertFalse(self.m.dirty())
        self.say('导入后的消息')
        asyncio.run(self.m.digest())
        stage1 = [c['prompt'] for c in self.calls() if '--output-schema' in c['args']]
        self.assertEqual(len(stage1), 1)
        self.assertIn('导入后的消息', stage1[0]); self.assertNotIn('导入前的消息', stage1[0])
        raw = (self.m.root / 'raw_memories.md').read_text()
        self.assertIn('旧记忆内容', raw); self.assertIn('导入后的消息', raw)
        with self.assertRaises(ValueError):
            self.m.seed(src, through, '/old')                            # 已有记忆：不覆盖

    def test_new_master_thread_gets_summary(self):
        self.assertEqual(read_path(self.tmp.name), '')
        self.say('偏好')
        asyncio.run(self.m.digest())
        text = read_path(self.tmp.name)
        self.assertIn('先给结论', text)
        self.assertIn('MEMORY.md', text)
        from cthulhu_v4.runtime import CodexRuntime
        a = self.c.store.get('agent', 'master')
        self.assertIn('先给结论', CodexRuntime(mock.Mock(), a).instructions())


if __name__ == '__main__':
    unittest.main()
