"""节点本机的 MCP HTTP 桥：只监听 loopback，只接受本回合 token。

mcp.py（stdio）由原生 CLI 启动，把 tools/list 与 tools/call 转发到这里；
这里再经节点到中心的同一条链路调用工具。节点不提供任何业务管理 API。
"""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .tools import tools_for


class Bridge:
    def __init__(self, node, loop):
        self.node, self.loop = node, loop
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def do_GET(self): self.handle_request()
            def do_POST(self): self.handle_request()

            def handle_request(self):
                try:
                    token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    length = int(self.headers.get('Content-Length', '0'))
                    body = json.loads(self.rfile.read(length)) if length else {}
                    future = asyncio.run_coroutine_threadsafe(bridge.route(self.command, self.path, token, body), bridge.loop)
                    status, value = 200, future.result(timeout=150)
                except PermissionError as e: status, value = 401, {'error': str(e)}
                except Exception as e: status, value = 400, {'error': str(e)}
                raw = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        node.bridge_url = f'http://127.0.0.1:{self.server.server_address[1]}'

    async def route(self, method, path, token, body):
        turn = self.node.turn_for_token(token)
        if not turn:
            raise PermissionError('无效或过期的本机回合 token')
        if method == 'GET' and path == '/internal/tools':
            return {'tools': tools_for(turn.agent, turn.job.get('feishu', False))}
        if method == 'POST' and path == '/internal/call':
            return await self.node.call_tool(turn.job, body['name'], body.get('arguments') or {})
        raise PermissionError('节点只提供本机 MCP 桥')

    def start(self):
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown(); self.server.server_close()
