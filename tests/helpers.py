"""测试辅助：真实 Center + 真实 Node（经进程内 LocalLink），不调用模型。"""
import asyncio
from datetime import datetime, timezone

from cthulhu_v4.bridge import Bridge
from cthulhu_v4.center import Center
from cthulhu_v4.node import Node
from cthulhu_v4.transport import LocalLink
from cthulhu_v4.util import LOCAL


def future(seconds=99999):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def task_args(cwd, **kw):
    return dict(title='测试', description='只读验证', acceptance='给出证据', deadline='2099-01-01T00:00:00+00:00',
                cwd=str(cwd)) | kw


class Harness:
    """中心 + 内嵌 local 节点，与 App 的组装方式一致，但由测试驱动同步节奏。"""

    def __init__(self, path, **kw):
        self.c = Center(path / 'center', **kw)
        self.node = Node(path / 'local-node', backends=['demo', 'codex', 'claude'])
        self.node.link = LocalLink(self.c.hub, self.node)
        self.c.hub.attach(LOCAL, self.node.link)
        self.bridge = Bridge(self.node, asyncio.get_running_loop()); self.bridge.start()

    async def sync(self, rounds=1):
        for _ in range(rounds):
            await self.node.sync_once(); await asyncio.sleep(0.02)

    async def settle(self, check=None, seconds=8):
        loop = asyncio.get_running_loop(); end = loop.time() + seconds
        while loop.time() < end:
            await self.node.sync_once()
            idle = not self.node.active and not self.node.outbox() and not any(
                m['state'] in ('queued', 'running') for m in self.c.store.all('message')
                if not self.c.store.get('agent', m['target'])['paused'])
            if (check() if check else idle):
                return
            await asyncio.sleep(0.03)
        raise AssertionError('等待超时')

    async def close(self):
        await self.node.close()
        await asyncio.to_thread(self.bridge.stop)
        self.node.store.close(); self.c.store.close()
