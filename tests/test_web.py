import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cthulhu_v4.app import App
from cthulhu_v4.util import ROOT


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name, port=0, ws_port=0, public_url='https://example.test:8001/v4')
        await self.app.start()
        self.url, self.token = self.app.web.url, self.app.web.token

    async def asyncTearDown(self):
        await self.app.stop(); self.tmp.cleanup()

    def call(self, path, body=None, token=None, origin=None):
        headers = {'Authorization': 'Bearer ' + (self.token if token is None else token), 'Content-Type': 'application/json'}
        if origin: headers['Origin'] = origin
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        return json.loads(raw) if raw[:1] in b'{[' else raw

    async def test_auth_origin_and_snapshot(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx: await asyncio.to_thread(self.call, '/api/state', token='')
        self.assertEqual(ctx.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            await asyncio.to_thread(self.call, '/api/state', origin='https://evil.example')
        self.assertEqual(ctx.exception.code, 403)
        state = await asyncio.to_thread(self.call, '/api/state', origin='https://example.test:8001')
        self.assertTrue(any(n['id'] == 'local' and n['online'] for n in state['nodes']))
        self.assertFalse(any('token' in n for n in state['nodes']))
        self.assertIn(b'<html', await asyncio.to_thread(self.call, '/', token=''))
        self.assertIn(b'#!/usr/bin/env bash', await asyncio.to_thread(self.call, '/install.sh', token=''))
        # 静态路径不能逃出 static 目录。
        self.assertNotIn(b'websockets', await asyncio.to_thread(self.call, '/../requirements.txt', token=''))

    async def test_state_polls_return_small_deltas_that_rebuild_the_snapshot(self):
        full = await asyncio.to_thread(self.call, '/api/state')
        self.assertIn('agents', full)
        same = await asyncio.to_thread(self.call, '/api/state?etag=' + full['etag'])
        self.assertTrue(same['unchanged'])
        with self.app.center.store.atomic():
            self.app.center.set_node_defaults({'node': 'local', 'max_turns': 5})
        r = await asyncio.to_thread(self.call, '/api/state?etag=' + full['etag'])
        self.assertEqual(r['base'], full['etag'])
        self.assertNotIn('agents', r['delta'])  # 没变的部分不重发
        self.assertLess(len(json.dumps(r['delta'])), len(json.dumps(full)))
        # 用前端真实的 applyDelta 还原，必须与此刻的全量快照一致。
        fresh = await asyncio.to_thread(self.call, '/api/state')
        rebuilt = await self.apply_js(full, r['delta'])
        drop = lambda s: {k: v for k, v in s.items() if k not in ('etag', 'now')}
        self.assertEqual(drop(rebuilt), drop(fresh))
        # 不认识的 etag（如中心重启后）回全量。
        self.assertIn('agents', await asyncio.to_thread(self.call, '/api/state?etag=unknown'))

    async def apply_js(self, old, delta):
        script = ("import { applyDelta } from %s; let t = ''; process.stdin.on('data', b => t += b);"
                  "process.stdin.on('end', () => { const [o, d] = JSON.parse(t); process.stdout.write(JSON.stringify(applyDelta(o, d))); });"
                  % json.dumps((ROOT / 'static' / 'js' / 'delta.js').as_uri()))
        p = await asyncio.create_subprocess_exec('node', '--input-type=module', '-e', script, stdin=asyncio.subprocess.PIPE,
                                                 stdout=asyncio.subprocess.PIPE)
        out, _ = await p.communicate(json.dumps([old, delta]).encode())
        self.assertEqual(p.returncode, 0)
        return json.loads(out)

    async def test_invite_command_uses_public_galaxy_url(self):
        self.app.galaxy_url = 'ws://127.0.0.1:%d' % self.app.ws_actual_port
        result = await asyncio.to_thread(self.call, '/api/invite', {'name': 'gpu1'})
        self.assertTrue(result['command'].startswith('curl -fsSL '))
        self.assertTrue(result['command'].endswith(result['blob']))
        with self.assertRaises(urllib.error.HTTPError):
            await asyncio.to_thread(self.call, '/api/invite', {'name': 'bad name'})

    async def test_actual_mcp_stdio_roundtrip_through_local_node(self):
        node = self.app.node
        # 构造一个活动回合，让 MCP 桥接受它的 token；工具调用走 node → hub → center。
        with self.app.center.store.atomic():
            m = self.app.center.enqueue('owner', 'master', 'hold')
        from unittest.mock import patch
        import cthulhu_v4.node as node_module
        gate = asyncio.Event()
        class Hold:
            def __init__(s, sink, a): pass
            async def run(s, text): await gate.wait(); return ''
            async def close(s): pass
        with patch.object(node_module, 'make_runtime', Hold):
            node.kick.set()
            for _ in range(50):
                if node.turns: break
                await asyncio.sleep(.05)
            turn = next(iter(node.turns.values()))
            env = dict(os.environ, CTHULHU_URL=node.bridge_url, CTHULHU_TOKEN=turn.token)
            frames = [dict(jsonrpc='2.0', id=1, method='initialize', params={'protocolVersion': '2024-11-05'}),
                      dict(jsonrpc='2.0', method='notifications/initialized'),
                      dict(jsonrpc='2.0', id=2, method='tools/list'),
                      dict(jsonrpc='2.0', id=3, method='tools/call', params={'name': 'list_tasks', 'arguments': {}}),
                      dict(jsonrpc='2.0', id=4, method='tools/call', params={'name': 'not_a_tool', 'arguments': {}})]
            proc = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / 'cthulhu_v4/mcp.py'), env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate(('\n'.join(json.dumps(f) for f in frames) + '\n').encode())
            gate.set()
        self.assertEqual(proc.returncode, 0, err)
        replies = [json.loads(x) for x in out.splitlines()]
        self.assertIn('create_task', [s['name'] for s in replies[1]['result']['tools']])
        self.assertFalse(replies[2]['result']['isError'])
        self.assertIn('该角色没有此工具', replies[3]['result']['content'][0]['text'])

    async def test_conversation_view_excludes_worker_native(self):
        s = self.app.center.store
        s.emit('native:item/agentMessage/delta', actor='master', data={'delta': 'Master 的过程'})
        s.emit('native:item/agentMessage/delta', actor='old_test', data={'delta': '执行者的过程'})
        s.emit('question', '执行者的问题', actor='old_test', target='master', visible=True)
        data = await asyncio.to_thread(self.call, '/api/events?view=conversation&limit=500')
        texts = [e['text'] or e['data'].get('delta') for e in data['events']]
        self.assertIn('Master 的过程', texts); self.assertIn('执行者的问题', texts)
        self.assertNotIn('执行者的过程', texts)


class PagingTests(unittest.TestCase):
    def test_tail_pages_skip_telemetry_and_deltas(self):
        from cthulhu_v4.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            s = Store(Path(tmp) / 'db')
            for i in range(30):
                s.emit('native:item/agentMessage/delta', actor='old_a', data={'itemId': 'x', 'delta': str(i)})
                s.emit('native:thread/tokenUsage/updated', actor='old_a', data={})
                s.emit('native:item/completed', actor='old_a', data={'item': {'id': f'i{i}', 'type': 'agentMessage', 'text': str(i)}})
            s.emit('native:item/completed', actor='old_a', data={'item': {'type': 'userMessage', 'content': 'x' * 1000}})
            page = s.events(before=0, limit=10, agent='old_a', ui=True, compact=True)
            self.assertEqual([e['data']['item']['id'] for e in page], [f'i{i}' for i in range(20, 30)])
            older = s.events(before=page[0]['seq'], limit=10, agent='old_a', ui=True, compact=True)
            self.assertEqual(older[-1]['data']['item']['id'], 'i19')
            tail = s.events(after=page[-1]['seq'] - 3, agent='old_a', ui=True)
            self.assertTrue(all(e['kind'] != 'native:thread/tokenUsage/updated' for e in tail))
            self.assertTrue(any(e['kind'].endswith('/delta') for e in s.events(after=0, agent='old_a', ui=True)))
            # 工具读取保持原始全集。
            self.assertEqual(len(s.events(after=0, limit=500, agent='old_a')), 91)
            s.close()
