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

from . import proxy
from .execution import FEATURE as COMMAND_FEATURE, NodeCommands
from .files import FEATURE, MAX_BYTES, read_path
from .models import REFRESH as MODELS_REFRESH, discover
from .runtime import make_runtime
from .update import FEATURE as UPDATE_FEATURE, digest, install as install_release, mark as mark_update
from .store import Store
from .transport import MUX
from .util import LEASE_MARGIN, PROTOCOL, process_alive

# data 连接：每批事件不超过 2MB、等 60 秒。慢链路上一批传不完不会影响续租，只是事件晚到。
UPLOAD_BYTES = 2 * 1024 * 1024
UPLOAD_TIMEOUT = 60
# 工具调用在 data 连接上等多久。不用 15 秒：中心并发处理，慢只会是因为结果大、链路慢，等久一点比反复重传划算。
TOOL_TIMEOUT = 60

VERSION = '4.1'


class Turn:
    """一个回合对运行时暴露的 Sink；所有记录都落到节点发件箱。"""

    def __init__(self, node, job):
        self.node, self.job = node, job
        self.agent = job['agent']
        self.token = secrets.token_urlsafe(32)  # 每回合的本机 MCP token；节点凭证绝不交给模型子进程。
        self.url = node.bridge_url
        self.proxy = node.model_proxy
        self.native_dir = node.state_dir / 'antigravity' / self.agent

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

    def model_evidence(self, profile, status, error, model):
        n = self.node
        evidence = {'id': profile, 'inference_status': status, 'inference_error': error[:300],
                    'inference_model': model, 'inference_at': time.time()}
        n.store.put('model_evidence', evidence)
        if n.model_profiles.get('antigravity') == profile and 'antigravity' in n.models:
            # 新字典保证同步中途出现的证据不会被旧回执清掉 dirty；只影响同配置的节点目录。
            n.models = {**n.models, 'antigravity': {**n.models['antigravity'], **{k: v for k, v in evidence.items() if k != 'id'}}}
            n.models_dirty = True; n.kick.set()

    def pid(self, pid, identity):
        a = self.node.store.get('agent', self.agent)
        a.update(pid=pid, pid_identity=identity); self.node.store.put('agent', a)

    async def tool(self, name, args):
        return await self.node.call_tool(self.job, name, args)


def file_reply(req, body):
    """文件读取结果的回传请求体。

    为什么文件 id 要放在 file 里而不只是 id：多路复用连接上，传输层给每个请求写入自己的 id 来匹配响应，
    会覆盖请求体里的同名字段。中心靠 id 找等待中的网页请求，被覆盖后永远找不到，网页只能等到超时；
    本机节点走进程内链路不加 id，所以只有远端节点读不了文件。
    仍保留 id：旧中心不支持多路复用，只认 id，且那条串行连接不会改写它。
    """
    return {'op': 'file', 'file': req['id'], 'id': req['id'], **body}


