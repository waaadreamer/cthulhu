"""Repeated real scheduled collection/report cycles without model rearming or owner wakes."""
import json
import time
import unittest

import test_continuation as base
from test_universe import HarnessServer
from cthulhu.store import Store


class MultipleCycles(HarnessServer):
    patrol_interval = .3

    def test_three_collections_then_done_and_owner_summary(self):
        (self.dir / 'cycle-observations.json').write_text(json.dumps([
            {'completed': 1, 'done': False}, {'completed': 2, 'done': False}, {'completed': 3, 'done': True}]))
        spawn = 'CALL temple_spawn ' + json.dumps({'name': 'cycle worker', 'galaxy_id': 'g1',
                      'task': 'Collect existing fixture', 'skill': 'MULTICYCLE_WORKER'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'title': 'Prepare existing worker', 'text': spawn}))
        store = Store(self.dir / 'temple.db')
        worker = self.wait(lambda: next(a for a in store.agents() if a['name'] == 'cycle worker'))
        self.wait(lambda: store.turn_log(worker['id']))
        maintenance_spawn = 'CALL temple_spawn ' + json.dumps({'name': 'unrelated maintenance worker', 'galaxy_id': 'g1',
                            'task': 'SLEEP 30', 'skill': 'Keep separate task active'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'title': 'Independent maintenance', 'text': maintenance_spawn}))
        maintenance = self.wait(lambda: next(a for a in store.agents() if a['name'] == 'unrelated maintenance worker'))
        self.wait(lambda: store.turn_log(maintenance['id']))
        maintenance_task = maintenance['spec']['task_id']
        request = 'CALL temple_send ' + json.dumps({'to': maintenance['id'], 'task_id': maintenance_task, 'text': 'SLEEP 30'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'task_id': maintenance_task, 'text': request}))
        self.wait(lambda: next(a for a in self.api('directory')['agents']
                              if a['id'] == maintenance['id'] and a['state'] == 'working'))
        task = store.add_task('MULTICYCLE_TEST')['id']
        m = store.add_message('az', 'sh', 'LATEST_CYCLE_SCOPE EXISTING_EXECUTOR=' + worker['id'], task_id=task)
        store.mark(m['id'], 'handled')
        owners = len(store.q("SELECT id FROM messages WHERE sender='owner'"))
        records = store.setting('continuations') or {}
        records[task] = {'phase': 'waiting', 'at': time.time()-1, 'check_at': time.time()+86400, 'evidence': 'First fixture sample due', 'instruction_id': m['id']}
        store.set_setting('continuations', records)
        transitions = []
        def finished():
            rec = (store.setting('continuations') or {}).get(task, {})
            signature = (rec.get('phase'), rec.get('message_id'))
            if not transitions or transitions[-1] != signature:
                transitions.append(signature)
            return store.task(task)['state'] == 'done'
        self.wait(finished, 30)
        notes = self.wait(lambda: [t for t in self.api('chat?agent_id=az&limit=100')
                                  if t.get('task_id') == task and t.get('kind') == 'summary'], 10)
        self.assertIn('3/3', notes[0]['reply'])
        messages = store.messages(task_id=task)
        reminders = [m for m in messages if m['sender'] == 'yo' and m['receiver'] == 'sh']
        self.assertEqual(len(reminders), 3)
        reports = [m for m in messages if m['sender'] == worker['id']]
        self.assertEqual(len(reports), 3)
        self.assertTrue(all(m['task_id'] == task for m in reports))
        progress = [t for t in self.api('chat?agent_id=az&limit=100') if t.get('task_id') == task and t.get('kind') == 'progress']
        self.assertEqual(len(progress), 2)
        waits = [mid for phase, mid in transitions if phase == 'reminded']
        self.assertGreaterEqual(len(set(waits)), 3)
        self.assertEqual(len(store.q("SELECT id FROM messages WHERE sender='owner'")), owners)
        self.assertEqual(len([a for a in store.agents() if a['name'] == 'cycle worker']), 1)
        time.sleep(1.5)
        self.assertEqual(len([m for m in store.messages(task_id=task) if m['sender'] == 'yo']), 3)
        self.assertEqual(next(a for a in self.api('directory')['agents'] if a['id'] == maintenance['id'])['state'], 'working')
        print('MULTICYCLE_SERVER ' + json.dumps({'task_id': task, 'worker': worker['id'], 'collections': 3,
              'patrol_reminders': waits, 'owner_progress': 2, 'owner_summary': notes[0]['id'],
              'new_owner_messages': 0, 'model_rearm_calls': 0, 'unrelated_task_worker_active': maintenance['id']}), flush=True)
        store.db.close()


