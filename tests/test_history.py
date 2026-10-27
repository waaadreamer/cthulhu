import tempfile
import unittest
from pathlib import Path

from cthulhu_v4 import history
from cthulhu_v4.history import History
from cthulhu_v4.store import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'state.db')
        self.root = Path(self.tmp.name) / 'master-history'

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def text(self):
        return ''.join(p.read_text() for p in sorted(self.root.glob('*.md')))

    def test_keeps_real_messages_and_drops_templates_and_tools(self):
        s = self.store
        s.emit('message', '把 R6003 换成 pt6-30k', actor='owner', target='master')
        s.emit('message', '十分钟增量审核：list_tasks 查看任务', actor='system', target='master')
        s.emit('harness_tool', 'send_message', actor='master', data={'result': 'x' * 1000})
        s.emit('message', '切换 GPU0', actor='master', target='old_1', task='t_1')
        s.emit('message', '别的执行者之间', actor='old_1', target='old_2')
        s.emit('chat', '已安排切换', actor='master')
        s.emit('feishu_out', '@马成磊 已切换', actor='master')
        self.assertEqual(History(s, self.root).sync(), 4)
        out = self.text()
        for kept in ('owner → master\n把 R6003', 'master → old_1 (t_1)', 'master → owner\n已安排', 'master → feishu'):
            self.assertIn(kept, out)
        for dropped in ('十分钟增量审核', 'xxxx', '别的执行者之间'):
            self.assertNotIn(dropped, out)

    def test_incremental_and_no_duplicates_after_restart(self):
        s = self.store
        s.emit('chat', '第一条', actor='master')
        History(s, self.root).sync()
        s.emit('chat', '第二条', actor='master')
        History(s, self.root).sync()          # 新实例：从游标继续
        History(s, self.root).sync()          # 无新事件：不重复追加
        out = self.text()
        self.assertEqual((out.count('第一条'), out.count('第二条')), (1, 1))

    def test_crash_between_append_and_cursor_does_not_duplicate(self):
        s = self.store
        s.emit('chat', '只能出现一次', actor='master')
        History(s, self.root).sync()
        # 模拟追加完文件、写游标前崩溃：游标回退，但文件里已有该 seq。
        (self.root / '.cursor').write_text('0')
        History(s, self.root).sync()
        self.assertEqual(self.text().count('只能出现一次'), 1)

    def test_backfill_is_bounded_per_call(self):
        s, old = self.store, history.WINDOW
        history.WINDOW = 10
        try:
            for i in range(100):
                s.emit('chat', f'消息{i}', actor='master')
            h = History(s, self.root)
            self.assertEqual(h.sync(windows=2), 20)   # 每次只扫有限窗口，不长时间占住事件循环
            while h.sync():
                pass
            self.assertEqual(sum(f'消息{i}\n' in self.text() for i in range(100)), 100)
        finally:
            history.WINDOW = old


if __name__ == '__main__':
    unittest.main()
