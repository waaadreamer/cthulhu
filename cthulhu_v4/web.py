"""零依赖 HTTP 外壳：浏览器 API、静态页面和节点安装脚本。业务只在 asyncio 主线程执行。

仅绑定 loopback，可由反向代理暴露。浏览器 token 与节点凭证相互独立；
拒绝未声明的跨站 Origin，避免本地服务被任意网页调用。单用户，不做多租户鉴权。
"""
import asyncio
import gzip
import hashlib
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .canvas import BOARD_CSP
from .tools import HTML_TEXT_LIMIT
from .util import ROOT, cut

UI_TEXT, UI_FIELD = 20000, 8000
SNAPSHOTS = 64  # 最近的快照按 etag 保留，用来给各标签页算增量；更旧的 etag 直接回全量
_MISSING = object()


def snapshot_delta(old, new):
    """两份快照的差异。带 id 的对象列表按 id 只发变化的条目，字典按键，其余整体替换。

    为什么：网页每秒轮询，原先任何变化都重发约 150KB 的全量快照，是中心出站流量的大头。
    客户端用 static/js/delta.js 的 applyDelta 还原，测试保证两边结果一致。
    """
    out = {}
    for k, v in new.items():
        o = old.get(k, _MISSING)
        if o == v:
            continue
        if isinstance(v, list) and isinstance(o, list) and all(isinstance(x, dict) and 'id' in x for x in v + o):
            before = {x['id']: x for x in o}
            d = {'set': [x for x in v if before.get(x['id']) != x]}
            ids = [x['id'] for x in v]
            if ids != [x['id'] for x in o]:
                d['ids'] = ids  # 只在增删或顺序变化时才发 id 列表
            out[k] = {'list': d}
        elif isinstance(v, dict) and isinstance(o, dict):
            out[k] = {'dict': {'set': {kk: vv for kk, vv in v.items() if o.get(kk, _MISSING) != vv},
                               'del': [kk for kk in o if kk not in v]}}
        else:
            out[k] = {'value': v}
    for k in old:
        if k not in new:
            out[k] = {'drop': True}
    return out

def ui_event(e):
    """单个字段过长时截断（注明原长度）：一条大工具输出不能让一页事件变成几 MB。完整内容仍在库里。
    HTML 画布例外：截断后的 HTML 会渲染成半张图甚至跑不起来，按它自己的上限整段发给浏览器。"""
    html = e['kind'] == 'chat' and (e['data'] or {}).get('format') == 'html'
    return e | {'text': cut(e['text'], HTML_TEXT_LIMIT if html else UI_TEXT), 'data': cut(e['data'], UI_FIELD, 200)}


STATIC = ROOT / 'static'
MIME = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
        '.sh': 'text/x-shellscript', '.woff2': 'font/woff2'}
TEXT_MIME = ('text/', 'image/svg')
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
# HTML 画布宿主页（static/canvas.html）只在 sandbox="allow-scripts" 的 iframe 里运行，来源不透明，碰不到凭证。
# 为了能画图：允许内联脚本/样式与 https 上的图表库、图片、字体；禁止一切网络请求，表单也不能提交；只许本站嵌入。
CANVAS_CSP = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https:; style-src 'unsafe-inline' https:; "
              "img-src data: blob: https:; font-src data: https:; media-src data: blob:; connect-src 'none'; "
              "form-action 'none'; base-uri 'none'; frame-ancestors 'self'")


