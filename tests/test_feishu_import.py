import copy
import tempfile
import unittest
from pathlib import Path

from cthulhu.feishu_import import import_history, validate
from cthulhu.roles import Denied
from cthulhu.store import Store


class HistoryImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db')
        self.config = {'feishu': {'enabled': False, 'app_id': 'app',
            'owner': {'sender_id': 'owner', 'chat_id': 'chat', 'verified_by': 'trusted-fixture', 'legacy_scope_id': 'old-chat'}}}
        self.bundle = {'version': 1, 'event': {'app_id': 'app', 'sender_id': 'owner', 'chat_id': 'chat'},
            'source': {'scope_id': 'old-chat', 'threads': [{'thread_id': 'old-thread', 'sha256': 'a'*64, 'scope_ids': ['old-chat']}]},
            'turns': [{'source_id': 'turn1', 'source_thread': 'old-thread', 'input': 'old input', 'reply': 'old result',
                       'created_at': 1, 'status': 'completed'}]}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_scope_isolation_idempotency_and_no_execution_records(self):
        self.store.log_turn('nyarlathotep', 'codex', None, 'web', 'private web', 'private', 'completed')
        result = import_history(self.store, self.config, self.bundle)
        self.assertEqual(result['imported'], 1)
        self.assertTrue(import_history(self.store, self.config, self.bundle)['duplicate'])
        self.assertEqual(len(self.store.turn_log('nyarlathotep')), 1)
        rows = self.store.turn_log('nyarlathotep', scope=validate(self.config, self.bundle)[0])
        self.assertEqual([r['reply'] for r in rows], ['old result'])
        self.assertEqual(rows[0]['backend'], 'imported')
        for table in ('messages', 'chat_turns', 'channel_events'):
            self.assertEqual(self.store.q('SELECT * FROM ' + table), [])
        changed = copy.deepcopy(self.bundle); changed['turns'][0]['reply'] = 'changed'
        with self.assertRaises(ValueError): import_history(self.store, self.config, changed)

    def test_rejects_unbound_other_topic_live_and_malformed_atomically(self):
        for field in ('app_id', 'sender_id', 'chat_id', 'topic_id'):
            bundle = copy.deepcopy(self.bundle); bundle['event'][field] = 'other'
            with self.assertRaises((ValueError, Denied)): import_history(self.store, self.config, bundle)
        config = copy.deepcopy(self.config); config['feishu']['owner'].pop('verified_by')
        with self.assertRaises(Denied): import_history(self.store, config, self.bundle)
        config = copy.deepcopy(self.config); config['feishu']['enabled'] = True
        with self.assertRaises(ValueError): import_history(self.store, config, self.bundle)
        bundle = copy.deepcopy(self.bundle); bundle['turns'].append(bundle['turns'][0])
        with self.assertRaises(ValueError): import_history(self.store, self.config, bundle)
        self.assertEqual(self.store.q('SELECT * FROM turn_log'), [])
        self.assertEqual(self.store.q('SELECT * FROM settings'), [])

    def test_sql_failure_rolls_back_import_and_marker(self):
        self.store.x("CREATE TRIGGER reject_import BEFORE INSERT ON turn_log BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with self.assertRaises(Exception): import_history(self.store, self.config, self.bundle)
        self.assertEqual(self.store.q('SELECT * FROM settings'), [])
        self.assertEqual(self.store.q('SELECT * FROM turn_log'), [])

    def test_existing_scope_rejected(self):
        scope = validate(self.config, self.bundle)[0]
        self.store.channel_accept('nyarlathotep', 'app', 'event', scope, {'text': 'live', 'prompt': 'live'})
        with self.assertRaises(ValueError): import_history(self.store, self.config, self.bundle)

    def test_source_mapping_multiple_threads_and_shared_threads_held(self):
        import json
        bundle = copy.deepcopy(self.bundle)
        bundle['source']['threads'][0]['scope_ids'].append('other-chat')
        with self.assertRaises(ValueError): import_history(self.store, self.config, bundle)
        bundle = copy.deepcopy(self.bundle)
        bundle['source']['threads'].append({'thread_id': 'second', 'sha256': 'b'*64, 'scope_ids': ['old-chat']})
        bundle['turns'].append(bundle['turns'][0] | {'source_thread': 'second', 'created_at': 2})
        self.assertEqual(import_history(self.store, self.config, bundle)['imported'], 2)
        receipt = json.loads(self.store.one('SELECT value FROM settings')['value'])
        self.assertEqual(len(receipt['mapping']), 2)
        self.assertNotEqual(receipt['mapping'][0]['turn_log_id'], receipt['mapping'][1]['turn_log_id'])

    def test_cli_checks_transport_hash_and_dry_run_does_not_create_database(self):
        import hashlib
        import json
        import subprocess
        import sys
        root = Path(self.tmp.name)
        config, bundle, db = root/'config.json', root/'bundle.json', root/'must-not-exist.db'
        config.write_text(json.dumps(self.config)); bundle.write_text(json.dumps(self.bundle))
        command = [sys.executable, '-m', 'cthulhu.feishu_import', '--config', str(config),
                   '--bundle', str(bundle), '--db', str(db), '--sha256']
        bad = subprocess.run(command + ['bad'], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        good = subprocess.run(command + [hashlib.sha256(bundle.read_bytes()).hexdigest()], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertTrue(json.loads(good.stdout)['valid'])
        self.assertFalse(db.exists())
