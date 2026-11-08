"""独立 local Canvas 服务：无本地业务库，通过中心 API 采集/展示。

所有进程执行仍由中心 executions 派发至节点，服务既不 SSH 也不创建子进程。
"""
import argparse
import asyncio
import json
import secrets
import signal
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .canvas import BOARD_CSP
from .util import ROOT
from .web import CSP, MIME


class CanvasServer:
    def __init__(self, center_url, token, *, port=8780, tick_interval=1):
        parsed = urlsplit(center_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Canvas 服务必须通过 local HTTP 地址连接中心，如 http://127.0.0.1:8767')
        if not token or not 0.1 <= tick_interval <= 5:
            raise ValueError('需要中心凭证，tick 间隔必须在 0.1–5 秒之间')
        self.center_url, self.token = center_url.rstrip('/'), token
        self.tick_interval, self.worker = tick_interval, 'canvas-' + secrets.token_hex(12)
        self.last_tick, self.error = None, None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()

            def respond(self, status, value, mime='application/json; charset=utf-8', csp=CSP):
                raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', csp)
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass

            def handle_request(self):
                path = urlsplit(self.path).path
                if self.command == 'GET' and not path.startswith('/api/'):
                    target = (ROOT / 'static' / (path.lstrip('/') or 'boards.html')).resolve()
                    if not target.is_relative_to(ROOT / 'static') or not target.is_file():
                        return self.respond(404, {'error': '文件不存在'})
                    mime = MIME.get(target.suffix, 'application/octet-stream')
                    return self.respond(200, target.read_bytes(), mime, BOARD_CSP if target.name == 'board-frame.html' else CSP)
                if self.headers.get('Origin') and self.headers['Origin'] not in owner.origins:
                    return self.respond(403, {'error': '跨站请求被拒绝'})
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                if not secrets.compare_digest(token, owner.token):
                    return self.respond(401, {'error': '请填写中心访问凭证'})
                # 代理白名单不能泛化为任意中心 API：模板或服务调用者都不能借它绕到执行/凭证接口。
                if not (path == '/api/canvas' or path.startswith('/api/canvas/')):
                    return self.respond(404, {'error': '未知接口'})
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 <= length <= 1024 * 1024:
                        raise ValueError('请求体过大')
                    body = json.loads(self.rfile.read(length)) if length else None
                    result = owner.request(self.command, path, body)
                    self.respond(200, result)
                except urllib.error.HTTPError as e:
                    self.respond(e.code, json.load(e))
                except Exception as e:
                    self.respond(400, {'error': str(e)})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.port = self.server.server_address[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.origins = {self.url, f'http://localhost:{self.port}'}

    def request(self, method, path, body=None):
        req = urllib.request.Request(self.center_url + path, method=method,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    async def tick_once(self):
        # HTTP 响应丢失时只重发中心 tick，不重造命令。去重依据是中心已提交的 inflight 票据，
        # 因此不论重启还是多实例，都无需本地锁/租约/第二份持久状态。
        result = await asyncio.to_thread(self.request, 'POST', '/api/canvas/tick', {'worker': self.worker})
        self.last_tick, self.error = result['now'], None
        return result

    async def run(self):
        while True:
            try:
                await self.tick_once()
            except (OSError, ValueError) as e:
                error = str(e)
                if error != self.error:
                    print(f'Canvas 中心暂不可用：{error}', flush=True)
                self.error = error
            await asyncio.sleep(self.tick_interval)

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


async def serve(args):
    token = Path(args.token_file).read_text().strip()
    server = CanvasServer(args.center, token, port=args.port, tick_interval=args.tick_interval)
    server.start()
    runner = asyncio.create_task(server.run())
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    print(f'Canvas 服务已启动：{server.url}；中心：{server.center_url}；业务状态仅保存在中心', flush=True)
    try:
        await stop.wait()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await asyncio.to_thread(server.stop)


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立 local Canvas 服务（不调用模型）')
    parser.add_argument('--center', default='http://127.0.0.1:8767')
    parser.add_argument('--token-file', required=True, help='中心 web-token 文件；不会注入模板或页面')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--tick-interval', type=float, default=1)
    asyncio.run(serve(parser.parse_args(argv)))


if __name__ == '__main__':
    main()
