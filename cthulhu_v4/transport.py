"""中心 ↔ 节点的传输层：本机内存通道与 WebSocket 通道，对上提供同一个 request() 接口。

所有业务通信（同步、工具调用、邀请兑换）都走节点主动建立的一条出站连接；
节点不需要任何入站端口，也不需要共享文件系统。
"""
import asyncio
import inspect
import base64
import hashlib
import json
import ssl
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory, ServerPerMessageDeflateFactory

from .util import LOCAL, MAX_FRAME, PROTOCOL


# ---- 压缩 ----
# websockets 默认的 permessage-deflate 窗口只有 4KB（12 bit），省内存但压缩率差。
# 节点只有几个、出站流量计费，所以两端都用最大 32KB 窗口；旧节点协商时自动退回较小窗口。
DEFLATE = dict(server_max_window_bits=15, client_max_window_bits=15, compress_settings={'memLevel': 8})
DEFLATE_SERVER = lambda: ServerPerMessageDeflateFactory(**DEFLATE)
DEFLATE_CLIENT = lambda: ClientPerMessageDeflateFactory(**DEFLATE)


# ---- 邀请 blob ----
def encode_invite(data):
    return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode().rstrip('=')


def decode_invite(blob):
    blob = blob.strip()
    try:
        return json.loads(base64.urlsafe_b64decode(blob + '=' * (-len(blob) % 4)))
    except Exception as e:
        raise ValueError('邀请内容无法解析，请完整复制网页上的命令') from e


def cert_fingerprint(der):
    return hashlib.sha256(der).hexdigest()


def fetch_fingerprint(url, timeout=5):
    """取中心公网地址实际出示的证书指纹（经反向代理时即代理的证书）。"""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme not in ('https', 'wss'):
        return None
    pem = ssl.get_server_certificate((u.hostname, u.port or 443), timeout=timeout)
    return cert_fingerprint(ssl.PEM_cert_to_DER_cert(pem))


# ---- 本机通道 ----
class LocalLink:
    """中心进程内的 local 节点直接调用 hub，不走网络；与远端节点共享全部协议语义。"""

    def __init__(self, hub, node=None):
        self.hub, self.node = hub, node

    async def request(self, body, timeout=None):
        result = self.hub.request(LOCAL, body)
        return await result if inspect.isawaitable(result) else result

    def wake(self):
        if self.node:
            self.node.kick.set()


# ---- 远端客户端 ----
class _Conn:
    """一条已握手的连接与它的在途请求。"""

    def __init__(self, ws, mux):
        self.ws, self.mux = ws, mux
        self.pending = {}    # 请求 id → Future
        self.reader = None


class RemoteLink:
    """节点侧 WebSocket 客户端，断线自动重建。

    中心支持 MUX 时，一条连接上可以同时有多个请求在途，按 id 对应响应：
    一个大请求（大工具结果、大批事件）不再挡住其后的请求。旧中心退回一问一答的串行模式。
    channel 区分同一节点的控制连接与数据连接（见 Node.run）；旧中心不认识它，只接受单连接。
    """

    def __init__(self, url, name, *, token=None, pin=None, hello=None, log=print, channel=None, timeout=15):
        self.url, self.name, self.token, self.pin = url, name, token, pin
        self.hello = hello or {}
        self.channel, self.timeout = channel, timeout
        self.conn = None
        self.features = set()
        self.opening = asyncio.Lock()
        self.lock = asyncio.Lock()   # 只用于串行模式
        self.next_id = 0
        self.log = log
        self.announced = False

    def ssl_context(self):
        if not self.url.startswith('wss:'):
            return None
        if not self.pin:
            return ssl.create_default_context()
        # 自签证书：不依赖 CA，改为连接后逐字节核对证书指纹（pinning）。
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def check_pin(self, ws):
        if not self.pin:
            return
        obj = ws.transport.get_extra_info('ssl_object')
        der = obj and obj.getpeercert(binary_form=True)
        if not der or cert_fingerprint(der) != self.pin.lower():
            raise PermissionError('中心证书指纹不匹配，拒绝发送凭证；若中心更换了证书，请在网页重新生成命令')

    async def open(self, first):
        opts = {'ssl': self.ssl_context()} if self.url.startswith('wss:') else {}
        ws = await connect(self.url, max_size=MAX_FRAME, open_timeout=10, close_timeout=2,
                           proxy=None, compression=None, extensions=[DEFLATE_CLIENT()], **opts)
        try:
            self.check_pin(ws)
            await ws.send(json.dumps(first))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except BaseException:
            await ws.close(); raise
        if 'error' in reply:
            await ws.close()
            raise PermissionError(reply['error'])
        return ws, reply

    async def redeem(self, invite_code):
        """用一次性邀请码换取长期节点凭证；同一 instance 重试拿到同一凭证。"""
        ws, reply = await self.open({'protocol': PROTOCOL, 'invite': invite_code, 'node': self.name} | self.hello)
        await ws.close()
        return reply['token']

    async def connected(self):
        async with self.opening:
            if self.conn is None:
                first = {'protocol': PROTOCOL, 'node': self.name, 'token': self.token} | self.hello
                if self.channel:
                    first['channel'] = self.channel
                try:
                    ws, reply = await self.open(first)
                except (OSError, asyncio.TimeoutError, InvalidHandshake, InvalidURI, ConnectionClosed) as e:
                    raise ConnectionError(f'无法连接中心 {self.url}：{e}') from e
                self.features = set(reply.get('features') or ())
                conn = self.conn = _Conn(ws, MUX in self.features)
                if conn.mux:
                    conn.reader = asyncio.create_task(self.read(conn))
                if not self.announced:
                    self.log(f'已连接中心：{self.url} · 节点 {self.name}' + (f' · {self.channel}' if self.channel else ''), flush=True)
                    self.announced = True
            return self.conn

    async def read(self, conn):
        """多路复用：按请求 id 把响应交给对应的等待者。连接断开时，本连接上所有在途请求都以连接中断结束。"""
        try:
            async for raw in conn.ws:
                msg = json.loads(raw)
                fut = conn.pending.pop(msg.get('id'), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        except (ConnectionClosed, json.JSONDecodeError):
            pass
        finally:
            self.drop(conn)

    def drop(self, conn):
        # 只清理这一条连接：重连后的新连接有自己的 pending，不能被旧连接的收尾波及。
        if self.conn is conn:
            self.conn = None
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError('中心连接中断'))
        conn.pending.clear()

    async def request(self, body, timeout=None):
        conn = await self.connected()
        timeout = timeout or self.timeout
        if not conn.mux:
            return await self.serial(conn, body, timeout)
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        conn.pending[rid] = fut
        try:
            async with asyncio.timeout(timeout):
                await conn.ws.send(json.dumps(body | {'id': rid}, ensure_ascii=False))
                reply = await fut
        except asyncio.TimeoutError as e:
            # 有请求 id，迟到的响应会被丢弃、不会被别的请求误收，所以超时不必断开连接；
            # 连接真断了由 websockets 的 ping/pong 发现并关闭。调用方按自己的规则重试。
            raise ConnectionError(f'中心 {timeout} 秒内未响应') from e
        except (OSError, ConnectionClosed) as e:
            await conn.ws.close(); self.drop(conn)
            raise ConnectionError(f'中心连接中断：{e or type(e).__name__}') from e
        finally:
            conn.pending.pop(rid, None)
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply['result']

    async def serial(self, conn, body, timeout):
        """旧中心不支持多路复用：一条连接上一问一答，串行发送。"""
        async with self.lock:
            try:
                async with asyncio.timeout(timeout):
                    await conn.ws.send(json.dumps(body, ensure_ascii=False))
                    reply = json.loads(await conn.ws.recv())
            except (asyncio.TimeoutError, OSError, ConnectionClosed) as e:
                # 读超时后不能复用这条连接，否则迟到响应会被下一个请求误收。
                await conn.ws.close(); self.drop(conn)
                raise ConnectionError(f'中心连接中断：{e or type(e).__name__}') from e
            if 'error' in reply:
                raise RuntimeError(reply['error'])
            return reply['result']

    async def close(self):
        conn = self.conn
        if conn:
            await conn.ws.close(); self.drop(conn)
            if conn.reader:
                await asyncio.gather(conn.reader, return_exceptions=True)