class Web:
    def __init__(self, app, loop, port, host='127.0.0.1'):
        self.app, self.loop = app, loop
        token_file = app.center.state_dir / 'web-token'
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32)); token_file.chmod(0o600)
        self.token = token_file.read_text().strip()
        self.snapshots = {}  # etag → 快照；只在事件循环线程读写
        web = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()

            def handle_request(self):
                url = urlsplit(self.path)
                # 静态文件先于来源检查：它们本来就公开（不含凭证），而浏览器加载 <script type=module>
                # 会带 Origin 头。用白名单外的地址（如域名而非 IP）打开时，若这里拒掉 app.js，
                # 页面只剩静态 HTML：登录框不出现、按钮都没绑定，看起来就是“什么都点不动”。
                if self.command == 'GET' and not url.path.startswith('/api/'):
                    return self.static(url.path)
                origin = self.headers.get('Origin')
                if origin and origin not in web.allowed_origins:
                    return self.respond(403, {'error': f'跨站请求被拒绝：{origin} 不在允许的来源里（中心启动参数 --allowed-origin）'})
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
                # 字体是二进制，不能带 charset；也不再 gzip（woff2 本身已压缩）。
                self.respond(200, target.read_bytes(), mime + '; charset=utf-8' if mime.startswith(TEXT_MIME) else mime,
                             csp=BOARD_CSP if target.name == 'board-frame.html' else CANVAS_CSP if target.name == 'canvas.html' else CSP)

            def respond(self, status, value, content_type='application/json; charset=utf-8', csp=CSP):
                raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                # 自己压缩文本响应：反向代理默认只压缩 HTML，JSON 原样外发，而出站流量计费。
                gz = len(raw) > 1024 and 'gzip' in self.headers.get('Accept-Encoding', '') and content_type.startswith(('application/json', 'text/'))
                if gz:
                    raw = gzip.compress(raw, 6)
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                if gz:
                    self.send_header('Content-Encoding', 'gzip')
                self.send_header('Vary', 'Accept-Encoding')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', csp)
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
        # 同一个反向代理常有多个入口（IP 与域名）；每个都要显式列出，仍不从请求头推断。
        self.allowed_origins.update(o.rstrip('/') for o in app.allowed_origins)
        self.url = f'http://127.0.0.1:{self.port}'

    async def route(self, method, path, query, token, body):
        if not secrets.compare_digest(token, self.token):
            raise PermissionError('请填写访问凭证')
        c, app = self.app.center, self.app
        q = lambda k, d=None: query.get(k, [d])[0]
        if path == '/api/canvas' or path.startswith('/api/canvas/'):
            return await c.canvases.route(method, path, query, body)
        if method == 'GET':
            if path == '/api/state':
                # 状态没变就不重发：客户端带上次的 etag，只回 now（倒计时用）。now 不参与 etag。
                # 变了且还记得客户端那一版：只回增量；中心重启或版本太旧时才回全量。
                snap = c.snapshot()
                now = snap.pop('now')
                etag = hashlib.sha1(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
                base = q('etag')
                if base == etag:
                    return {'unchanged': True, 'etag': etag, 'now': now}
                self.snapshots.pop(etag, None); self.snapshots[etag] = snap
                while len(self.snapshots) > SNAPSHOTS:
                    self.snapshots.pop(next(iter(self.snapshots)))
                old = self.snapshots.get(base) if base else None
                if old is not None:
                    return {'delta': snapshot_delta(old, snap), 'base': base, 'etag': etag, 'now': now}
                return snap | {'etag': etag, 'now': now}
            if path == '/api/events':
                # 两种读法：before 向前翻历史（不含流式增量）；after 跟随最新（含增量以便流式显示）。
                # highwater 与查询在同一个读快照里取：客户端以它作为跟随游标，被过滤掉的事件不会重复拉取。
                # 查询放到线程里：扫大表时不能占住事件循环，否则节点心跳停摆、租约过期（见 Store.events_snapshot）。
                limit = int(q('limit', 200))
                before = q('before')
                events, top = await asyncio.to_thread(
                    c.store.events_snapshot, after=int(q('after', 0)), limit=limit, agent=q('agent'),
                    conversation=q('view') == 'conversation', ui=True, harness=q('harness') == '1',
                    before=None if before is None else int(before), compact=before is not None)
                events = [ui_event(e) for e in events]
                return {'events': events, 'more': len(events) >= limit, 'highwater': top}
            if path == '/api/executions':
                if q('id'):
                    return c.executions.get(q('id'))
                return c.executions.list(q('node_id'), q('limit', 20))
            if path == '/api/file':
                return await c.files.read(q('ref'), refresh=q('refresh') == '1')
            if path == '/api/history':
                limit = int(q('limit', 50))
                events = await asyncio.to_thread(c.store.history, q('q', ''), q('before'), limit, q('aside') == '1')
                events = [ui_event(e) for e in events]
                return {'events': events, 'more': len(events) >= limit}
            if path == '/api/monitor':
                return c.monitor.panels()
            if path == '/api/feishu/acl':
                return c.feishu.acl_list() if c.feishu else []
            if path == '/api/deploy':
                c.deployer.available = await asyncio.to_thread(c.deployer.check)
                return c.deployer.status()
            raise ValueError('未知接口')
        if path == '/api/executions':
            return c.executions.create(**body)
        if path == '/api/executions/cancel':
            return c.executions.cancel(body['id'])
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
