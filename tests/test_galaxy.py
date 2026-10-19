"""真实 WebSocket + 独立节点进程集成测试；不调用付费模型。"""
import asyncio
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from websockets.asyncio.server import serve
from websockets.asyncio.client import connect

from cthulhu_v4.core import Core, ROOT
from cthulhu_v4.galaxy import MAX_FRAME, EXECUTION_POLICY


def task_args(node, cwd, **kw):
    return dict(title='跨机试用', description='模拟目标', acceptance='有完成证据',
                deadline=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                node=node, cwd=str(cwd), backend='demo') | kw


class GalaxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.c = Core(self.path/'center', heartbeat=60)
        self.hub = self.c.galaxies
        self.tokens = {n: self.hub.provision(n) for n in ('alpha', 'beta')}
        self.server = await serve(self.hub.handle, '127.0.0.1', 0, max_size=MAX_FRAME)
        self.url = 'ws://127.0.0.1:' + str(self.server.sockets[0].getsockname()[1])
        self.procs, self.logs = [], []
        self.scheduler = asyncio.create_task(self.c.serve())

    async def asyncTearDown(self):
        for p in self.procs:
            if p.returncode is None: p.terminate()
        for p in self.procs:
            try: await asyncio.wait_for(p.wait(), 10)
            except asyncio.TimeoutError: p.kill(); await p.wait()
        self.scheduler.cancel(); await self.c.close()
        await asyncio.gather(self.scheduler, return_exceptions=True)
        self.server.close(); await self.server.wait_closed()
        self.c.store.db.close()
        for f in self.logs: f.close()
        self.tmp.cleanup()

    async def wait(self, check, seconds=12):
        deadline = asyncio.get_running_loop().time()+seconds
        while asyncio.get_running_loop().time() < deadline:
            result = check()
            if result: return result
            await asyncio.sleep(.1)
        logs = '\n'.join(Path(f.name).read_text() for f in self.logs)
        self.fail('等待条件超时\n'+logs)

    async def start_node(self, ident, env=None):
        log = (self.path/(ident+'.log')).open('a'); self.logs.append(log)
        p = await asyncio.create_subprocess_exec(sys.executable, '-m', 'cthulhu_v4.node',
            '--id', ident, '--center', self.url, '--state', str(self.path/ident),
            '--token-file', str(self.tokens[ident]), stdout=log, stderr=log, cwd=ROOT, env=env)
        self.procs.append(p)
        await self.wait(lambda: any(n['id'] == ident and n['online'] for n in self.hub.nodes()))
        return p

    async def test_two_process_nodes_questions_messages_and_original_sessions(self):
        await self.start_node('alpha'); await self.start_node('beta')
        tasks = [self.c.create_task(task_args(n, self.path/n)) for n in ('alpha', 'beta')]
        await self.wait(lambda: len(self.c.store.all('question')) == 2)
        await self.wait(lambda: not any(self.hub.busy(t['agent']) for t in tasks))
        sessions = {t['agent']: self.c.store.get('agent', t['agent'])['session'] for t in tasks}
        self.assertTrue(all(sessions.values()))
        # 同一中心队列承载跨节点 agent 消息，接收方读取到明确来源。
        m = self.c.tool(tasks[0]['agent'], 'send_message', {'to': tasks[1]['agent'], 'text': '来自 alpha 的证据'})
        await self.wait(lambda: self.c.store.get('message', m['id'])['state'] == 'handled')
        self.assertEqual(m['sender'], tasks[0]['agent'])
        for q in self.c.store.all('question'):
            self.c.answer('owner', {'question_id': q['id'], 'version': q['version'], 'answer': '继续'})
        await self.wait(lambda: all(t['state'] == 'completed' for t in self.c.store.all('task')))
        await self.wait(lambda: not any(self.hub.busy(t['agent']) for t in tasks))
        for aid, session in sessions.items(): self.assertEqual(self.c.store.get('agent', aid)['session'], session)
        events = self.c.store.events(limit=500)
        self.assertTrue(any(e['kind']=='native:demo' and 'alpha' in e['data']['text'] for e in events))
        self.assertTrue(any(e['kind']=='native:demo' and 'beta' in e['data']['text'] for e in events))
        self.assertNotIn('token', self.hub.nodes()[0])
        # 同一磁盘节点重启仍能重连，不创建第二个 agent 或 session。
        self.procs[0].terminate(); await self.procs[0].wait()
        await self.wait(lambda: 'alpha' not in self.hub.connections)
        await self.start_node('alpha')
        self.assertEqual(len(self.c.store.all('agent')), 3)

    async def test_native_mcp_bridge_and_priority10_interrupt(self):
        bin_dir = self.path/'bin'; bin_dir.mkdir()
        shim = bin_dir/'codex'
        shim.write_text('#!/bin/sh\nexec '+sys.executable+' '+str(ROOT/'tests/fake_remote.py')+'\n')
        shim.chmod(0o755)
        await self.start_node('alpha', dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH']))
        task = self.c.create_task(task_args('alpha', self.path/'alpha', backend='codex', description='BLOCK'))
        aid = task['agent']
        await self.wait(lambda: any(e['kind']=='native:test/blocked' for e in self.c.store.events(limit=500)))
        session = self.c.store.get('agent', aid)['session']
        old = next(j for j in self.c.store.all('lease') if j['agent']==aid)
        urgent = self.c.enqueue('owner', aid, '立即停止旧目标，报告现状', 10)
        with self.assertRaises(PermissionError): self.hub.valid_job('alpha', old['id'])
        await self.wait(lambda: self.c.store.get('message', urgent['id'])['state']=='handled')
        self.assertEqual(self.c.store.get('lease', old['id'])['state'], 'interrupted')
        self.assertEqual(self.c.store.get('agent', aid)['session'], session)
        self.assertFalse(self.c.store.get('agent', aid)['paused'])
        self.assertTrue(any(e['kind']=='harness_tool' and e['text']=='list_tasks' for e in self.c.store.events(limit=500)))

    async def test_protocol_dedup_fencing_and_center_restart(self):
        t = self.c.create_task(task_args('alpha', '/only/on/the/remote/machine'))
        payload = {'instance':'disk-alpha','capacity':2,'backends':['demo'],'events':[]}
        first = self.hub.sync('alpha', payload); j = first['jobs'][0]
        self.assertEqual(self.hub.sync('alpha', payload)['jobs'][0]['id'], j['id'])
        call = {'op':'tool','job':j['id'],'call_id':'one','name':'ask_master',
                'arguments':{'text':'继续？','recommendation':'继续','blocking':True}}
        q = self.hub.request('alpha', call)
        self.assertEqual(self.hub.request('alpha', call), q)
        self.assertEqual(len(self.c.store.all('question')), 1)
        with self.assertRaises(PermissionError): self.hub.request('beta', call)
        payload['events'] = [{'seq':1,'job':j['id'],'kind':'session','data':{'session':'original'}}]
        self.hub.sync('alpha', payload); self.hub.sync('alpha', payload)
        self.assertEqual(self.c.store.get('node', 'alpha')['ack'], 1)
        # 中心重建时保留远端租约，不使用中心机 PID 或把远端消息误判成已退出。
        other = Core(self.path/'center')
        try:
            self.assertEqual(other.store.get('message', j['message'])['state'], 'running')
            self.assertEqual(other.store.get('agent', t['agent'])['session'], 'original')
        finally: other.store.db.close()
        self.c.clock = lambda: j['until']+1
        self.hub.expire()
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        with self.assertRaises(PermissionError): self.hub.request('alpha', call)
        self.assertTrue(any(e['kind']=='node_attention' for e in self.c.store.events(limit=500)))
        with self.assertRaises(ValueError): self.hub.sync('alpha', payload | {'instance':'different-disk'})

    async def test_legacy_runtime_is_blocked_before_native_dispatch_and_resume(self):
        t = self.c.create_task(task_args('alpha', '/remote/project', backend='codex'))
        self.c.save_session(t['agent'], 'keep-original-session')
        payload = {'instance':'disk-alpha','capacity':2,'backends':['demo','codex'],'events':[]}
        self.assertEqual(self.hub.sync('alpha', payload)['jobs'], [])
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.assertTrue(self.c.store.get('agent', t['agent'])['paused'])
        self.hub.sync('alpha', payload)
        self.assertEqual(self.c.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='node_attention'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, '旧启动协议'):
            self.c.tool('master', 'control_task', {'task_id':t['id'],'action':'resume','reason':'重试'})
        # 更新并重连后仍由明确恢复操作继续；不自动重放，也不重置原 session。
        payload['execution_policy'] = EXECUTION_POLICY
        self.hub.sync('alpha', payload)
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.c.tool('master', 'control_task', {'task_id':t['id'],'action':'resume','reason':'节点代码已更新'})
        jobs = self.hub.sync('alpha', payload)['jobs']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['agent_spec']['session'], 'keep-original-session')

    async def test_running_native_job_is_not_renewed_after_runtime_downgrade(self):
        t = self.c.create_task(task_args('alpha', '/remote/project', backend='claude'))
        payload = {'instance':'disk-alpha','capacity':2,'backends':['claude'],'events':[],
                   'execution_policy':EXECUTION_POLICY}
        job = self.hub.sync('alpha', payload)['jobs'][0]
        payload.pop('execution_policy')
        self.assertEqual(self.hub.sync('alpha', payload)['jobs'], [])
        self.assertEqual(self.c.store.get('lease', job['id'])['state'], 'unknown')
        self.assertEqual(self.c.store.get('message', job['message'])['state'], 'unknown')
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')

    async def test_websocket_auth_and_duplicate_node_rejection(self):
        async with connect(self.url) as ws:
            await ws.send(json.dumps({'node':'alpha','token':'wrong'}))
            with self.assertRaises(Exception): await ws.recv()
        await self.start_node('alpha')
        async with connect(self.url) as ws:
            await ws.send(json.dumps({'node':'alpha','token':self.tokens['alpha'].read_text()}))
            with self.assertRaises(Exception): await ws.recv()
        self.assertIn('alpha', self.hub.connections)

    async def test_reconnect_replays_events_once_and_lease_expiry_stops_process(self):
        bin_dir = self.path/'bin'; bin_dir.mkdir()
        shim = bin_dir/'codex'
        shim.write_text('#!/bin/sh\nexec '+sys.executable+' '+str(ROOT/'tests/fake_remote.py')+'\n')
        shim.chmod(0o755)
        await self.start_node('alpha', dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH']))
        t = self.c.create_task(task_args('alpha', self.path/'alpha', backend='codex', description='BLOCK'))
        await self.wait(lambda: any(e['kind']=='native:test/blocked' for e in self.c.store.events(limit=500)))
        original = self.c.store.get('agent', t['agent'])['session']
        # 主动断开真实 WebSocket 后，节点自动重连且不会重开同一回合。
        ws = self.hub.connections['alpha']; await ws.close()
        await self.wait(lambda: self.hub.connections.get('alpha') not in (None, ws))
        await asyncio.sleep(1.2)
        self.assertEqual(sum(e['kind']=='native:test/blocked' for e in self.c.store.events(limit=500)), 1)
        self.assertEqual(self.c.store.get('agent', t['agent'])['session'], original)
        # 切断服务端口，等待节点的单调时钟租约到期，检查真实进程退出。
        self.server.close(); await self.server.wait_closed()
        from cthulhu_v4.store import Store
        local = Store(self.path/'alpha'/'node.db')
        try:
            await self.wait(lambda: all(j['state']=='stopped' for j in local.all('execution')), seconds=25)
            self.assertIsNone(local.get('agent', t['agent']).get('pid'))
        finally: local.db.close()
        self.c.clock = lambda: __import__('time').time()+60
        self.hub.expire()
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
