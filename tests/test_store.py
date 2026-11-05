import sqlite3
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

    def test_agent_events_use_indexes_not_a_full_scan(self):
        # 按执行者读事件走 actor/target 索引；否则线上大表要倒序扫整张 events 表，几秒到几十秒。
        for i in range(50):
            self.store.emit('message', actor='old_a', target='master', task=f't{i % 5}')
            self.store.emit('reply', actor='master', target='old_b', task='t_other')
        self.store.put('task', {'id': 't0', 'agent': 'old_a', 'state': 'running'})
        sql = []
        db = sqlite3.connect(f'file:{self.store.path}?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        db.set_trace_callback(lambda q: sql.append(q) if q.startswith('SELECT * FROM events') else None)
        self.store.events(db=db, before=0, limit=10, agent='old_a', ui=True, compact=True)
        plan = ' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql[-1]))
        db.close()
        self.assertIn('events_actor', plan)
        self.assertIn('events_target', plan)
        self.assertNotIn('SCAN events', plan)

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
