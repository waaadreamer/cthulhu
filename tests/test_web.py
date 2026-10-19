import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

from cthulhu_v4.core import Core, ROOT
from cthulhu_v4.web import Web


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.c = Core(self.tmp.name)
        self.web = Web(self.c, asyncio.get_running_loop(), 0)
        self.thread = threading.Thread(target=self.web.server.serve_forever, daemon=True); self.thread.start()

    async def asyncTearDown(self):
        await self.c.close(); await asyncio.to_thread(self.web.server.shutdown)
        self.web.server.server_close(); self.thread.join(); self.c.store.db.close(); self.tmp.cleanup()

    def get(self, path, token=None, origin=None):
        headers = {'Authorization': 'Bearer '+(token or '')}
        if origin: headers['Origin'] = origin
        with urllib.request.urlopen(urllib.request.Request(self.c.url+path, headers=headers)) as r:
            return r.read()

    async def test_auth_and_origin_and_public_snapshot(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx: await asyncio.to_thread(self.get, '/api/state')
        self.assertEqual(ctx.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            await asyncio.to_thread(self.get, '/api/state', self.web.token, 'https://evil.example')
        self.assertEqual(ctx.exception.code, 403)
        state = json.loads(await asyncio.to_thread(self.get, '/api/state', self.web.token))
        self.assertNotIn('token', state['agents'][0])
        self.assertIn(b'activity', await asyncio.to_thread(self.get, '/'))

    async def test_actual_mcp_stdio_roundtrip(self):
        import os
        env = dict(os.environ, CTHULHU_URL=self.c.url, CTHULHU_TOKEN=self.c.store.get('agent', 'master')['token'])
        frames = [dict(jsonrpc='2.0',id=1,method='initialize',params={'protocolVersion':'2024-11-05'}),
                  dict(jsonrpc='2.0',method='notifications/initialized'),
                  dict(jsonrpc='2.0',id=2,method='tools/list'),
                  dict(jsonrpc='2.0',id=3,method='tools/call',params={'name':'list_tasks','arguments':{}}),
                  dict(jsonrpc='2.0',id=4,method='tools/call',params={'name':'not_a_tool','arguments':{}})]
        proc = await asyncio.create_subprocess_exec(sys.executable, str(ROOT/'cthulhu_v4/mcp.py'), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(('\n'.join(json.dumps(f) for f in frames)+'\n').encode())
        self.assertEqual(proc.returncode,0,err)
        replies = [json.loads(x) for x in out.splitlines()]
        self.assertEqual(len(replies),4)
        self.assertIn('create_task',[s['name'] for s in replies[1]['result']['tools']])
        self.assertFalse(replies[2]['result']['isError'])
        self.assertTrue(replies[3]['result']['isError'])
        self.assertIn('该角色没有此工具', replies[3]['result']['content'][0]['text'])

    async def test_conversation_includes_master_native_and_excludes_worker_native(self):
        self.c.record_native('master', 'item/agentMessage/delta', {'delta': 'Master 的过程'})
        self.c.record_native('old_test', 'item/agentMessage/delta', {'delta': '执行者的过程'})
        self.c.store.emit('question', '执行者的问题', actor='old_test', target='master', visible=True)
        self.c.store.emit('chat', '主人的消息', actor='owner', visible=True)
        self.c.store.emit('review', '进展汇报', actor='master', visible=True)
        data = json.loads(await asyncio.to_thread(self.get, '/api/events?view=conversation&limit=500', self.web.token))
        texts = [e['text'] or e['data'].get('delta') for e in data['events']]
        self.assertIn('Master 的过程', texts)
        self.assertIn('执行者的问题', texts)
        self.assertIn('主人的消息', texts)
        self.assertIn('进展汇报', texts)
        self.assertNotIn('执行者的过程', texts)
        after = data['events'][-1]['seq']
        later = json.loads(await asyncio.to_thread(self.get, f'/api/events?view=conversation&after={after}', self.web.token))
        self.assertEqual(later['events'], [])
        self.assertIn(b'window.Transcript', await asyncio.to_thread(self.get, '/transcript.js'))

    async def test_explicit_public_origin_allows_proxy_but_not_arbitrary_hosts(self):
        self.web.allowed_origins.add('https://47.115.202.248:8001')
        raw = await asyncio.to_thread(self.get, '/api/state', self.web.token, 'https://47.115.202.248:8001')
        self.assertIn('nodes', json.loads(raw))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            await asyncio.to_thread(self.get, '/api/state', self.web.token, 'https://evil.example')
        self.assertEqual(ctx.exception.code, 403)
