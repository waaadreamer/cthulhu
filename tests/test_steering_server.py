"""Real temple/galaxy/agent flow; inference remains an isolated fixture."""
import json
import sqlite3

from tests.test_universe import HarnessServer


class SteeringServer(HarnessServer):
    patrol_interval = 3600

    def runtime(self):
        return next(a for a in self.api('directory')['agents'] if a['id'] == 'sh')['runtime']

    def send(self, text, task_id=None):
        args = {'to': 'sh', 'text': text, 'priority': 5}
        if task_id:
            args['task_id'] = task_id
        else:
            args['title'] = 'STEERING_ACCEPTANCE'
        self.chat('az', 'CALL temple_send ' + json.dumps(args))
        return self.wait(lambda: next((m for m in self.api('messages?agent_id=sh')
                                      if m['text'] == text), None))

    def message(self, mid):
        return next(m for m in self.api('messages?agent_id=sh') if m['id'] == mid)

    def logs(self):
        db = sqlite3.connect(f'file:{self.dir / "temple.db"}?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute('select * from turn_log where agent_id=? order by id', ('sh',))]
        finally:
            db.close()

    def test_other_task_waits_while_same_task_correction_is_consumed_and_saved(self):
        first = self.send('WAIT_STEER_FIXTURE task-isolation')
        self.wait(lambda: self.api('latest?agent_id=sh').get('current_task') == first['task_id'])
        other = self.send('OTHER_TASK_MUST_WAIT')
        self.assertNotEqual(first['task_id'], other['task_id'])
        self.assertEqual(self.message(other['id'])['state'], 'delivered')
        self.assertIsNone(self.message(other['id'])['handled_at'])
        correction = self.send('SAME_TASK_STEERED_CORRECTION', first['task_id'])
        self.wait(lambda: all(self.message(m['id'])['state'] == 'handled' for m in (first, correction, other)))
        rows = self.logs()
        corrected = next(r for r in rows if 'SAME_TASK_STEERED_CORRECTION' in r['input'])
        self.assertIn('WAIT_STEER_FIXTURE task-isolation', corrected['input'])
        self.assertNotIn('OTHER_TASK_MUST_WAIT', corrected['input'])
        self.assertIn('SAME_TASK_STEERED_CORRECTION', corrected['reply'])
        self.assertTrue(any('OTHER_TASK_MUST_WAIT' in r['input'] and r['id'] > corrected['id'] for r in rows))
        self.assertEqual(sum('SAME_TASK_STEERED_CORRECTION' in r['input'] for r in rows), 1)
        print('STEERING_ISOLATION_SERVER ' + json.dumps({'task': first['task_id'], 'other_task': other['task_id'],
                    'correction_saved': True, 'other_task_separate': True}), flush=True)

    def test_saved_correction_survives_long_input_and_backend_handoff(self):
        first = self.send('WAIT_STEER_FIXTURE long-input ' + 'original context ' * 180)
        correction = self.send('RECENT_CORRECTION_KEEP_EXISTING_RESULTS', first['task_id'])
        self.wait(lambda: all(self.message(m['id'])['state'] == 'handled' for m in (first, correction)))
        recorded = next(r for r in self.logs() if 'RECENT_CORRECTION_KEEP_EXISTING_RESULTS' in r['input'])
        self.assertGreater(len(recorded['input']), 2000)
        self.assertEqual(recorded['input'].count('RECENT_CORRECTION_KEEP_EXISTING_RESULTS'), 1)
        self.api('runtimes', {'key': 'shub', 'config': {'backend': 'claude', 'model': 'claude-opus-5-5'}})
        self.wait(lambda: self.runtime()['backend'] == 'claude')
        try:
            follow = self.send('HANDOFF_INSPECT_ONLY', first['task_id'])
            self.wait(lambda: self.message(follow['id'])['state'] == 'handled')
            row = next(r for r in self.logs() if r['input'].endswith('HANDOFF_INSPECT_ONLY'))
            self.assertEqual(row['backend'], 'claude')
            self.assertIn('RECENT_CORRECTION_KEEP_EXISTING_RESULTS', row['reply'])
            print('STEERING_HANDOFF_SERVER ' + json.dumps({'saved_turn': recorded['id'],
                       'next_backend': row['backend'], 'bounded_handoff_keeps_correction': True}), flush=True)
        finally:
            self.api('runtimes', {'key': 'shub', 'config': None})
            self.wait(lambda: self.runtime()['backend'] == 'codex')

    def test_native_claude_keeps_correction_queued_until_next_normal_turn(self):
        self.api('runtimes', {'key': 'shub', 'config': {'backend': 'claude', 'model': 'claude-opus-5-5'}})
        self.wait(lambda: self.runtime()['backend'] == 'claude')
        try:
            first = self.send('SLEEP 2 CLAUDE_FIRST_' + 'bounded context ' * 8)
            self.wait(lambda: self.api('latest?agent_id=sh').get('current_task') == first['task_id'])
            correction = self.send('CLAUDE_NEXT_BATCH_CORRECTION', first['task_id'])
            self.assertEqual(self.message(first['id'])['state'], 'delivered')
            self.assertEqual(self.message(correction['id'])['state'], 'delivered')
            self.assertIsNone(self.message(correction['id'])['handled_at'])
            self.wait(lambda: all(self.message(m['id'])['state'] == 'handled' for m in (first, correction)))
            rows = self.logs()
            initial = next(r for r in rows if 'CLAUDE_FIRST_' in r['input'])
            self.assertNotIn('CLAUDE_NEXT_BATCH_CORRECTION', initial['input'])
            received = [r for r in rows if 'CLAUDE_NEXT_BATCH_CORRECTION' in r['input']]
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['backend'], 'claude')
            self.assertGreater(received[0]['id'], initial['id'])
            self.assertIn('CLAUDE_NEXT_BATCH_CORRECTION', received[0]['reply'])
            print('STEERING_CLAUDE_QUEUE_SERVER ' + json.dumps({'backend': 'claude',
                    'mid_turn_injection': False, 'next_batch_only': True}), flush=True)
        finally:
            self.api('runtimes', {'key': 'shub', 'config': None})
            self.wait(lambda: self.runtime()['backend'] == 'codex')
