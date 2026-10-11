"""Task identity survives delegation without granting task-status authority."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cthulhu.roles import Role, Denied, require_send
from cthulhu.store import Store
from cthulhu.temple import Temple


class TaskRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'temple.db')
        self.temple = SimpleNamespace(store=self.store)
        self.task = self.store.add_task('Upgrade remote runtime')['id']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def fields(self, sender, receiver, **params):
        return Temple.task_fields(self.temple, sender, receiver, params)

    def test_delegation_and_field_reply_keep_explicit_task_without_status(self):
        for sender, receiver in [(Role.SHUB, Role.OLD_ONE), (Role.OLD_ONE, Role.SHUB),
                                 (Role.OLD_ONE, Role.OLD_ONE), (Role.YOG, Role.OLD_ONE)]:
            with self.subTest(sender=sender, receiver=receiver):
                require_send(sender, receiver)
                self.assertEqual(self.fields(sender, receiver, task_id=self.task, status='done'),
                                 (self.task, None))
        self.assertEqual(self.store.task(self.task)['state'], 'open')

    def test_text_does_not_infer_task_and_unknown_explicit_task_rejected(self):
        self.assertEqual(self.fields(Role.SHUB, Role.OLD_ONE, text=self.task), (None, None))
        with self.assertRaisesRegex(ValueError, 'unknown task'):
            self.fields(Role.SHUB, Role.OLD_ONE, task_id='missing')

    def test_task_reference_does_not_reopen_closed_task(self):
        self.store.set_task(self.task, 'done')
        self.assertEqual(self.fields(Role.OLD_ONE, Role.SHUB, task_id=self.task), (self.task, None))
        self.assertEqual(self.store.task(self.task)['state'], 'done')

    def test_report_validation_and_message_permissions_remain(self):
        with self.assertRaisesRegex(ValueError, 'reports to azathoth'):
            self.fields(Role.SHUB, Role.AZATHOTH, task_id=self.task)
        self.assertEqual(self.fields(Role.SHUB, Role.AZATHOTH, task_id=self.task, status='progress'),
                         (self.task, 'progress'))
        with self.assertRaises(Denied):
            require_send(Role.SHUB, Role.GHROTH)
        with self.assertRaises(Denied):
            require_send(Role.GHROTH, Role.OLD_ONE)