class CollectionRearm(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ContinuationGuards.asyncSetUp
    asyncTearDown = base.ContinuationGuards.asyncTearDown
    instruction = base.ContinuationGuards.instruction
    review = base.ContinuationGuards.review
    records = base.ContinuationGuards.records

    async def test_completed_inspection_is_collected_again_next_patrol_without_timer(self):
        await self.review('waiting')
        records = self.records();records[self.task]['check_at'] = 0
        self.t.store.set_setting('continuations', records)
        await self.c.scan('yog')
        start = self.records()[self.task]['at']
        stamp = start + .01
        self.result.update(last_task=self.task, finished_at=stamp, activity=['actual readonly tool'], last_status='completed')
        scan = await self.c.scan('yog')
        rec = self.records()[self.task]
        self.assertEqual(rec['phase'], 'reminded')
        self.assertGreater(rec['at'], start)
        self.assertNotIn('check_at', rec)
        self.assertIn('deployment', scan)
        mid = rec['message_id']
        self.instruction('Keep existing scope; no new external evidence')
        await self.review('waiting', evidence='Rephrased old result while collection is pending')
        self.assertEqual(self.records()[self.task]['message_id'], mid)

    async def test_inflight_tools_alone_do_not_claim_completed_collection(self):
        await self.review('waiting')
        records = self.records();records[self.task]['check_at'] = 0
        self.t.store.set_setting('continuations', records)
        await self.c.scan('yog')
        self.result.update(state='working', current_task=self.task, working_since=time.time(), live=['tool'])
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'executing')
        self.result.update(state='idle', last_task=self.task, finished_at=time.time(), activity=['tool'], last_status='completed')
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'reminded')

    async def test_deploy_request_does_not_make_maintainer_a_task_executor(self):
        self.t.store.upsert_agent('ghroth', 'maintainer', 'ghroth', 'g')
        self.t.store.add_message('ghroth', 'azathoth', 'deploy fix', kind='deploy_request', task_id=self.task)
        row = await self.c.snapshot(self.task)
        self.assertEqual([r['agent_id'] for r in row['executors']], ['old'])
        self.t.store.add_message('azathoth', 'ghroth', 'actual maintenance assignment', task_id=self.task)
        self.t.latest_result.side_effect = lambda aid: dict(self.result, agent_id=aid)
        row = await self.c.snapshot(self.task)
        self.assertEqual({r['agent_id'] for r in row['executors']}, {'old', 'ghroth'})

    async def test_started_but_failed_collection_escalates_once(self):
        await self.review('waiting')
        records = self.records()
        records[self.task]['check_at'] = 0
        self.t.store.set_setting('continuations', records)
        await self.c.scan('yog')
        self.result.update(state='working', current_task=self.task, working_since=time.time(), live=['tool'])
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'executing')
        records = self.records()
        records[self.task]['deadline'] = 0
        self.t.store.set_setting('continuations', records)
        self.result.update(state='idle', last_task=self.task, finished_at=time.time(), activity=['tool'], last_status='failed')
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'escalated')
        messages = self.t.store.messages(task_id=self.task)
        self.assertEqual(len([m for m in messages if m['id'].startswith('continuation_escalation_')]), 1)
        await self.c.scan('yog')
        self.assertEqual(self.t.store.messages(task_id=self.task), messages)

    async def test_maintenance_churn_does_not_change_eval_revision_or_guard(self):
        self.t.store.upsert_agent('ghroth', 'maintainer', 'ghroth', 'g')
        self.t.store.add_message('ghroth', 'azathoth', 'deploy fix', kind='deploy_request', task_id=self.task)
        row = await self.c.snapshot(self.task)
        # Field runtime activity evolves while the supervisor reasons: meaningful state is unchanged.
        self.result.update(last_activity_at=time.time(), activity_task=self.task)
        newer = await self.c.snapshot(self.task)
        self.assertEqual(row['revision'], newer['revision'])
        self.assertFalse(newer['active'])
        answer = await self.c.review('yog', {'task_id': self.task, 'revision': row['revision'],
                                      'state': 'waiting', 'evidence': 'existing background work'})
        self.assertEqual(answer['state'], 'waiting')
        await self.c.scan('yog')
        self.assertEqual(self.records()[self.task]['phase'], 'reminded')
        self.result.update(state='working', current_task=self.task)
        self.assertNotEqual(newer['revision'], (await self.c.snapshot(self.task))['revision'])

    async def test_two_waiting_tasks_only_guard_their_own_executor(self):
        from unittest.mock import AsyncMock
        other = self.t.store.add_task('Independent task')['id']
        self.t.store.upsert_agent('second', 'second worker', 'old_one', 'g', 'shub', {'task_id': other})
        second = dict(self.result, agent_id='second', last_task=other)
        self.t.latest_result = AsyncMock(side_effect=lambda aid: dict(self.result if aid == 'old' else second))
        self.result.update(state='working', current_task=self.task)
        self.t.store.set_setting('continuations', {self.task: {'phase':'waiting'}, other: {'phase':'waiting'}})
        before = await self.c.snapshot(other)
        await self.c.scan('yog')
        records = self.records()
        self.assertEqual(records[self.task]['phase'], 'waiting')
        self.assertEqual(records[other]['phase'], 'reminded')
        self.result.update(last_activity_at=time.time(), finished_at=time.time(), active_runs=3)
        after = await self.c.snapshot(other)
        self.assertEqual(before['revision'], after['revision'])
        # A worker previously associated with another task cannot churn it while working elsewhere.
        self.t.store.add_message('shub', 'old', 'historical assignment', task_id=other)
        self.t.store.mark(self.t.store.messages(task_id=other)[-1]['id'], 'handled')
        before = await self.c.snapshot(other)
        self.result.update(current_task='third_task', last_task='third_task', finished_at=time.time())
        self.assertEqual(before['revision'], (await self.c.snapshot(other))['revision'])
