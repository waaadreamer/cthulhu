"""真实 shell/进程组与 WS 验证；不调用模型，不把测试节点当成线上升级证据。"""
import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest

from websockets.asyncio.server import serve

from cthulhu_v4.center import Center
from cthulhu_v4.execution import ACTIVE, FEATURE, OUTPUT_LIMIT
from cthulhu_v4.node import Node
from cthulhu_v4.transport import RemoteLink, make_handler
from cthulhu_v4.util import MAX_FRAME, ROOT


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.c = Center(self.root / 'center')
        self.registered = self.c.hub.register('remote')
        self.server = await serve(make_handler(self.c), '127.0.0.1', 0, max_size=MAX_FRAME)
        self.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.node = Node(self.root / 'node', backends=['demo'])
        self.node.link = self.link()
        self.node.data = self.link(channel='data')
        self.run = asyncio.create_task(self.node.run())
        await self.wait(lambda: FEATURE in self.c.store.get('node', 'remote').get('features', []))
        self.sentinel = await asyncio.create_subprocess_exec('sleep', '60')
        self.workload = self.root / 'tree.py'
        self.workload.write_text('''import os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
p=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
Path(sys.argv[1]).write_text(str(os.getpid())+' '+str(p.pid))
print('started',flush=True)
time.sleep(60)
''')

    def link(self, channel=None):
        return RemoteLink(self.url, 'remote', token=self.registered['token'],
                          hello=self.node.hello(), channel=channel, log=lambda *a, **kw: None)

    async def asyncTearDown(self):
        for pidfile in self.root.glob('pipe-child-*'):
            (self.root / ('pipe-release-' + pidfile.name.removeprefix('pipe-child-'))).touch()
        await self.node.close()
        self.run.cancel(); await asyncio.gather(self.run, return_exceptions=True)
        await self.node.link.close(); await self.node.data.close()
        if self.sentinel.returncode is None:
            self.sentinel.terminate(); await self.sentinel.wait()
        self.server.close(); await self.server.wait_closed()
        self.node.store.close(); self.c.store.close(); self.tmp.cleanup()

    async def wait(self, check, seconds=10):
        async with asyncio.timeout(seconds):
            while True:
                result = check()
                if result: return result
                await asyncio.sleep(.03)

    def create(self, script, **kw):
        return self.c.executions.create('remote', str(self.root), script, **kw)

    async def done(self, item):
        return await self.wait(lambda: (x if x['state'] not in ACTIVE else None)
                               if (x := self.c.executions.get(item['id'])) else None)

    def tree(self, name):
        pidfile = self.root / (name + '.pids')
        return f'{shlex.quote(sys.executable)} {shlex.quote(str(self.workload))} {shlex.quote(str(pidfile))}', pidfile

    async def test_streaming_cwd_stderr_exit_and_idempotent_dispatch(self):
        script = 'pwd; printf before; sleep .4; printf after; printf err >&2; exit 7'
        item = self.create(script, request_id='one')
        self.assertEqual(self.create(script, request_id='one')['id'], item['id'])
        with self.assertRaisesRegex(ValueError, '不同命令'): self.create('echo changed', request_id='one')
        await self.wait(lambda: 'before' in self.c.executions.get(item['id'])['stdout'])
        with self.assertRaisesRegex(RuntimeError, '旧节点'):
            duplicate = Node(self.root / 'node', backends=['demo'])
        self.assertIn(self.c.executions.get(item['id'])['state'], ACTIVE)
        renewed = await self.node.link.request({'op': 'sync', 'instance': self.node.instance,
            'commands_have': [item['id']], 'features': [FEATURE], 'backends': ['demo'], 'events': []})
        self.assertEqual(renewed['commands'], [{'id': item['id'], 'state': 'running'}])
        # 多次真实同步会重发票据；副作用计数只能追加一次。
        counter = self.root / 'count'
        second = self.create('echo once >> count; sleep .3')
        result = await self.done(item); await self.done(second)
        self.assertEqual(counter.read_text(), 'once\n')
        self.assertEqual(result['stdout'], str(self.root) + '\nbeforeafter')
        self.assertEqual((result['stderr'], result['exit_code'], result['state']), ('err', 7, 'completed'))
        self.assertLessEqual(result['created_at'], result['started_at'])
        self.assertLessEqual(result['started_at'], result['finished_at'])
        self.assertGreaterEqual(result['received_at'], result['created_at'])
        # 终态取消不会把已成功命令改写成 cancelled。
        self.assertEqual(self.c.executions.cancel(item['id'])['state'], 'completed')

    async def test_combined_source_limit_drains_and_data_does_not_block_sync(self):
        marker = self.root / 'drained'
        script = shlex.quote(sys.executable) + ' -c ' + shlex.quote(
            "import os,time;os.write(1,b'a'*800000);os.write(2,b'b'*2400000);open('drained','w').write('yes');time.sleep(.4)")
        item = self.create(script)
        await self.wait(lambda: self.c.executions.get(item['id'])['truncated'])
        self.assertIn(self.c.executions.get(item['id'])['state'], ACTIVE)
        result = await self.done(item)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['stdout'].encode()) + len(result['stderr'].encode()), OUTPUT_LIMIT)
        self.assertEqual(marker.read_text(), 'yes')
        # 限制首先在节点发件箱生效，不能只在中心截断后宣称源端有限。
        emitted = [e for e in self.node.store.events(limit=1000) if e['kind'] == 'command:output']
        self.assertEqual(sum(len(e['data']['payload']['text'].encode()) for e in emitted), OUTPUT_LIMIT)
        self.assertTrue(all(len(e['data']['payload']['text'].encode()) <= 16384 for e in emitted))
        self.assertTrue(self.c.hub.online('remote'))

    async def test_slow_output_upload_keeps_control_connection_responsive(self):
        await self.node.data.connected()
        real = self.node.data.request
        held, release = asyncio.Event(), asyncio.Event()

        async def slow(body, timeout=None):
            if body.get('op') == 'events':
                held.set(); await release.wait()
            return await real(body, timeout=timeout)

        self.node.data.request = slow
        try:
            item = self.create('printf stream; sleep .5')
            await asyncio.wait_for(held.wait(), 3)
            # 输出上传仍未完成时，通过真实控制 WS 请求续租，不以 mock 心跳代替网络。
            async with asyncio.timeout(2):
                for _ in range(3):
                    response = await self.node.link.request({'op': 'sync', 'instance': self.node.instance,
                        'backends': ['demo'], 'features': [FEATURE], 'events': []})
                    self.assertIn('lease_seconds', response)
            self.assertFalse(release.is_set())
            await self.wait(lambda: any(e['kind'] == 'command:output' for e in self.node.outbox()))
            events = self.node.outbox()
            body = {'op': 'events', 'instance': self.node.instance, 'events': events}
            first = await real(body)
            self.assertEqual(await real(body), first)
            self.assertEqual(self.c.executions.get(item['id'])['stdout'], 'stream')
        finally:
            release.set(); self.node.data.request = real
        self.assertEqual((await self.done(item))['stdout'], 'stream')

    async def test_sigkill_node_lifeline_cleans_tree_and_restart_is_unknown(self):
        registered = self.c.hub.register('crash')
        script, pidfile = self.tree('crash')
        runner = """import asyncio,sys
from cthulhu_v4.node import Node
from cthulhu_v4.transport import RemoteLink
async def main():
 n=Node(sys.argv[1],backends=['demo'])
 n.link=RemoteLink(sys.argv[2],'crash',token=sys.argv[3],hello=n.hello())
 n.data=RemoteLink(sys.argv[2],'crash',token=sys.argv[3],hello=n.hello(),channel='data')
 await n.run()
asyncio.run(main())
"""
        async def start():
            return await asyncio.create_subprocess_exec(sys.executable, '-c', runner, str(self.root / 'crash-node'),
                self.url, registered['token'], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = await start()
        try:
            await self.wait(lambda: FEATURE in self.c.store.get('node', 'crash').get('features', []))
            item = self.c.executions.create('crash', str(self.root), script)
            await self.wait(pidfile.exists)
            pids = [int(x) for x in pidfile.read_text().split()]
            proc.kill(); await proc.wait()
            await self.wait(lambda: all(not alive(pid) for pid in pids))
            await self.wait(lambda: not self.c.hub.online('crash'))
            proc = await start()
            result = await self.done(item)
            self.assertEqual(result['state'], 'unknown')
            self.assertIsNone(result['exit_code'])
            # 恢复窗口内中心仍可能重发旧票据；节点持久票据使它不会再次运行同一脚本。
            self.assertEqual([int(x) for x in pidfile.read_text().split()], pids)
            self.assertIsNone(self.sentinel.returncode)
        finally:
            if proc.returncode is None: proc.kill(); await proc.wait()

    async def test_timeout_and_cancel_kill_only_command_process_group(self):
        for name, timeout in [('timeout', .8), ('cancel', 20)]:
            script, pidfile = self.tree(name)
            item = self.create(script, timeout=timeout)
            await self.wait(pidfile.exists)
            pids = [int(x) for x in pidfile.read_text().split()]
            if name == 'cancel':
                self.assertEqual(self.c.executions.cancel(item['id'])['state'], 'cancelling')
            result = await self.done(item)
            self.assertEqual(result['state'], 'timed_out' if name == 'timeout' else 'cancelled')
            await self.wait(lambda: all(not alive(pid) for pid in pids))
            self.assertIsNone(self.sentinel.returncode)

    async def test_queued_cancel_concurrency_offline_and_transaction_reuse(self):
        # 创建与取消在同一个无 await 段中发生，节点尚未领单。
        with self.c.store.atomic():
            item = self.create('touch should-not-exist')
            self.assertEqual(self.c.executions.cancel(item['id'])['state'], 'cancelled')
        await asyncio.sleep(.2)
        self.assertFalse((self.root / 'should-not-exist').exists())
        items = [self.create('sleep 5') for _ in range(4)]
        with self.assertRaisesRegex(ValueError, '4 个'): self.create('echo fifth')
        for x in items: self.c.executions.cancel(x['id'])
        for x in items: await self.done(x)
        self.c.hub.links.pop('remote')
        with self.assertRaisesRegex(ValueError, '离线'): self.create('touch offline')
        self.assertFalse((self.root / 'offline').exists())

    async def test_disconnect_reconnect_does_not_replay_and_expiry_is_unknown(self):
        item = self.create('echo once >> reconnect; sleep 1')
        await self.wait(lambda: (self.root / 'reconnect').exists())
        for ws in list(self.server.connections): await ws.close()
        result = await self.done(item)
        self.assertEqual(result['state'], 'completed')
        self.assertEqual((self.root / 'reconnect').read_text(), 'once\n')
        # data 可以先于控制连接恢复并补传终态；收到结果并不等于节点已经可接新命令。
        await self.wait(lambda: self.c.hub.online('remote'))
        script, pidfile = self.tree('expiry')
        item = self.create(script)
        await self.wait(pidfile.exists)
        pids = [int(x) for x in pidfile.read_text().split()]
        # 保留真实 data WS 上传结局，切断续租并缩短本次节点租约，验证执行进程自己停组。
        real = self.node.link.request
        async def disconnected(body, timeout=None):
            if body.get('op') == 'sync': raise ConnectionError('隔离测试断开续租')
            return await real(body, timeout=timeout)
        self.node.link.request = disconnected
        try:
            # 先关闭真实控制 WS，排除旧的在途 sync 回包把刚缩短的租约重新延长的竞态。
            await self.node.link.close()
            self.node.commands.expires[item['id']] = 0
            result = await self.done(item)
            self.assertEqual(result['state'], 'unknown')
            await self.wait(lambda: all(not alive(pid) for pid in pids))
        finally: self.node.link.request = real
        await self.wait(lambda: self.c.hub.online('remote'))
        # 中心先到期的票据同样封存，既不派发也不自动重放。
        stored = self.create('touch expired')
        stored['until'] = self.c.clock() - 1
        self.c.store.put('command', stored); self.c.hub.expire()
        self.assertEqual(self.c.executions.get(stored['id'])['state'], 'unknown')
        self.assertEqual(self.c.executions.requests_for('remote'), [])
        self.assertFalse((self.root / 'expired').exists())

    async def test_center_restart_unknown_late_result_and_node_restart_no_replay(self):
        script, pidfile = self.tree('restart')
        item = self.create(script)
        await self.wait(pidfile.exists)
        other = Center(self.root / 'center')
        try:
            frozen = other.executions.get(item['id'])
            self.assertEqual(frozen['state'], 'unknown')
            received = frozen['received_at']
        finally: other.store.close()
        # 原对象共享同一 SQLite，真实 sync 会看到已封存命令并停止，迟到结果只作留档。
        await self.wait(lambda: self.c.executions.get(item['id']).get('late_result'))
        self.assertEqual(self.c.executions.get(item['id'])['state'], 'unknown')
        self.assertEqual(self.c.executions.get(item['id'])['received_at'], received)
        pids = [int(x) for x in pidfile.read_text().split()]
        await self.wait(lambda: all(not alive(pid) for pid in pids))
        await self.node.close(); self.run.cancel(); await asyncio.gather(self.run, return_exceptions=True)
        self.node.store.close()
        # 刚启动但未获得结局的票据恢复为 unknown；重复派发绝不再启动同 ID。
        from cthulhu_v4.store import Store
        disk = Store(self.root / 'node' / 'node.db')
        disk.put('command', {'id': 'cmd_recover', 'state': 'running', 'started_at': 1})
        disk.close()
        replacement = Node(self.root / 'node', backends=['demo'])
        try:
            replacement.commands.accept([dict(id='cmd_recover', cwd=str(self.root), script='touch replayed',
                                             timeout=10, state='dispatched')], 0)
            self.assertFalse(replacement.commands.active)
            self.assertFalse((self.root / 'replayed').exists())
            events = replacement.outbox(limit=1000)
            self.assertTrue(any(e['job'] == 'cmd_recover' and e['data'].get('state') == 'unknown' for e in events))
        finally:
            replacement.store.close()
        # tearDown 仍关闭一个有效 store。
        self.node.store = Store(self.root / 'node' / 'node.db')

    def inherited_pipe_script(self, *, wait=False, label='pipe'):
        # 隔离子进程故意持有FD，测试显式释放或8秒自限退出；不靠调大生产预算掩盖竞态。
        code = f'''import os,time
from pathlib import Path
r,w=os.pipe()
if os.fork()==0:
 os.close(r);os.setsid();Path('pipe-child-'+{label!r}).write_text(str(os.getpid()))
 os.write(w,b'1');os.close(w);end=time.monotonic()+8
 while not Path('pipe-release-'+{label!r}).exists() and time.monotonic()<end:time.sleep(.03)
 os._exit(0)
os.close(w);os.read(r,1);os.close(r)
with open({label!r},'a') as f:f.write('once\\n')
print('{{"sample":2}}',flush=True)
{('time.sleep(30)' if wait else '')}
'''
        return shlex.quote(sys.executable) + ' -c ' + shlex.quote(code)

    async def release_pipe(self, label):
        (self.root / ('pipe-release-' + label)).touch()
        pid = int((self.root / ('pipe-child-' + label)).read_text())
        await self.wait(lambda: not alive(pid))

    async def test_inherited_pipe_drain_keeps_receipt_cancel_timeout_and_no_replay(self):
        for mode in ['complete', 'cancel', 'timeout', 'cancel-after-exit']:
            with self.subTest(mode=mode):
                item = self.create(self.inherited_pipe_script(wait=mode in ('cancel', 'timeout'), label=mode),
                                   timeout=.5 if mode == 'timeout' else 10, request_id=mode)
                receipt = self.node.commands.root / (item['id'] + '.json')
                await self.wait(lambda: (self.root / mode).exists())
                if mode == 'cancel-after-exit':
                    await self.wait(receipt.exists)
                    self.assertEqual(json.loads(receipt.read_text())['state'], 'completed')
                if mode.startswith('cancel'):
                    self.c.executions.cancel(item['id'])
                result = await self.done(item)
                self.assertEqual(result['state'], {'complete': 'completed', 'cancel-after-exit': 'completed',
                                 'cancel': 'cancelled', 'timeout': 'timed_out'}[mode])
                self.assertEqual(result['stdout'], '{"sample":2}\n')
                self.assertTrue(result['truncated'])
                self.assertIn('输出管道排空超时', result['error'])
                self.assertIsNotNone(result['exit_code'])
                if mode in ('complete', 'cancel-after-exit'):
                    self.assertEqual(result['exit_code'], 0)
                same = self.c.executions.create(result['node_id'], result['cwd'], result['script'],
                                               timeout=result['timeout'], request_id=mode)
                self.assertEqual(same['id'], item['id'])
                self.assertEqual((self.root / mode).read_text(), 'once\n')
                self.assertIsNone(self.sentinel.returncode)
                await self.release_pipe(mode)

    async def test_concurrent_pipe_revoke_preserves_unknown_and_other_command(self):
        item = self.create(self.inherited_pipe_script(label='revoked'), timeout=10)
        other = self.create('printf independent; sleep .2; printf done', timeout=10)
        receipt = self.node.commands.root / (item['id'] + '.json')
        await self.wait(receipt.exists)
        # 真WS同步撤销本票据：即使看守已确认退出，也不能把中心封存unknown改写成成功。
        self.c.executions.finish(self.c.executions.get(item['id']), 'unknown', '隔离中心撤销')
        await self.wait(lambda: self.c.executions.get(item['id']).get('late_result'))
        frozen = self.c.executions.get(item['id'])
        self.assertEqual(frozen['state'], 'unknown')
        self.assertEqual(frozen['late_result']['state'], 'unknown')
        self.assertTrue(frozen['late_result']['truncated'])
        result = await self.done(other)
        self.assertEqual((result['state'], result['exit_code'], result['stdout']),
                         ('completed', 0, 'independentdone'))
        self.assertEqual((self.root / 'revoked').read_text(), 'once\n')
        self.assertIsNone(self.sentinel.returncode)
        await self.release_pipe('revoked')

    async def test_inherited_pipe_without_valid_receipt_remains_unknown(self):
        for mode in ['missing', 'invalid']:
            item = self.create(self.inherited_pipe_script(label=mode), timeout=10, request_id=mode)
            receipt = self.node.commands.root / (item['id'] + '.json')
            await self.wait(receipt.exists)
            if mode == 'missing': receipt.unlink()
            else: receipt.write_text('{broken')
            result = await self.done(item)
            self.assertEqual(result['state'], 'unknown')
            self.assertIsNone(result['exit_code'])
            self.assertTrue(result['truncated'])
            self.assertIn('回执', result['error'])
            if mode == 'invalid': self.assertIn('JSONDecodeError', result['error'])
            self.assertFalse(any(x['id'] == item['id'] for x in self.c.executions.requests_for('remote')))
            self.assertEqual((self.root / mode).read_text(), 'once\n')
            await self.release_pipe(mode)

    async def test_canvas_incomplete_pipe_retains_data_without_unknown_replay(self):
        from cthulhu_v4.canvas import Canvases
        api = Canvases(self.c)
        board = api.create(dict(name='隔离管道fixture', node_id='remote', cwd=str(self.root),
                           script="printf '{\"sample\":1}'", interval=1, timeout=10,
                           html_template='<pre>fixture</pre>', task_ids=[]))
        async def collect():
            await api.route('POST', '/api/canvas/tick', {}, {'worker': 'fixture'})
            current = self.c.store.get('canvas', board['id'])
            result = await self.done({'id': current['inflight']['command_id']})
            await api.route('POST', '/api/canvas/tick', {}, {'worker': 'fixture'})
            return result, self.c.store.get('canvas', board['id'])
        _, current = await collect()
        self.assertEqual(current['last_success']['data'], {'sample': 1})
        api.edit(board['id'], {'revision': current['revision'], 'script': self.inherited_pipe_script(label='canvas')})
        result, current = await collect()
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(current['last_attempt']['truncated'])
        self.assertEqual(current['last_success']['data'], {'sample': 1})
        self.assertEqual(current['state'], 'active')
        self.assertFalse(current['blocked_unknown'])
        self.assertFalse(api.data(current)['fresh'])
        api.edit(board['id'], {'revision': current['revision'], 'script': "printf '{\"sample\":3}'"})
        _, current = await collect()
        self.assertEqual(current['last_success']['data'], {'sample': 3})
        self.assertTrue(api.data(current)['fresh'])
        self.assertEqual((self.root / 'canvas').read_text(), 'once\n')
        await self.release_pipe('canvas')

    async def test_invalid_utf8_is_bounded_and_bad_cwd_fails(self):
        script = shlex.quote(sys.executable) + ' -c ' + shlex.quote("import os;os.write(1,b'\\xff'*500000)")
        result = await self.done(self.create(script))
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['output_bytes'], OUTPUT_LIMIT)
        item = self.c.executions.create('remote', str(self.root / 'missing'), 'echo no')
        result = await self.done(item)
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['exit_code'])


class ExecutionWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_owner_auth_local_execution_cancel_and_metadata(self):
        import urllib.error
        import urllib.request
        from cthulhu_v4.app import App
        with tempfile.TemporaryDirectory() as root:
            # 此用例验证命令 HTTP；无关 GitHub 更新检查快速拒绝，不能拖住 executor 收尾。
            app = App(root, port=0, ws_port=0, git_proxy='http://127.0.0.1:1')
            await app.start()
            def call(path, body=None, token=None, origin=None):
                headers = {'Authorization': 'Bearer ' + (app.web.token if token is None else token),
                           'Content-Type': 'application/json'}
                if origin: headers['Origin'] = origin
                request = urllib.request.Request(app.web.url + path,
                    data=None if body is None else json.dumps(body).encode(), headers=headers)
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.loads(response.read())
            async def wait(check):
                async with asyncio.timeout(8):
                    while not check(): await asyncio.sleep(.03)
            try:
                await wait(lambda: FEATURE in app.center.store.get('node', 'local').get('features', []))
                args = dict(node_id='local', cwd=root, script='printf local; printf stderr >&2; sleep 5', timeout=10,
                            request_id='http-one')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    await asyncio.to_thread(call, '/api/executions', args, '')
                self.assertEqual(error.exception.code, 401)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    await asyncio.to_thread(call, '/api/executions', args, None, 'https://foreign.invalid')
                self.assertEqual(error.exception.code, 403)
                self.assertEqual(app.center.executions.list(), [])
                item = await asyncio.to_thread(call, '/api/executions', args)
                self.assertEqual((await asyncio.to_thread(call, '/api/executions', args))['id'], item['id'])
                await wait(lambda: app.center.executions.get(item['id'])['stdout'] == 'local')
                streamed = await asyncio.to_thread(call, '/api/executions?id=' + item['id'])
                self.assertEqual(streamed['stderr'], 'stderr')
                stopped = await asyncio.to_thread(call, '/api/executions/cancel', {'id': item['id']})
                self.assertEqual(stopped['state'], 'cancelling')
                await wait(lambda: app.center.executions.get(item['id'])['state'] == 'cancelled')
                meta = await asyncio.to_thread(call, '/api/executions?node_id=local&limit=2')
                self.assertEqual(meta[0]['id'], item['id'])
                self.assertNotIn('stdout', meta[0]); self.assertNotIn('script', meta[0])
            finally: await app.stop()
