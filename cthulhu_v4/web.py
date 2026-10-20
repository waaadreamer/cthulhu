"""零依赖 HTTP 外壳：浏览器 API、静态页面和节点安装脚本。业务只在 asyncio 主线程执行。

仅绑定 loopback，可由反向代理暴露。浏览器 token 与节点凭证相互独立；
拒绝未声明的跨站 Origin，避免本地服务被任意网页调用。单用户，不做多租户鉴权。
"""
import asyncio
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .util import ROOT

STATIC = ROOT / 'static'
MIME = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
        '.sh': 'text/x-shellscript'}


class Web:
    def __init__(self, app, loop, port, host='127.0.0.1'):
        self.app, self.loop = app, loop
        token_file = app.center.state_dir / 'web-token'
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32)); token_file.chmod(0o600)
        self.token = token_file.read_text().strip()
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()

            def handle_request(self):
                url = urlsplit(self.path)
                origin = self.headers.get('Origin')
                if origin and origin not in web.allowed_origins:
                    return self.respond(403, {'error': '跨站请求被拒绝'})
                if self.command == 'GET' and not url.path.startswith('/api/'):
                    return self.static(url.path)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length > 1024 * 1024: raise ValueError('请求体过大')
                    body = json.loads(self.rfile.read(length)) if length else {}
                    token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    future = asyncio.run_coroutine_threadsafe(
                        web.route(self.command, url.path, parse_qs(url.query), token, body), web.loop)
                    self.respond(200, future.result(timeout=30))
                except PermissionError as e: self.respond(401, {'error': str(e)})
                except Exception as e: self.respond(400, {'error': str(e)})

            def static(self, path):
                if path == '/install.sh':
                    target = ROOT / 'deploy' / 'install.sh'
                else:
                    target = (STATIC / (path.lstrip('/') or 'index.html')).resolve()
                    if not target.is_relative_to(STATIC) or not target.is_file():
                        target = STATIC / 'index.html'
                mime = MIME.get(target.suffix, 'application/octet-stream')
                self.respond(200, target.read_bytes(), mime + '; charset=utf-8')

            def respond(self, status, value, content_type='application/json; charset=utf-8'):
                raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.port = self.server.server_address[1]
        # 只信任显式配置的公网来源，不根据客户端 Host/Forwarded 头扩大白名单。
        self.allowed_origins = {f'http://127.0.0.1:{self.port}', f'http://localhost:{self.port}'}
        if app.public_url:
            u = urlsplit(app.public_url)
            self.allowed_origins.add(f'{u.scheme}://{u.netloc}')
        self.url = f'http://127.0.0.1:{self.port}'

    async def route(self, method, path, query, token, body):
        if not secrets.compare_digest(token, self.token):
            raise PermissionError('请填写访问凭证')
        c, app = self.app.center, self.app
        q = lambda k, d=None: query.get(k, [d])[0]
        if method == 'GET':
            if path == '/api/state':
                return c.snapshot()
            if path == '/api/events':
                # 两种读法：before 向前翻历史（不含流式增量）；after 跟随最新（含增量以便流式显示）。
                # highwater 与查询在同一线程读取：客户端以它作为跟随游标，被过滤掉的事件不会重复拉取。
                limit = int(q('limit', 200))
                before = q('before')
                events = c.store.events(after=int(q('after', 0)), limit=limit, agent=q('agent'),
                                        conversation=q('view') == 'conversation', ui=True,
                                        before=None if before is None else int(before), compact=before is not None)
                return {'events': events, 'more': len(events) >= limit, 'highwater': c.store.highwater()}
            if path == '/api/file':
                return await c.files.read(q('ref'), refresh=q('refresh') == '1')
            if path == '/api/deploy':
                c.deployer.available = await asyncio.to_thread(c.deployer.check)
                return c.deployer.status()
            raise ValueError('未知接口')
        if path == '/api/deploy':
            # 部署会 fetch、跑测试并最终重启中心：只能由主人在网页触发，agent 没有对应工具。
            return await c.deployer.start()
        with c.store.atomic():
            if path == '/api/chat':
                return c.enqueue('owner', body.get('to', 'master'), body['text'], body.get('priority', 9))
            if path == '/api/task':
                return c.create_task(body, initiator='owner')
            if path == '/api/answer':
                return c.answer('owner', body)
            if path == '/api/question/delegate':
                return c.delegate(body)
            if path == '/api/settings':
                return c.set_master_decides(body.get('master_decides'))
            if path == '/api/memory':
                return c.memory('owner', body)
            if path == '/api/task/close':
                return c.close_task(body, actor='owner')
            if path == '/api/task/reopen':
                return c.reopen_task(body, actor='owner')
            if path == '/api/task/archive':
                return c.archive_task(body)
            if path == '/api/control':
                return c.control(body, actor='owner')
            if path == '/api/master':
                return c.control_master(body['action'])
            if path == '/api/node/update':
                return c.retry_update(body['node'])
            if path == '/api/node':
                return c.set_node_defaults(body)
            if path == '/api/agent':
                return c.set_agent_config(body)
            if path == '/api/invite':
                return app.invite(body.get('name', ''))
        if path == '/api/demo/escalate':
            if c.store.get('agent', 'master')['backend'] != 'demo':
                raise ValueError('仅模拟模式可用')
            return c.tool('master', 'ask_owner', body)
        raise ValueError('未知接口')

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close()
