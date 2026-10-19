"""远程更新：打包、校验、原子换新、导入探测、回滚，以及中心的“排空后再更新”调度。不调用模型。"""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cthulhu_v4 import update
from cthulhu_v4.center import Center
from cthulhu_v4.util import ROOT


def copy_tree(dest):
    for rel in update.files(ROOT):
        target = dest / rel; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    return dest


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.app = copy_tree(self.path / 'app')
        (self.app / 'cthulhu_v4' / 'marker.py').write_text('OLD = True\n')  # 旧版本独有的文件

    def tearDown(self):
        self.tmp.cleanup()

    def release(self, mutate=None):
        src = copy_tree(self.path / 'src')
        (src / 'cthulhu_v4' / 'new_feature.py').write_text('NEW = True\n')
        if mutate: mutate(src)
        r = update.Release(src); shutil.rmtree(src)
        return r

    def test_install_swaps_atomically_and_keeps_old_copy(self):
        r = self.release()
        old = update.install(self.app, r.download(), r.offer())
        self.assertEqual(update.digest(self.app), r.code)
        self.assertTrue((self.app / 'cthulhu_v4' / 'new_feature.py').exists())
        self.assertFalse((self.app / 'cthulhu_v4' / 'marker.py').exists())
        self.assertTrue((old / 'cthulhu_v4' / 'marker.py').exists())   # 旧代码留作回滚
        self.assertFalse(self.app.with_name('app.new').exists())

    def test_refuses_tampered_package_shape_change_and_broken_code(self):
        before = update.digest(self.app)
        r = self.release()
        with self.assertRaisesRegex(ValueError, '校验'):
            update.install(self.app, r.download(), r.offer() | {'sha256': '0' * 64})
        shaped = self.release(lambda src: (src / 'pyproject.toml').write_text(
            (src / 'pyproject.toml').read_text().replace('"websockets==15.0.1"', '"websockets==15.0.1", "rich==13.0.0"')))
        with self.assertRaisesRegex(ValueError, '重跑安装命令'):
            update.install(self.app, shaped.download(), shaped.offer())
        broken = self.release(lambda src: (src / 'cthulhu_v4' / 'node.py').write_text('def broken(:\n'))
        with self.assertRaisesRegex(ValueError, '试导入失败'):
            update.install(self.app, broken.download(), broken.offer())
        self.assertEqual(update.digest(self.app), before)          # 任何失败都不动现有代码
        self.assertFalse(self.app.with_name('app.new').exists())

    def test_rollback_after_repeated_failed_starts_and_confirm_cleans_up(self):
        base = self.path
        r = self.release()
        old = update.install(self.app, r.download(), r.offer())
        update.mark(base, 'a', r.code, old)
        for _ in range(update.MAX_ATTEMPTS):
            self.assertFalse(update.check_rollback(base, self.app))
        self.assertTrue(update.check_rollback(base, self.app))
        self.assertTrue((self.app / 'cthulhu_v4' / 'marker.py').exists())   # 旧代码回来了
        self.assertTrue((base / 'update-rollback.json').exists())
        # 正常路径：首次同步成功即确认，旧代码被清理。
        r2 = self.release()
        old = update.install(self.app, r2.download(), r2.offer())
        update.mark(base, 'a', r2.code, old)
        self.assertEqual(update.confirm(base)['to'], r2.code)
        self.assertFalse(old.exists()); self.assertFalse((base / update.MARKER).exists())


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = Center(self.tmp.name)
        self.c.release = self.clean_release()
        with self.c.store.atomic(): self.c.hub.register('gpu')
        self.body = {'instance': 'disk', 'capacity': 2, 'backends': ['demo'], 'events': [],
                     'features': ['files', 'update'], 'code': 'oldcode', 'managed': True}

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    @staticmethod
    def clean_release():
        r = update.Release(ROOT); r.clean = True  # 调度测试与工作区是否干净无关
        return r

    def test_dirty_center_tree_is_never_pushed(self):
        self.c.release.clean = False
        r = self.c.hub.sync('gpu', self.body)
        self.assertNotIn('update', r)
        self.assertNotIn('update', self.c.store.get('node', 'gpu'))

    def task(self):
        return self.c.create_task(dict(title='t', description='d', acceptance='a', node='gpu', cwd='/w', backend='demo',
                                       deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))

    def test_drains_running_turns_then_offers_update_without_losing_messages(self):
        # 先在旧版本上派发一个回合。
        self.c.release = None
        t = self.task()
        job = self.c.hub.sync('gpu', self.body)['jobs'][0]
        self.c.release = self.clean_release()
        with self.c.store.atomic(): self.c.enqueue('owner', t['agent'], '新消息')
        r = self.c.hub.sync('gpu', self.body)
        self.assertEqual([j['id'] for j in r['jobs']], [job['id']])     # 只续租已有回合，不派新回合
        self.assertNotIn('update', r)
        self.assertEqual(self.c.store.get('node', 'gpu')['update']['state'], 'draining')
        # 回合结束 → 下发更新；排队的新消息仍在队列里。
        self.body['events'] = [{'seq': 1, 'job': job['id'], 'kind': 'finished', 'data': {'outcome': 'completed', 'result': ''}}]
        r = self.c.hub.sync('gpu', self.body)
        self.assertEqual(r['update']['code'], self.c.release.code)
        self.assertEqual(r['jobs'], [])
        self.assertTrue(self.c.has_queued(t['agent']))
        self.assertEqual(self.c.hub.request('gpu', {'op': 'release'})['code'], self.c.release.code)
        # 节点带着新代码回来：更新完成，恢复派发。
        self.body.update(events=[], code=self.c.release.code)
        r = self.c.hub.sync('gpu', self.body)
        self.assertEqual(len(r['jobs']), 1)
        self.assertNotIn('update', self.c.store.get('node', 'gpu'))
        self.assertTrue(any(e['kind'] == 'node_updated' for e in self.c.store.events(visible=True, limit=500)))

    def test_failure_resumes_dispatch_and_manual_nodes_are_left_alone(self):
        t = self.task()
        self.assertIn('update', self.c.hub.sync('gpu', self.body))
        self.c.hub.request('gpu', {'op': 'update_failed', 'error': '依赖有变化'})
        r = self.c.hub.sync('gpu', self.body)
        self.assertNotIn('update', r); self.assertEqual(len(r['jobs']), 1)   # 失败后继续用旧版本工作
        with self.c.store.atomic(): self.c.retry_update('gpu')
        # 手动启动（非安装脚本管理）的节点不推送更新，也不暂停派发。
        with self.c.store.atomic(): self.c.hub.register('dev')
        r = self.c.hub.sync('dev', self.body | {'instance': 'disk2', 'managed': False})
        self.assertNotIn('update', r)
        with self.assertRaisesRegex(ValueError, '没有待下发'):
            self.c.hub.request('dev', {'op': 'release'})