class Node:
    def __init__(self, state_dir, *, backends=None, link=None, protected=(), app=None, managed=False,
                 model_proxy=None):
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.store = Store(self.state_dir / 'node.db')
        self.model_proxy = model_proxy
        # 网页可以打开节点上的文件，但凭证与状态目录除外（含 node.json 里的节点 token）。
        self.protected = [Path(p).resolve() for p in (self.state_dir, *protected)]
        self.reads = set()
        # 远程更新：app 是本节点运行的代码目录；managed 表示由安装脚本管理，可以整目录替换。
        self.app, self.managed = (Path(app) if app else None), managed
        self.code = digest(self.app) if self.app else None
        self.updating = False
        self.restart = False
        self.on_synced = None
        self.backends = backends or ['demo'] + [b for b, cli in (('codex', 'codex'), ('claude', 'claude'), ('antigravity', 'agy')) if shutil.which(cli)]
        # 本机可用模型：后台定期发现；有变化时随下一次同步上报一次，中心持久保存。
        self.models, self.models_dirty = {}, False
        self.models_wake = asyncio.Event()
        self.model_profiles = {}
        self.models_requested, self.models_completed = None, None
        self.discover_models = not backends   # 测试显式指定后端时不启动真实 CLI
        self.link = link             # 由传输层注入：request(body) → result；控制连接（同步与续租）
        self.data = None             # 远端节点的 data 连接（见 data_link）；本机节点为 None
        self.uploaded = asyncio.Event()
        self.bridge_url = ''         # 本机 MCP 桥（HTTP loopback）
        self.active, self.turns, self.expires = {}, {}, {}
        self.grace = 0
        self.steering = set()  # (job id, message id)：已尝试插入的消息，中心重复下发时不重复插入
        self.stops = {}  # job id → 停止原因；CancelledError 本身不带原因，回报时须如实区分
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

        self.commands = NodeCommands(self)

    @property
    def instance(self):
        return self.settings['instance']

    def queue(self, job, kind, data, text='', visible=False):
        seq = self.store.emit(kind, text, data={'job': job, 'payload': data, 'visible': visible})
        self.kick.set(); self.uploaded.set()
        return seq

    def outbox(self, limit=64, max_bytes=8 * 1024 * 1024):
        """按字节限制一批事件，避免大工具输出超过 WebSocket 帧上限。"""
        batch, size = [], 0
        for e in self.store.events(after=self.settings['ack'], limit=limit):
            item = {'seq': e['seq'], 'job': e['data']['job'], 'kind': e['kind'], 'text': e['text'],
                    'at': e['at'], 'data': e['data']['payload'], 'visible': e['data']['visible']}
            cost = len(json.dumps(item, ensure_ascii=False).encode())
            # 命令输出分成小批走 data；旧中心回退控制连接时也限制每批，避免挤占心跳。
            cap = min(max_bytes, 128 * 1024) if item['kind'].startswith('command:') else max_bytes
            if batch and size + cost > cap:
                break
            batch.append(item); size += cost
        return batch

    def hello(self):
        return {'protocol': PROTOCOL, 'version': VERSION, 'instance': self.instance}

    async def refresh_models(self):
        env = proxy.model_env(os.environ, self.model_proxy)
        env.pop('CLAUDECODE', None)
        while not self.closed:
            try:
                self.models_wake.clear()
                requested = self.models_requested
                found = await discover(self.backends, env, self.models, cwd=self.state_dir / 'model-probe')
                for backend, info in found.items():
                    profile = info.pop('_profile', None)
                    self.model_profiles[backend] = profile
                    evidence = self.store.find('model_evidence', profile) if profile else None
                    if evidence:
                        info.update({k: v for k, v in evidence.items() if k != 'id'})
                completed = self.models_completed
                self.models_completed = requested
                if found != self.models or completed != requested:
                    self.models, self.models_dirty = found, True
                    self.kick.set()
            except Exception as e:
                print('模型发现失败：' + str(e), flush=True)
            # 刷新只唤醒同一个发现循环，避免连点启动重叠 CLI；发现期间的新请求保留到下一轮。
            try: await asyncio.wait_for(self.models_wake.wait(), MODELS_REFRESH)
            except asyncio.TimeoutError: pass

    def data_link(self):
        """工具调用、事件上传、文件与更新包走 data 连接；中心不支持时退回控制连接。
        为什么分两条连接：续租心跳只有几百字节，但 S 这类远端节点上传 1–2MB 的工具结果要十几秒，
        同一条连接上心跳排在后面，超过 60 秒没续上，节点就以为与中心失联而停掉进程。"""
        if self.data and MUX in getattr(self.link, 'features', ()):
            return self.data
        return None

    async def upload(self):
        """data 连接上持续上传发件箱；控制连接的同步不再夹带事件。"""
        while not self.closed:
            data = self.data_link()
            events = self.outbox(max_bytes=UPLOAD_BYTES) if data else []
            if events:
                try:
                    r = await data.request({'op': 'events', 'instance': self.instance, 'events': events}, timeout=UPLOAD_TIMEOUT)
                    self.ack(r['ack'])
                    continue
                except (ConnectionError, RuntimeError) as e:
                    print('事件上传失败，稍后重试：' + str(e), flush=True)
                    await asyncio.sleep(1)
                    continue
            self.uploaded.clear()
            try: await asyncio.wait_for(self.uploaded.wait(), 1)
            except asyncio.TimeoutError: pass

    def ack(self, value):
        # 控制连接与 data 连接都会带回确认游标，先后不定：只前进不后退，否则已确认的事件会被重发。
        if value > self.settings['ack']:
            self.settings['ack'] = value; self.store.put('settings', self.settings)

    async def sync_once(self):
        events = [] if self.data_link() else self.outbox(max_bytes=64 * 1024)
        started = time.monotonic()
        dirty = self.models_dirty
        reported = self.models
        extra = {'models': reported, 'models_completed': self.models_completed} if dirty else {}
        result = await self.link.request(extra | {'op': 'sync', 'instance': self.instance, 'base': str(self.state_dir.parent),
                                          # 已收到全文的回合：中心续租时只回 id，不再重发 prompt。
                                          'have': list(self.expires), 'commands_have': list(self.commands.known),
                                          'backends': self.backends, 'version': VERSION,
                                          'host': socket.gethostname(), 'home': str(Path.home()), 'features': [FEATURE, UPDATE_FEATURE, COMMAND_FEATURE, 'steer', 'models-refresh'], 'code': self.code,
                                          'managed': self.managed, 'events': events})
        self.accept(result, started)
        if dirty and self.models is reported:
            # 只有中心确认收到才清掉标记；连接中断时下一次同步重报，列表不会丢。
            self.models_dirty = False
        if self.on_synced:
            hook, self.on_synced = self.on_synced, None
            hook()
        return len(events)

    def accept(self, result, started):
        self.ack(result['ack'])
        requested = result.get('models_refresh')
        if requested and requested != self.models_requested:
            self.models_requested = requested
            self.models_wake.set()
        command_base = (result['now'] - time.time() + time.monotonic()) if 'now' in result else started
        self.commands.accept(result.get('commands', []), command_base)
        if result.get('update') and self.managed and not self.updating and not self.active and not self.commands.active:
            self.updating = True
            task = asyncio.create_task(self.apply_update(result['update']))
            self.reads.add(task); task.add_done_callback(self.reads.discard)
        for req in result.get('files', []):
            task = asyncio.create_task(self.serve_file(req))
            self.reads.add(task); task.add_done_callback(self.reads.discard)
        allowed = {j['id'] for j in result['jobs']}
        # 已结束的回合不再算“已持有”，否则 have 会随历史无限增长。
        for jid in list(self.expires):
            if jid not in allowed and jid not in self.active:
                self.expires.pop(jid, None)
        # 失联容忍时长由中心下发的租约决定，报错文案用实际值，不写死。
        self.grace = result['lease_seconds'] - LEASE_MARGIN
        for jid, task in list(self.active.items()):
            # 中心不再续租的回合（被打断、暂停、租约失效）立即停止进程。
            if jid not in allowed and not task.cancelling():
                self.stop(jid, task, '中心不再续租（已要求停止、暂停或租约在中心已失效）')
        for job in result['jobs']:
            jid = job['id']
            # 用中心下发的当前时间计算到期时刻，消除心跳 RTT 的影响。
            # 旧中心不下发 now：退回请求发出时的单调时钟（可能受 RTT 影响提前到期）。
            # 为什么：节点的到期基准是"请求发出时"，中心是"响应发出时"；RTT 超过 LEASE_MARGIN
            # 时节点会比中心提前停止任务。用中心的 now 后两边基准一致，容忍任意长的 RTT。
            # now 是墙上时钟，依赖两机时钟大致同步（NTP 偏差通常在几百毫秒内，远小于 10 秒余量）。
            # expires 存储单调时钟：墙上时钟减去当前墙上时钟再加上当前单调时钟，转成本地单调基准。
            if 'now' in result:
                expires_wall = result['now'] + result['lease_seconds'] - LEASE_MARGIN
                self.expires[jid] = expires_wall - time.time() + time.monotonic()
            else:
                # 旧中心：用单调时钟，保持原有行为
                self.expires[jid] = started + result['lease_seconds'] - LEASE_MARGIN
            if jid in self.active or self.store.find('execution', jid):
                continue
            if 'agent_spec' not in job:
                # 中心以为本机已有全文（have 与本地状态不一致，理论上不会发生）：
                # 不猜测内容，丢掉记录，下一次同步会收到全文。
                self.expires.pop(jid, None); continue
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
        for job in result['jobs']:
            for item in job.get('steer') or ():
                key = (job['id'], item['id'])
                if key in self.steering or job['id'] not in self.turns:
                    continue  # 已尝试过；或回合不在本机运行（结束报告会让中心退回队列）
                self.steering.add(key)
                task = asyncio.create_task(self.steer(job['id'], item))
                self.reads.add(task); task.add_done_callback(self.reads.discard)

    async def steer(self, jid, item):
        """把中心转来的主人消息插进正在运行的回合，并如实报告结果；失败时中心把消息退回队列。"""
        ok, error = False, ''
        try:
            turn = self.turns.get(jid)
            for _ in range(100):     # execute 刚开始时运行时可能还没创建
                if not turn or getattr(turn, 'runtime', None): break
                await asyncio.sleep(.05)
            if not turn or not getattr(turn, 'runtime', None):
                raise RuntimeError('回合已结束')
            await turn.runtime.steer(item['text'])
            ok = True
        except Exception as e:
            error = str(e) or type(e).__name__
        self.queue(jid, 'steer', {'message': item['id'], 'ok': ok, 'error': error})

    async def execute(self, job, turn):
        outcome, result, error, runtime = 'failed', '', '', None
        try:
            a = self.store.get('agent', job['agent'])
            if not Path(a['cwd']).is_dir():
                raise ValueError('节点上的工作目录不存在：' + a['cwd'])
            runtime = make_runtime(turn, a)
            turn.runtime = runtime
            result = await runtime.run(job['prompt'])
            outcome = 'completed'
        except asyncio.CancelledError:
            outcome = 'stopped'
            error = self.stops.get(job['id'], '回合被取消') + '；须核验已有副作用'
        except Exception as e:
            # 有些异常没有文字（如超时），至少带上类型名，界面不能出现空白的失败原因。
            error = str(e) or type(e).__name__
        finally:
            # 每轮结束关闭进程，下一轮用原生 session ID 恢复；MCP token 随回合失效。
            if runtime:
                try: await runtime.close()
                except Exception as e: error = error or f'关闭运行时失败：{e}'
            with self.store.atomic():
                self.store.put('execution', {'id': job['id'], 'state': 'stopped', 'agent': job['agent']})
                self.queue(job['id'], 'finished', {'outcome': outcome, 'result': result, 'error': error})
            self.active.pop(job['id'], None); self.turns.pop(job['id'], None); self.stops.pop(job['id'], None)
            self.expires.pop(job['id'], None)
            self.steering = {k for k in self.steering if k[0] != job['id']}

    async def call_tool(self, job, name, arguments):
        if time.monotonic() >= self.expires.get(job['id'], 0):
            raise PermissionError('本机回合租约已失效')
        body = {'op': 'tool', 'job': job['id'], 'call_id': str(uuid.uuid4()), 'name': name, 'arguments': arguments}
        # 连接抖动时用同一 call_id 重试；中心把结果与业务变更同事务落盘，重试不会重复写。
        for attempt in range(8):
            data = self.data_link()
            try:
                if data:
                    return await data.request(body, timeout=TOOL_TIMEOUT)
                return await self.link.request(body)
            except ConnectionError:
                if attempt == 7:
                    raise ConnectionError('工具调用确认未知；请先核验状态，不要盲目重试副作用')
                await asyncio.sleep(1)

    async def apply_update(self, offer):
        """下载、校验、换新代码，然后让 run() 退出，由 CLI 用同一组参数重启进程。"""
        try:
            payload = await (self.data_link() or self.link).request({'op': 'release'}, timeout=UPLOAD_TIMEOUT)
            old = await asyncio.to_thread(install_release, self.app, payload, offer)
            mark_update(self.state_dir.parent, self.code, offer['code'], old)
            print(f"已更新代码 {self.code} → {offer['code']}，正在重启", flush=True)
            self.restart = True
            self.kick.set()
        except Exception as e:
            # 失败时不重启、不改代码；中心记录失败并恢复派发，节点继续用原版本工作。
            print('自动更新失败：' + str(e), flush=True)
            try:
                await self.link.request({'op': 'update_failed', 'error': str(e)})
            except Exception:
                pass
            self.updating = False

    async def serve_file(self, req):
        """读文件放到线程里，避免大文件或慢盘拖住续租心跳。回传失败就算了：网页会超时，可重试。"""
        body = await asyncio.to_thread(read_path, req['path'], req.get('known'), req.get('max', MAX_BYTES), self.protected)
        try:
            await (self.data_link() or self.link).request(file_reply(req, body), timeout=UPLOAD_TIMEOUT)
        except (ConnectionError, RuntimeError):
            pass

    def turn_for_token(self, token):
        for jid, turn in self.turns.items():
            if jid in self.active and secrets.compare_digest(turn.token, token):
                return turn
        return None

    def stop(self, jid, task, reason):
        # 先记原因再取消：execute 的 finally 据此回报，中心与 Master 才知道是谁停的。
        self.stops.setdefault(jid, reason)
        task.cancel()

    async def watchdog(self):
        while not self.closed:
            for jid, task in list(self.active.items()):
                if time.monotonic() >= self.expires.get(jid, 0) and not task.cancelling():
                    self.stop(jid, task, f"与中心失联超过 {self.grace} 秒，本机租约到期")
            await asyncio.sleep(.25)

    async def run(self):
        """同步循环：有新事件立即上传；否则每秒一次心跳续租。连接由 link 负责重建。"""
        monitor = asyncio.create_task(self.watchdog())
        finder = asyncio.create_task(self.refresh_models()) if self.discover_models else None
        uploader = asyncio.create_task(self.upload()) if self.data else None
        try:
            while not self.closed and not self.restart:
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
            extra = [t for t in (finder, uploader) if t]
            monitor.cancel()
            for t in extra: t.cancel()
            await asyncio.gather(monitor, *extra, return_exceptions=True)

    async def close(self):
        self.closed = True
        jobs = list(self.active.values())
        for jid, task in list(self.active.items()):
            if not task.cancelling(): self.stop(jid, task, '节点进程关闭')
        await asyncio.gather(*jobs, return_exceptions=True)
        await self.commands.close()
