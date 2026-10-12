"""Run browser assertions against an isolated real Temple (node + Playwright required)."""
import asyncio
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cthulhu.temple import Temple, password_hash
from test_universe import free_port


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        t = Temple({'secret': secrets.token_hex(16), 'db': tmp + '/db',
                    'galaxies': [{'id': 'test', 'name': 'Test', 'token': secrets.token_hex(16)}],
                    'gods': [{'id': 'az', 'role': 'azathoth', 'galaxy': 'test'}, {'id': 'nyarlathotep', 'role': 'nyarlathotep', 'galaxy': 'test'}]})
        channel_row, _ = t.store.channel_accept('nyarlathotep', 'fixture-app', 'fixture-event', 'fixture-scope', {'prompt': '迁移后的飞书入口', 'text': '迁移后的飞书入口'})
        channel_mid = channel_row['message_id']
        t.store.update_turn(channel_mid, reply='奈亚接入测试回复', status='done')
        t.store.x('UPDATE channel_events SET receipt=? WHERE message_id=?', (json.dumps({'state':'sending'}),channel_mid))
        salt = secrets.token_hex(16)
        t.store.set_account('owner', salt, password_hash('test-password', salt))
        for tid, title in [('task_a', '分组任务甲'), ('task_b', '分组任务乙')]:
            t.store.x('INSERT INTO tasks(id,title,state,created_at,updated_at) VALUES (?,?,?,?,?)', (tid,title,'open',100,100))
        for i in range(108):
            t.store.x('INSERT INTO tasks(id,title,state,created_at,updated_at) VALUES (?,?,?,?,?)',
                      (f'history_{i:03}', f'历史任务 {i:03}', 'done' if i%2 == 0 else 'open', 1, 1))
        def turn(tid, task, text, stamp, user=''):
            t.store.add_turn(tid, 'az', user)
            t.store.update_turn(tid, task_id=task, reply=text, kind='progress' if task else None, status='done')
            t.store.x('UPDATE chat_turns SET created_at=?,updated_at=? WHERE id=?', (stamp,stamp,tid))
        turn('a1','task_a','甲第一条',10)
        turn('b1','task_b','乙第一条',20)
        turn('owner1',None,'独立主人回复',25,'独立主人消息')
        turn('a2','task_a','甲第二条',30)
        for i in range(130):
            m=t.store.add_message('az','worker',f'甲消息 {i:03}',task_id='task_a')
            t.store.x('UPDATE messages SET created_at=2,state=\'handled\' WHERE id=?',(m['id'],))
        m=t.store.add_message('az','worker','旧任务唯一消息',task_id='history_000');t.store.mark(m['id'],'handled')
        t.store.upsert_agent('worker','长提示执行者','old_one','test','az',{'task': '\n'.join(['长职责内容 '*20]*400)})
        cfg={'backend':'codex','model':'gpt-6-astra'}
        t.status['worker']={'state':'working','runtime':cfg | {'config':cfg,'default':cfg,'fallback_active':False,
            'recovery':{'phase':'retrying','attempt':3,'retry':2,'retries':5,'source':'codex:gpt-6-astra','fallback':False}}}
        server=uvicorn.Server(uvicorn.Config(t.app(),host='127.0.0.1',port=port,log_level='error'))
        serve=asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(.05)
        proc=await asyncio.create_subprocess_exec('node',str(Path(__file__).with_suffix('.mjs')),str(port),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            async for line in proc.stdout:
                text=line.decode().strip()
                print(text,flush=True)
                if text == 'RECOVERY_DONE':
                    status=t.status['worker']
                    status['runtime']['recovery'].update(phase='completed',attempt=7,retry=0,source='codex:gpt-6.1-sol',fallback=True)
                    status['runtime'].update(model='gpt-6.1-sol',fallback_active=True)
                    await t.broadcast({'type':'status','agent_id':'worker','status':status})
                if text == 'FEISHU_RECEIPT':
                    t.store.x('UPDATE channel_events SET receipt=? WHERE message_id=?', (json.dumps({'state':'sent','message_id':'fixture-platform-receipt'}),channel_mid))
                    await t.broadcast({'type':'chat','turn':t.store.turn(channel_mid)})
                if text == 'STREAM_REQUEST':
                    turn('b2','task_b','',40)
                    t.store.update_turn('b2',status='running')
                    await t.broadcast({'type':'chat','turn':t.store.turn('b2')})
                    await t.broadcast({'type':'stream','agent_id':'az','turn_id':'b2','delta':'实时新增进展','activity':['只读收证']})
                if text == 'FINISH_REQUEST':
                    t.store.update_turn('b2',status='done',reply='实时新增进展，已收证')
                    await t.broadcast({'type':'chat','turn':t.store.turn('b2')})
            code=await proc.wait()
            if code:
                raise RuntimeError(f'browser assertions failed: {code}')
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            server.should_exit=True
            await serve
            t.store.db.close()


if __name__=='__main__':
    asyncio.run(main())
