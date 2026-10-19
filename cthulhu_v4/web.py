"""零依赖 HTTP 外壳。业务/SQLite 只在 asyncio 主线程运行。

仅绑定 loopback；浏览器与 MCP 使用不同 token。拒绝跨站 Origin，避免本地
工具服务被任意网页调用。它是单用户试用界面，不承担公网托管与多租户鉴权。
"""
import asyncio
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .core import ROOT, process_alive
from .tools import tools_for


class Web:
    def __init__(self, core, loop, port, public_origins=()):
        self.core, self.loop = core, loop
        token_file = core.state_dir / 'web-token'
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32)); token_file.chmod(0o600)
        self.token = token_file.read_text().strip()
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()

            def handle_request(self):
                path = urlsplit(self.path).path
                if self.headers.get('Origin') and self.headers['Origin'] not in web.allowed_origins:
                    return self.respond(403, {'error': '跨站请求被拒绝'})
                if self.command == 'GET' and path in {'/', '/app.js', '/transcript.js', '/style.css'}:
                    name = {'/': 'index.html', '/app.js': 'app.js', '/transcript.js': 'transcript.js', '/style.css': 'style.css'}[path]
                    mime = {'/': 'text/html', '/app.js': 'text/javascript', '/transcript.js': 'text/javascript', '/style.css': 'text/css'}[path]
                    return self.respond(200, (ROOT / 'static' / name).read_bytes(), mime + '; charset=utf-8')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if length > 1024 * 1024: raise ValueError('请求体过大')
                    body = json.loads(self.rfile.read(length)) if length else {}
                    future = asyncio.run_coroutine_threadsafe(web.route(
                        self.command, path, parse_qs(urlsplit(self.path).query),
                        self.headers.get('Authorization', '').removeprefix('Bearer '), body), web.loop)
                    result = future.result(timeout=30)
                    self.respond(200, result)
                except PermissionError as e: self.respond(401, {'error': str(e)})
                except Exception as e: self.respond(400, {'error': str(e)})

            def respond(self, status, value, content_type='application/json; charset=utf-8'):
                raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.server.server_address[1]
        # 只信任显式配置的公网来源，不根据客户端 Host/Forwarded 头扩大白名单。
        self.allowed_origins = {f'http://127.0.0.1:{self.port}', f'http://localhost:{self.port}', *public_origins}
        core.url = f'http://127.0.0.1:{self.port}'

    async def route(self, method, path, query, token, body):
        c = self.core
        if path.startswith('/internal/'):
            actor = next((a['id'] for a in c.store.all('agent') if secrets.compare_digest(a['token'], token)), None)
            if not actor: raise PermissionError('无效 agent token')
            if method == 'GET' and path == '/internal/tools': return {'tools': tools_for(actor)}
            if method == 'POST' and path == '/internal/call':
                # 暂停后拒绝迟到的 MCP 写操作，避免旧进程继续改变任务状态。
                if c.store.get('agent', actor)['paused']: raise ValueError('agent 已暂停')
                return c.tool(actor, body['name'], body.get('arguments', {}))
            raise ValueError('未知内部接口')
        if not secrets.compare_digest(token, self.token): raise PermissionError('请填写本地访问 token')
        if method == 'GET' and path == '/api/state': return c.snapshot()
        if method == 'GET' and path == '/api/events':
            return {'events': c.store.events(after=int(query.get('after', [0])[0]),
                    limit=int(query.get('limit', [200])[0]),
                    agent=query.get('agent', [None])[0], visible=query.get('visible', ['0'])[0] == '1',
                    conversation=query.get('view', [''])[0] == 'conversation')}
        if method != 'POST': raise ValueError('未知接口')
        if path == '/api/chat':
            with c.store.atomic():
                return c.enqueue('owner', body.get('to', 'master'), body['text'], body.get('priority', 9))
        if path == '/api/task':
            with c.store.atomic(): return c.create_task(body, initiator='owner')
        if path == '/api/answer':
            with c.store.atomic(): return c.answer('owner', body)
        if path == '/api/control':
            with c.store.atomic():
                c.store.emit('owner_control', body['action'], actor='owner', task=body['task_id'], visible=True)
                return c.control(body)
        if path == '/api/master':
            a = c.store.get('agent', 'master')
            if body['action'] == 'pause':
                a['paused'] = True
                if 'master' in c.running: c.running['master'].cancel()
            elif body['action'] == 'resume':
                if 'master' in c.running: raise ValueError('原回合正在结束，请稍后恢复')
                if process_alive(a) and 'master' not in c.runtimes:
                    raise ValueError(f"旧运行时 PID {a['pid']} 仍存活，请先核验并结束它")
                a.update(paused=False, failures=0)
                with c.store.atomic():
                    c.enqueue('owner', 'master', '恢复工作。用 read_messages 核对 failed/unknown 的原始请求，核验是否已有副作用，再接续目标；同时检查 list_questions。', 9)
            else: raise ValueError('未知 action')
            c.store.put('agent', a)
            c.store.emit('master_control', body['action'], actor='owner', visible=True)
            return {'ok': True}
        if path == '/api/demo/escalate':
            if c.store.get('agent', 'master')['backend'] != 'demo': raise ValueError('仅模拟模式可用')
            c.store.emit('demo_control', '用户触发模拟 master 请求主人回答', actor='owner', visible=True)
            return c.tool('master', 'ask_owner', body)
        raise ValueError('未知接口')
