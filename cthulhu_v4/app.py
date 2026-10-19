"""中心进程的组装：Center + 内嵌 local 节点 + 星系 WebSocket + 网页。"""
import asyncio
import json
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from .bridge import Bridge
from .center import Center
from .node import Node
from .transport import LocalLink, encode_invite, fetch_fingerprint, make_handler
from .util import LOCAL, MAX_FRAME
from .web import Web

INSTALL_URL = 'https://raw.githubusercontent.com/waaadreamer/cthulhu/v4/deploy/install.sh'


class App:
    def __init__(self, state, *, backend='demo', model=None, reasoning_effort=None, port=8767,
                 ws_host='127.0.0.1', ws_port=8770, public_url=None, galaxy_url=None,
                 tls_cert=None, tls_key=None, install_url=INSTALL_URL, local_capacity=4, **timing):
        self.state = Path(state).resolve()
        self.center = Center(self.state, backend, model, reasoning_effort=reasoning_effort, **timing)
        self.center.configure_master(backend, model, reasoning_effort)
        self.port, self.ws_host, self.ws_port = port, ws_host, ws_port
        self.public_url = public_url.rstrip('/') if public_url else None
        self.galaxy_url = galaxy_url
        self.tls_cert, self.tls_key = tls_cert, tls_key
        self.install_url = install_url
        self.local_capacity = local_capacity
        self.tasks = []

    def center_ws_url(self):
        if self.galaxy_url:
            return self.galaxy_url
        if self.public_url:
            u = urlsplit(self.public_url)
            return ('wss' if u.scheme == 'https' else 'ws') + '://' + u.netloc + u.path + '/galaxy'
        scheme = 'wss' if self.tls_cert else 'ws'
        return f'{scheme}://{self.ws_host}:{self.ws_actual_port}'

    def invite(self, name):
        url = self.center_ws_url()
        pin = None
        if url.startswith('wss:'):
            # 节点以证书指纹校验中心，因此自签证书也不用分发 CA 文件。
            try: pin = fetch_fingerprint(url)
            except OSError as e: raise ValueError(f'无法读取 {url} 的证书指纹：{e}') from e
        info = self.center.create_invite(name, url, pin)
        blob = encode_invite({k: info[k] for k in ('center', 'code', 'node', 'pin')})
        return {'node': name, 'expires': info['expires'], 'blob': blob,
                'command': f'curl -fsSL {self.install_url} | bash -s -- {blob}'}

    async def start(self):
        from websockets.asyncio.server import serve
        loop = asyncio.get_running_loop()
        c = self.center
        # 本机节点：与远端节点同一实现，只是链路在进程内。
        self.node = Node(self.state / 'local-node', capacity=self.local_capacity)
        self.node.link = LocalLink(c.hub, self.node)
        c.hub.attach(LOCAL, self.node.link)
        self.bridge = Bridge(self.node, loop); self.bridge.start()
        tls = None
        if self.tls_cert:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(self.tls_cert, self.tls_key)
        self.ws = await serve(make_handler(c), self.ws_host, self.ws_port, ssl=tls,
                              max_size=MAX_FRAME, origins=None)
        self.ws_actual_port = self.ws.sockets[0].getsockname()[1]
        self.web = Web(self, loop, self.port); self.web.start()
        (self.state / 'center.json').write_text(json.dumps({'port': self.web.port, 'ws_port': self.ws_actual_port}))
        self.tasks = [asyncio.create_task(self.node.run()), asyncio.create_task(self.scheduler())]

    async def scheduler(self):
        c = self.center
        next_tick = c.clock()
        while True:
            if c.clock() >= next_tick:
                c.tick(); next_tick = c.clock() + c.heartbeat
            c.hub.expire()
            await asyncio.sleep(1)

    async def stop(self):
        await self.node.close()
        for t in self.tasks: t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.ws.close(); await self.ws.wait_closed()
        await asyncio.to_thread(self.web.stop)
        await asyncio.to_thread(self.bridge.stop)
        self.node.store.close()
        self.center.store.close()
