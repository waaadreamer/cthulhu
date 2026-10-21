"""一键更新中心：把中心快进到 GitHub 上的 v4，测试通过后在空闲时重启。远端节点随后自动更新。

为什么这样设计：
- 只认远端分支上的提交，并且只允许快进：不部署本地未提交的改动，也不会把中心回退到旧版本。
- 先在独立的临时目录里跑测试（不碰运行中的代码），失败就停在原版本。
- 中心重启会结束本机的原生进程：等中心机节点上没有运行中的回合再重启（期间暂停向它派发新回合，
  消息照常排队）。远端节点的回合不受影响：中心重启期间它们在租约内重连。
- 部署是人在网页上点的；Master 只负责改代码、测试、推送，然后请主人点更新。
"""
import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .proxy import git_env, strip
from .update import shape
from .util import LOCAL, ROOT

BRANCH = 'v4'
TEST_TIMEOUT = 600
DRAIN_TIMEOUT = 1800


def git(*args, cwd, timeout=60, env=None):
    out = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=timeout, env=env)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(out.stderr or out.stdout).strip()[-500:]}")
    return out.stdout.strip()


class Deployer:
    CHECK_EVERY = 300

    def __init__(self, app, root=ROOT, exit_after=None, proxy=None):
        self.app, self.root = app, Path(root)
        self.env = git_env(proxy)  # 只有访问 GitHub 的 git 命令用代理
        self.state = {'phase': 'idle'}
        self.task = None
        self.available = None   # 后台定期检查的结果，网页据此提示“有新版本”
        self.restart = False    # 已切换到新代码：进程退出时用非零码，让 systemd 重新拉起
        self.exit_after = exit_after or (lambda: os.kill(os.getpid(), signal.SIGTERM))

    @property
    def draining(self):
        return self.app.center.hold_local

    @draining.setter
    def draining(self, value):
        # hub 读取 center.hold_local：为 True 时不向中心机节点派发新回合，消息照常排队。
        self.app.center.hold_local = value

    def git(self, *args, timeout=60):
        return git(*args, cwd=self.root, timeout=timeout, env=self.env)

    async def watch(self):
        """后台定期看远端有没有新提交，只读：不会自动部署。"""
        while True:
            try:
                self.available = await asyncio.to_thread(self.check)
            except Exception as e:
                self.available = {'error': str(e)[:300]}
            await asyncio.sleep(self.CHECK_EVERY)

    # ---- 查询 ----
    def check(self):
        """对比本地与远端：有没有新提交、能不能快进、工作区是否干净。"""
        g = self.git
        g('fetch', '--quiet', 'origin', BRANCH, timeout=120)
        remote = g('rev-parse', f'origin/{BRANCH}')
        # 以“正在运行的代码”为准，而不是目录里的 HEAD：直接在运行目录提交后 HEAD 已是新版本，
        # 但进程还没重启，此时仍需要更新（_apply 的快进是空操作，只重启）。
        release = getattr(self.app.center, 'release', None)
        running = (release.commit or '').rstrip('+') if release else ''
        head = g('rev-parse', running or 'HEAD')
        dirty = g('status', '--porcelain', '--untracked-files=no')
        ahead = int(g('rev-list', '--count', f'{head}..origin/{BRANCH}'))
        ff = subprocess.run(['git', '-C', str(self.root), 'merge-base', '--is-ancestor', 'HEAD', f'origin/{BRANCH}']).returncode == 0
        log = g('log', '--format=%h %s', f'{head}..origin/{BRANCH}', '-n', '20') if ahead else ''
        return {'head': head[:7], 'remote': remote[:7], 'behind': ahead, 'fast_forward': ff, 'dirty': bool(dirty),
                'commits': [line for line in log.splitlines() if line], 'checked_at': time.time()}

    def status(self):
        return dict(self.state) | {'available': self.available}

    def _set(self, **kw):
        self.state = {**self.state, **kw, 'at': time.time()}

    def _emit(self, kind, text, level_visible=True):
        c = self.app.center
        with c.store.atomic():
            c.store.emit(kind, text, actor='owner', data=self.status(), visible=level_visible)

    # ---- 执行 ----
    async def start(self):
        if self.task and not self.task.done():
            raise ValueError('已有更新正在进行')
        info = await asyncio.to_thread(self.check)
        self.available = info
        if info['dirty']:
            raise ValueError('中心代码目录有未提交的改动，不能自动更新；请先提交或清理')
        if not info['fast_forward']:
            raise ValueError(f"本地 {info['head']} 不是 origin/{BRANCH} 的祖先，不能快进；需要人工处理")
        if not info['behind']:
            raise ValueError(f"已经是最新版本 {info['head']}")
        self.state = {'phase': 'testing', 'from': info['head'], 'to': info['remote'], 'commits': info['commits'], 'started': time.time()}
        self.task = asyncio.create_task(self._run(info))
        return self.status()

    async def _run(self, info):
        target = info['remote']
        try:
            self._emit('deploy', f"开始更新中心：{info['head']} → {target}（{info['behind']} 个提交），先运行测试")
            await asyncio.to_thread(self._test, target)
            self._set(phase='draining')
            self._emit('deploy', '测试通过，等中心机上运行中的回合结束后重启；期间新消息照常排队')
            self.draining = True
            await self._drain()
            self._set(phase='restarting')
            await asyncio.to_thread(self._apply, target)
            self._emit('deploy', f'已切换到 {target}，中心正在重启；远端节点会在空闲时自动更新')
            # 交给 systemd 拉起新进程：本进程按正常流程收尾（关闭原生进程组、数据库），
            # 再以退出码 75 结束；服务配置为 Restart=on-failure，非零退出会在几秒内用新代码重新启动。
            self.restart = True
            await asyncio.sleep(0.5)
            self.exit_after()
        except Exception as e:
            self.draining = False
            self._set(phase='failed', error=str(e)[-2000:])
            self._emit('deploy_failed', f'中心更新未执行：{e}'[:1000])

    def _test(self, target):
        """在临时目录检出目标提交并跑默认测试；不影响运行中的代码与状态目录。"""
        tmp = Path(tempfile.mkdtemp(prefix='cthulhu-deploy-'))
        try:
            subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '--detach', '--quiet', str(tmp / 'src'), target],
                           check=True, capture_output=True, text=True, timeout=120)
            src = tmp / 'src'
            if shape(src) != shape(self.root):
                raise RuntimeError('新版本的依赖或命令入口有变化，需要人工安装后重启')
            env = {k: v for k, v in strip(os.environ).items() if not k.startswith('CTHULHU_')} | {'PYTHONPATH': str(src)}
            out = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'], cwd=src, env=env,
                                 capture_output=True, text=True, timeout=TEST_TIMEOUT)
            tail = (out.stderr or out.stdout).strip().splitlines()[-1:] or ['']
            self._set(tests=tail[0])
            if out.returncode != 0:
                failed = [l for l in out.stderr.splitlines() if l.startswith(('FAIL:', 'ERROR:'))][:10]
                raise RuntimeError('测试未通过：' + '；'.join(failed or tail))
        finally:
            subprocess.run(['git', '-C', str(self.root), 'worktree', 'remove', '--force', str(tmp / 'src')], capture_output=True)
            shutil.rmtree(tmp, ignore_errors=True)

    async def _drain(self):
        c, end = self.app.center, time.monotonic() + DRAIN_TIMEOUT
        while True:
            busy = [j['agent'] for j in c.store.where('lease', 'running') if j['node'] == LOCAL]
            if not busy:
                return
            self._set(waiting_for=busy)
            if time.monotonic() > end:
                raise RuntimeError(f"等待中心机回合结束超时：{', '.join(busy)}")
            await asyncio.sleep(2)

    def _apply(self, target):
        # 只快进：如果在测试期间本地或远端发生了变化，merge --ff-only 会拒绝，不会覆盖任何东西。
        self.git('merge', '--ff-only', '--quiet', target)
