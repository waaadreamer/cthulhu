import tempfile
import unittest
from pathlib import Path

from cthulhu_v4.store import Store


class StoreWhereTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 's.db')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_where_matches_full_scan_filter(self):
        for i, state in enumerate(['done', 'running', 'unknown', 'running', 'failed']):
            self.store.put('lease', {'id': f'j{i}', 'state': state})
        self.store.put('message', {'id': 'm0', 'state': 'running'})
        expect = [j for j in self.store.all('lease') if j['state'] == 'running']
        self.assertEqual(self.store.where('lease', 'running'), expect)
        self.assertEqual([j['id'] for j in self.store.where('lease', 'done', 'failed')], ['j0', 'j4'])

    def test_where_sees_state_updates_in_place(self):
        # 状态变化后索引必须同步：结束的回合不能再被当作 running 续租。
        self.store.put('lease', {'id': 'j', 'state': 'running'})
        self.store.put('lease', {'id': 'j', 'state': 'done'})
        self.assertEqual(self.store.where('lease', 'running'), [])

    def test_where_uses_state_index(self):
        plan = self.store.db.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM objects WHERE kind=? AND json_extract(data, '$.state') IN (?) ORDER BY rowid",
            ('lease', 'running')).fetchall()
        self.assertIn('objects_state', ' '.join(str(tuple(r)) for r in plan))


if __name__ == '__main__':
    unittest.main()
