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


class LegacyWaitServer(HarnessServer):
    patrol_interval = 1

    def test_legacy_wait_expiry_collects_existing_worker_without_owner_wake(self):
        spawn = 'CALL temple_spawn ' + json.dumps({'name': 'legacy wait worker', 'galaxy_id': 'g1',
                  'task': 'Keep existing completed fixture available', 'skill': 'LEGACY_WAIT_WORKER'})
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': 'sh', 'title': 'Prepare legacy worker', 'text': spawn}))
        store = Store(self.dir / 'temple.db')
        worker = self.wait(lambda: next(a for a in store.agents() if a['name'] == 'legacy wait worker'))
        self.wait(lambda: store.turn_log(worker['id']))
        task = store.add_task('LEGACY_WAIT_TEST')
        msg = store.add_message('az', 'sh', 'LATEST_WAIT_INSTRUCTION EXISTING_EXECUTOR=' + worker['id'], task_id=task['id'])
        store.mark(msg['id'], 'handled')
        # The historical delegation omitted task_id; do not manufacture an association.
        old = store.add_message('sh', worker['id'], 'Existing background fixture work', task_id=None)
        store.mark(old['id'], 'handled')
        records = store.setting('continuations') or {}
        records[task['id']] = {'phase': 'waiting', 'at': time.time()-901, 'revision': 'legacy',
                             'evidence': 'Old background observer evidence; completion unknown.', 'instruction_id': msg['id']}
        store.set_setting('continuations', records)
        owners = len(store.q("SELECT id FROM messages WHERE sender='owner'"))
        done = self.wait(lambda: store.task(task['id'])['state'] == 'done', 20)
        messages = store.messages(task_id=task['id'])
        reminders = [m for m in messages if m['sender'] == 'yo' and m['receiver'] == 'sh']
        self.assertEqual(len(reminders), 1)
        self.assertIn('NOT authorization to restart', reminders[0]['text'])
        requests = [m for m in messages if m['receiver'] == worker['id']]
        self.assertEqual(len(requests), 1)
        self.assertIn('READONLY_WAIT_COLLECTION', requests[0]['text'])
        shub_turn = next(r for r in store.turn_log('sh') if 'Automatic waiting-state inspection' in r['input'])
        self.assertIn('"executors": []', shub_turn['input'])
        self.assertIn('LATEST_WAIT_INSTRUCTION', shub_turn['input'])
        self.assertEqual(len(store.q("SELECT id FROM messages WHERE sender='owner'")), owners)
        self.assertEqual(len([a for a in store.agents() if a['name'] == 'legacy wait worker']), 1)
        print('LEGACY_WAIT_SERVER ' + json.dumps({'task_id': task['id'], 'executor': worker['id'],
              'reminder': reminders[0]['id'], 'state': 'done', 'new_owner_messages': 0,
              'new_workloads': 0, 'readonly_requests': 1}), flush=True)
        store.db.close()

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
        self.wait(lambda: store.task(task['id'])['state'] == 'done', 10)
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


