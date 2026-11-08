"""隔离中心 + 独立 HTTP Canvas 服务 + 真实本机命令；不调用模型。"""
import asyncio
import json
import shlex
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from cthulhu_v4.app import App
from cthulhu_v4.canvas_server import CanvasServer
from cthulhu_v4.node import Node
from cthulhu_v4.transport import RemoteLink
from cthulhu_v4.util import ROOT


class CanvasE2ETests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = App(self.root / 'center', port=0, ws_port=0)
        await self.app.start()
        # Canvas 验收只需要命令能力；禁止后台探测本机真实 CLI，保持默认测试不碰模型运行时。
        self.app.node.discover_models = False
        # 版本检查与 Canvas 不变量无关；不让后台 GitHub fetch 占住测试 executor 的退出。
        self.app.deployer.check = lambda: {}
        self.server = CanvasServer(self.app.web.url, self.app.web.token, port=0, tick_interval=.1)
        self.server.start()
        # 第一轮节点同步报告 commands-v1；不凭模型/伪输出宣称采集成功。
        for _ in range(50):
            if 'commands-v1' in self.app.center.store.get('node', 'local').get('features', []): break
            await asyncio.sleep(.02)

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.stop)
        await self.app.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, *, token=None, origin=None, method=None):
        headers = {'Authorization': 'Bearer ' + (self.server.token if token is None else token)}
        if origin: headers['Origin'] = origin
        req = urllib.request.Request(self.server.url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            raw = response.read()
            return (response.headers, json.loads(raw) if response.headers['Content-Type'].startswith('application/json') else raw)

    async def api(self, path, body=None):
        return (await asyncio.to_thread(self.request, path, body))[1]

    async def create(self, script, **kw):
        spec = dict(name='两个实验', html_template=(ROOT / 'static/examples/canvas-summary.html').read_text(),
                    node_id='local', cwd=str(self.root), script=script, interval=1, timeout=2, task_ids=[])
        return await self.api('/api/canvas', spec | kw)

    async def result(self, ident, check=lambda data: data['attempt'] is not None):
        for _ in range(160):
            await self.server.tick_once()
            data = await self.api('/api/canvas/' + ident + '/data')
            if check(data): return data
            await asyncio.sleep(.03)
        self.fail('真实 Canvas 采集超时')

    async def test_real_multi_experiment_files_update_through_independent_service(self):
        for name, value in [('a', 3), ('b', 5)]:
            (self.root / f'{name}.json').write_text(json.dumps({'status': 'fixture', 'metrics': {'completed': value}}))
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'experiments': [{'name': 'a', 'path': 'a.json'}, {'name': 'b', 'path': 'b.json'}]}))
        script = f'python3 {shlex.quote(str(ROOT / "examples/canvas_collect.py"))} {shlex.quote(str(manifest))}'
        board = await self.create(script)
        data = await self.result(board['id'])
        self.assertEqual([x['metrics']['completed'] for x in data['data']['experiments']], [3, 5])
        self.assertFalse(data['data']['demo'])
        self.assertTrue(data['fresh'])
        first = data['attempt']['command_id']
        (self.root / 'a.json').write_text(json.dumps({'status': 'fixture updated', 'metrics': {'completed': 4}}))
        data = await self.result(board['id'], lambda d: d['attempt'] and d['attempt']['command_id'] != first)
        self.assertEqual(data['data']['experiments'][0]['metrics']['completed'], 4)
        self.assertEqual(self.app.center.store.all('task'), [])
        await self.api('/api/canvas/' + board['id'] + '/close', {})
        self.assertEqual((await self.api('/api/canvas/' + board['id']))['state'], 'closed')

    async def test_two_workers_and_worker_restart_execute_real_command_once(self):
        board = await self.create("printf 'once\\n' >> calls.txt; sleep .2; printf '{\"ok\":true}'")
        second = CanvasServer(self.app.web.url, self.app.web.token, port=0)
        try:
            await asyncio.gather(self.server.tick_once(), second.tick_once(), self.server.tick_once())
            # 新 worker 没有任何旧内存，仍复用中心票据，不能再启动一个采集。
            await asyncio.gather(second.tick_once(), self.server.tick_once())
            data = await self.result(board['id'])
            self.assertEqual(data['data'], {'ok': True})
            self.assertEqual((self.root / 'calls.txt').read_text().splitlines(), ['once'])
            self.assertEqual(len(self.app.center.store.all('command')), 1)
        finally:
            second.server.server_close()

    async def test_canvas_uses_node_initiated_websocket_and_keeps_data_when_node_leaves(self):
        registered = self.app.center.hub.register('canvas-remote')
        node = Node(self.root / 'remote-node', backends=['demo'])
        node.discover_models = False
        url = 'ws://127.0.0.1:' + str(self.app.ws_actual_port)
        def link(channel=None):
            return RemoteLink(url, 'canvas-remote', token=registered['token'], hello=node.hello(),
                              channel=channel, log=lambda *a, **kw: None)
        node.link, node.data = link(), link('data')
        runner = asyncio.create_task(node.run())
        try:
            async with asyncio.timeout(10):
                while 'commands-v1' not in self.app.center.store.get('node', 'canvas-remote').get('features', []):
                    await asyncio.sleep(.03)
            cwd = self.root / 'remote-work'; cwd.mkdir()
            (cwd / 'source.json').write_text('{"experiments":[{"name":"WS file A"},{"name":"WS file B"}]}')
            board = await self.create('cat source.json', node_id='canvas-remote', cwd=str(cwd), interval=60)
            data = await self.result(board['id'])
            self.assertEqual([item['name'] for item in data['data']['experiments']], ['WS file A', 'WS file B'])
            self.assertTrue(data['fresh'])
        finally:
            await node.close()
            runner.cancel(); await asyncio.gather(runner, return_exceptions=True)
            await node.link.close(); await node.data.close()
            node.store.close()
        async with asyncio.timeout(10):
            while self.app.center.hub.online('canvas-remote'):
                await asyncio.sleep(.03)
        data = await self.api('/api/canvas/' + board['id'] + '/data')
        self.assertEqual(data['status'], 'node_offline')
        self.assertFalse(data['fresh'])
        self.assertEqual(len(data['data']['experiments']), 2)

    async def test_real_timeout_stderr_and_nonzero_exit_keep_success_data(self):
        board = await self.create("printf 'last success'")
        await self.result(board['id'])
        board = await self.api('/api/canvas/' + board['id'], {'revision': 1, 'script': 'sleep 10', 'timeout': .15})
        data = await self.result(board['id'], lambda d: d['attempt'] and d['attempt']['state'] == 'timed_out')
        self.assertEqual(data['data'], 'last success'); self.assertFalse(data['fresh'])
        self.assertEqual(data['status'], 'failed')
        await self.api('/api/canvas/' + board['id'], {'revision': board['revision'], 'script': 'echo broken >&2; exit 7'})
        data = await self.result(board['id'], lambda d: d['attempt'] and d['attempt']['exit_code'] == 7)
        self.assertIn('broken', data['attempt']['stderr'])
        self.assertEqual(data['data'], 'last success')

    async def test_real_source_truncation_keeps_last_success_and_reports_combined_limit(self):
        board = await self.create("printf 'last success'")
        await self.result(board['id'])
        await self.api('/api/canvas/' + board['id'], {'revision': 1, 'timeout': 5,
            'script': "python3 -c \"import sys;sys.stdout.write('a'*700000);sys.stderr.write('b'*700000)\""})
        data = await self.result(board['id'], lambda d: d['attempt'] and d['attempt'].get('truncated'))
        self.assertEqual(data['attempt']['exit_code'], 0)
        self.assertEqual(len(data['attempt']['stdout'].encode()) + len(data['attempt']['stderr'].encode()), 1024 * 1024)
        self.assertEqual(data['data'], 'last success')
        self.assertEqual(data['status'], 'failed')
        self.assertFalse(data['fresh'])

    async def test_service_and_center_auth_frame_csp_and_no_credential_in_static(self):
        for path, kwargs, code in [('/api/canvas', {'token': ''}, 401),
                                   ('/api/canvas', {'origin': 'null'}, 403),
                                   ('/api/executions', {}, 404)]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                await asyncio.to_thread(self.request, path, **kwargs)
            self.assertEqual(caught.exception.code, code)
        headers, raw = await asyncio.to_thread(self.request, '/board-frame.html', token='')
        self.assertIn("connect-src 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('https:', headers['Content-Security-Policy'])
        self.assertNotIn(self.server.token.encode(), raw)
        _, raw = await asyncio.to_thread(self.request, '/boards.html', token='')
        self.assertNotIn(self.server.token.encode(), raw)
        self.assertIn(('id="board-home" href="' + self.app.web.url + '/"').encode(), raw)
        with urllib.request.urlopen(self.app.web.url + '/boards.html', timeout=10) as response:
            self.assertIn(b'id="board-home" href="./"', response.read())
        with self.assertRaises(ValueError): CanvasServer('https://example.com', self.server.token, port=0)

    async def test_standalone_home_link_preserves_prefix_and_escapes_html(self):
        service = CanvasServer('http://127.0.0.1:1/v4/"<test>', self.server.token, port=0)
        service.start()
        try:
            def read():
                with urllib.request.urlopen(service.url + '/', timeout=10) as response:
                    return response.read()
            raw = await asyncio.to_thread(read)
            self.assertIn(b'id="board-home" href="http://127.0.0.1:1/v4/&quot;&lt;test&gt;/"', raw)
            self.assertNotIn(self.server.token.encode(), raw)
        finally:
            await asyncio.to_thread(service.stop)

    async def test_close_inflight_real_command_is_not_cancelled_and_result_is_discarded(self):
        board = await self.create("sleep .3; printf 'finished' > collector-finished; printf 'late data'")
        await self.server.tick_once()
        ident = self.app.center.store.get('canvas', board['id'])['inflight']['command_id']
        await self.api('/api/canvas/' + board['id'] + '/close', {})
        self.assertNotEqual(self.app.center.executions.get(ident)['state'], 'cancelled')
        data = await self.result(board['id'])
        self.assertIsNone(data['data'])
        self.assertTrue(data['attempt']['discarded'])
        self.assertEqual(data['state'], 'closed')
        self.assertEqual((self.root / 'collector-finished').read_text(), 'finished')
