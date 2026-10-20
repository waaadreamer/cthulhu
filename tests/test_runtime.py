"""原生适配器协议：用协议夹具子进程验证启动参数、权限回执与 session 恢复，不调用模型。"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cthulhu_v4.runtime import make_runtime


class FakeSink:
    url, token, proxy = 'http://127.0.0.1:1', 'test-token', None

    def __init__(self): self.events, self.sessions, self.pids = [], [], []
    def native(self, kind, payload): self.events.append(kind)
    def session(self, sid): self.sessions.append(sid)
    def pid(self, pid, identity): self.pids.append(pid)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def check_backend(self, backend, configured=False):
        sink = FakeSink()
        agent = {'id': 'master', 'backend': backend, 'cwd': str(Path.cwd()), 'session': None,
                 'model': 'gpt-6-astra' if configured else None, 'reasoning_effort': 'medium' if configured else None}
        original = asyncio.create_subprocess_exec
        launches = []
        async def spawn(*argv, **kwargs):
            launches.append(argv)
            return await original(sys.executable, str(Path(__file__).with_name('fake_native.py')), backend, **kwargs)
        first = 'require-medium' if configured else 'one'
        with patch('asyncio.create_subprocess_exec', spawn):
            rt = make_runtime(sink, agent)
            try:
                self.assertEqual(await asyncio.wait_for(rt.run(first), 3), 'native result')
                self.assertTrue(sink.sessions)
                self.assertEqual(await asyncio.wait_for(rt.run('two'), 3), 'native result')
            finally: await rt.close()
            agent['session'] = sink.sessions[0]
            rt = make_runtime(sink, agent)
            try: self.assertEqual(await asyncio.wait_for(rt.run(first), 3), 'native result')
            finally: await rt.close()
        self.assertEqual(set(sink.sessions), {agent['session']})
        self.assertIn(None, sink.pids)  # 关闭后清除 PID 记录
        self.assertTrue(sink.events)
        self.assertEqual(len(launches), 2)
        if backend == 'claude':
            self.assertIn('--resume', launches[1])
            for argv in launches:
                self.assertIn('--dangerously-skip-permissions', argv)
                self.assertEqual(argv[argv.index('--permission-mode') + 1], 'bypassPermissions')
        else:
            for argv in launches: self.assertIn('--yolo', argv)

    async def test_codex_protocol_and_resume(self): await self.check_backend('codex')
    async def test_claude_protocol_and_resume(self): await self.check_backend('claude')
    async def test_codex_explicit_model_and_effort_survive_resume(self): await self.check_backend('codex', configured=True)

    async def test_resume_timeout_reports_which_step_timed_out(self):
        from cthulhu_v4.runtime import CodexRuntime
        sink = FakeSink()
        agent = {'id': 'master', 'backend': 'codex', 'cwd': str(Path.cwd()), 'session': 'hang', 'model': None, 'reasoning_effort': None}
        original = asyncio.create_subprocess_exec
        async def spawn(*argv, **kwargs):
            return await original(sys.executable, str(Path(__file__).with_name('fake_native.py')), 'codex', **kwargs)
        with patch('asyncio.create_subprocess_exec', spawn), patch.dict(CodexRuntime.TIMEOUTS, {'thread/resume': 0.5}):
            rt = CodexRuntime(sink, agent)
            try:
                with self.assertRaisesRegex(RuntimeError, 'thread/resume 超过 0.5 秒未响应'):
                    await rt.run('x')
            finally:
                await rt.close()
        self.assertIn('resume_timing', sink.events)
        self.assertGreaterEqual(CodexRuntime.TIMEOUTS['thread/resume'], 300)

    def test_proxy_only_for_model_cli_not_for_agent_commands(self):
        from cthulhu_v4 import proxy
        from cthulhu_v4.runtime import CodexRuntime
        sink = FakeSink(); sink.proxy = 'http://127.0.0.1:7897'
        agent = {'id': 'master', 'backend': 'codex', 'cwd': '/', 'session': None}
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://leaked:1', 'no_proxy': 'x'}):
            env = CodexRuntime(sink, agent).env()
            self.assertEqual(env['HTTPS_PROXY'], 'http://127.0.0.1:7897')       # 外层漏进来的代理被替换
            self.assertIn('127.0.0.1', env['NO_PROXY'])                           # 本机 MCP 桥直连
            sink.proxy = None
            env = CodexRuntime(sink, agent).env()
            self.assertEqual(env['HTTPS_PROXY'], 'http://leaked:1')               # 未显式配置：保持节点原环境
        self.assertIn('shell_environment_policy.exclude=["*proxy*", "*PROXY*"]', ' '.join(proxy.codex_args('http://p:1')))
        self.assertEqual(proxy.codex_args(None), [])
        g = proxy.git_env('http://127.0.0.1:7897')
        self.assertIn('nc -X connect -x 127.0.0.1:7897', g['GIT_SSH_COMMAND'])

    async def test_lines_reader_handles_lines_over_asyncio_limit(self):
        from cthulhu_v4.runtime import lines, bounded
        reader = asyncio.StreamReader()
        big = b'{"a": "' + b'x' * (20 * 1024 * 1024) + b'"}\n'
        reader.feed_data(b'first\n' + big + b'tail-without-newline'); reader.feed_eof()
        got = [l async for l in lines(reader)]
        self.assertEqual([len(l) for l in got], [6, len(big), 20])
        small = bounded({'a': 'x' * (20 * 1024 * 1024)}, len(big))
        self.assertLess(len(str(small)), 100000)
        self.assertIn('截断', small['_truncated'])
