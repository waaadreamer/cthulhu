"""Real isolated server workflow and guarded supervision decisions; no production tasks."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from cthulhu.temple import Temple
from cthulhu.roles import Denied
from cthulhu.store import Store
from test_universe import HarnessServer


class ContinuationServer(HarnessServer):
    patrol_interval = 1

    def test_periodic_yog_resumes_without_owner_message(self):
        store = Store(self.dir / 'temple.db')
        task = store.add_task('AUTO_CONTINUATION_TEST')
        for text in ('obsolete instruction', 'LATEST_ONLY_6789 preserve policies and training'):
            msg = store.add_message('az', 'sh', text, task_id=task['id'])
            store.mark(msg['id'], 'handled')
        reminder = self.wait(lambda: next(m for m in self.api('messages?agent_id=sh')
                            if m['sender'] == 'yo' and m['task_id'] == task['id']), 20)
        worker = self.wait(lambda: next(a for a in self.api('directory')['agents']
                           if a['role'] == 'old_one' and a['task'] == 'LATEST_ONLY_6789 preserve policies and training'), 20)
        completed = self.wait(lambda: store.turn_log(worker['id']), 20)
        self.assertEqual(completed[-1]['status'], 'completed')
        self.assertIn('tool:', completed[-1]['reply'])
        self.wait(lambda: (store.setting('continuations') or {}).get(task['id'], {}).get('phase') == 'executing', 10)
        self.assertEqual(store.agent(worker['id'])['spec']['task_id'], task['id'])
        shub_turn = next(r for r in store.turn_log('sh') if 'Automatic continuation review' in r['input'])
        self.assertIn('LATEST_ONLY_6789', shub_turn['input'])
        self.assertIn('续办前最新状态', shub_turn['input'])
        time.sleep(2)
        reminders = [m for m in self.api('messages?agent_id=sh') if m['sender'] == 'yo' and m['task_id'] == task['id']]
        self.assertEqual(len(reminders), 1)
        self.assertFalse(store.q("SELECT * FROM messages WHERE sender='owner'"))
        self.assertEqual(len([a for a in store.agents() if a['role'] == 'old_one']), 1)
        print('CONTINUATION_SERVER ' + json.dumps({'port': self.port, 'task_id': task['id'],
              'reminder': reminder['id'], 'executor': worker['id'], 'turn_status': completed[-1]['status'],
              'reminder_seconds': reminder['created_at'] - task['created_at'], 'owner_messages': 0}), flush=True)
        store.set_task(task['id'], 'done')
        store.db.close()


class ContinuationGuards(unittest.IsolatedAsyncioTestCase):
    def test_shub_batches_different_tasks_separately(self):
        from cthulhu.gods import Shub
        shub = object.__new__(Shub)
        shub.queue = [{'id': str(i), 'task_id': task, 'priority': 0, 'created_at': i}
                      for i, task in enumerate(('a', 'b', 'a'))]
        self.assertEqual([m['id'] for m in shub.take_batch()], ['0', '2'])
        self.assertEqual([m['id'] for m in shub.queue], ['1'])

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Temple({'secret': 'test', 'db': str(Path(self.tmp.name) / 'db'),
                         'galaxies': [{'id': 'g', 'name': 'g', 'token': 'test'}],
                         'gods': [{'id': r, 'role': r, 'galaxy': 'g'} for r in ('azathoth', 'shub', 'yog')]})
        self.c = self.t.continuations
        self.task = self.t.store.add_task('test')['id']
        self.instruction('initial authorized goal')
        self.t.store.upsert_agent('old', 'worker', 'old_one', 'g', 'shub', {'task_id': self.task})
        self.result = {'agent_id': 'old', 'online': True, 'state': 'idle', 'last_task': self.task, 'finished_at': 1}
        self.t.latest_result = AsyncMock(side_effect=lambda _: dict(self.result))
        self.t.deliver = AsyncMock()
        self.t.broadcast = AsyncMock()

    async def asyncTearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    def instruction(self, text):
        m = self.t.store.add_message('azathoth', 'shub', text, task_id=self.task)
        self.t.store.mark(m['id'], 'handled')
        return m

    async def review(self, state='ready', **kw):
        snap = await self.c.snapshot(self.task)
        return await self.c.review('yog', {'task_id': self.task, 'revision': snap['revision'],
                                         'state': state, 'evidence': 'Verified no external workload; dependency satisfied.', **kw})

    def records(self):
        return self.t.store.setting('continuations') or {}

    def expire(self):
        records = self.records()
        records[self.task]['deadline'] = time.time() - 1
        self.t.store.set_setting('continuations', records)

    async def test_dedup_persists_and_delivery_is_not_progress_escalates_once(self):
        first = await self.review()
        self.assertEqual(first['state'], 'reminded')
        self.t.store.mark(first['message_id'], 'handled')
        self.assertEqual((await self.review(evidence='Different wording'))['state'], 'duplicate')
        from cthulhu.continuation import Continuations
        self.c = self.t.continuations = Continuations(self.t)
        self.assertEqual((await self.review())['state'], 'duplicate')
        # A Shub progress report / dispatch receipt is not actual executor activity.
        self.t.store.add_message('shub', 'azathoth', 'sent', task_id=self.task, status='progress')
        self.expire()
        await self.c.scan('yog')
        await self.c.scan('yog')
        rows = self.t.store.messages(task_id=self.task)
        self.assertEqual(len([m for m in rows if m['sender'] == 'yog' and m['receiver'] == 'azathoth']), 1)
        self.assertEqual(self.records()[self.task]['phase'], 'escalated')

    async def test_active_wait_done_and_owner_decision_guards(self):
        for field, value in [('state', 'working'), ('active_runs', 1), ('background_busy', True),
                             ('draining', True), ('online', False)]:
            before = dict(self.result)
            self.result[field] = value
            self.assertEqual((await self.review())['state'], 'guarded')
            self.result = before
        self.assertEqual((await self.review('waiting'))['state'], 'waiting')
        self.assertEqual((await self.review())['state'], 'waiting')
        # A new external observer result is evidence of a readiness transition.
        self.result['finished_at'] = 2
        self.assertEqual((await self.review())['state'], 'reminded')
        self.t.store.set_task(self.task, 'done')
        self.assertEqual((await self.review())['state'], 'done')
        self.t.store.set_task(self.task, 'open')
        m = self.t.store.add_message('shub', 'azathoth', 'Owner decision required', task_id=self.task, status='blocked')
        self.t.store.mark(m['id'], 'handled')
        self.assertEqual((await self.review())['state'], 'guarded')
        self.instruction('Owner explicitly resolved decision')
        self.assertEqual((await self.review())['state'], 'reminded')

    async def test_progress_requires_task_scoped_actual_activity(self):
        await self.review()
        self.expire()
        self.result.update(state='working', current_task=self.task, working_since=time.time(), live=['tool()'])
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'executing')
        self.assertFalse([m for m in self.t.store.messages(task_id=self.task) if m['receiver'] == 'azathoth'])

    async def test_stale_assessment_and_new_instruction(self):
        row = await self.c.snapshot(self.task)
        self.instruction('newest boundary')
        self.assertEqual((await self.review(revision=row['revision']))['state'], 'changed')
        r = await self.review()
        self.assertIn('newest boundary', self.t.store.message(r['message_id'])['text'])
        self.assertEqual((await self.review())['state'], 'duplicate')

    async def test_wait_can_resume_on_new_external_evidence_without_message(self):
        await self.review('waiting', evidence='Download PID 123 is alive; checkpoint incomplete.')
        self.assertEqual((await self.review(evidence='Download finished; checksum verified; process exited.'))['state'], 'reminded')
        self.assertEqual((await self.review('waiting', evidence='Reworded unchanged state'))['state'], 'duplicate')
        self.assertEqual((await self.review())['state'], 'duplicate')

    async def test_owner_blocker_cannot_be_retried_with_reworded_evidence(self):
        await self.review('owner_blocked')
        self.result['finished_at'] = 5
        self.assertEqual((await self.review(evidence='Retry anyway'))['state'], 'owner_blocked')
        self.assertEqual((await self.review('waiting', evidence='Relabel without approval'))['state'], 'owner_blocked')
        self.instruction('Owner resolved the blocker; continue')
        self.assertEqual((await self.review())['state'], 'reminded')

    async def test_failed_idle_executor_can_retry_and_stopped_one_can_be_reassigned(self):
        self.result.update(error='previous turn failed', last_status='failed')
        self.assertEqual((await self.review())['state'], 'reminded')
        self.instruction('Reassign under existing authorization')
        self.t.store.set_agent('old', desired='stopped')
        self.assertEqual((await self.review())['state'], 'reminded')

    async def test_concurrent_reviews_and_reserved_message_recovery(self):
        results = await asyncio.gather(*(self.review() for _ in range(5)))
        self.assertEqual(sum(r['state'] == 'reminded' for r in results), 1)
        records = self.records()
        # Simulate the durable reservation made just before the message was stored.
        records[self.task]['message_id'] = 'reserved_unstored_test'
        self.t.store.set_setting('continuations', records)
        await self.c.scan('yog')
        await self.c.scan('yog')
        self.assertIsNotNone(self.t.store.message('reserved_unstored_test'))

    async def test_permissions_cannot_bypass_reminder_or_manage_executor(self):
        from types import SimpleNamespace
        for aid, method, p in [('shub', 'continuation_review', {}), ('old', 'continuation_tasks', {}),
                               ('yog', 'send', {'to': 'shub', 'text': 'bypass'}),
                               ('yog', 'restart', {'agent_id': 'old'})]:
            with self.assertRaises(Denied):
                await self.t.handle_agent_call(SimpleNamespace(id=aid), method, p)
