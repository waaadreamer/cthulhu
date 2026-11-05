"""网页每秒轮询的快照：只带界面用到的数据，且不为此整表读取任务/问题/白名单。不调用模型。"""
import tempfile
import unittest
from pathlib import Path

from cthulhu_v4.center import Center
from tests.helpers import task_args


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Center(Path(self.tmp.name) / 'center')
        self.t = self.c.tool('master', 'create_task', task_args(Path(self.tmp.name)))

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def test_tasks_carry_only_list_fields_never_the_long_ones(self):
        with self.c.store.atomic():
            t = self.c.store.get('task', self.t['id'])
            t.update(history=[{'text': 'x' * 5000}], claim={'result': 'r'}, evidence='e' * 5000, result='r' * 5000)
            self.c.store.put('task', t)
        [row] = self.c.snapshot()['tasks']
        self.assertEqual(set(row), {'id', 'agent', 'title', 'node', 'state', 'deadline', 'created_at', 'review_round'} & set(row))
        for long in ('history', 'claim', 'evidence', 'result'):
            self.assertNotIn(long, row)
        self.assertEqual((row['id'], row['agent']), (t['id'], t['agent']))

    def test_only_open_questions_without_history_are_sent(self):
        with self.c.store.atomic():
            for i, state in enumerate(['master', 'owner', 'answered', 'closed']):
                self.c.store.put('question', {'id': f'q{i}', 'task': self.t['id'], 'agent': 'a', 'text': 'q', 'recommendation': '',
                                              'blocking': False, 'state': state, 'version': 1, 'deadline': None,
                                              'history': [{'text': 'h'}], 'created_at': 1})
        questions = self.c.snapshot()['questions']
        self.assertEqual([q['id'] for q in questions], ['q0', 'q1'])
        self.assertTrue(all('history' not in q for q in questions))

    def test_snapshot_does_not_scan_whole_tables(self):
        scanned = []
        real_all = self.c.store.all
        self.c.store.all = lambda kind: (scanned.append(kind), real_all(kind))[1]
        try:
            self.c.snapshot()
        finally:
            self.c.store.all = real_all
        # agent/node 很小，另行处理；任务、问题、白名单、消息、租约不能再整表读取。
        self.assertEqual(sorted(set(scanned) & {'task', 'question', 'feishu_acl', 'message', 'lease'}), [], scanned)


if __name__ == '__main__':
    unittest.main()
