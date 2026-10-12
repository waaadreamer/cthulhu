"""Repeated real scheduled collection/report cycles without model rearming or owner wakes."""
import json
import time
import unittest

import test_continuation as base
from test_universe import HarnessServer
from cthulhu.store import Store


class MultipleCycles(HarnessServer):
    patrol_interval = .3
    temple_options = {'continuation_recheck_seconds': 1}

    def test_three_collections_then_done_and_owner_summary(self):
        (self.dir / 'cycle-observations.json').write_text(json.dumps([
            {'completed': 1, 'done': False}, {'completed': 2, 'done': False}, {'completed': 3, 'done': True}]))
        spawn = 'CALL temple_spawn ' + json.dumps({'name': 'cycle worker', 'galaxy_id': 'g1',
                      'task': 'Collect existing fixture', 'skill': 'MULTICYCLE_WORKER'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'title': 'Prepare existing worker', 'text': spawn}))
        store = Store(self.dir / 'temple.db')
        worker = self.wait(lambda: next(a for a in store.agents() if a['name'] == 'cycle worker'))
        self.wait(lambda: store.turn_log(worker['id']))
        task = store.add_task('MULTICYCLE_TEST')['id']
        m = store.add_message('az', 'sh', 'LATEST_CYCLE_SCOPE EXISTING_EXECUTOR=' + worker['id'], task_id=task)
        store.mark(m['id'], 'handled')
        owners = len(store.q("SELECT id FROM messages WHERE sender='owner'"))
        records = store.setting('continuations') or {}
        records[task] = {'phase': 'waiting', 'at': time.time()-1, 'check_at': time.time()+.1,
                         'recheck_seconds': 1, 'evidence': 'First fixture sample due', 'instruction_id': m['id']}
        store.set_setting('continuations', records)
        transitions = []
        def finished():
            rec = (store.setting('continuations') or {}).get(task, {})
            signature = (rec.get('phase'), rec.get('check_at'))
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
        waits = [deadline for phase, deadline in transitions if phase == 'waiting']
        self.assertGreaterEqual(len(set(waits)), 3)
        self.assertEqual(len(store.q("SELECT id FROM messages WHERE sender='owner'")), owners)
        self.assertEqual(len([a for a in store.agents() if a['name'] == 'cycle worker']), 1)
        time.sleep(1.5)
        self.assertEqual(len([m for m in store.messages(task_id=task) if m['sender'] == 'yo']), 3)
        print('MULTICYCLE_SERVER ' + json.dumps({'task_id': task, 'worker': worker['id'], 'collections': 3,
              'waiting_deadlines': waits, 'owner_progress': 2, 'owner_summary': notes[0]['id'],
              'new_owner_messages': 0, 'model_rearm_calls': 0}), flush=True)
        store.db.close()


class CollectionRearm(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.ContinuationGuards.asyncSetUp
    asyncTearDown = base.ContinuationGuards.asyncTearDown
    instruction = base.ContinuationGuards.instruction
    review = base.ContinuationGuards.review
    records = base.ContinuationGuards.records

    async def test_completed_inspection_rearms_without_model_and_anchors_to_evidence(self):
        await self.review('waiting')
        records = self.records();records[self.task]['check_at'] = 0
        self.t.store.set_setting('continuations', records)
        await self.c.scan('yog')
        start = self.records()[self.task]['at']
        stamp = start + .01
        self.result.update(last_task=self.task, finished_at=stamp, activity=['actual readonly tool'], last_status='completed')
        scan = await self.c.scan('yog')
        rec = self.records()[self.task]
        self.assertEqual(rec['phase'], 'waiting')
        self.assertEqual(rec['check_at'], stamp+900)
        self.assertEqual(rec['source']['agent_id'], 'old')
        self.assertIn('deployment', scan)
        # A new instruction/record revision with only reworded OLD evidence cannot postpone refresh.
        self.instruction('Keep existing scope; no new external evidence')
        await self.review('waiting', evidence='Rephrased old tool result')
        self.assertEqual(self.records()[self.task]['check_at'], stamp+900)

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
        self.assertEqual(self.records()[self.task]['phase'], 'waiting')

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
