"""端到端远程更新：真实 WebSocket、真实节点进程、真实 execv 重启。不调用模型。

节点从“安装脚本管理”的 ~/.cthulhu/app 运行；中心发布一个新版本，节点在空闲时下载、
换新代码、重启，并用新代码重新连上中心。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from websockets.asyncio.server import serve

from cthulhu_v4 import update
from cthulhu_v4.center import Center
from cthulhu_v4.transport import encode_invite, make_handler
from cthulhu_v4.util import MAX_FRAME, ROOT


def copy_tree(dest):
    for rel in update.files(ROOT):
        target = dest / rel; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, target)
    return dest


class UpdateE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.home = self.path / 'home'
        self.app = copy_tree(self.home / 'app')                       # 节点运行的代码 = 安装脚本的位置
        self.c = Center(self.path / 'center')
        self.server = await serve(make_handler(self.c), '127.0.0.1', 0, max_size=MAX_FRAME)
        self.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.log = (self.path / 'node.log').open('a')
        self.proc = None

    async def asyncTearDown(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try: await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError: self.proc.kill(); await self.proc.wait()
        self.server.close(); await self.server.wait_closed()
        self.c.store.close(); self.log.close(); self.tmp.cleanup()

    async def wait(self, check, seconds=30):
        loop = asyncio.get_running_loop(); end = loop.time() + seconds
        while loop.time() < end:
            if check(): return
            await asyncio.sleep(.1)
        self.fail('等待超时\n' + (self.path / 'node.log').read_text())

    def env(self):
        # 只从节点自己的代码目录导入，确认重启后跑的确实是换上的新代码。
        return {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'} | {'PYTHONPATH': str(self.app)}

    async def test_idle_node_updates_restarts_and_reconnects_with_new_code(self):
        with self.c.store.atomic():
            info = self.c.create_invite('gpu', self.url)
        blob = encode_invite({k: info[k] for k in ('center', 'code', 'node', 'pin')})
        join = await asyncio.create_subprocess_exec(sys.executable, '-m', 'cthulhu_v4', 'node', 'join', blob, '--home', str(self.home),
                                                    cwd=self.app, env=self.env(), stdout=self.log, stderr=self.log)
        self.assertEqual(await join.wait(), 0)
        old_code = update.digest(self.app)
        self.proc = await asyncio.create_subprocess_exec(sys.executable, '-m', 'cthulhu_v4', 'node', 'run', '--home', str(self.home),
                                                         cwd=self.app, env=self.env(), stdout=self.log, stderr=self.log)
        await self.wait(lambda: self.c.store.get('node', 'gpu').get('code') == old_code)
        self.assertTrue(self.c.store.get('node', 'gpu')['managed'])
        pid = self.proc.pid

        # 中心发布新版本：内容不同的一份代码。
        src = copy_tree(self.path / 'src')
        (src / 'cthulhu_v4' / 'release_probe.py').write_text('VERSION = "probe"\n')
        self.c.release = update.Release(src); self.c.release.clean = True
        expire = asyncio.create_task(self.expire_loop())
        try:
            await self.wait(lambda: self.c.store.get('node', 'gpu').get('code') == self.c.release.code, seconds=40)
        finally:
            expire.cancel()
        self.assertEqual(self.proc.pid, pid)                           # execv 原地重启，PID 不变
        self.assertIsNone(self.proc.returncode)
        self.assertTrue((self.app / 'cthulhu_v4' / 'release_probe.py').exists())
        await self.wait(lambda: not (self.home / update.MARKER).exists())   # 连上后确认并清理
        self.assertFalse(self.app.with_name('app.old').exists())
        self.assertNotIn('update', self.c.store.get('node', 'gpu'))
        log = (self.path / 'node.log').read_text()
        self.assertIn('更新已生效', log)

    async def expire_loop(self):
        while True:
            self.c.hub.expire(); await asyncio.sleep(.5)
