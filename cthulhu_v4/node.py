"""星系执行节点：仅主动连中心；原生 MCP 经本机 loopback 转 WebSocket。

本机 SQLite 是可靠事件发件箱和 session/PID 记录，不是第二份业务数据库。
每次接单先落盘。中心重发同一票据不会重新执行；重启后的未知回合只报备。
"""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import ssl
import threading
import time
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .core import process_alive
from .galaxy import MAX_FRAME, EXECUTION_POLICY
from .runtime import make_runtime
from .store import Store
from .tools import tools_for
from .web import Web


class RuntimeStore:
    def __init__(self, node, job): self.node, self.job = node, job
    def get(self, kind, ident): return self.node.store.get(kind, ident)
    def put(self, kind, value): return self.node.store.put(kind, value)
    def emit(self, kind, text='', **kw):
        return self.node.queue(self.job, kind, kw.get('data', {}), text, kw.get('visible', False))


class RuntimeContext:
    def __init__(self, node, job):
        self.node, self.job = node, job
        self.store = RuntimeStore(node, job['id'])
        self.url = node.url

    def task_id(self, _): return self.job['task']['id']
    def record_native(self, _, kind, payload): self.store.emit('native:' + kind, data=payload)
    def save_session(self, aid, session):
        a = self.store.get('agent', aid)
        if a['session'] and a['session'] != session:
            raise RuntimeError('原生 session 变化，拒绝切换')
        # session 与待上传通知同事务；断电后以本机原生 session 为准恢复。
        with self.node.store.atomic():
            a['session'] = session; self.store.put('agent', a)
            self.node.queue(self.job['id'], 'session', {'session': session})


class NodeWeb(Web):
    async def route(self, method, path, query, token, body):
        n = self.core
        job = next((j for j in n.jobs.values() if secrets.compare_digest(j['token'], token)), None)
        if not job or job['id'] not in n.active: raise PermissionError('无效或过期的本机回合 token')
        if path == '/internal/tools' and method == 'GET': return {'tools': tools_for(job['agent'])}
        if path == '/internal/call' and method == 'POST':
            return await n.tool(job, body['name'], body.get('arguments', {}))
        raise PermissionError('星系节点只提供本机 MCP 桥，不提供业务管理 API')


