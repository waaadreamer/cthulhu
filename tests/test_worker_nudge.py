"""Direct worker supervision on isolated real processes plus task/health guards."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace

import test_continuation as continuation_tests
from test_universe import HarnessServer
from cthulhu.roles import Denied
from cthulhu.store import Store


class DirectWorkerServer(HarnessServer):
    patrol_interval = 1

    def test_scheduled_direct_worker_nudge_without_shub_relay(self):
        spawn = 'CALL temple_spawn ' + json.dumps({'name': 'nudge worker', 'galaxy_id': 'g1',
                 'task': 'Inspect existing fixture', 'skill': 'DIRECT_NUDGE_WORKER'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'title': 'Prepare worker', 'text': spawn}))
        store = Store(self.dir / 'temple.db')
        worker = self.wait(lambda: next(a for a in store.agents() if a['name'] == 'nudge worker'))
        self.wait(lambda: store.turn_log(worker['id']))
        task = store.add_task('DIRECT_NUDGE_TEST')['id']
        for sender, receiver, text in [('az', 'sh', 'LATEST_NUDGE_SCOPE preserve policies; do not restart workloads'),
                                       ('sh', worker['id'], 'Existing assignment is stalled in the test fixture')]:
            m = store.add_message(sender, receiver, text, task_id=task)
            store.mark(m['id'], 'handled')
        owner_count = len(store.q("SELECT id FROM messages WHERE sender='owner'"))
        self.wait(lambda: (store.setting('worker_nudges') or {}).get(task, {}).get(worker['id'], {}).get('phase') == 'responding', 20)
        messages = store.messages(task_id=task)
        nudges = [m for m in messages if m['sender'] == 'yo']
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0]['receiver'], worker['id'])
        self.assertIn('LATEST_NUDGE_SCOPE', nudges[0]['text'])
        self.assertIn('does not create work', nudges[0]['text'])
        self.assertFalse([m for m in nudges if m['receiver'] == 'sh'])
        self.assertIn('tool:', store.turn_log(worker['id'])[-1]['reply'])
        self.assertEqual(len(store.q("SELECT id FROM messages WHERE sender='owner'")), owner_count)
        time.sleep(2)
        self.assertEqual(len([m for m in store.messages(task_id=task) if m['sender'] == 'yo']), 1)
        print('DIRECT_WORKER_SERVER ' + json.dumps({'task_id': task, 'worker': worker['id'],
              'nudge': nudges[0]['id'], 'state': 'responding', 'new_owner_messages': 0, 'shub_relays': 0}), flush=True)
        store.set_task(task, 'done')
        store.db.close()


# Share setup helpers, not the parent regression tests.
class WorkerGuards(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = continuation_tests.ContinuationGuards.asyncSetUp
    asyncTearDown = continuation_tests.ContinuationGuards.asyncTearDown
    instruction = continuation_tests.ContinuationGuards.instruction
    review = continuation_tests.ContinuationGuards.review
    records = continuation_tests.ContinuationGuards.records
    async def worker_review(self, state='stalled', **kw):
        row = await self.c.snapshot(self.task)
        return await self.c.worker_review('yog', {'task_id': self.task, 'agent_id': 'old',
               'revision': row['revision'], 'state': state, 'evidence': 'Verified stalled existing assignment; no external work.', **kw})

    async def test_direct_dedup_persistent_unresponsiveness_escalates_once(self):
        self.result.update(state='working', current_task=self.task)
        r = await self.worker_review()
        self.assertEqual(r['state'], 'nudged')
        self.t.store.mark(r['message_id'], 'handled')
        self.assertEqual((await self.worker_review(evidence='Reworded evidence'))['state'], 'duplicate')
        records = self.t.store.setting('worker_nudges');records[self.task]['old']['deadline'] = 0
        self.t.store.set_setting('worker_nudges', records)
        await self.c.scan('yog');await self.c.scan('yog')
        self.assertEqual(self.t.store.setting('worker_nudges')[self.task]['old']['phase'], 'escalated')
        messages = self.t.store.messages(task_id=self.task)
        self.assertEqual(len([m for m in messages if m['sender'] == 'yog' and m['receiver'] == 'azathoth']), 1)
        self.assertFalse([m for m in messages if m['sender'] == 'yog' and m['receiver'] == 'shub'])

    async def test_healthy_activity_wait_done_ownerblocked_wrong_worker_guards(self):
        self.result.update(activity_task=self.task, last_activity_at=time.time())
        self.assertEqual((await self.worker_review())['state'], 'recent_activity')
        self.result.pop('last_activity_at')
        self.assertEqual((await self.worker_review('healthy'))['state'], 'healthy')
        self.assertEqual((await self.worker_review())['state'], 'healthy')
        await self.review('waiting')
        self.result['finished_at'] = 10
        self.assertEqual((await self.worker_review())['state'], 'guarded')
        self.t.store.set_setting('continuations', {})
        m = self.t.store.add_message('shub', 'azathoth', 'Needs owner decision', task_id=self.task, status='blocked')
        self.assertEqual((await self.worker_review())['state'], 'guarded')
        self.instruction('Owner resolves blocker')
        self.result['current_task'] = 'other_task'
        self.assertEqual((await self.worker_review())['state'], 'guarded')
        self.result['current_task'] = None
        self.t.store.set_task(self.task, 'done')
        self.assertEqual((await self.worker_review())['state'], 'guarded')
        with self.assertRaises(ValueError):
            await self.worker_review(agent_id='unrelated')
        with self.assertRaises(Denied):
            await self.t.handle_agent_call(SimpleNamespace(id='shub'), 'worker_review', {})

    async def test_actual_activity_after_nudge_stops_escalation(self):
        r = await self.worker_review()
        self.result.update(last_activity_at=time.time(), activity_task=self.task)
        await self.c.scan('yog')
        self.assertEqual(self.t.store.setting('worker_nudges')[self.task]['old']['phase'], 'responding')

    async def test_latest_scope_and_stale_revision(self):
        row = await self.c.snapshot(self.task)
        self.instruction('LATEST_SCOPE no new work or restarts')
        self.assertEqual((await self.worker_review(revision=row['revision']))['state'], 'changed')
        r = await self.worker_review()
        self.assertIn('LATEST_SCOPE', self.t.store.message(r['message_id'])['text'])

    async def test_wait_deadline_is_untouched_and_nudge_reservation_survives_restart(self):
        await self.review('waiting', check_after_seconds=900)
        original = self.records()[self.task].copy()
        self.assertEqual((await self.worker_review())['state'], 'guarded')
        self.assertEqual(self.records()[self.task], original)
        self.t.store.set_setting('continuations', {})
        r = await self.worker_review()
        from cthulhu.continuation import Continuations
        self.c = self.t.continuations = Continuations(self.t)
        self.assertEqual((await self.worker_review())['state'], 'duplicate')
        records = self.t.store.setting('worker_nudges')
        records[self.task]['old']['message_id'] = 'reserved_worker_nudge'
        self.t.store.set_setting('worker_nudges', records)
        await self.c.scan('yog');await self.c.scan('yog')
        self.assertEqual(self.t.store.message('reserved_worker_nudge')['receiver'], 'old')
