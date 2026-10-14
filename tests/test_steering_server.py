"""Real temple/galaxy/hub/old_one flow; inference remains an isolated fixture.

Azathoth's follow-up for the task an old_one is working on is steered into the running
turn (Codex) or queued for the next turn (native Claude), and saved in the hub's turn log.
"""
import json
import sqlite3

from tests.test_universe import HarnessServer


class SteeringServer(HarnessServer):
    temple_extra = {"old_one_runtime": {"backend": "codex", "model": "fake"}}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        turn = cls.chat_cls('CALL new_task ' + json.dumps({'galaxy': 'g1', 'description': 'steering fixture'}))
        r = json.loads(turn['reply'].removeprefix('tool: '))
        cls.task_id, cls.old = r['task_id'], r['agent_id']
        cls.wait(lambda: cls.agent_of(cls.old)['online'], 60)
        cls.wait(lambda: cls.agent_of(cls.old)['state'] == 'idle', 60)

    @classmethod
    def chat_cls(cls, text):
        msg = cls.api("send", {"to": "az", "text": text})
        return cls.wait(lambda: next(t for t in cls.api("chat?agent_id=az") if t["id"] == msg["id"] and t["status"] == "done"), 60)

    @classmethod
    def agent_of(cls, aid):
        return next(a for a in cls.api('directory')['agents'] if a['id'] == aid)

    def runtime(self):
        return self.agent_of(self.old)['runtime']

    def send(self, text):
        self.chat('az', 'CALL temple_send ' + json.dumps({'to': self.old, 'text': text, 'priority': 5, 'task_id': self.task_id}))
        return self.wait(lambda: next((m for m in self.api(f'hub_messages?agent_id={self.old}') if m['text'].startswith(text)), None))

    def message(self, mid):
        return next(m for m in self.api(f'hub_messages?agent_id={self.old}') if m['id'] == mid)

    def logs(self):
        db = sqlite3.connect(f'file:{self.dir / "galaxy" / "hub.db"}?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute('select * from turn_log where agent_id=? order by id', (self.old,))]
        finally:
            db.close()

    def test_same_task_correction_is_steered_into_the_running_turn_and_saved(self):
        first = self.send('WAIT_STEER_FIXTURE long-input ' + 'original context ' * 180)
        self.wait(lambda: self.api(f'latest?agent_id={self.old}').get('current_task') == self.task_id)
        correction = self.send('SAME_TASK_STEERED_CORRECTION')
        self.wait(lambda: all(self.message(m['id'])['state'] == 'handled' for m in (first, correction)))
        corrected = next(r for r in self.logs() if 'SAME_TASK_STEERED_CORRECTION' in r['input'])
        self.assertIn('WAIT_STEER_FIXTURE long-input', corrected['input'])
        self.assertIn('SAME_TASK_STEERED_CORRECTION', corrected['reply'])
        self.assertGreater(len(corrected['input']), 2000)
        self.assertEqual(sum('SAME_TASK_STEERED_CORRECTION' in r['input'] for r in self.logs()), 1)

    def test_native_claude_keeps_correction_queued_until_next_normal_turn(self):
        self.api('runtimes', {'key': self.old, 'config': {'backend': 'claude', 'model': 'claude-opus-5-5'}})
        self.wait(lambda: self.runtime()['backend'] == 'claude')
        try:
            first = self.send('SLEEP 2 CLAUDE_FIRST_' + 'bounded context ' * 8)
            self.wait(lambda: self.api(f'latest?agent_id={self.old}').get('current_task') == self.task_id)
            correction = self.send('CLAUDE_NEXT_BATCH_CORRECTION')
            self.assertIsNone(self.message(correction['id'])['handled_at'])
            self.wait(lambda: all(self.message(m['id'])['state'] == 'handled' for m in (first, correction)))
            rows = self.logs()
            initial = next(r for r in rows if 'CLAUDE_FIRST_' in r['input'])
            self.assertNotIn('CLAUDE_NEXT_BATCH_CORRECTION', initial['input'])
            received = [r for r in rows if 'CLAUDE_NEXT_BATCH_CORRECTION' in r['input']]
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]['backend'], 'claude')
            self.assertGreater(received[0]['id'], initial['id'])
        finally:
            self.api('runtimes', {'key': self.old, 'config': None})
            self.wait(lambda: self.runtime()['backend'] == 'codex')
