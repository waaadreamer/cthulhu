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
        self.wait(lambda: (store.setting('continuations') or {}).get(task, {}).get(worker['id'], {}).get('phase') == 'responded', 20)
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
              'nudge': nudges[0]['id'], 'state': 'responded', 'new_owner_messages': 0, 'shub_relays': 0}), flush=True)
        store.set_task(task, 'done')
        store.db.close()