class Node:
    def __init__(self, state, ident, url, token, *, capacity=2, backends=None):
        self.state_dir = Path(state).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.state_dir / 'node.db')
        self.ident, self.center, self.token, self.capacity = ident, url, token, capacity
        self.backends = backends or ['demo'] + [b for b in ('codex', 'claude') if shutil.which(b)]
        self.ws, self.url = None, ''
        self.lock = asyncio.Lock()
        self.active, self.jobs, self.expires = {}, {}, {}
        # 中心可据此区分更新后的节点与仍在内存中运行旧适配器的节点。
        self.runtime_revision = hashlib.sha256(Path(__file__).with_name('runtime.py').read_bytes()).hexdigest()[:16]
        self.closed = False
        settings = self.store.all('settings')
        self.settings = settings[0] if settings else {'id': 'node', 'instance': str(uuid.uuid4()), 'ack': 0}
        self.store.put('settings', self.settings)
        for a in self.store.all('agent'):
            if process_alive(a):
                raise RuntimeError(f"旧进程 {a['pid']} 仍存活。请核验并结束旧进程后重启节点，拒绝并发恢复原生会话。")
        for j in self.store.all('execution'):
            if j['state'] == 'running':
                with self.store.atomic():
                    j['state'] = 'stopped'; self.store.put('execution', j)
                    self.queue(j['id'], 'finished', {'error': '节点进程曾退出，上一回合结果未知；未重放'})

    def queue(self, job, kind, data, text='', visible=False):
        return self.store.emit(kind, text, data={'job': job, 'payload': data, 'visible': visible})

    async def rpc(self, body):
        # 一条连接串行 request/response；工具耗时不跨中心 await，避免读响应串线。
        async with self.lock:
            ws = self.ws
            if not ws: raise ConnectionError('中心连接不可用')
            try:
                async with asyncio.timeout(8):
                    await ws.send(json.dumps(body, ensure_ascii=False))
                    reply = json.loads(await ws.recv())
            except BaseException:
                # 读超时后不能继续复用这条连接，否则迟到响应会被下一请求误收。
                await ws.close()
                raise
            if 'error' in reply: raise RuntimeError(reply['error'])
            return reply['result']

    async def tool(self, job, name, arguments):
        if time.monotonic() >= self.expires.get(job['id'], 0):
            raise PermissionError('本机回合租约已失效')
        body = {'op': 'tool', 'job': job['id'], 'call_id': str(uuid.uuid4()),
                'name': name, 'arguments': arguments}
        # 保留 call_id 重试一次；中心将结果与业务操作一起落盘，防止网络重试重复写。
        try: return await self.rpc(body)
        except (ConnectionError, OSError, ConnectionClosed):
            previous = self.ws
            for _ in range(8):
                await asyncio.sleep(1)
                if self.ws and self.ws is not previous: return await self.rpc(body)
            raise ConnectionError('工具调用确认未知；请先核验状态，不要盲目重试副作用')

    async def demo(self, job, ctx, a):
        if not a['session']: ctx.save_session(a['id'], 'demo-' + a['id'])
        ctx.record_native(a['id'], 'demo', {'text': f'节点 {self.ident} 接收任务；模拟执行，不运行模型。'})
        await asyncio.sleep(.15)
        task = job['task']
        if task['state'] == 'completed': return '[模拟] 已完成任务收到后续消息。'
        if task['state'] == 'waiting': return '[模拟] 等待回复。'
        message = json.loads(job['prompt'].split('\n', 1)[1])['message']
        if '回答/修正' in message['text']:
            await self.tool(job, 'finish_task', {'result': '[模拟] 跨节点任务完成',
                'evidence': f'由星系节点 {self.ident} 执行模拟问答；未执行业务命令'})
            return '[模拟] 完成'
        await self.tool(job, 'ask_master', {'text': f'节点 {self.ident}：是否继续任务？',
            'recommendation': '建议继续模拟流程', 'blocking': True})
        return '[模拟] 等待 Master'

    async def execute(self, job):
        result, error, runtime = '', '', None
        try:
            a = self.store.get('agent', job['agent'])
            if not Path(a['cwd']).is_dir(): raise ValueError('节点上的工作目录不存在：' + a['cwd'])
            ctx = RuntimeContext(self, job)
            if a['backend'] == 'demo': result = await self.demo(job, ctx, a)
            else:
                runtime = make_runtime(ctx, a)
                result = await runtime.run(job['prompt'])
        except asyncio.CancelledError: error = '中心要求停止、连接租约到期或节点关闭；须核验已有副作用'
        except Exception as e: error = str(e)
        finally:
            # 每轮关闭进程，下一轮由原生 session ID 恢复；凭证仅本回合有效。
            # 原生上下文仍由 CLI 持久管理，不在两台机器之间拼接聊天记录。
            if runtime: await runtime.close()
            with self.store.atomic():
                self.store.put('execution', {'id': job['id'], 'state': 'stopped'})
                self.queue(job['id'], 'finished', {'result': result, 'error': error})
            self.active.pop(job['id'], None)

    def accept(self, result, started):
        self.settings['ack'] = result['ack']; self.store.put('settings', self.settings)
        allowed = {j['id'] for j in result['jobs']}
        for jid, task in list(self.active.items()):
            if jid not in allowed and not task.cancelling(): task.cancel()
        for job in result['jobs']:
            jid = job['id']
            # 使用请求开始时的单调时钟，扣除停止进程余量，不依赖两机时间一致。
            self.expires[jid] = started + result['lease_seconds'] - 10
            if jid in self.active: continue
            if any(x['id'] == jid for x in self.store.all('execution')): continue
            if time.monotonic() >= self.expires[jid]: continue
            a = job['agent_spec'].copy()
            previous = next((v for v in self.store.all('agent') if v['id'] == a['id']), None)
            if previous:
                if a['session'] and previous['session'] != a['session']:
                    raise RuntimeError('中心和本机 session 不一致，拒绝执行')
                a['session'] = previous['session']
            # 每轮 MCP token 只在本机流转，中心节点凭证绝不交给模型子进程。
            job['token'] = secrets.token_urlsafe(32); a['token'] = job['token']
            with self.store.atomic():
                self.store.put('agent', a)
                self.store.put('execution', {'id': jid, 'state': 'running'})
            self.jobs[jid] = job
            self.active[jid] = asyncio.create_task(self.execute(job))

    async def watchdog(self):
        while not self.closed:
            for jid, task in list(self.active.items()):
                if time.monotonic() >= self.expires[jid] and not task.cancelling(): task.cancel()
            await asyncio.sleep(.25)

    async def serve(self, ssl_context=None):
        monitor = asyncio.create_task(self.watchdog())
        try:
            while not self.closed:
                try:
                    opts = {'ssl': ssl_context} if self.center.startswith('wss:') else {}
                    async with connect(self.center, max_size=MAX_FRAME, open_timeout=10,
                                       close_timeout=2, proxy=None, **opts) as ws:
                        await ws.send(json.dumps({'node': self.ident, 'token': self.token}))
                        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
                        if not hello.get('ok'): raise PermissionError('节点注册失败')
                        self.ws = ws
                        synced = False
                        while not self.closed:
                            events = [{'seq': e['seq'], 'job': e['data']['job'], 'kind': e['kind'],
                                       'text': e['text'], 'at': e['at'], 'data': e['data']['payload'], 'visible': e['data']['visible']}
                                      for e in self.store.events(after=self.settings['ack'], limit=32)]
                            # 按字节限制一批活动，避免多个大工具输出超过 WebSocket 帧上限。
                            size, batch = 0, []
                            for event in events:
                                cost = len(json.dumps(event, ensure_ascii=False).encode())
                                if batch and size + cost > 8 * 1024 * 1024: break
                                batch.append(event); size += cost
                            events = batch
                            started = time.monotonic()
                            result = await self.rpc({'op': 'sync', 'instance': self.settings['instance'],
                                'capacity': self.capacity, 'backends': self.backends, 'runtime_revision': self.runtime_revision,
                                'execution_policy': EXECUTION_POLICY, 'events': events})
                            self.accept(result, started)
                            if not synced:
                                # WebSocket 鉴权成功还不等于节点接入成功；先核验磁盘身份与事件游标。
                                print(f'星系连接中心：{self.ident} · runtime={self.runtime_revision} · policy={EXECUTION_POLICY}'
                                      f' · state={self.state_dir} · instance={self.settings["instance"]}', flush=True)
                                synced = True
                            await asyncio.sleep(.1 if len(events) == 32 else 1)
                except asyncio.CancelledError: raise
                except Exception as e:
                    print('星系连接暂不可用：' + str(e) + '；状态目录：' + str(self.state_dir), flush=True)
                    await asyncio.sleep(2)
                finally: self.ws = None
        finally:
            self.closed = True; monitor.cancel()
            jobs = list(self.active.values())
            for task in jobs:
                if not task.cancelling(): task.cancel()
            await asyncio.gather(monitor, *jobs, return_exceptions=True)


