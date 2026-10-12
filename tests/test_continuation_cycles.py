"""Repeated real scheduled collection/report cycles without model rearming or owner wakes."""
import json
import time
import unittest

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
            rec = (store.setting('continuations') or {}).get(task, {}).get('sh', {})
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
        waits = [mid for phase, mid in transitions if phase == 'sent']
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