# ---- 中心服务端 ----
class _ServerLink:
    """中心视角的远端连接。远端节点每秒同步一次，无需服务端推送唤醒。"""

    def wake(self):
        pass


# 中心能力：同一连接上多个请求带 id 并发处理；同一节点可另开一条 data 连接。旧节点不发 id，按原样一问一答。
MUX = 'mux'
CHANNELS = (None, 'data')
# 同一条连接上同时处理的请求上限。中心只有两核，请求本身是同步的短事务，并发主要省的是等待网络发送的时间；
# 再多只会让大响应同时占内存。超出的请求排队，不拒绝。
MAX_INFLIGHT = 8


def make_handler(center):
    hub = center.hub

    async def answer(ws, node, msg, gate):
        async with gate:
            try:
                result = hub.request(node, msg)
                response = {'result': await result if inspect.isawaitable(result) else result}
            except Exception as e:
                response = {'error': str(e)}
            if 'id' in msg:
                response['id'] = msg['id']
            try:
                await ws.send(json.dumps(response, ensure_ascii=False))
            except ConnectionClosed:
                pass   # 节点已断开：它会用同一 call_id 重试，中心的去重保证不重复执行

    async def handle(ws):
        node, link, channel, inflight = None, _ServerLink(), None, set()
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            try:
                if hello.get('invite'):
                    with center.store.atomic():
                        token = center.redeem_invite(hello)
                    await ws.send(json.dumps({'ok': True, 'token': token}))
                    return
                channel = hello.get('channel')
                if channel not in CHANNELS:
                    raise ValueError(f'未知连接通道 {channel}')
                node = hub.check_hello(hello)
                hub.attach(node, link, channel)
            except (PermissionError, ValueError) as e:
                node = None
                await ws.send(json.dumps({'error': str(e)}, ensure_ascii=False))
                return
            await ws.send(json.dumps({'ok': True, 'protocol': PROTOCOL, 'features': [MUX]}))
            if channel is None:
                center.store.emit('node_online', f'节点 {node} 已连接', data={'node': node, 'at': time.time()})
            gate = asyncio.Semaphore(MAX_INFLIGHT)
            async for raw in ws:
                msg = json.loads(raw)
                if 'id' not in msg:
                    await answer(ws, node, msg, gate)   # 旧节点：严格一问一答
                    continue
                # 每个请求独立处理、各自回复：慢请求或大响应不挡后面的同步。
                # hub.request 内部是同步事务，不跨 await，所以并发处理不会交错写库。
                task = asyncio.create_task(answer(ws, node, msg, gate))
                inflight.add(task); task.add_done_callback(inflight.discard)
        except (ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        finally:
            # 在途请求不取消，让它们处理完：飞书等工具在等外部子进程，中途取消会让副作用变成未知；
            # 回复发不出去也没关系，节点会用同一 call_id 重试，拿到去重后的原结果。
            if node:
                hub.detach(node, link, channel)
                if channel is None:
                    center.store.emit('node_offline', f'节点 {node} 已断开', data={'node': node})

    return handle
