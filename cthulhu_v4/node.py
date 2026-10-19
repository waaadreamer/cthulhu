"""执行节点：领取回合、运行原生 CLI、可靠上传事件。中心机内嵌的 local 节点与远端星系共用此实现。

本机 SQLite 是事件发件箱和 session/PID 记录，不是第二份业务数据库：
- 接单先落盘 execution，再启动进程；同一票据重发不会再执行。
- 事件按自增序号排队，中心确认后才前移游标；断线后从游标补传。
- session 指针先在本机落盘再排队上传，断电后以本机记录为准恢复。
- 本机租约按单调时钟计算，在中心租约到期前 10 秒主动停止进程。
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import time
import uuid

from .runtime import make_runtime
from .store import Store
from .util import PROTOCOL, process_alive

VERSION = '4.1'


class Turn:
    """一个回合对运行时暴露的 Sink；所有记录都落到节点发件箱。"""

    def __init__(self, node, job):
        self.node, self.job = node, job
        self.agent = job['agent']
        self.token = secrets.token_urlsafe(32)  # 每回合的本机 MCP token；节点凭证绝不交给模型子进程。
        self.url = node.bridge_url

    def native(self, kind, payload):
        self.node.queue(self.job['id'], 'native:' + kind, payload)

    def session(self, sid):
        n = self.node
        a = n.store.get('agent', self.agent)
        if a.get('session') and a['session'] != sid:
            raise RuntimeError('原生 session 变化，拒绝切换')
        with n.store.atomic():
            a['session'] = sid; n.store.put('agent', a)
            n.queue(self.job['id'], 'session', {'session': sid})

    def pid(self, pid, identity):
        a = self.node.store.get('agent', self.agent)
        a.update(pid=pid, pid_identity=identity); self.node.store.put('agent', a)

    async def tool(self, name, args):
        return await self.node.call_tool(self.job, name, args)


class Node:
    def __init__(self, state_dir, *, capacity=2, backends=None, link=None):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.state_dir / 'node.db')
        self.capacity = capacity
        self.backends = backends or ['demo'] + [b for b in ('codex', 'claude') if shutil.which(b)]
        self.link = link             # 由传输层注入：request(body) → result
        self.bridge_url = ''         # 本机 MCP 桥（HTTP loopback）
        self.active, self.turns, self.expires = {}, {}, {}
        self.kick = asyncio.Event()
        self.closed = False
        settings = self.store.find('settings', 'node')
        if not settings:
            settings = {'id': 'node', 'instance': str(uuid.uuid4()), 'ack': 0}
            self.store.put('settings', settings)
        self.settings = settings
        for a in self.store.all('agent'):
            if process_alive(a):
                raise RuntimeError(f"旧原生进程 {a['pid']}（{a['id']}）仍存活。请核验并结束它后再启动节点，拒绝并发恢复同一会话。")
        with self.store.atomic():
            for j in self.store.all('execution'):
                if j['state'] == 'running':
                    # 节点进程曾退出：回合结果未知，只报备，绝不重放。
                    j['state'] = 'stopped'; self.store.put('execution', j)
                    self.queue(j['id'], 'finished', {'outcome': 'unknown', 'error': '节点进程曾退出，上一回合结果未知；未重放'})

    @property
    def instance(self):
        return self.settings['instance']

    def queue(self, job, kind, data, text='', visible=False):
        seq = self.store.emit(kind, text, data={'job': job, 'payload': data, 'visible': visible})
        self.kick.set()
        return seq

    def outbox(self, limit=64, max_bytes=8 * 1024 * 1024):
        """按字节限制一批事件，避免大工具输出超过 WebSocket 帧上限。"""
        batch, size = [], 0
        for e in self.store.events(after=self.settings['ack'], limit=limit):
            item = {'seq': e['seq'], 'job': e['data']['job'], 'kind': e['kind'], 'text': e['text'],
                    'at': e['at'], 'data': e['data']['payload'], 'visible': e['data']['visible']}
            cost = len(json.dumps(item, ensure_ascii=False).encode())
            if batch and size + cost > max_bytes:
                break
            batch.append(item); size += cost
        return batch

    def hello(self):
        return {'protocol': PROTOCOL, 'version': VERSION, 'instance': self.instance}

    async def sync_once(self):
        events = self.outbox()
        started = time.monotonic()
        result = await self.link.request({'op': 'sync', 'instance': self.instance, 'capacity': self.capacity,
                                          'backends': self.backends, 'version': VERSION,
                                          'host': socket.gethostname(), 'home': str(Path.home()), 'events': events})
        self.accept(result, started)
        return len(events)

    def accept(self, result, started):
        self.settings['ack'] = result['ack']; self.store.put('settings', self.settings)
        allowed = {j['id'] for j in result['jobs']}
        for jid, task in list(self.active.items()):
            # 中心不再续租的回合（被打断、暂停、租约失效）立即停止进程。
            if jid not in allowed and not task.cancelling():
                task.cancel()
        for job in result['jobs']:
            jid = job['id']
            # 用请求发出时的单调时钟，扣除停止进程的余量，不依赖两机时钟一致。
            self.expires[jid] = started + result['lease_seconds'] - 10
            if jid in self.active or self.store.find('execution', jid):
                continue
            if time.monotonic() >= self.expires[jid]:
                continue
            spec = dict(job['agent_spec'])
            local = self.store.find('agent', spec['id'])
            # 主人在中心换了后端时 epoch 增加：本机旧 session 属于另一个后端，不能复用。
            if local and local.get('epoch', 0) == spec.get('epoch', 0):
                if spec.get('session') and local.get('session') and local['session'] != spec['session']:
                    raise RuntimeError('中心和本机 session 不一致，拒绝执行')
                spec['session'] = local.get('session') or spec.get('session')
            with self.store.atomic():
                self.store.put('agent', spec)
                self.store.put('execution', {'id': jid, 'state': 'running', 'agent': spec['id']})
            turn = Turn(self, job)
            self.turns[jid] = turn
            self.active[jid] = asyncio.create_task(self.execute(job, turn))

    async def execute(self, job, turn):
        outcome, result, error, runtime = 'failed', '', '', None
        try:
            a = self.store.get('agent', job['agent'])
            if not Path(a['cwd']).is_dir():
                raise ValueError('节点上的工作目录不存在：' + a['cwd'])
            runtime = make_runtime(turn, a)
            result = await runtime.run(job['prompt'])
            outcome = 'completed'
        except asyncio.CancelledError:
            outcome, error = 'stopped', '中心要求停止、租约到期或节点关闭；须核验已有副作用'
        except Exception as e:
            error = str(e)
        finally:
            # 每轮结束关闭进程，下一轮用原生 session ID 恢复；MCP token 随回合失效。
            if runtime:
                try: await runtime.close()
                except Exception as e: error = error or f'关闭运行时失败：{e}'
            with self.store.atomic():
                self.store.put('execution', {'id': job['id'], 'state': 'stopped', 'agent': job['agent']})
                self.queue(job['id'], 'finished', {'outcome': outcome, 'result': result, 'error': error})
            self.active.pop(job['id'], None); self.turns.pop(job['id'], None)

    async def call_tool(self, job, name, arguments):
        if time.monotonic() >= self.expires.get(job['id'], 0):
            raise PermissionError('本机回合租约已失效')
        body = {'op': 'tool', 'job': job['id'], 'call_id': str(uuid.uuid4()), 'name': name, 'arguments': arguments}
        # 连接抖动时用同一 call_id 重试；中心把结果与业务变更同事务落盘，重试不会重复写。
        for attempt in range(8):
            try:
                return await self.link.request(body)
            except ConnectionError:
                if attempt == 7:
                    raise ConnectionError('工具调用确认未知；请先核验状态，不要盲目重试副作用')
                await asyncio.sleep(1)

    def turn_for_token(self, token):
        for jid, turn in self.turns.items():
            if jid in self.active and secrets.compare_digest(turn.token, token):
                return turn
        return None

    async def watchdog(self):
        while not self.closed:
            for jid, task in list(self.active.items()):
                if time.monotonic() >= self.expires.get(jid, 0) and not task.cancelling():
                    task.cancel()
            await asyncio.sleep(.25)

    async def run(self):
        """同步循环：有新事件立即上传；否则每秒一次心跳续租。连接由 link 负责重建。"""
        monitor = asyncio.create_task(self.watchdog())
        try:
            while not self.closed:
                try:
                    sent = await self.sync_once()
                except ConnectionError:
                    await asyncio.sleep(1); continue
                if sent >= 64:
                    continue
                self.kick.clear()
                try: await asyncio.wait_for(self.kick.wait(), 1)
                except asyncio.TimeoutError: pass
        finally:
            await self.close()
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def close(self):
        self.closed = True
        jobs = list(self.active.values())
        for task in jobs:
            if not task.cancelling(): task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