class ReconciliationServer(HarnessServer):
    patrol_interval = .3

    def test_patrol_reconciles_text_result_while_next_work_runs_and_keeps_missing_collection(self):
        store = Store(self.dir / 'temple.db')
        task = store.add_task('RECONCILE_RECEIPTS')['id']
        def dispatch(text):
            return self.chat('az', 'CALL temple_send ' + json.dumps({'to':'sh', 'task_id':task, 'text':text}))
        def spawn(name, work):
            dispatch('CALL temple_spawn ' + json.dumps({'name':name,'galaxy_id':'g1',
                     'task_id':task,'task':work,'skill':'Isolated receipt reconciliation fixture'}))
            return self.wait(lambda: next(a for a in store.agents() if a['name']==name))
        busy = spawn('next phase worker', 'Existing next phase')
        dispatch('CALL temple_send '+json.dumps({'to':busy['id'],'task_id':task,'text':'SLEEP 30'}))
        self.wait(lambda: next(a for a in self.api('directory')['agents'] if a['id']==busy['id'])['state']=='working')
        missing = store.add_task('UNFINISHED_COLLECTION')['id']
        at = time.time()
        records = store.setting('continuations') or {}
        for tid in (task, missing):
            records[tid] = {'sh':{'phase':'escalated','at':at,'deadline':at-1,
                'message_id':'fixture_reminder_'+tid,'text':'Collect existing results'}}
        store.set_setting('continuations',records)
        source = spawn('text receipt worker', 'Return verified existing receipt')
        dispatch('CALL temple_send '+json.dumps({'to':source['id'],'task_id':task,
                  'text':'REPLY Verified SOURCE_HASH and completed regression receipts'}))
        result = self.wait(lambda: next(r for r in store.turn_log(source['id']) if r['reply'].startswith('Verified SOURCE_HASH')))
        self.assertEqual(result['status'],'completed')
        dispatch('CALL temple_send '+json.dumps({'to':'az','task_id':task,'status':'progress',
                  'text':'Verified collected source/hash/regression receipts; next phase is independently running'}))
        owners = len(store.q("SELECT id FROM messages WHERE sender='owner'"))
        self.wait(lambda: (store.setting('continuations') or {})[task]['sh']['phase']=='waiting')
        time.sleep(1)
        self.assertEqual((store.setting('continuations') or {})[missing]['sh']['phase'],'escalated')
        self.assertEqual(next(a for a in self.api('directory')['agents'] if a['id']==busy['id'])['state'],'working')
        self.assertFalse([m for m in store.messages(task_id=task) if m['sender']=='yo' and m['receiver']=='sh'])
        self.assertEqual(len(store.q("SELECT id FROM messages WHERE sender='owner'")),owners)
        print('RECONCILIATION_SERVER '+json.dumps({'task':task,'phase':'waiting','missing_task':missing,
              'missing_phase':'escalated','busy_worker':busy['id'],'new_reminder_sends':0,'new_owner_messages':0}),flush=True)
        for tid in (task, missing): store.set_task(tid,'done')
        store.db.close()


