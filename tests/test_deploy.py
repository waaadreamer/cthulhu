"""一键更新中心：只快进到远端提交、拒绝脏工作区与非快进、等本机回合结束再切换。不跑真实测试套件、不重启进程。"""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cthulhu_v4.center import Center
from cthulhu_v4.deploy import Deployer


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class FakeApp:
    def __init__(self, center): self.center = center


class DeployTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); p = Path(self.tmp.name)
        self.remote, self.work, self.live = p / 'remote.git', p / 'work', p / 'live'
        sh('git', 'init', '--bare', '-q', '-b', 'v4', str(self.remote), cwd=p)
        sh('git', 'clone', '-q', str(self.remote), str(self.work), cwd=p)
        for repo in (self.work,):
            sh('git', 'config', 'user.email', 't@t', cwd=repo); sh('git', 'config', 'user.name', 't', cwd=repo)
        (self.work / 'a.txt').write_text('1'); sh('git', 'add', '.', cwd=self.work); sh('git', 'commit', '-qm', 'one', cwd=self.work)
        sh('git', 'push', '-q', 'origin', 'HEAD:v4', cwd=self.work)
        sh('git', 'clone', '-q', '-b', 'v4', str(self.remote), str(self.live), cwd=p)
        self.c = Center(p / 'state')
        self.exits = []
        self.d = Deployer(FakeApp(self.c), root=self.live, exit_after=lambda: self.exits.append(True))

    async def asyncTearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def push(self, msg):
        (self.work / 'a.txt').write_text(msg); sh('git', 'commit', '-qam', msg, cwd=self.work)
        sh('git', 'push', '-q', 'origin', 'HEAD:v4', cwd=self.work)

    async def finish(self):
        await asyncio.wait_for(self.d.task, 10)

    async def test_up_to_date_dirty_and_non_ff_are_refused(self):
        with self.assertRaisesRegex(ValueError, '已经是最新'):
            await self.d.start()
        self.push('two')
        (self.live / 'a.txt').write_text('local edit')
        with self.assertRaisesRegex(ValueError, '未提交'):
            await self.d.start()
        sh('git', 'checkout', '-q', '--', 'a.txt', cwd=self.live)
        sh('git', 'config', 'user.email', 't@t', cwd=self.live); sh('git', 'config', 'user.name', 't', cwd=self.live)
        (self.live / 'b.txt').write_text('x'); sh('git', 'add', '.', cwd=self.live); sh('git', 'commit', '-qm', 'local', cwd=self.live)
        with self.assertRaisesRegex(ValueError, '不能快进'):
            await self.d.start()

    async def test_waits_for_local_turns_then_fast_forwards_and_exits(self):
        self.push('two')
        lease = {'id': 'turn_1', 'node': 'local', 'agent': 'master', 'message': 'm', 'state': 'running', 'until': 9e12}
        self.c.store.put('lease', lease)
        with patch.object(Deployer, '_test', lambda self, target: None):
            info = await self.d.start()
            self.assertEqual(info['phase'], 'testing')
            for _ in range(50):
                if self.d.state['phase'] == 'draining': break
                await asyncio.sleep(.05)
            self.assertTrue(self.c.hold_local)                    # 不再向本机派发新回合
            self.assertEqual((self.live / 'a.txt').read_text(), '1')   # 回合结束前不切换
            lease['state'] = 'done'; self.c.store.put('lease', lease)
            await self.finish()
        self.assertEqual((self.live / 'a.txt').read_text(), 'two')
        self.assertTrue(self.d.restart); self.assertEqual(self.exits, [True])
        self.assertTrue(any(e['kind'] == 'deploy' for e in self.c.store.events(visible=True, limit=50)))

    async def test_running_commit_counts_even_if_head_already_moved(self):
        class R: commit = None
        r = R(); r.commit = subprocess.run(['git', '-C', str(self.live), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
        self.c.release = r
        self.push('two')
        sh('git', 'pull', '-q', '--ff-only', cwd=self.live)          # 运行目录已拉到新提交，但进程还是旧代码
        info = await asyncio.to_thread(self.d.check)
        self.assertEqual(info['behind'], 1)

    async def test_failed_tests_keep_current_version_and_resume_dispatch(self):
        self.push('two')
        def fail(self, target): raise RuntimeError('测试未通过：FAIL: test_x')
        with patch.object(Deployer, '_test', fail):
            await self.d.start(); await self.finish()
        self.assertEqual(self.d.state['phase'], 'failed')
        self.assertFalse(self.c.hold_local)
        self.assertEqual((self.live / 'a.txt').read_text(), '1')
        self.assertFalse(self.exits)
        self.assertTrue(any(e['kind'] == 'deploy_failed' for e in self.c.store.events(visible=True, limit=50)))

    async def test_hold_local_stops_dispatch_to_center_node(self):
        self.c.hold_local = True
        with self.c.store.atomic(): self.c.enqueue('owner', 'master', 'hi')
        r = self.c.hub.sync('local', {'instance': 'i', 'capacity': 2, 'backends': ['demo'], 'events': []})
        self.assertEqual(r['jobs'], [])
        self.c.hold_local = False
        self.assertEqual(len(self.c.hub.sync('local', {'instance': 'i', 'capacity': 2, 'backends': ['demo'], 'events': []})['jobs']), 1)
