"""手动：真实 agy + 回环 HTTP 400 夹具，验证失败和原生恢复，不调用外部模型。

不属于默认 unittest；成功只证明真实 CLI 协议，不证明任何模型推理能力。
运行 python3 tests/native_agy_protocol.py（需要 agy 1.5.3+）。
"""
import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from helpers import Harness


async def main():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers['Content-Length'])).decode()))
            self.send_response(400); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"error":{"code":400,"message":"fixture protocol unsupported","status":"INVALID_ARGUMENT"}}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='cthulhu-agy-native-') as tmp:
            h = Harness(Path(tmp), backend='antigravity', model='gemini-4-argon-medium')
            h.node.backends.append('antigravity')
            mcp_requests = []
            route = h.bridge.route
            async def observed_route(method, path, token, body):
                value = await route(method, path, token, body)
                mcp_requests.append(path)
                return value
            h.bridge.route = observed_route
            empty = Path(tmp) / 'empty'; empty.mkdir()
            a = h.c.store.get('agent', 'master'); a['cwd'] = str(empty); h.c.store.put('agent', a)
            try:
                # 只向回环夹具送非秘密测试 key，不读取或修改本机用户认证，不访问真实网关。
                with patch('cthulhu_v4.agy_runtime.claude_credentials', return_value=('protocol-fixture-key', f'http://127.0.0.1:{server.server_port}')):
                    for i, text in enumerate(('Remember protocol_silverfish. Reply OK without tools.', 'Recall the marker in the previous user turn. No tools.')):
                        a = h.c.store.get('agent', 'master'); a['paused'] = False; h.c.store.put('agent', a)
                        h.c.enqueue('owner', 'master', text)
                        await h.settle(seconds=45)
                        a = h.c.store.get('agent', 'master')
                        assert a['session'], 'native pointer missing'
                        if i == 0: first = a['session']
                        else: assert a['session'] == first, 'session changed on load'
                events = h.c.store.events(limit=1000)
                errors = [e['text'] for e in events if e['kind'] == 'runtime_error']
                assert len(errors) == 2 and all('fixture protocol unsupported' in e for e in errors), errors
                turns = [body for url, body in requests if '/models/gemini-4-argon:streamGenerateContent' in url]
                assert len(turns) == 2, 'unexpected replay or retries'
                assert json.loads(turns[1])['generationConfig']['thinkingConfig']['thinkingLevel'] == 'MEDIUM', 'selected medium effort not applied'
                assert 'protocol_silverfish' in turns[1], 'native load lost previous user input'
                assert '你的 agent ID：master' in json.dumps(json.loads(turns[1]), ensure_ascii=False), 'native load lost harness system instructions'
                assert '/internal/tools' in mcp_requests, 'native MCP discovery did not reach the harness bridge'
                assert not list(empty.iterdir()), 'empty smoke workspace changed'
                print(json.dumps({'result': 'PASS', 'cli': 'agy ACP', 'session': first,
                    'checks': ['native MCP discovery reaches per-turn bridge', 'native new/load retains user input and harness system instructions', 'HTTP 400 despite end_turn becomes failure',
                               'same node identity and native session', 'no automatic prompt replay', 'empty workspace unchanged'],
                    'protocol_evidence': {'terminal': [e['data'] for e in events if e['kind'] == 'native:agy_result'],
                        'fatal_notice': [e['data'] for e in events if e['kind'] == 'native:agy_update' and 'terminated due to error' in json.dumps(e['data'])],
                        'runtime_errors': errors},
                    'inference': 'not tested; loopback HTTP error fixture only'}, ensure_ascii=False))
            finally: await h.close()
    finally:
        server.shutdown(); thread.join(); server.server_close()


if __name__ == '__main__':
    asyncio.run(main())
