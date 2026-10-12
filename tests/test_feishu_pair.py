"""No live connection or credentials: exercise pairing with SDK-shaped events."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = Path('/opt/cthulhu-feishu/node_modules/node/bin/node')


@unittest.skipUnless(NODE.exists(), 'Node deployment dependency not installed')
class Pairing(unittest.TestCase):
    def test_one_shot_ready_strict_scope_expiry_ambiguity_and_no_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = '''
import assert from 'node:assert/strict';
import {capture,candidate} from PAIR_MODULE;
import {readFile,stat} from 'node:fs/promises';
import {normalize} from '/opt/cthulhu-feishu/node_modules/@larksuite/channel/dist/index.mjs';
const dir = OUTPUT_DIR;
function event(phrase, at=Date.now(), sender='owner', chat='chat') {
 return {messageId:'message',chatId:chat,chatType:'p2p',senderId:sender,senderType:'user',
  rawContentType:'text',content:phrase,createTime:at,
  raw:{sender:{sender_type:'user',sender_id:{open_id:sender}},
       message:{message_id:'message',chat_id:chat,chat_type:'p2p',message_type:'text',
                create_time:String(at),content:JSON.stringify({text:phrase})}}};
}
const now=Date.now(), phrase='test phrase';
assert(candidate(event(phrase,now),phrase,now-1,now+100,now));
const normalized=await normalize(event(phrase,now).raw,{includeRaw:true});
assert(candidate(normalized,phrase,now-1,now+100,now));
for (const msg of [event('wrong',now),event(phrase,now-10),event(phrase,now+10),
 {...event(phrase,now),chatType:'group'}, {...event(phrase,now),senderType:'bot'},
 {...event(phrase,now),threadId:'topic'}, {...event(phrase,now),rawContentType:'post'},
 {...event(phrase,now),senderId:'forged'}]) {
 assert.equal(candidate(msg,phrase,now-1,now+100,now),null);
}
assert.equal(candidate(event(phrase,now),phrase,now-1,now+1,now+2),null);
class Channel {
 handlers={};connected=false;disconnected=false;
 on(name,fn){this.handlers[name]=fn;}
 async connect(){this.connected=true;this.handlers.message(event('pre-ready'));}
 async disconnect(){this.disconnected=true;}
 send(){throw new Error('pairing must never send');}
}
const channel=new Channel(); let ready;
const result=await capture(channel,{appId:'app',output:dir+'/evidence.json',ttl:1000,settle:5,
 ready: async r=>{ready=r;assert(channel.connected);assert(r.expires_at>r.ready_at);
   channel.handlers.message(event(r.phrase));channel.handlers.message(event(r.phrase));}});
assert(channel.disconnected); assert.equal(result.sender_id,'owner');
assert.equal(result.legacy_scope_id,'chat'); assert.equal((await stat(dir+'/evidence.json')).mode&0o777,0o600);
assert(!String(await readFile(dir+'/evidence.json')).includes(ready.phrase));
const conflict=new Channel();
await assert.rejects(capture(conflict,{appId:'app',output:dir+'/conflict.json',ttl:1000,settle:5,
 ready:async r=>{conflict.handlers.message(event(r.phrase));conflict.handlers.message(event(r.phrase,Date.now(),'other'));}}),/ambiguous/);
await assert.rejects(stat(dir+'/conflict.json'));
const expired=new Channel();
await assert.rejects(capture(expired,{appId:'app',output:dir+'/expired.json',ttl:5,
 ready:async()=>{}}),/expired/);
assert(expired.disconnected);
const failed=new Channel();failed.connect=async()=>{throw new Error('handshake');};
await assert.rejects(capture(failed,{appId:'app',output:dir+'/failed.json',ready:async()=>{throw new Error('should not be ready');}}),/handshake/);
assert(failed.disconnected);
console.log(JSON.stringify({single_match:true,ambiguity_rejected:true,expired_rejected:true,no_outbound:true,no_pre_handshake_challenge:true}));
'''.replace('PAIR_MODULE', json.dumps((ROOT/'integrations/feishu/pair.mjs').as_uri())).replace('OUTPUT_DIR', json.dumps(tmp))
            p = subprocess.run([str(NODE), '--input-type=module', '-e', script], capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue(json.loads(p.stdout)['no_outbound'])

    def test_missing_credentials_or_stop_receipt_does_not_create_challenge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'temple-feishu.disabled.json').write_text(json.dumps({'feishu': {'enabled': False, 'worker': {'enabled': False}}}))
            (root/'config.json').write_text(json.dumps({'profiles': {'nyarl': {'accounts': {'app': {'id': 'fixture-app', 'tenant': 'feishu'}}}}}))
            p = subprocess.run([str(NODE), str(ROOT/'integrations/feishu/pair.mjs'), '--root',tmp,
                '--install-root','/opt/cthulhu-feishu','--stop-receipt',str(root/'missing.json')], capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertNotIn('phrase', p.stdout)
            self.assertFalse((root/'pairing-ready.json').exists())
            self.assertFalse((root/'pairing-evidence.json').exists())
