"""Deploy visibility and preflight, using isolated repositories and the real HTTP ASGI app."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cthulhu.deploy import local_info
from cthulhu.protocol import sha256
from cthulhu.temple import Temple


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class DeployStatus(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.export = self.root / 'export'
        self.export.mkdir()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
            'commit', '-q', '--allow-empty', '-m', 'main')
        self.main = git(self.repo, 'rev-parse', 'HEAD')
        self.t = Temple({'secret': 'test-only', 'db': str(self.root / 'temple.db'),
                         'deploy': {'code_root': str(self.export)}})
        self.t.store.add_session(sha256('test-session'), 'owner', 60)
        self.t.maintenance_state = AsyncMock()
        self.d = self.t.deployer

    async def http(self, path, method='GET', body=None, authenticated=True, csrf=True):
        headers = []
        if authenticated:
            headers.append((b'cookie', b'cthulhu_session=test-session'))
        if csrf:
            headers.append((b'x-cthulhu', b'1'))
        scope = {'type': 'http', 'http_version': '1.1', 'scheme': 'http',
                 'method': method, 'path': '/api/' + path, 'raw_path': ('/api/' + path).encode(),
                 'query_string': b'', 'headers': headers, 'server': ('test', 80),
                 'client': ('127.0.0.1', 1), 'root_path': ''}
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(body or {}).encode(), 'more_body': False}
        events = []
        async def send(event):
            events.append(event)
        await self.t.app()(scope, receive, send)
        return events[0]['status'], json.loads(b''.join(e.get('body', b'') for e in events))

    async def test_export_without_origin_is_explicit_and_cannot_drain(self):
        code, status = await self.http('deploy')
        self.assertEqual(code, 200)
        self.assertEqual(status['temple']['revision'], '')
        self.assertFalse(status['temple']['writable'])
        self.assertIn('Git', status['blocked']['__temple__'])
        code, latest = await self.http('deploy_latest')
        self.assertEqual(code, 200)
        self.assertIsNone(latest['revision'])
        self.assertIn('origin', latest['error'])
        code, rejected = await self.http('deploy', 'POST', {'galaxies': 'all'})
        self.assertEqual(code, 400)
        self.assertIn('Git', rejected['error'])
        self.assertEqual(self.d.jobs(), [])
        self.t.maintenance_state.assert_not_awaited()

    async def test_configured_repo_reads_only_main_even_from_export(self):
        git(self.repo, 'checkout', '-q', '-b', 'v3')
        git(self.repo, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
            'commit', '-q', '--allow-empty', '-m', 'v3')
        self.d.repo = str(self.repo)
        code, latest = await self.http('deploy_latest')
        self.assertEqual(code, 200)
        self.assertEqual(latest, {'revision': self.main, 'source': 'deploy.repo', 'ref': 'refs/heads/main', 'blocked': {}})
        self.assertIn('__temple__', self.d.status()['blocked'])
        self.d.root = self.repo
        self.d.me = local_info(self.repo)
        self.assertEqual(self.d.status()['blocked'], {})
        self.assertEqual(self.d.me['revision'], git(self.repo, 'rev-parse', 'HEAD'))
        git(self.repo, 'remote', 'add', 'origin', str(self.repo))
        self.d.repo = None
        self.assertEqual((await self.http('deploy_latest'))[1]['source'], 'origin')

    async def test_main_behind_v3_is_visible_but_rejected_before_drain(self):
        self.d.root = self.repo
        self.d.repo = str(self.repo)
        git(self.repo, 'checkout', '-q', '-b', 'v3')
        git(self.repo, '-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
            'commit', '-q', '--allow-empty', '-m', 'v3')
        self.d.me = local_info(self.repo)
        self.t.galaxies['peer'] = SimpleNamespace(deploy={**self.d.me, 'revision': self.main})
        _, latest = await self.http('deploy_latest')
        self.assertEqual(latest['revision'], self.main)
        self.assertIn('禁止回退', latest['blocked']['__temple__'])
        self.assertIn('peer', latest['blocked'])
        code, result = await self.http('deploy', 'POST', {'galaxies': 'all'})
        self.assertEqual(code, 400)
        self.assertIn('无法快进', result['error'])
        self.assertEqual(self.d.jobs(), [])
        self.t.maintenance_state.assert_not_awaited()
        git(self.repo, 'branch', '-f', 'main', 'v3')
        self.assertEqual((await self.http('deploy_latest'))[1]['blocked'], {})

    async def test_new_commit_missing_locally_keeps_authoritative_update_checks(self):
        self.d.root = self.repo
        self.d.me = local_info(self.repo)
        self.assertEqual(await self.d.target_blocks('f' * 40), {})

    async def test_export_cannot_offer_unverified_remote_rollback(self):
        self.d.repo = str(self.repo)
        self.t.galaxies['remote'] = SimpleNamespace(deploy={
            'host': 'remote', 'root': '/code', 'revision': 'a' * 40, 'writable': True})
        _, latest = await self.http('deploy_latest')
        self.assertEqual(latest['revision'], self.main)
        self.assertIn('无法核验', latest['blocked']['remote'])
        code, _ = await self.http('deploy', 'POST', {'galaxies': ['remote']})
        self.assertEqual(code, 400)
        self.t.maintenance_state.assert_not_awaited()

    async def test_v3_only_repository_does_not_become_main(self):
        git(self.repo, 'branch', '-m', 'v3')
        self.d.repo = str(self.repo)
        _, latest = await self.http('deploy_latest')
        self.assertIsNone(latest['revision'])
        self.assertIn('refs/heads/main', latest['error'])

    async def test_remote_failure_is_bounded_and_does_not_expose_credentials(self):
        self.d.repo = 'https://user:secret@example.invalid/private'
        with patch('cthulhu.deploy.git', AsyncMock(side_effect=RuntimeError(self.d.repo))) as query:
            code, latest = await self.http('deploy_latest')
        self.assertEqual(code, 200)
        self.assertIsNone(latest['revision'])
        self.assertIn('查询失败或超时', latest['error'])
        self.assertNotIn('secret', json.dumps(latest))
        self.assertEqual(query.call_args.kwargs['timeout'], 30)

    async def test_http_authentication_and_csrf_are_preserved(self):
        self.assertEqual((await self.http('deploy', authenticated=False))[0], 401)
        self.assertEqual((await self.http('deploy', 'POST', csrf=False))[0], 401)
        self.t.maintenance_state.assert_not_awaited()

    async def test_shared_group_needs_writable_updater_but_cannot_borrow_for_temple(self):
        def connection(root, writable):
            return SimpleNamespace(deploy={'host': 'node', 'root': root, 'revision': self.main, 'writable': writable})
        self.t.galaxies = {'a': connection('/code', False), 'b': connection('/code', True)}
        self.assertNotIn('error', self.d.groups(['a'])[0])
        self.t.galaxies['b'].deploy['writable'] = False
        self.assertIn('权限', self.d.groups(['a'])[0]['error'])
        self.d.me = dict(self.t.galaxies['a'].deploy)
        self.t.galaxies['b'].deploy['writable'] = True
        self.assertIn('error', self.d.groups(['a'])[0])
        with self.assertRaisesRegex(ValueError, '权限'):
            await self.d.start(['a'], by='owner', revision=self.main)
        self.t.maintenance_state.assert_not_awaited()

    async def test_busy_and_recovery_reasons_match_start_guards(self):
        for state, groups, reason in [('running', [], '进行中'),
                                     ('failed', [{'recovery_required': True}], '未确认的回滚')]:
            self.t.store.set_setting('deploy_jobs', [{'id': 'job', 'state': state, 'groups': groups}])
            self.assertIn(reason, self.d.status()['blocked_reason'])
            with self.assertRaisesRegex(ValueError, reason):
                await self.d.start('all', by='owner', revision=self.main)
        self.t.maintenance_state.assert_not_awaited()

    def test_export_inside_repository_does_not_inherit_parent_revision(self):
        nested = self.repo / 'export'
        nested.mkdir()
        self.assertEqual(local_info(nested)['revision'], '')
        self.assertFalse(local_info(nested)['writable'])

    @unittest.skipIf(os.geteuid() == 0, 'root ignores filesystem permission bits')
    def test_worktree_checks_common_git_directory_permissions(self):
        linked = self.root / 'linked'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'linked', str(linked))
        self.assertTrue(local_info(linked)['writable'])
        common = self.repo / '.git'
        original_mode = common.stat().st_mode
        try:
            common.chmod(0o555)
            info = local_info(linked)
            self.assertEqual(info['revision'], self.main)
            self.assertFalse(info['writable'])
            self.assertIn('权限', info['error'])
        finally:
            common.chmod(original_mode)
