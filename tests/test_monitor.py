"""流量监控：按小时累计出站/入站差值，跨中心重启补记、机器重启归零、首次只建基线。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cthulhu_v4.monitor import Traffic
from cthulhu_v4.store import Store


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'db')
        self.now = datetime(2026, 10, 20, 12, 30).timestamp()
        self.counts = {'eth0': (1000, 5000)}
        self.boot = 'b1'
        self.t = self.make()

    def make(self):
        return Traffic(self.store, 'eth0', clock=lambda: self.now, reader=lambda: dict(self.counts), boot=lambda: self.boot)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_first_sample_is_baseline_then_deltas_accumulate_per_hour(self):
        self.assertEqual(self.t.sample(), (0, 0))                     # 开机以来的累计不算成本小时流量
        self.counts['eth0'] = (1500, 9000); self.now += 60
        self.assertEqual(self.t.sample(), (500, 4000))
        self.assertAlmostEqual(self.t.rate['tx'], 4000 / 60)
        self.counts['eth0'] = (1600, 9500); self.now += 3600       # 下一个小时
        self.t.sample()
        s = self.t.summary()
        self.assertEqual(len(s['hours']), 24)
        self.assertEqual([(h['rx'], h['tx']) for h in s['hours'][-2:]], [(500, 4000), (100, 500)])
        self.assertEqual(s['today'], {'rx': 600, 'tx': 4500})
        self.assertEqual(s['month']['tx'], 4500)

    def test_center_restart_backfills_and_reboot_resets(self):
        self.t.sample()
        self.counts['eth0'] = (3000, 8000); self.now += 600
        self.make().sample()                                          # 新进程：从持久化的计数续上
        self.assertEqual(self.t.summary()['today'], {'rx': 2000, 'tx': 3000})
        self.boot = 'b2'; self.counts['eth0'] = (100, 200); self.now += 60
        self.t.rate = None
        self.t.sample()                                               # 机器重启：计数归零，开机以来都是新流量
        self.assertEqual(self.t.summary()['today'], {'rx': 2100, 'tx': 3200})
        self.assertIsNone(self.t.rate)                                # 跨重启的差值不算速率

    def test_missing_interface_is_skipped(self):
        self.counts = {'wg': (1, 1)}
        self.assertIsNone(self.t.sample())
