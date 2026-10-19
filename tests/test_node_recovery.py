"""原磁盘恢复：UUID、会话、事件三者一致才允许复用。"""
import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from cthulhu_v4.core import ROOT
from cthulhu_v4.store import Store

spec = importlib.util.spec_from_file_location('recover_node', ROOT / 'deploy/recover-node.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.expected = {'instance': 'original', 'sessions': {'old_a': 'session-a'}, 'minimum_event_seq': 1}

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, name, instance='original', session='session-a', events=True):
        p = self.root / name / '.state-node'; p.mkdir(parents=True)
        s = Store(p / 'node.db')
        s.put('settings', {'id': 'node', 'instance': instance, 'ack': 1})
        s.put('agent', {'id': 'old_a', 'session': session})
        if events: s.emit('native:demo', '原始记录')
        s.db.close()
        return p

    def test_finds_original_and_preserves_database(self):
        original = self.state('original directory with spaces')
        self.state('new', instance='new-identity')
        before = (original / 'node.db').read_bytes()
        self.assertEqual(recovery.find_states([self.root, self.root], self.expected), [original])
        target = self.root / 'node-state-path'
        recovery.pin_state(original, target)
        self.assertEqual(target.read_text().strip(), str(original))
        self.assertEqual((original / 'node.db').read_bytes(), before)

    def test_rejects_stale_copy_with_missing_session_or_events(self):
        self.state('wrong-session', session='another-session')
        self.state('missing-events', events=False)
        self.assertEqual(recovery.find_states([self.root], self.expected), [])

    def test_does_not_silently_choose_between_two_valid_copies(self):
        a, b = self.state('a'), self.state('b')
        self.assertEqual(recovery.find_states([self.root], self.expected), [a, b])

    def test_running_original_node_cannot_be_pinned(self):
        original = self.state('original')
        with (original / 'node.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, '仍在运行'):
                recovery.pin_state(original, self.root / 'node-state-path')
        self.assertFalse((self.root / 'node-state-path').exists())
