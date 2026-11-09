"""Canvas 网络表示不变量：真实隔离HTTP，不启动模型或业务采集。"""
import asyncio
import gzip
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cthulhu_v4.app import App
from cthulhu_v4.canvas_http import canvas_etag
from cthulhu_v4.canvas_server import CanvasServer


class CanvasHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name), port=0, ws_port=0)
        await self.app.start()
        self.app.node.discover_models = False
        self.app.deployer.check = lambda: {}
        self.server = CanvasServer(self.app.web.url, self.app.web.token, port=0)
        self.server.start()
        self.board = self.app.center.canvases.create(dict(name='网络隔离fixture', node_id='local', cwd=self.tmp.name,
            script='printf unused', html_template='<h1>fixture</h1>', interval=60, timeout=2, task_ids=[]))
        self.ident = self.board['id']

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.stop)
        await self.app.stop()
        self.tmp.cleanup()

    async def get(self, base, path, **headers):
        def request():
            req = urllib.request.Request(base + path, headers={'Authorization': 'Bearer ' + self.app.web.token} | headers)
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                return response.status, response.headers, response.read()
        return await asyncio.to_thread(request)

    def sample(self, raw, data, kind='json', command='ok'):
        store = self.app.center.store
        board = store.get('canvas', self.ident)
        board.update(state='active', last_success={'command_id': command, 'sampled_at': 123, 'freshness_at': 123,
            'revision': board['revision'], 'format': kind, 'data': data},
            last_attempt={'command_id': command, 'stdout': raw, 'stderr': '', 'state': 'completed',
                'exit_code': 0, 'discarded': False, 'truncated': False, 'collected_at': 123})
        store.put('canvas', board)

    async def test_config_and_reversible_compact_through_both_services(self):
        data = {'payload': 'long-' * 2000, 'unicode': '图表', 'value': [1, 0.5, None]}
        raw = json.dumps(data, ensure_ascii=False) + '\n'
        self.sample(raw, data)
        for base in [self.app.web.url, self.server.url]:
            status, headers, body = await self.get(base, '/api/canvas/' + self.ident + '?view=config')
            self.assertEqual(status, 200)
            config = json.loads(body)
            self.assertNotIn('last_success', config)
            self.assertNotIn('last_attempt', config)
            self.assertEqual(config['html_template'], self.board['html_template'])
            status, headers, body = await self.get(base, '/api/canvas/' + self.ident + '/data?compact=1', **{'Accept-Encoding': 'gzip'})
            self.assertEqual(headers['Content-Encoding'], 'gzip')
            compact = json.loads(gzip.decompress(body))
            self.assertNotIn('data', compact)
            self.assertEqual(compact['data_from_stdout'], 'json')
            self.assertEqual(compact['attempt']['stdout'], raw)
            self.assertEqual(json.loads(compact['attempt']['stdout']), data)
            full = json.loads((await self.get(base, '/api/canvas/' + self.ident + '/data'))[2])
            self.assertEqual(full['data'], data)
            self.assertNotIn('data_from_stdout', full)

    async def test_failed_discarded_missing_and_lexically_unsafe_never_replace_last_success(self):
        path = '/api/canvas/' + self.ident + '/data?compact=1'
        missing = json.loads((await self.get(self.app.web.url, path))[2])
        self.assertIsNone(missing['data'])
        self.assertNotIn('data_from_stdout', missing)
        self.sample('{"a": 1}', {'a': 1})
        board = self.app.center.store.get('canvas', self.ident)
        board['last_attempt'] = {'command_id': 'failure', 'stdout': 'new failure', 'stderr': 'exit 7',
            'state': 'failed', 'exit_code': 7, 'discarded': True, 'truncated': False, 'collected_at': 124}
        self.app.center.store.put('canvas', board)
        failed = json.loads((await self.get(self.app.web.url, path))[2])
        self.assertEqual(failed['data'], {'a': 1})
        self.assertEqual(failed['attempt']['stdout'], 'new failure')
        self.assertNotIn('data_from_stdout', failed)
        for raw, data in [('-0', 0), ('{\n "a" : 1\n}', {'a': 1})]:
            self.sample(raw, data)
            unsafe = json.loads((await self.get(self.app.web.url, path))[2])
            self.assertIn('data', unsafe)
            self.assertNotIn('data_from_stdout', unsafe)
        self.sample('text\n图表', 'text\n图表', kind='text')
        text = json.loads((await self.get(self.app.web.url, path))[2])
        self.assertEqual(text['data_from_stdout'], 'text')
        self.assertEqual(text['attempt']['stdout'], 'text\n图表')

    async def test_conditional_read_auth_time_and_real_state(self):
        for base in [self.app.web.url, self.server.url]:
            status, headers, body = await self.get(base, '/api/canvas')
            tag = headers['ETag']
            self.assertEqual(status, 200)
            status, fresh_headers, body = await self.get(base, '/api/canvas', **{'If-None-Match': tag})
            self.assertEqual((status, body), (304, b''))
            self.assertIn('private', fresh_headers['Cache-Control'])
            self.assertIn('Authorization', fresh_headers['Vary'])
            self.assertGreater(float(fresh_headers['X-Canvas-Now']), float(headers['X-Canvas-Now']))
            self.assertEqual((await self.get(base, '/api/canvas', **{'If-None-Match': tag, 'Authorization': 'Bearer wrong'}))[0], 401)
            self.assertEqual((await self.get(base, '/api/canvas', **{'If-None-Match': tag, 'Origin': 'https://untrusted.invalid'}))[0], 403)
            self.app.center.canvases.control(self.ident, 'pause')
            self.assertEqual((await self.get(base, '/api/canvas', **{'If-None-Match': tag}))[0], 200)
            self.app.center.canvases.control(self.ident, 'resume')
        for field, value in [('fresh', False), ('stale', True), ('revision', 2), ('node_online', False),
                             ('collector_online', False), ('status', 'failed')]:
            original = {'now': 1, 'fresh': True, 'stale': False, 'revision': 1, 'node_online': True,
                        'collector_online': True, 'status': 'fresh', 'data': {'now': 100}}
            self.assertNotEqual(canvas_etag(original), canvas_etag(original | {field: value}))
        self.assertEqual(canvas_etag({'now': 1, 'data': {'now': 100}}), canvas_etag({'now': 2, 'data': {'now': 100}}))
        self.assertNotEqual(canvas_etag({'data': {'now': 100}}), canvas_etag({'data': {'now': 101}}))
