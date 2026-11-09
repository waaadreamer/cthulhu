import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, AsyncMock

from cthulhu_v4.antigravity import claude_credentials, native_env, gateway_models, catalog
from cthulhu_v4.runtime import make_runtime
from cthulhu_v4.store import Store
from cthulhu_v4.node import Turn
from cthulhu_v4.models import discover, clean
from test_runtime import FakeSink
from helpers import Harness, task_args


class ConfigTests(unittest.TestCase):
    def test_native_stream_survives_ui_history_filter_and_counts_as_review_evidence(self):
        store = Store(':memory:')
        try:
            seq = store.emit('native:agy_update', actor='old_test', data={
                'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'evidence'}})
            self.assertEqual([e['seq'] for e in store.events(ui=True, compact=True)], [seq])
            self.assertEqual(store.review_highwater(), seq)
        finally: store.close()

    def test_settings_override_stale_shell_and_session_project_overrides_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); user = root / 'claude'; user.mkdir()
            (user / 'settings.json').write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'correct', 'ANTHROPIC_BASE_URL': 'https://gateway.example'}}))
            env = {'CLAUDE_CONFIG_DIR': str(user), 'ANTHROPIC_AUTH_TOKEN': 'stale', 'ANTHROPIC_BASE_URL': 'https://stale.example'}
            self.assertEqual(claude_credentials(env), ('correct', 'https://gateway.example'))
            (root / '.claude').mkdir()
            (root / '.claude/settings.local.json').write_text(json.dumps({'env': {'ANTHROPIC_AUTH_TOKEN': 'session'}}))
            self.assertEqual(claude_credentials(env, root), ('session', 'https://gateway.example'))

    def test_missing_base_never_sends_inherited_key_to_google_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, '不会回退'):
                claude_credentials({'CLAUDE_CONFIG_DIR': tmp, 'ANTHROPIC_AUTH_TOKEN': 'secret'})
        env = native_env({'GEMINI_API_KEY': 'other', 'GOOGLE_API_KEY': 'other', 'GOOGLE_GEMINI_BASE_URL': 'https://google.example'}, 'same-key', 'https://same.example')
        self.assertEqual(env, {'GEMINI_API_KEY': 'same-key', 'GOOGLE_GEMINI_BASE_URL': 'https://same.example'})

    def test_redirect_does_not_forward_auth_and_unknown_path_is_not_guessed(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, self.headers.get('x-goog-api-key')))
                self.send_response(302); self.send_header('Location', '/stolen'); self.end_headers()
            def log_message(self, *a): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever); thread.start()
        try:
            with self.assertRaisesRegex(ValueError, 'HTTP 302'):
                gateway_models('secret', f'http://127.0.0.1:{server.server_port}', {})
            self.assertEqual(calls, [('/v1beta/models', 'secret')])
            with self.assertRaisesRegex(ValueError, '路径'):
                gateway_models('secret', 'https://gateway.example/v1', {})
        finally:
            server.shutdown(); thread.join(); server.server_close()


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_directory_does_not_claim_gateway_inference_available(self):
        class Proc:
            returncode = 0
            async def communicate(self): return b'gemini-4-argon-high\tGemini 4\n', b''
        with patch('cthulhu_v4.antigravity.claude_credentials', return_value=('secret', 'https://same.example')), \
             patch('asyncio.create_subprocess_exec', AsyncMock(return_value=Proc())) as launch, \
             patch('cthulhu_v4.antigravity.gateway_models', return_value=[{'name': 'claude-opus'}]):
            result = await catalog({})
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['list'][0]['availability'], 'unavailable')
        self.assertIn('不代表', result['source'])
        self.assertEqual(launch.call_args.kwargs['env']['GOOGLE_GEMINI_BASE_URL'], 'https://same.example')
        self.assertNotIn('secret', json.dumps(result))

    async def test_unconfigured_and_failed_refresh_never_invent_models(self):
        with patch('cthulhu_v4.antigravity.claude_credentials', side_effect=ValueError('待配置')):
            result = await discover(['antigravity'], {})
        self.assertEqual(result['antigravity']['list'], [])
        self.assertEqual(result['antigravity']['status'], 'unconfigured')
        self.assertEqual(clean(result)['antigravity']['status'], 'unconfigured')


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = FakeSink(); self.sink.native_dir = Path(self.tmp.name) / 'native'
        self.agent = {'id': 'master', 'backend': 'antigravity', 'cwd': self.tmp.name, 'session': None, 'model': 'gemini-4-argon-high'}
        self.launches, self.envs = [], []
        original = asyncio.create_subprocess_exec
        async def spawn(*argv, **kwargs):
            self.launches.append(argv); self.envs.append(kwargs['env'])
            return await original(sys.executable, str(Path(__file__).with_name('fake_agy.py')), *argv[1:], **kwargs)
        self.patches = [patch('asyncio.create_subprocess_exec', spawn),
                        patch('cthulhu_v4.agy_runtime.claude_credentials', return_value=('secret-key', 'https://same.example'))]
        for p in self.patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    async def run_turn(self, text):
        rt = make_runtime(self.sink, self.agent)
        try: return await asyncio.wait_for(rt.run(text), 3)
        finally: await rt.close()

    async def test_native_pointer_mcp_model_and_reload_without_replayed_text(self):
        self.assertEqual(await self.run_turn('one'), 'native result')
        self.agent['session'] = self.sink.sessions[0]
        self.assertEqual(await self.run_turn('two'), 'native result')
        self.assertEqual(self.sink.sessions, ['agy-session'])
        for argv, env in zip(self.launches, self.envs):
            self.assertEqual(argv[:2], ('agy', 'acp'))
            self.assertIn('--dangerously-skip-permissions', argv)
            self.assertEqual(env['GEMINI_API_KEY'], 'secret-key')
            self.assertEqual(env['GOOGLE_GEMINI_BASE_URL'], 'https://same.example')
            self.assertEqual(argv[argv.index('--gemini-dir') + 1], str(self.sink.native_dir))
        self.assertEqual(self.sink.pids[-1], None)

    async def test_failed_load_never_creates_replacement_session(self):
        self.agent['session'] = 'agy-session'
        with patch.dict(os.environ, FAKE_AGY_MODE='load-fails'):
            with self.assertRaisesRegex(RuntimeError, 'session missing'): await self.run_turn('one')
        self.assertEqual(self.sink.sessions, [])
        self.assertEqual(len(self.launches), 1)

    async def test_native_fatal_notification_overrides_misleading_end_turn(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP 400 protocol unsupported') as caught:
            await self.run_turn('fatal')
        self.assertNotIn('secret-key', str(caught.exception))

    async def test_plain_quote_is_not_fatal_and_old_log_does_not_pollute_next_turn(self):
        rt = make_runtime(self.sink, self.agent)
        try:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
                await asyncio.wait_for(rt.run('fatal'), 3)
            result = await asyncio.wait_for(rt.run('quote'), 3)
            self.assertIn('The CLI may report:', result)
            self.assertEqual(await asyncio.wait_for(rt.run('next'), 3), 'native result')
            with self.assertRaisesRegex(RuntimeError, '详见所属节点原生日志') as caught:
                await asyncio.wait_for(rt.run('fatal-nolog'), 3)
            self.assertNotIn('HTTP 400', str(caught.exception))
        finally: await rt.close()

    async def test_fatal_notice_without_log_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'CLI 已报告终止错误'):
            await self.run_turn('fatal-nolog')

    async def test_cross_session_events_and_cancelled_results_fail(self):
        for text in ('wrong-session', 'cancelled', 'rpc-error'):
            with self.assertRaises(RuntimeError) as caught: await self.run_turn(text)
            self.assertNotIn('secret-key', str(caught.exception))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_failure_evidence_is_scoped_to_node_credential_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            try:
                h.node.models = {'antigravity': {'list': [], 'at': 1, 'error': None}}
                h.node.model_profiles['antigravity'] = 'profile-a'
                turn = Turn(h.node, {'id': 'fixture', 'agent': 'master'})
                turn.model_evidence('profile-a', 'protocol_unsupported', '不支持 Gemini 协议', 'gemini-4-argon-high')
                await h.sync()
                info = h.c.store.get('node', 'local')['models']['antigravity']
                self.assertEqual(info['inference_status'], 'protocol_unsupported')
                turn.model_evidence('profile-b', 'completed', '', 'gemini-4-argon-medium')
                self.assertEqual(h.node.models['antigravity']['inference_status'], 'protocol_unsupported')
                self.assertEqual(h.node.store.get('model_evidence', 'profile-b')['inference_status'], 'completed')
                self.assertNotIn('profile-a', json.dumps(h.c.hub.nodes()))
            finally: await h.close()

    async def test_backend_model_and_refresh_round_trip_use_existing_node_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(Path(tmp))
            try:
                h.node.backends.append('antigravity')
                await h.sync()
                identity = h.node.instance
                n = h.c.store.get('node', 'local')
                n['models'] = {'antigravity': {'list': [{'id': 'gemini-4-argon-high'}]}}
                h.c.store.put('node', n)
                task = h.c.create_task(task_args(tmp, backend='antigravity', model='gemini-4-argon-high'))
                with self.assertRaisesRegex(ValueError, '完整模型 slug'):
                    h.c.create_task(task_args(tmp, backend='antigravity', model='gemini-4-argon'))
                a = h.c.store.get('agent', task['agent'])
                self.assertEqual((a['node'], a['backend'], a['model']), ('local', 'antigravity', 'gemini-4-argon-high'))
                # 暂停该协议任务，测试只验证刷新路由，不隐式启动真实原生 CLI。
                a['paused'] = True; h.c.store.put('agent', a)
                request = h.c.refresh_node_models('local')['requested']
                self.assertEqual(h.c.refresh_node_models('local')['requested'], request)
                await h.sync()
                self.assertEqual(h.node.models_requested, request)
                h.node.models = {'antigravity': {'list': [], 'at': 1, 'error': '待配置', 'status': 'unconfigured'}}
                h.node.models_completed = request; h.node.models_dirty = True
                await h.sync()
                n = h.c.store.get('node', 'local')
                self.assertNotIn('models_refresh', n)
                self.assertEqual(n['models']['antigravity']['status'], 'unconfigured')
                self.assertEqual(h.node.instance, identity)
            finally: await h.close()
