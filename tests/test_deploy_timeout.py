"""Real hung deployment subprocesses must settle before returning an RPC error."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cthulhu import deploy
from cthulhu.protocol import Link


class CommandTimeout(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pids = self.root / 'pids.json'
        self.command = self.root / 'git'
        self.command.write_text(
            '#!' + sys.executable + '\n'
            'import json, os, signal, subprocess, sys, time\n'
            'from pathlib import Path\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            # Deliberately take longer to start than the command timeout. Startup is a separate handshake.
            'time.sleep(0.75)\n'
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f'ready = Path({str(self.pids)!r})\n'
            'ready.with_suffix(".tmp").write_text(json.dumps([os.getpid(), child.pid]))\n'
            'ready.with_suffix(".tmp").replace(ready)\n'
            'time.sleep(60)\n')
        self.command.chmod(0o755)
        self.communicate = deploy._communicate

    def tearDown(self):
        self.temp.cleanup()

    def assert_settled(self):
        self.assertTrue(self.pids.exists(), 'fixture command actually started')
        for pid in json.loads(self.pids.read_text()):
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                   capture_output=True, text=True).stdout.strip()
            self.assertTrue(not state or state.startswith('Z'), (pid, state))

    async def started(self):
        while not self.pids.exists():
            await asyncio.sleep(0.01)

    async def after_start(self, process, timeout, operation):
        # Test the original timeout/kill/wait path after both fixture PIDs exist.
        # Otherwise a loaded machine may correctly kill Python before it writes the marker.
        try:
            await asyncio.wait_for(self.started(), 5)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise
        return await self.communicate(process, timeout, operation)

    async def test_git_timeout_has_nonempty_rpc_error_and_no_running_children(self):
        async def on_frame(frame):
            return await deploy.git(self.root, 'fetch', 'origin', 'main', timeout=0.5)
        replies = []
        async def emit(frame):
            replies.append(frame)
        link = SimpleNamespace(on_frame=on_frame, emit=emit)
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}), \
             patch.object(deploy, '_communicate', self.after_start):
            with self.assertLogs('cthulhu.protocol', level='ERROR'):
                await Link._dispatch(link, {'type': 'rpc', 'id': 'fixture-update', 'method': 'deploy_update'})
        self.assertEqual(replies[0]['id'], 'fixture-update')
        self.assertFalse(replies[0]['ok'])
        self.assertEqual(replies[0]['error'], 'git fetch timed out after 0.5s; subprocess group stopped')
        self.assert_settled()

    async def test_dependency_command_timeout_settles_its_children(self):
        with patch.object(deploy, '_communicate', self.after_start), \
             self.assertRaisesRegex(RuntimeError, 'timed out after 0.5s'):
            await deploy._run(str(self.command), cwd=self.root, timeout=0.5)
        self.assert_settled()

    async def test_cancellation_preserved_after_subprocess_cleanup(self):
        task = asyncio.create_task(deploy._run(str(self.command), cwd=self.root))
        try:
            await asyncio.wait_for(self.started(), 5)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_settled()
