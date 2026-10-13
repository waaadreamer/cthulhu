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

    async def test_scoped_recovery_keeps_one_ingress_and_final_reply(self):
        from unittest.mock import patch
        from cthulhu import backends
        self.n.pending_runtime={'backend':'codex','model':'recovery-primary',
            'fallback':{'backend':'codex','model':'recovery-fallback'}}
        with patch.object(backends,'RETRY_DELAYS',(0,)*5):
            events=await self.run_event(prompt='RECOVERY_FIXTURE channel')
        self.assertEqual(len([e for e in events if e['type']=='final_text']),1)
        self.assertEqual(len([e for e in events if e['type']=='correlation']),1)
        self.assertEqual(self.t.store.q('select count(*) n from channel_events')[0]['n'],1)
        self.assertEqual(self.t.store.q('select count(*) n from turn_log where scope != ?',[''])[0]['n'],7)
        self.assertEqual(self.t.store.turn_log('nyarlathotep'),[])
        self.assertEqual((self.path/'recovery-channel-effect').read_text(),'once')
        self.assertEqual(self.n.recovery['attempt'],7)

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

    async def test_admission_config_admits_external_contact_with_its_own_identity(self):
        yor={'app_id':'fixture-app','sender_id':'fixture-yor','chat_id':'fixture-group','event_id':'y1'}
        # Default deny: no config, or a sender not in it, never reaches the temple.
        with self.assertRaises(PermissionError): await self.n.authorize(yor)
        with self.assertRaises(PermissionError): await self.run_event(yor,prompt='REPLY bad')
        entry={'identity':{'feishu_sender_id':'fixture-yor','feishu_app_id':'yor-app'},
               'allow':['只读访问 fixture 会议文档'],'deny':['共享或修改任何资源']}
        self.n.admission_path.write_text(json.dumps({'version':1,'default':'deny','contacts':{'约尔':entry}},ensure_ascii=False))
        with self.assertRaises(PermissionError): await self.n.authorize(yor|{'sender_id':'other-bot'})
        # The temple itself refuses a non-owner ingress without an admission contact.
        with self.assertRaises(Exception):
            await self.n.link.call('channel_ingress',**yor,prompt='bad',text='bad')
        self.assertEqual(self.t.store.q('select * from messages'),[])
        self.t.store.remember('nyarlathotep','WEB_MEMORY_ONLY','fixture')
        owner=await self.run_event(prompt='SHOW_CONTEXT')
        seen=json.loads(next(e['content'] for e in (await self.run_event(yor,prompt='SHOW_CONTEXT')) if e['type']=='final_text'))
        self.assertIn('外部联系人「约尔」',seen['instructions']); self.assertNotIn('已核验主人',seen['instructions'])
        self.assertNotIn('WEB_MEMORY_ONLY',seen['instructions']+seen['input'])
        self.assertIn('只读访问 fixture 会议文档',seen['input']); self.assertTrue(seen['tools'])
        rows=self.t.store.q('select m.sender, c.origin, e.scope from channel_events e join messages m on m.id=e.message_id '
                            'join chat_turns c on c.id=e.message_id order by m.created_at')
        self.assertEqual([(r['sender'],r['origin']) for r in rows],[('owner','飞书'),('feishu:fixture-yor','飞书·约尔')])
        self.assertIn(':fixture-yor:fixture-group',rows[1]['scope'])
        self.assertEqual(self.t.sender_meta('feishu:fixture-yor')['sender_role'],'external')
        # Separate thread from the owner's; Nyarl's own config edits apply on the next event.
        owner_thread=next(e['threadId'] for e in owner if e['type']=='system')
        b=await self.run_event(yor|{'event_id':'y2'},prompt='REPLY ok',thread=owner_thread)
        self.assertNotEqual(next(e['threadId'] for e in b if e['type']=='system'),owner_thread)
        self.n.admission_path.write_text(json.dumps({'version':1,'default':'deny','contacts':{}}))
        with self.assertRaises(PermissionError): await self.run_event(yor|{'event_id':'y3'},prompt='REPLY bad')

    async def test_nyarl_memory_also_distills_azathoth(self):
        self.assertEqual(self.n.memory_sources,{'owner','azathoth'})

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
        # Bridge policy alone is insufficient: an external sender needs Nyarl's admission config.
        self.assertFalse((await self.n.link.call('channel_authorize',**(self.event|{'sender_id':'external'})))['owner'])
        with self.assertRaises(PermissionError):
            await self.n.authorize(self.event|{'sender_id':'external'})
        self.assertNotIn('buildCodexArgs',transformed)
        self.assertNotIn('new ClaudeAdapter',transformed)
        bad=self.path/'unreviewed.js';bad.write_text(source.read_text()+'\n// unknown edit')
        with self.assertRaises(ValueError): build(bad,target)

    async def test_generated_bridge_channel_dependencies_execute(self):
        from integrations.feishu.build import build
        source = NODE.parents[2] / 'lark-channel-bridge/dist/cli.js'
        if not NODE.exists() or not source.exists():
            self.skipTest('bridge deployment dependency not installed')
        target = self.path / 'bridge.mjs'
        build(source, target)
        (self.path / 'node_modules').symlink_to(NODE.parents[2], target_is_directory=True)
        # Evaluate the complete built module without invoking its CLI/network.
        # Exercise the same shared bindings used before the channel handshake.
        script = target.read_text().split('program.parseAsync(process.argv)', 1)[0]
        script += '''
          if (typeof createLarkChannel !== 'function') throw Error('channel factory missing');
          const nonce = new CallbackNonceStore(join21(dirname18(process.argv[1]), 'nonce-fixture.json'));
          await nonce.load();
          const capability = codexCapability({permissions: {maxAccess: 'full'}});
          if (!capability.systemPrompt || capability.permissions.maxAccess !== 'full')
            throw Error('shared capability prompt missing');
          if (!prefixBridgeSystemPrompt('fixture input').includes('fixture input'))
            throw Error('shared prompt composer missing');
          console.log('channel dependencies ready');
        '''
        target.write_text(script)
        proc = await asyncio.create_subprocess_exec(str(NODE), str(target),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 15)
        self.assertEqual(proc.returncode, 0, err.decode())
        self.assertIn(b'channel dependencies ready', out)

    async def test_imported_history_handoff_on_real_server_without_foreign_resume(self):
        from cthulhu.feishu_import import import_history
        import copy
        config = copy.deepcopy(self.t.config)
        config['feishu']['enabled'] = False
        config['feishu']['owner']['legacy_scope_id'] = 'old-chat'
        bundle = {'version': 1, 'event': self.event,
            'source': {'scope_id': 'old-chat', 'threads': [{'thread_id': 'old-thread', 'sha256': 'a'*64, 'scope_ids': ['old-chat']}]}, 'turns': [
            {'source_id': 'old1', 'source_thread': 'old-thread', 'input': 'historical question', 'reply': 'REPLY prior scoped result',
             'status': 'completed', 'created_at': 1}]}
        import_history(self.t.store, config, bundle)
        events = await self.run_event(prompt='new question', thread='foreign-old-thread')
        thread = next(e['threadId'] for e in events if e['type'] == 'system')
        self.assertNotEqual(thread, 'foreign-old-thread')
        final = next(e['content'] for e in events if e['type'] == 'final_text')
        self.assertIn('prior scoped result', final)  # Fake runtime echoes the actual handoff input.
        self.assertEqual(self.t.store.turn_log('nyarlathotep'), [])

    async def test_converted_bundle_import_and_real_server_handoff(self):
        from test_feishu_convert import fixture
        from cthulhu.feishu_convert import convert, write_bundle
        from cthulhu.feishu_import import import_history
        config, catalog, home, review = fixture(self.path / 'conversion')
        review['turns'][0]['reply'] = 'REPLY reviewed continuity receipt'
        bundle = convert(config, catalog, home, review)
        digest = write_bundle(self.path / 'continuity.json', bundle)
        self.assertEqual(import_history(self.t.store, config, bundle)['imported'], 2)
        self.assertTrue(import_history(self.t.store, config, bundle)['duplicate'])
        self.assertEqual(self.t.store.q('SELECT * FROM channel_events'), [])
        with self.assertRaises(Exception):
            await self.n.link.call('turn_log', scope='feishu:fixture-app:fixture-owner:fixture-chat')
        events = await self.run_event(prompt='new question', thread='thread-a')
        thread = next(e['threadId'] for e in events if e['type'] == 'system')
        self.assertNotIn(thread, ('thread-a', 'thread-b'))
        final = next(e['content'] for e in events if e['type'] == 'final_text')
        self.assertIn('reviewed continuity receipt', final)
        self.assertNotIn('DO_NOT_EXPORT', final)
        self.assertEqual(self.t.store.turn_log('nyarlathotep'), [])
        self.assertEqual(len(self.t.store.q('SELECT * FROM channel_events')), 1)
        print('CONVERT_IMPORT_SERVER ' + json.dumps({'fixture': True, 'source_threads': 2,
              'imported_turns': 2, 'duplicate_import': False, 'new_thread': thread,
              'pre_ingress_history_denied': True, 'bundle_sha256': digest}), flush=True)
