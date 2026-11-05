"""节点上传事件的批处理不变量：同一批共用回合归属与任务 id，但改状态的事件仍读最新租约。不调用模型。"""
import tempfile
import unittest

from cthulhu_v4.center import Center
from cthulhu_v4.util import LOCAL


class IngestBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Center(self.tmp.name, clock=lambda: 1000.0)
        with self.c.store.atomic():
            first = self.c.enqueue('owner', 'master', '开工', 8)
            self.c.store.put('lease', {'id': 'turn_a', 'node': LOCAL, 'agent': 'master', 'message': first['id'],
                                       'messages': [first['id']], 'state': 'running', 'until': 1070.0, 'started': 1000.0})
            self.c.set_messages([first['id']], 'running')

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def events(self, kinds):
        return [{'seq': i + 1, 'job': 'turn_a', 'kind': k, 'text': k, 'data': {}} for i, k in enumerate(kinds)]

    def counting(self):
        """记录 ingest 期间读租约、查任务的次数；这两项曾在每条事件上各做一遍。"""
        calls = {'lease': 0, 'task': 0}
        real_get, real_lookup = self.c.store.get, self.c.store.lookup
        def get(kind, ident):
            calls['lease'] += kind == 'lease'
            return real_get(kind, ident)
        def lookup(kind, field, value, limit=None):
            calls['task'] += kind == 'task'
            return real_lookup(kind, field, value, limit)
        self.c.store.get, self.c.store.lookup = get, lookup
        return calls

    def test_one_batch_reads_the_lease_and_task_once(self):
        calls = self.counting()
        n = self.c.store.get('node', LOCAL); calls['lease'] = 0
        with self.c.store.atomic():
            self.c.hub.ingest_batch(LOCAL, n, self.events(['native:x'] * 30))
        self.assertEqual((n['ack'], calls), (30, {'lease': 1, 'task': 1}))
        # 每条事件仍各自入库，缓存只省查询，不合并事件。
        self.assertEqual(sum(e['kind'] == 'native:x' for e in self.c.store.events(limit=500)), 30)

    def test_finished_in_the_middle_still_sees_fresh_lease(self):
        n = self.c.store.get('node', LOCAL)
        batch = self.events(['native:x', 'native:x'])
        batch.insert(1, {'seq': 2, 'job': 'turn_a', 'kind': 'finished', 'text': '',
                         'data': {'outcome': 'completed', 'result': 'ok'}})
        for i, e in enumerate(batch): e['seq'] = i + 1
        with self.c.store.atomic():
            self.c.hub.ingest_batch(LOCAL, n, batch)
        # finished 之后租约已结束；后续事件照常入库，不会因为缓存而重复结算或报错。
        self.assertEqual(self.c.store.get('lease', 'turn_a')['state'], 'done')
        self.assertEqual(n['ack'], 3)

    def test_event_for_another_nodes_job_is_rejected_even_when_cached(self):
        n = self.c.store.get('node', LOCAL)
        with self.assertRaises(PermissionError):
            with self.c.store.atomic():
                self.c.hub.ingest_batch('other', dict(n, ack=0), self.events(['native:x']))


if __name__ == '__main__':
    unittest.main()
