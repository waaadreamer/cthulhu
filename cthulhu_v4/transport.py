"""中心 ↔ 节点的传输层：本机内存通道与 WebSocket 通道，对上提供同一个 request() 接口。

所有业务通信（同步、工具调用、邀请兑换）都走节点主动建立的一条出站连接；
节点不需要任何入站端口，也不需要共享文件系统。
"""
import asyncio
import base64
import hashlib
import json
import ssl
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from .util import LOCAL, MAX_FRAME, PROTOCOL


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

    async def request(self, body):
        return self.hub.request(LOCAL, body)

    def wake(self):
        if self.node:
            self.node.kick.set()


# ---- 远端客户端 ----
class RemoteLink:
    """节点侧 WebSocket 客户端。一条连接上串行 request/response，断线自动重建。"""

    def __init__(self, url, name, *, token=None, pin=None, hello=None, log=print):
        self.url, self.name, self.token, self.pin = url, name, token, pin
        self.hello = hello or {}
        self.ws = None
        self.lock = asyncio.Lock()
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
                           proxy=None, **opts)
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
        if self.ws is None:
            try:
                self.ws, _ = await self.open({'protocol': PROTOCOL, 'node': self.name, 'token': self.token} | self.hello)
            except (OSError, asyncio.TimeoutError, InvalidHandshake, InvalidURI, ConnectionClosed) as e:
                raise ConnectionError(f'无法连接中心 {self.url}：{e}') from e
            if not self.announced:
                self.log(f'已连接中心：{self.url} · 节点 {self.name}', flush=True)
                self.announced = True
        return self.ws

    async def request(self, body):
        async with self.lock:
            ws = await self.connected()
            try:
                async with asyncio.timeout(15):
                    await ws.send(json.dumps(body, ensure_ascii=False))
                    reply = json.loads(await ws.recv())
            except (asyncio.TimeoutError, OSError, ConnectionClosed) as e:
                # 读超时后不能复用这条连接，否则迟到响应会被下一个请求误收。
                self.ws = None
                await ws.close()
                raise ConnectionError(f'中心连接中断：{e or type(e).__name__}') from e
            if 'error' in reply:
                raise RuntimeError(reply['error'])
            return reply['result']

    async def close(self):
        if self.ws:
            await self.ws.close(); self.ws = None


# ---- 中心服务端 ----
class _ServerLink:
    """中心视角的远端连接。远端节点每秒同步一次，无需服务端推送唤醒。"""

    def wake(self):
        pass


def make_handler(center):
    hub = center.hub

    async def handle(ws):
        node, link = None, _ServerLink()
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            try:
                if hello.get('invite'):
                    with center.store.atomic():
                        token = center.redeem_invite(hello)
                    await ws.send(json.dumps({'ok': True, 'token': token}))
                    return
                node = hub.check_hello(hello)
                hub.attach(node, link)
            except (PermissionError, ValueError) as e:
                node = None
                await ws.send(json.dumps({'error': str(e)}, ensure_ascii=False))
                return
            await ws.send(json.dumps({'ok': True, 'protocol': PROTOCOL}))
            center.store.emit('node_online', f'节点 {node} 已连接', data={'node': node, 'at': time.time()})
            async for raw in ws:
                try:
                    response = {'result': hub.request(node, json.loads(raw))}
                except Exception as e:
                    response = {'error': str(e)}
                await ws.send(json.dumps(response, ensure_ascii=False))
        except (ConnectionClosed, asyncio.TimeoutError, json.JSONDecodeError):
            pass
        finally:
            if node:
                hub.detach(node, link)
                center.store.emit('node_offline', f'节点 {node} 已断开', data={'node': node})

    return handle
