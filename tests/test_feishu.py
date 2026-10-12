"""Real local adapter -> authenticated temple -> existing Codex Runtime, no Feishu account."""
import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

import uvicorn
from cthulhu.gods import Nyarlathotep
from cthulhu.protocol import agent_token
from cthulhu.temple import Temple
from cthulhu.roles import Denied

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/opt/cthulhu-feishu/node_modules/node/bin/node')


@contextmanager
def no_signals():
    yield


class FeishuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.t = Temple({'secret':'fixture-secret','db':str(self.path/'db'),
            'galaxies':[{'id':'g','name':'g','token':'fixture-galaxy'}],
            'gods':[{'id':r,'role':r,'galaxy':'g'} for r in ('azathoth','shub','ghroth','yog')],
            'feishu':{'enabled':True,'app_id':'fixture-app','galaxy':'g',
                'owner':{'sender_id':'fixture-owner','chat_id':'fixture-chat','verified_by':'owner-fixture'}}})
        sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(16)
        port=sock.getsockname()[1]
        self.server=uvicorn.Server(uvicorn.Config(self.t.app(),log_level='error',access_log=False,lifespan='off'))
        self.server.capture_signals=no_signals
        self.server_task=asyncio.create_task(self.server.serve(sockets=[sock]))
        while not self.server.started: await asyncio.sleep(.01)
        self.n=Nyarlathotep(f'ws://127.0.0.1:{port}/ws',
            {'id':'nyarlathotep','name':'奈亚拉托提普','token':agent_token('fixture-secret','nyarlathotep',1),'spec':{}},
            str(ROOT/'tests/fake_codex.py'),str(self.path),self.path/'agent')
        self.link_task=asyncio.create_task(self.n.link.run())
        await asyncio.wait_for(self.n.ready.wait(),5)
        self.event={'app_id':'fixture-app','sender_id':'fixture-owner','chat_id':'fixture-chat','event_id':'e1'}
        self.sock=self.path/'bridge.sock'
        self.ipc=await asyncio.start_unix_server(self.n.ipc,path=self.sock)

    async def asyncTearDown(self):
        self.ipc.close(); await self.ipc.wait_closed()
        await self.n.backends.close()
        self.link_task.cancel(); await asyncio.gather(self.link_task,return_exceptions=True)
        self.server.should_exit=True; await self.server_task
        self.t.store.db.close(); self.tmp.cleanup()

    async def run_event(self, event=None, prompt='REPLY fixture-result', thread=None):
        events=[]
        async def emit(e): events.append(e)
        await self.n.channel_execute({'runId':'run-'+(event or self.event)['event_id'],
            'event':event or self.event,'prompt':prompt,'text':prompt,'threadId':thread},emit)
        return events

    async def test_real_runtime_and_scoped_history_catalog(self):
        a=await self.run_event(prompt='CALL temple_agents {}')
        thread=next(e['threadId'] for e in a if e['type']=='system')
        mid=next(e['message_id'] for e in a if e['type']=='correlation')
        self.assertIn('tool:',self.t.store.turn(mid)['reply'])
        scope=self.t.store.one('select scope from channel_events')['scope']
        self.assertEqual(self.t.store.turn_log('nyarlathotep'),[])
        self.assertEqual(len(self.t.store.turn_log('nyarlathotep',scope=scope)),1)
        b=await self.run_event(self.event|{'event_id':'e2'},thread=thread)
        self.assertEqual(next(e['threadId'] for e in b if e['type']=='system'),thread)
        other=await self.run_event(self.event|{'event_id':'e3','topic_id':'another-topic'},thread=thread)
        self.assertNotEqual(next(e['threadId'] for e in other if e['type']=='system'),thread)
        self.assertEqual(self.t.store.q('select count(*) n from turn_log')[0]['n'],3)

    async def test_atomic_duplicate_and_replay_never_executes_twice(self):
        a=await self.run_event()
        b=await self.run_event()
        self.assertTrue(any(e['type']=='duplicate' for e in b))
        self.assertEqual(self.t.store.q('select count(*) n from messages')[0]['n'],1)
        self.assertEqual(self.t.store.q('select count(*) n from turn_log')[0]['n'],1)
        scope=self.t.store.one('select scope from channel_events')['scope']
        with self.assertRaises(ValueError):
            self.t.store.channel_accept('nyarlathotep','fixture-app','e1',scope+'wrong',{'prompt':'bad','text':'bad'})
        self.assertEqual(self.t.store.q('select count(*) n from chat_turns')[0]['n'],1)

    async def test_authority_and_dormant_scopes(self):
        for change in ({'sender_id':'outsider'},{'chat_id':'other-chat'},{'app_id':'other-app'},{'scope_id':'forged'}):
            with self.assertRaises(Exception):
                await self.n.link.call('channel_ingress',**(self.event|change),prompt='bad',text='bad')
        self.assertEqual(self.t.store.q('select * from messages'),[])
        with self.assertRaises(Denied):
            await self.t.handle_agent_call(SimpleNamespace(id='shub'),'channel_authorize',self.event)
        for method in ('spawn','deploy','messages'):
            with self.assertRaises((Denied,KeyError)):
                await self.t.handle_agent_call(SimpleNamespace(id='nyarlathotep'),method,{})
        self.t.config['feishu']['owner'].pop('verified_by')
        with self.assertRaises(Exception): await self.run_event()

    async def test_peer_permissions_task_association(self):
        with self.assertRaises(Denied): await self.t.route('nyarlathotep','ghroth','unscoped')
        task=self.t.store.add_task('migration')['id']
        await self.t.route('shub','nyarlathotep','delegated',task_id=task)
        with self.assertRaises(Denied): await self.t.route('nyarlathotep','ghroth','unrelated',task_id=task)
        await self.t.route('azathoth','ghroth','related',task_id=task)
        await self.t.route('nyarlathotep','ghroth','coordinate',task_id=task)
        await self.t.route('ghroth','nyarlathotep','receipt',task_id=task)
        self.assertEqual(self.t.store.task(task)['state'],'open')

    async def test_task_supervision_observes_active_nyarl(self):
        task=self.t.store.add_task('Feishu task')['id']
        await self.t.route('shub','nyarlathotep','authorized task',task_id=task)
        self.n.current_task=task
        self.n.state='working'
        row=await self.t.continuations.snapshot(task)
        self.assertEqual([r['agent_id'] for r in row['executors']],['nyarlathotep'])
        self.assertTrue(row['active'])
        await self.t.route('nyarlathotep','yog','task supervision',task_id=task)

    async def test_channel_never_receives_web_memory_and_scope_auth_is_server_side(self):
        self.t.store.remember('nyarlathotep','WEB_MEMORY_ONLY','fixture')
        self.assertIn('WEB_MEMORY_ONLY',await self.n.instructions())
        token=self.n.channel_scope.set('fixture-channel')
        try:
            self.assertNotIn('WEB_MEMORY_ONLY',await self.n.instructions())
        finally: self.n.channel_scope.reset(token)
        with self.assertRaises(Exception):
            await self.n.link.call('turn_log',scope='not-admitted')

    async def test_concurrent_event_transactions_keep_one_message(self):
        payload={'prompt':'REPLY once','text':'once'}
        rows=await asyncio.gather(*[asyncio.to_thread(self.t.store.channel_accept,
            'nyarlathotep','fixture-app','same-event','same-scope',payload) for _ in range(8)])
        self.assertEqual(sum(created for row,created in rows),1)
        self.assertEqual(len({row['message_id'] for row,created in rows}),1)
        self.assertEqual(len(self.t.store.q('select * from messages')),1)
        self.assertEqual(len(self.t.store.q('select * from chat_turns')),1)

    async def test_cancel_targets_only_owned_runtime_and_drain_waits_receipt(self):
        running=asyncio.create_task(self.run_event(prompt='WAIT_INTERRUPT_FIXTURE'))
        for _ in range(200):
            if self.n.active: break
            await asyncio.sleep(.01)
        self.assertIsNotNone(self.n.active)
        self.n.draining=True
        self.assertFalse(self.n.drain_status()['safe'])
        reader,writer=await asyncio.open_unix_connection(self.sock)
        writer.write(json.dumps({'method':'cancel','params':{'runId':'run-e1'}}).encode()+b'\n')
        await writer.drain()
        self.assertTrue(json.loads(await reader.readline())['result'])
        writer.close();await writer.wait_closed()
        events=await asyncio.wait_for(running,3)
        mid=next(e['message_id'] for e in events if e['type']=='correlation')
        self.assertEqual(self.t.store.turn(mid)['status'],'interrupted')
        self.assertFalse(self.n.drain_status()['safe'])
        reader,writer=await asyncio.open_unix_connection(self.sock)
        writer.write(json.dumps({'method':'receipt','params':{'message_id':mid,'receipt':{'state':'ambiguous'}}}).encode()+b'\n')
        await writer.drain();await reader.readline();writer.close();await writer.wait_closed()
        self.assertTrue(self.n.drain_status()['safe'])
        self.assertTrue(self.n.runtime.alive)

    async def test_node_adapter_and_final_platform_receipt(self):
        if not NODE.exists(): self.skipTest('Node deployment dependency not installed')
        source=f'''import {{CodexAdapter,finalReply}} from {json.dumps((ROOT/'integrations/feishu/adapter.mjs').as_uri())};
          const a=new CodexAdapter();
          const opts={{runId:'node-run',templeEvent:{json.dumps(self.event)},templeText:'REPLY node-result',prompt:'REPLY node-result'}};
          await a.prepareRun(opts); const run=a.run(opts); const events=[];
          for await(const e of run.events) events.push(e);
          const receipt=await finalReply({{sendOpts:{{replyTo:'e1'}}}},async()=>({{messageId:'platform-fixture-receipt'}}));
          console.log(JSON.stringify({{events,receipt,exited:await run.waitForExit(1000)}}));'''
        proc=await asyncio.create_subprocess_exec(str(NODE),'--input-type=module','-e',source,
            env=os.environ|{'CTHULHU_FEISHU_SOCKET':str(self.sock)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(proc.communicate(),15)
        self.assertEqual(proc.returncode,0,err.decode())
        result=json.loads(out)
        self.assertTrue(result['exited'])
        self.assertIn({'type':'final_text','content':'node-result'},result['events'])
        receipt=json.loads(self.t.store.one('select receipt from channel_events')['receipt'])
        self.assertEqual(receipt,{'state':'sent','message_id':'platform-fixture-receipt'})
        channel_turn=self.t.store.turn(self.t.store.one('select message_id from channel_events')['message_id'])
        self.assertEqual(channel_turn['channel_receipt'],receipt)
        # Failed/uncertain acknowledgements must never downgrade a confirmed receipt.
        mid=self.t.store.one('select message_id from channel_events')['message_id']
        with self.assertRaises(Exception):
            await self.n.link.call('channel_receipt',message_id=mid,receipt={'state':'ambiguous'})

    async def test_node_failed_or_abandoned_publisher_settles_without_replay(self):
        if not NODE.exists(): self.skipTest('Node deployment dependency not installed')
        source=f'''import {{CodexAdapter,finalReply,abandonReply}} from {json.dumps((ROOT/'integrations/feishu/adapter.mjs').as_uri())};
          const a=new CodexAdapter(); let sends=0;
          async function execute(id) {{
            const opts={{runId:id,templeEvent:{{...{json.dumps(self.event)},event_id:id}},templeText:'REPLY result',prompt:'REPLY result'}};
            const run=a.run(opts); for await(const e of run.events) {{}}
          }}
          await execute('failed');
          try {{ await finalReply({{sendOpts:{{replyTo:'failed'}}}},async()=>{{sends++;throw Error('fixture failure')}}); }} catch {{}}
          await execute('abandoned');
          await abandonReply({{sendOpts:{{replyTo:'abandoned'}}}});
          await execute('failed');
          await finalReply({{sendOpts:{{replyTo:'failed'}}}},async()=>{{sends++;return {{messageId:'unexpected'}}}});
          await abandonReply({{sendOpts:{{replyTo:'failed'}}}});
          console.log(JSON.stringify({{sends}}));'''
        proc=await asyncio.create_subprocess_exec(str(NODE),'--input-type=module','-e',source,
            env=os.environ|{'CTHULHU_FEISHU_SOCKET':str(self.sock)},stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(proc.communicate(),15)
        self.assertEqual(proc.returncode,0,err.decode())
        self.assertEqual(json.loads(out)['sends'],1)
        self.assertEqual(len(self.t.store.q('select * from turn_log')),2)
        self.assertEqual([json.loads(r['receipt'])['state'] for r in self.t.store.q('select receipt from channel_events')],
                         ['ambiguous','ambiguous'])
        self.n.draining=True
        self.assertTrue(self.n.drain_status()['safe'])

    async def test_reconnect_pending_channel_does_not_replay_model(self):
        admitted=await self.n.link.call('channel_ingress',**self.event,prompt='REPLY once',text='once')
        mid=admitted['message_id']
        self.t.store.update_turn(mid,status='running')
        await self.n.receive([{'id':mid,'kind':'channel'}])
        self.assertEqual(self.t.store.turn(mid)['status'],'interrupted')
        events=await self.run_event()
        self.assertTrue(any(e['type']=='duplicate' for e in events))
        self.assertEqual(self.t.store.q('select * from turn_log'),[])

    async def test_reviewed_custom_bridge_keeps_blocklist_and_central_authority(self):
        from integrations.feishu.build import build
        source=NODE.parents[2]/'lark-channel-bridge/dist/cli.js'
        if not NODE.exists() or not source.exists(): self.skipTest('bridge deployment dependency not installed')
        target=self.path/'bridge.mjs'
        build(source,target)
        transformed=target.read_text()
        policy=transformed.split('// src/policy/access.ts\n',1)[1].split('// src/config/config-ops.ts',1)[0]
        script=policy+'''
          const profile={mode:'team',access:{blockedUsers:['blocked'],allowedUsers:['blocked'],admins:['blocked'],allowedChats:['chat']}};
          const controls={ownerRefreshState:'unknown'};
          console.log(JSON.stringify([
            canUseDm(profile,controls,'blocked'),canUseGroup(profile,controls,'chat','blocked'),
            canRunAdminCommand(profile,controls,'blocked'),canUseComment(profile,controls,'blocked'),
            canUseDm(profile,controls,'external')
          ]));'''
        proc=await asyncio.create_subprocess_exec(str(NODE),'--input-type=module','-e',script,stdout=asyncio.subprocess.PIPE)
        out,_=await proc.communicate()
        self.assertEqual(proc.returncode,0)
        policies=json.loads(out)
        self.assertTrue(all(not p['ok'] for p in policies[:4]))
        self.assertTrue(policies[4]['ok']) # Old team policy alone is insufficient authority.
        with self.assertRaises(Exception):
            await self.n.link.call('channel_authorize',**(self.event|{'sender_id':'external'}))
        self.assertNotIn('buildCodexArgs',transformed)
        self.assertNotIn('new ClaudeAdapter',transformed)
        bad=self.path/'unreviewed.js';bad.write_text(source.read_text()+'\n// unknown edit')
        with self.assertRaises(ValueError): build(bad,target)