async def main(args):
    os.umask(0o077)
    path = Path(args.state).resolve(); path.mkdir(parents=True, exist_ok=True)
    lock = (path / 'node.lock').open('w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('该节点状态目录已运行')
    node = Node(path, args.id, args.center, Path(args.token_file).read_text().strip(), capacity=args.capacity)
    web = NodeWeb(node, asyncio.get_running_loop(), 0)
    threading.Thread(target=web.server.serve_forever, daemon=True).start()
    context = ssl.create_default_context(cafile=args.ca) if args.center.startswith('wss:') else None
    task = asyncio.create_task(node.serve(context))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT): loop.add_signal_handler(sig, task.cancel)
    try: await task
    except asyncio.CancelledError: pass
    finally:
        await asyncio.to_thread(web.server.shutdown); web.server.server_close()
        node.store.db.close(); lock.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='主动连接中心 WebSocket 的星系执行节点')
    p.add_argument('--id', required=True)
    p.add_argument('--center', required=True, help='wss://中心域名:8770（本机联调可用 ws://）')
    p.add_argument('--token-file', required=True)
    p.add_argument('--state', required=True, help='持久本机目录，包含原生会话指针和事件发件箱')
    p.add_argument('--capacity', type=int, default=2)
    p.add_argument('--ca', help='可选私有 CA 证书；始终校验 TLS 证书')
    args = p.parse_args()
    if not 1 <= args.capacity <= 32: p.error('capacity 必须为 1..32')
    if not args.center.startswith(('ws://', 'wss://')): p.error('center 必须是 ws:// 或 wss:// 地址')
    asyncio.run(main(args))
