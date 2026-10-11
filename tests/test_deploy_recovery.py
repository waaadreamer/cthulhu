import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cthulhu.deploy import Deployer
from cthulhu.temple import Temple, Denied

PREV = 'a' * 40
TARGET = 'b' * 40


class Store:
    def __init__(self, jobs=()):
        self.data = {'deploy_jobs': list(jobs)}

    def setting(self, key):
        return copy.deepcopy(self.data.get(key))

    def set_setting(self, key, value):
        self.data[key] = copy.deepcopy(value)


class Recovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = SimpleNamespace(deploy={'revision': PREV, 'writable': True},
                                    capabilities=['deploy_recovery_v1'], rpc=AsyncMock())
        self.temple = SimpleNamespace(config={}, store=Store(), galaxies={'g': self.conn},
                                     broadcast=AsyncMock(), notify_deploy=AsyncMock(),
                                     maintenance_state=AsyncMock(return_value={'safe_to_restart': True}))
        self.d = Deployer(self.temple)
        self.d.me['revision'] = PREV
        self.d.restart_temple = Mock()
        self.d.wait_online = AsyncMock()
        self.d.wait_drained = AsyncMock()
        self.g = {'galaxies': ['g'], 'temple': True, 'state': 'pending'}
        self.job = {'id': 'job', 'state': 'running', 'groups': [self.g], 'revision': TARGET,
                    'steps': [], 'finished_at': None}

    async def test_unexpected_restart_resumes_once_and_releases_drain(self):
        self.temple.store.set_setting('deploy_jobs', [self.job])
        self.conn.deploy['revision'] = TARGET
        self.d.me['revision'] = TARGET
        with patch('cthulhu.deploy.git', AsyncMock(return_value=TARGET)), \
             patch('cthulhu.deploy.update_code', AsyncMock(return_value={
                 'prev': TARGET, 'revision': TARGET, 'unchanged': True})):
            await self.d.resume()
            task = self.d.running['job']
            await self.d.resume()
            self.assertIs(task, self.d.running['job'])
            await task
        self.assertEqual(self.d.job('job')['state'], 'done')
        self.temple.maintenance_state.assert_any_await('g', False)

    async def test_crash_during_rollback_never_republishes_failed_target(self):
        self.g.update(recovery_required=True, previous_revision=PREV, phase='restarting')
        self.temple.store.set_setting('deploy_jobs', [self.job])
        self.d.restart_galaxies = AsyncMock(return_value=True)
        with patch('cthulhu.deploy.git', AsyncMock(return_value=PREV)), \
             patch('cthulhu.deploy.rollback', AsyncMock()), \
             patch('cthulhu.deploy.update_code', AsyncMock()) as update:
            await self.d.resume()
            await self.d.running['job']
            update.assert_not_awaited()
        self.assertEqual(self.d.job('job')['state'], 'failed')
        self.assertNotIn('recovery_required', self.d.job('job')['groups'][0])

    async def test_new_temple_process_must_reload_rollback_before_undrain(self):
        self.d.me['revision'] = TARGET
        self.g.update(recovery_required=True, previous_revision=PREV)
        self.d.restart_galaxies = AsyncMock(return_value=True)
        with patch('cthulhu.deploy.git', AsyncMock(return_value=PREV)), \
             patch('cthulhu.deploy.rollback', AsyncMock()):
            self.assertFalse(await self.d.recover_group(self.job, self.g))
            self.assertTrue(self.g['recovery_required'])
            self.d.restart_temple.assert_called_once()
            self.assertNotIn(unittest.mock.call('g', False), self.temple.maintenance_state.await_args_list)
            self.d.me['revision'] = PREV
            self.assertTrue(await self.d.recover_group(self.job, self.g))

    async def test_restart_exception_rolls_back_before_resuming_delivery(self):
        events = []
        async def update(*args):
            persisted = self.d.job('job')['groups'][0]
            self.assertEqual(persisted['previous_revision'], PREV)
            self.assertEqual(persisted['phase'], 'updating')
            return {'prev': PREV, 'revision': TARGET}
        async def rollback(*args):
            events.append('rollback')
        async def restart(*args, **kwargs):
            if not events:
                raise ConnectionError('lost RPC')
            events.append('confirmed')
            return True
        async def maintenance(gid, enabled):
            if not enabled:
                events.append('resume')
        self.temple.maintenance_state.side_effect = maintenance
        self.d.restart_galaxies = AsyncMock(side_effect=restart)
        with patch('cthulhu.deploy.git', AsyncMock(return_value=PREV)), \
             patch('cthulhu.deploy.update_code', AsyncMock(side_effect=update)), \
             patch('cthulhu.deploy.rollback', AsyncMock(side_effect=rollback)):
            await self.d.run(self.job)
        self.assertEqual(events, ['rollback', 'confirmed', 'resume'])
        self.assertEqual(self.job['state'], 'failed')
        self.assertNotIn('recovery_required', self.g)

    async def test_failed_rollback_stays_drained_and_has_explicit_retry(self):
        self.d.restart_galaxies = AsyncMock(return_value=False)
        with patch('cthulhu.deploy.git', AsyncMock(return_value=PREV)), \
             patch('cthulhu.deploy.update_code', AsyncMock(return_value={'prev': PREV, 'revision': TARGET})), \
             patch('cthulhu.deploy.rollback', AsyncMock()):
            await self.d.run(self.job)
            self.assertTrue(self.g['recovery_required'])
            self.assertNotIn(unittest.mock.call('g', False), self.temple.maintenance_state.await_args_list)
            with self.assertRaisesRegex(ValueError, '未确认的回滚'):
                await self.d.start([], by='owner', revision=TARGET)
            self.d.restart_galaxies.return_value = True
            await self.d.recover('job', by='owner')
            await self.d.running['job']
        self.assertFalse(self.d.job('job')['groups'][0].get('recovery_required'))
        self.temple.maintenance_state.assert_any_await('g', False)
        self.assertEqual(self.d.job('job')['state'], 'failed')  # Failed release isn't relabelled success.

    async def test_unchanged_checkout_still_restarts_stale_runtime(self):
        self.g.update(previous_revision=PREV, phase='restarting')
        self.d.restart_galaxies = AsyncMock(return_value=True)
        with patch('cthulhu.deploy.update_code', AsyncMock(return_value={
                'prev': TARGET, 'revision': TARGET, 'unchanged': True})):
            await self.d.deploy_group(self.job, self.g)
        self.d.restart_galaxies.assert_awaited_once()
        self.assertEqual(self.g['update']['prev'], PREV)

    async def test_lost_restart_ack_uses_actual_authenticated_revision(self):
        async def lost_ack(*args, **kw):
            self.conn.deploy['revision'] = TARGET
            raise ConnectionError('ACK lost')
        self.conn.rpc.side_effect = lost_ack
        self.assertTrue(await self.d.restart_galaxies(
            self.job, ['g'], {'prev': PREV, 'revision': TARGET}, True, None))

    async def test_remote_without_recovery_capability_is_not_sent_unknown_rpc(self):
        self.g.update(temple=False, previous_revision=PREV, updater='g')
        self.conn.capabilities = []
        self.assertFalse(await self.d.recover_group(self.job, self.g))
        self.conn.rpc.assert_not_awaited()
        self.assertTrue(self.g['recovery_required'])

    async def test_remote_recovery_requires_ack_and_running_revision(self):
        self.g.update(temple=False, previous_revision=PREV, updater='g')
        self.d.restart_galaxies = AsyncMock(return_value=True)
        self.assertTrue(await self.d.recover_group(self.job, self.g))
        self.conn.rpc.assert_awaited_once_with('deploy_rollback', timeout=1200, prev=PREV, target=TARGET)
        self.d.restart_galaxies.assert_awaited_once()

    async def test_recovery_permission_is_server_enforced(self):
        temple = SimpleNamespace(deployer=self.d)
        for role in ['ghroth', 'shub', 'yog', 'old_one']:
            with self.assertRaises(Denied):
                await Temple.deploy_call(temple, role, 'agent', 'deploy', {'recover_job_id': 'job'})
        self.d.recover = AsyncMock(return_value={'ok': True})
        self.assertEqual(await Temple.deploy_call(temple, 'azathoth', 'az', 'deploy',
                                                {'recover_job_id': 'job'}), {'ok': True})
