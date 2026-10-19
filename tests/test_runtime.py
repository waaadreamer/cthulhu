import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cthulhu_v4.core import Core
from cthulhu_v4.runtime import make_runtime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def check_backend(self, backend, configured=False):
        with tempfile.TemporaryDirectory() as tmp:
            core = Core(tmp, backend, model='gpt-6-astra' if configured else None, reasoning_effort='medium' if configured else None); core.url = 'http://127.0.0.1:1'
            original = asyncio.create_subprocess_exec
            launches = []
            async def spawn(*argv, **kwargs):
                launches.append(argv)
                return await original(sys.executable, str(Path(__file__).with_name('fake_native.py')), backend, **kwargs)
            with patch('asyncio.create_subprocess_exec', spawn):
                rt = make_runtime(core, core.store.get('agent', 'master'))
                try:
                    self.assertEqual(await asyncio.wait_for(rt.run('require-medium' if configured else 'one'), 3), 'native result')
                    session = core.store.get('agent', 'master')['session']
                    self.assertTrue(session)
                    self.assertEqual(await asyncio.wait_for(rt.run('two'), 3), 'native result')
                finally: await rt.close()
                rt = make_runtime(core, core.store.get('agent', 'master'))
                try: self.assertEqual(await asyncio.wait_for(rt.run('require-medium' if configured else 'three'), 3), 'native result')
                finally: await rt.close()
                self.assertEqual(core.store.get('agent', 'master')['session'], session)
            self.assertTrue(any(e['kind'].startswith('native:') for e in core.store.events()))
            self.assertEqual(len(launches), 2)
            if backend == 'claude':
                self.assertIn('--resume', launches[1])
                for argv in launches:
                    self.assertIn('--dangerously-skip-permissions', argv)
                    self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')
                    self.assertNotIn('acceptEdits', argv)
            else:
                for argv in launches: self.assertIn('--yolo', argv)
            core.store.db.close()

    async def test_codex_protocol_and_resume(self): await self.check_backend('codex')
    async def test_claude_protocol_and_resume(self): await self.check_backend('claude')

    async def test_codex_explicit_model_and_effort_survive_resume(self): await self.check_backend("codex", configured=True)