class SendGuards(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Temple({'secret': 'test', 'db': str(Path(self.tmp.name) / 'db'),
                         'galaxies': [{'id': 'g', 'name': 'g', 'token': 'test'}],
                         'gods': [{'id': r, 'role': r, 'galaxy': 'g'} for r in ('azathoth', 'shub', 'yog')]})
        self.c = self.t.continuations
        self.task = self.t.store.add_task('test')['id']
        self.instruction('LATEST authorized scope')
        self.t.store.upsert_agent('old', 'worker', 'old_one', 'g', 'shub', {'task_id': self.task})
        self.result = {'agent_id':'old','online':True,'state':'idle','last_task':self.task,'finished_at':1}
        self.t.latest_result = AsyncMock(side_effect=lambda _:dict(self.result))
        self.t.deliver = AsyncMock()
        self.t.broadcast = AsyncMock()

    async def asyncTearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    def instruction(self, text):
        m=self.t.store.add_message('azathoth','shub',text,task_id=self.task)
        self.t.store.mark(m['id'],'handled')

    async def send(self,to='shub',**kw):
        from types import SimpleNamespace
        return await self.t.handle_agent_call(SimpleNamespace(id='yog'),'send',
            {'to':to,'task_id':self.task,'text':'fresh readonly collection; preserve workloads',**kw})

    def rec(self,target='shub'):
        return self.c.records()[self.task][target]

    async def test_public_send_atomic_dedup_latest_scope_and_recovery(self):
        answers=await asyncio.gather(*(self.send() for _ in range(5)))
        self.assertEqual(sum('suppressed' not in r for r in answers),1)
        self.assertEqual((await self.send('old'))['suppressed'],'in_flight')
        rec=self.rec();mid=rec['message_id']
        self.assertIn('LATEST authorized scope',self.t.store.message(mid)['text'])
        self.t.store.mark(mid,'handled')
        self.instruction('new latest scope while collection pending')
        self.assertEqual((await self.send())['suppressed'],'in_flight')
        from cthulhu.continuation import Continuations
        self.c=self.t.continuations=Continuations(self.t)
        self.assertEqual((await self.send())['suppressed'],'in_flight')
        records=self.c.records();records[self.task]['shub']['message_id']='reserved_send'
        self.t.store.set_setting('continuations',records)
        await self.c.scan('yog');await self.c.scan('yog')
        self.assertEqual(self.t.store.message('reserved_send')['task_id'],self.task)

    async def test_started_failed_collection_escalates_once_and_late_completion_releases(self):
        await self.send()
        records=self.c.records();records[self.task]['shub']['deadline']=0
        self.t.store.set_setting('continuations',records)
        self.result.update(current_task=self.task,state='working',last_activity_at=time.time(),activity_task=self.task)
        await self.c.scan('yog');await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'escalated')
        self.assertEqual(len([m for m in self.t.store.messages(task_id=self.task) if m['receiver']=='azathoth']),1)
        self.result.update(state='idle',current_task=None,last_status='completed',activity=['readonly'],finished_at=time.time())
        self.t.store.add_message('shub','azathoth','fresh collection verified',task_id=self.task,status='progress')
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'waiting')
        self.t.store.mark(self.rec()['message_id'],'handled')
        self.assertNotIn('suppressed',await self.send())

    async def test_text_only_completed_collection_reconciles_without_dummy_activity(self):
        await self.send()
        records=self.c.records();records[self.task]['shub']['phase']='escalated'
        self.t.store.set_setting('continuations',records)
        self.result.update(last_status='completed',activity=[],last_reply='Verified source hash and test receipts',finished_at=time.time())
        self.t.store.add_message('shub','azathoth','Collection receipts verified; next phase continues',task_id=self.task,status='progress')
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'waiting')

    async def test_consumed_collection_and_later_report_survive_worker_moving_on(self):
        await self.send()
        records=self.c.records();records[self.task]['shub']['phase']='escalated'
        self.t.store.set_setting('continuations',records)
        evidence=self.t.store.add_message('old','shub','Verified source/hash/test receipts',task_id=self.task)
        self.t.store.mark(evidence['id'],'handled')
        self.t.store.add_message('shub','azathoth','Received and verified collection; activation now in progress',task_id=self.task,status='progress')
        self.result.update(current_task=self.task,state='working',last_task='later-task',activity=[])
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'waiting')
        self.assertEqual((await self.send())['suppressed'],'in_flight') # Healthy current work still protected.

    async def test_missing_unconsumed_wrong_task_and_unreported_evidence_stay_escalated(self):
        await self.send()
        records=self.c.records();records[self.task]['shub']['deadline']=0
        self.t.store.set_setting('continuations',records)
        self.result.update(last_status='failed',activity=['attempted'],last_reply='collection failed',finished_at=time.time())
        self.t.store.add_message('shub','azathoth','Still waiting for collection',task_id=self.task,status='progress')
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'escalated')
        other=self.t.store.add_task('other')['id']
        wrong=self.t.store.add_message('old','shub','Other task evidence',task_id=other)
        self.t.store.mark(wrong['id'],'handled')
        evidence=self.t.store.add_message('old','shub','Actual task result',task_id=self.task)
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'escalated')
        self.t.store.mark(evidence['id'],'handled')
        await self.c.scan('yog') # Existing report predates this result.
        self.assertEqual(self.rec()['phase'],'escalated')
        self.t.store.add_message('shub','azathoth','Actual collection now verified',task_id=self.task,status='progress')
        await self.c.scan('yog')
        self.assertEqual(self.rec()['phase'],'waiting')

    async def test_task_local_active_pending_done_and_owner_blockers(self):
        self.result.update(state='working',current_task=self.task)
        self.assertEqual((await self.send())['suppressed'],'in_flight')
        # Historical association is not ownership of all future agent activity.
        self.result.update(current_task='unrelated',last_task='unrelated',last_activity_at=time.time())
        self.assertNotIn('suppressed',await self.send())
        self.t.store.set_setting('continuations',{})
        m=self.t.store.add_message('shub','old','collection in flight',task_id=self.task)
        self.assertEqual((await self.send())['suppressed'],'in_flight')
        self.t.store.mark(m['id'],'handled')
        self.t.store.add_message('shub','azathoth','needs owner',task_id=self.task,status='blocked')
        self.assertEqual((await self.send())['suppressed'],'done_or_owner_blocked')
        self.instruction('owner resolved blocker')
        self.t.store.set_task(self.task,'done')
        self.assertEqual((await self.send())['suppressed'],'done_or_owner_blocked')

    async def test_direct_worker_send_guards_response_and_escalation(self):
        self.result.update(activity_task=self.task,last_activity_at=time.time())
        self.assertEqual((await self.send('old'))['suppressed'],'recent_activity')
        self.result.update(last_activity_at=0,current_task='other')
        self.assertEqual((await self.send('old'))['suppressed'],'worker_unavailable_or_other_task')
        self.result.update(current_task=self.task,state='working')
        first=await self.send('old')
        self.t.store.mark(first['id'],'handled')
        self.assertEqual((await self.send('old'))['suppressed'],'in_flight')
        records=self.c.records();records[self.task]['old']['deadline']=0
        self.t.store.set_setting('continuations',records)
        await self.c.scan('yog')
        self.assertEqual(self.rec('old')['phase'],'escalated')
        self.result.update(last_activity_at=time.time())
        await self.c.scan('yog')
        self.assertEqual(self.rec('old')['phase'],'responded')

    async def test_migration_folds_both_legacy_records_without_wait_timers(self):
        self.t.store.set_setting('continuations',{self.task:{'phase':'waiting','check_at':time.time()+86400,'at':1}})
        self.t.store.set_setting('worker_nudges',{self.task:{'old':{'phase':'nudged','at':1,'deadline':time.time()+900,'message_id':'old_reminder','text':'scope'}}})
        records=self.c.records()
        self.assertEqual(records[self.task]['old']['phase'],'sent')
        self.assertEqual(records[self.task]['shub']['phase'],'waiting')
        self.assertNotIn('check_at',records[self.task]['shub'])
        self.assertIsNone(self.t.store.setting('worker_nudges'))
        self.assertEqual(self.c.records(),records)

    async def test_two_tasks_do_not_cross_block_and_deploy_is_not_assignment(self):
        other=self.t.store.add_task('maintenance')['id']
        self.t.store.upsert_agent('ghroth','maintenance','ghroth','g',spec={'task_id':other})
        self.t.store.add_message('ghroth','azathoth','deploy fix',task_id=self.task,kind='deploy_request')
        busy={'agent_id':'ghroth','online':True,'state':'working','current_task':other,'last_task':other}
        self.t.latest_result=AsyncMock(side_effect=lambda aid:dict(self.result if aid=='old' else busy))
        self.assertEqual([r['agent_id'] for r in (await self.c.snapshot(self.task))['executors']],['old'])
        self.assertNotIn('suppressed',await self.send())
        self.assertEqual((await self.send(task_id=other))['suppressed'],'in_flight')

    async def test_tool_schema_permissions_and_suppressed_is_not_reported_sent(self):
        from types import SimpleNamespace
        from cthulhu.gods import Yog
        from cthulhu.agent import Agent
        y=object.__new__(Yog)
        names={t['name'] for t in y.tools()}
        self.assertIn('temple_send',names)
        self.assertFalse({'temple_task_continuation','temple_worker_review'} & names)
        with self.assertRaises(Denied):await self.send(task_id=None)
        self.t.store.upsert_agent('other','unrelated','old_one','g')
        with self.assertRaises(Denied):await self.send('other')
        for method in ('continuation_review','worker_review','continuation_tasks','restart'):
            with self.assertRaises((Denied,ValueError)):
                await self.t.handle_agent_call(SimpleNamespace(id='yog'),method,{'agent_id':'old'})
        a=object.__new__(Agent);a.link=SimpleNamespace(call=AsyncMock(return_value={'suppressed':'in_flight','id':'prior'}));a.on_sent=AsyncMock()
        self.assertEqual((await a.send('shub','duplicate',task_id=self.task))['suppressed'],'in_flight')
        a.on_sent.assert_not_called()

    async def test_task_closed_during_scan_does_not_escalate(self):
        await self.send()
        records=self.c.records();records[self.task]['shub']['deadline']=0
        self.t.store.set_setting('continuations',records)
        snapshot=self.c.snapshot
        async def closing(*args):
            row=await snapshot(*args)
            self.t.store.set_task(self.task,'done')
            return row
        self.c.snapshot=closing
        await self.c.scan('yog')
        self.assertFalse([m for m in self.t.store.messages(task_id=self.task) if m['receiver']=='azathoth'])
