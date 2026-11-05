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
        # 每个分支（actor、target、各任务）各一条查询，每条都必须走索引。
        plans = [' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + q)) for q in sql]
        db.close()
        self.assertEqual(len(plans), 3)     # actor、target、一个任务
        self.assertTrue(any('events_actor' in p for p in plans))
        self.assertTrue(any('events_target' in p for p in plans))
        self.assertFalse(any('SCAN events' in p for p in plans))

    def test_agent_events_match_single_or_query(self):
        # 分支各取 limit 条再合并，必须与原来的单条 OR 查询返回同样的事件，翻页与跟随两个方向都一样。
        for i in range(60):
            self.store.emit('message', actor='old_a', target='master', task='t0')
            self.store.emit('reply', actor='master', target='old_a', task=None)
            self.store.emit('native:item/completed', actor='old_b', task='t0', data={'item': {'type': 'agentMessage'}})
            self.store.emit('native:thread/tokenUsage/updated', actor='old_a')
            self.store.emit('message', actor='old_c', target='old_d', task='t_other')
        self.store.put('task', {'id': 't0', 'agent': 'old_a', 'state': 'running'})
        reference = ("SELECT seq FROM events WHERE {w} AND (actor=? OR target=? OR task IN "
                     "(SELECT id FROM objects WHERE kind='task' AND json_extract(data,'$.agent')=?)) "
                     "AND (kind NOT LIKE 'native:%' OR kind IN ({m})) ORDER BY seq {o} LIMIT 25")
        from cthulhu_v4.store import UI_NATIVE
        marks = ','.join('?' * len(UI_NATIVE))
        top = self.store.highwater()
        for kw, w, o, arg in ((dict(before=0), '1', 'DESC', []), (dict(before=top // 2), 'seq<?', 'DESC', [top // 2]),
                              (dict(after=top // 3), 'seq>?', 'ASC', [top // 3])):
            got = [e['seq'] for e in self.store.events(limit=25, agent='old_a', ui=True, **kw)]
            want = [r[0] for r in self.store.db.execute(
                reference.format(w=w, o=o, m=marks), [*arg, 'old_a', 'old_a', 'old_a', *UI_NATIVE])]
            self.assertEqual(got, sorted(want), kw)
            self.assertEqual(len(got), 25)

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
