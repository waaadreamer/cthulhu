"""真实 WebSocket + 独立节点进程：邀请接入、跨机消息、P10、断线补传、证书指纹。不调用模型。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from cthulhu_v4.center import Center
from cthulhu_v4.transport import decode_invite, encode_invite, make_handler, RemoteLink
from cthulhu_v4.util import MAX_FRAME, PROTOCOL, ROOT


def task_args(node, cwd, **kw):
    return dict(title='跨机试用', description='模拟目标', acceptance='有完成证据',
                deadline=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                node=node, cwd=str(cwd), backend='demo') | kw


class GalaxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.c = Center(self.path / 'center')
        self.hub = self.c.hub
        self.server = await serve(make_handler(self.c), '127.0.0.1', 0, max_size=MAX_FRAME)
        self.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.procs, self.logs = [], []
        self.expirer = asyncio.create_task(self.expire_loop())

    async def expire_loop(self):
        while True:
            self.hub.expire(); await asyncio.sleep(.2)

    async def asyncTearDown(self):
        self.expirer.cancel()
        for p in self.procs:
            if p.returncode is None: p.terminate()
        for p in self.procs:
            try: await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError: p.kill(); await p.wait()
        self.server.close(); await self.server.wait_closed()
        self.c.store.close()
        for f in self.logs: f.close()
        self.tmp.cleanup()

    async def wait(self, check, seconds=15):
        loop = asyncio.get_running_loop(); end = loop.time() + seconds
        while loop.time() < end:
            result = check()
            if result: return result
            await asyncio.sleep(.1)
        self.fail('等待条件超时\n' + '\n'.join(Path(f.name).read_text() for f in self.logs))

    def cli(self, *args, env=None):
        return asyncio.create_subprocess_exec(sys.executable, '-m', 'cthulhu_v4', *args, cwd=ROOT, env=env,
                                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    async def join(self, name, env=None):
        home = self.path / ('home-' + name)
        with self.c.store.atomic():
            info = self.c.create_invite(name, self.url)
        blob = encode_invite({k: info[k] for k in ('center', 'code', 'node', 'pin')})
        p = await self.cli('node', 'join', blob, '--home', str(home), env=env)
        out, _ = await p.communicate()
        self.assertEqual(p.returncode, 0, out.decode())
        return home, blob

    async def start_node(self, name, env=None, home=None):
        home = home or (await self.join(name, env))[0]
        log = (self.path / (name + '.log')).open('a'); self.logs.append(log)
        p = await asyncio.create_subprocess_exec(sys.executable, '-m', 'cthulhu_v4', 'node', 'run', '--home', str(home),
                                                 stdout=log, stderr=log, cwd=ROOT, env=env)
        self.procs.append(p)
        await self.wait(lambda: any(n['id'] == name and n['online'] for n in self.hub.nodes()))
        return p, home

    def shim_env(self):
        bin_dir = self.path / 'bin'; bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / 'codex'
        shim.write_text('#!/bin/sh\nexec ' + sys.executable + ' ' + str(ROOT / 'tests/fake_remote.py') + '\n')
        shim.chmod(0o755)
        return dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'])

    async def test_invite_join_two_nodes_messages_and_sessions(self):
        (pa, home_a), _ = await self.start_node('alpha'), await self.start_node('beta')
        tasks = [self.c.create_task(task_args(n, self.path)) for n in ('alpha', 'beta')]
        await self.wait(lambda: len(self.c.store.all('question')) == 2)
        await self.wait(lambda: not any(self.hub.busy(t['agent']) for t in tasks))
        sessions = {t['agent']: self.c.store.get('agent', t['agent'])['session'] for t in tasks}
        self.assertTrue(all(sessions.values()))
        m = self.c.tool(tasks[0]['agent'], 'send_message', {'to': tasks[1]['agent'], 'text': '来自 alpha 的证据'})
        await self.wait(lambda: self.c.store.get('message', m['id'])['state'] == 'handled')
        for q in self.c.store.all('question'):
            with self.c.store.atomic():
                self.c.answer('owner', {'question_id': q['id'], 'version': q['version'], 'answer': '继续'})
        # 执行者只是申请结束；这里没有运行 Master，由测试代为审核通过。
        await self.wait(lambda: all(t['state'] == 'review' for t in self.c.store.all('task')))
        for t in self.c.store.all('task'):
            self.c.tool('master', 'close_task', {'task_id': t['id'], 'status': 'success', 'reason': '测试审核'})
        self.assertTrue(all(t['state'] == 'completed' for t in self.c.store.all('task')))
        for aid, session in sessions.items():
            self.assertEqual(self.c.store.get('agent', aid)['session'], session)
        self.assertNotIn('token', self.hub.nodes()[0])
        # 同一磁盘节点重启仍能重连，不创建新身份。
        pa.terminate(); await pa.wait()
        await self.wait(lambda: not self.hub.online('alpha'))
        await self.start_node('alpha', home=home_a)
        self.assertEqual(len(self.c.store.all('node')), 3)

    async def test_invite_is_single_machine_and_rerun_is_upgrade(self):
        home, blob = await self.join('gamma')
        # 重跑同一命令：已加入，保留身份，不消耗新邀请。
        p = await self.cli('node', 'join', blob, '--home', str(home)); out, _ = await p.communicate()
        self.assertEqual(p.returncode, 0); self.assertIn('保留原身份'.encode(), out)
        # 另一台机器（另一个 home）不能复用邀请。
        p = await self.cli('node', 'join', blob, '--home', str(self.path / 'other')); out, _ = await p.communicate()
        self.assertNotEqual(p.returncode, 0); self.assertIn('其他机器'.encode(), out)
        # 过期邀请被拒绝。
        with self.c.store.atomic():
            info = self.c.create_invite('delta', self.url, ttl=-1)
        p = await self.cli('node', 'join', encode_invite(info), '--home', str(self.path / 'd')); out, _ = await p.communicate()
        self.assertIn('过期'.encode(), out)

    async def test_native_mcp_bridge_and_priority10_interrupt(self):
        env = self.shim_env()
        await self.start_node('alpha', env)
        task = self.c.create_task(task_args('alpha', self.path, backend='codex', description='BLOCK'))
        aid = task['agent']
        await self.wait(lambda: any(e['kind'] == 'native:test/blocked' for e in self.c.store.events(limit=500)))
        session = self.c.store.get('agent', aid)['session']
        old = next(j for j in self.c.store.all('lease') if j['agent'] == aid)
        with self.c.store.atomic():
            urgent = self.c.enqueue('owner', aid, '立即停止旧目标，报告现状', 10)
        with self.assertRaises(PermissionError): self.hub.valid_job('alpha', old['id'])
        await self.wait(lambda: self.c.store.get('message', urgent['id'])['state'] == 'handled')
        self.assertEqual(self.c.store.get('lease', old['id'])['state'], 'interrupted')
        self.assertEqual(self.c.store.get('agent', aid)['session'], session)
        self.assertTrue(any(e['kind'] == 'harness_tool' and e['text'] == 'list_tasks' for e in self.c.store.events(limit=500)))

    async def test_protocol_dedup_fencing_and_instance_binding(self):
        with self.c.store.atomic(): self.hub.register('alpha'); self.hub.register('beta')
        t = self.c.create_task(task_args('alpha', '/only/on/the/remote/machine'))
        payload = {'instance': 'disk-alpha', 'capacity': 2, 'backends': ['demo'], 'events': []}
        j = self.hub.sync('alpha', payload)['jobs'][0]
        self.assertEqual(self.hub.sync('alpha', payload)['jobs'][0]['id'], j['id'])
        call = {'op': 'tool', 'job': j['id'], 'call_id': 'one', 'name': 'ask_master',
                'arguments': {'text': '继续？', 'recommendation': '继续', 'blocking': True}}
        q = self.hub.request('alpha', call)
        self.assertEqual(self.hub.request('alpha', call), q)
        self.assertEqual(len(self.c.store.all('question')), 1)
        with self.assertRaises(PermissionError): self.hub.request('beta', call)
        payload['events'] = [{'seq': 1, 'job': j['id'], 'kind': 'session', 'data': {'session': 'original'}}]
        self.hub.sync('alpha', payload); self.hub.sync('alpha', payload)
        self.assertEqual(self.c.store.get('node', 'alpha')['ack'], 1)
        # 中心重启保留远端租约，并给节点一个租约周期报告结局。
        other = Center(self.path / 'center')
        try:
            self.assertEqual(other.store.get('message', j['message'])['state'], 'running')
            self.assertEqual(other.store.get('agent', t['agent'])['session'], 'original')
        finally: other.store.close()
        with self.assertRaises(PermissionError): self.hub.sync('alpha', payload | {'instance': 'different-disk'})

    async def test_websocket_auth_protocol_and_duplicate_rejection(self):
        p, home = await self.start_node('alpha')
        token = json.loads((home / 'node.json').read_text())['token']
        for hello, reason in [({'node': 'alpha', 'token': 'wrong', 'protocol': PROTOCOL}, '凭证无效'),
                              ({'node': 'alpha', 'token': token, 'protocol': 1}, '协议'),
                              ({'node': 'alpha', 'token': token, 'protocol': PROTOCOL}, '已有连接')]:
            async with connect(self.url) as ws:
                await ws.send(json.dumps(hello))
                self.assertIn(reason, json.loads(await ws.recv())['error'])
        self.assertTrue(self.hub.online('alpha'))

    async def test_reconnect_replays_events_once_and_lease_expiry_stops_process(self):
        await self.start_node('alpha', self.shim_env())
        t = self.c.create_task(task_args('alpha', self.path, backend='codex', description='BLOCK'))
        await self.wait(lambda: any(e['kind'] == 'native:test/blocked' for e in self.c.store.events(limit=500)))
        original = self.c.store.get('agent', t['agent'])['session']
        # 中心主动断开：节点自动重连，不会重开同一回合。
        for ws in list(self.server.connections): await ws.close()
        await self.wait(lambda: not self.hub.online('alpha'))
        await self.wait(lambda: self.hub.online('alpha'))
        await asyncio.sleep(1.2)
        self.assertEqual(sum(e['kind'] == 'native:test/blocked' for e in self.c.store.events(limit=500)), 1)
        self.assertEqual(self.c.store.get('agent', t['agent'])['session'], original)
        # 切断服务端口：节点按单调时钟在租约到期前停止真实进程。
        self.server.close(); await self.server.wait_closed()
        from cthulhu_v4.store import Store
        local = Store(self.path / 'home-alpha' / 'state' / 'node.db')
        try:
            await self.wait(lambda: all(j['state'] == 'stopped' for j in local.all('execution')), seconds=30)
            self.assertIsNone(local.get('agent', t['agent']).get('pid'))
        finally: local.close()


class PinTests(unittest.IsolatedAsyncioTestCase):
    async def test_fingerprint_mismatch_refuses_to_send_credentials(self):
        import ssl
        tmp = tempfile.TemporaryDirectory(); path = Path(tmp.name)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=test',
                        '-keyout', str(path / 'k.pem'), '-out', str(path / 'c.pem')], check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(path / 'c.pem', path / 'k.pem')
        received = []
        async def handler(ws):
            try: received.append(await ws.recv()); await ws.send(json.dumps({'ok': True, 'token': 't'}))
            except Exception: pass
        server = await serve(handler, '127.0.0.1', 0, ssl=ctx)
        url = 'wss://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        try:
            from cthulhu_v4.transport import fetch_fingerprint
            good = await asyncio.to_thread(fetch_fingerprint, url)
            self.assertEqual(await RemoteLink(url, 'n', pin=good, hello={'instance': 'i'}).redeem('code'), 't')
            with self.assertRaisesRegex(PermissionError, '指纹'):
                await RemoteLink(url, 'n', pin='0' * 64, hello={'instance': 'i'}).redeem('code')
            self.assertEqual(len(received), 1)  # 指纹不符时凭证没有发出
        finally:
            server.close(); await server.wait_closed(); tmp.cleanup()

    def test_invite_blob_roundtrip(self):
        data = {'center': 'wss://h/v4/galaxy', 'code': 'abc', 'node': 'n', 'pin': None}
        self.assertEqual(decode_invite(encode_invite(data)), data)
