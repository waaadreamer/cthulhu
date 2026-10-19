"""原生协议夹具：真实子进程经本机 HTTP/MCP、WebSocket 调用中心；不调用模型。"""
import json
import os
import time
import urllib.request


def emit(value): print(json.dumps(value), flush=True)

for line in __import__('sys').stdin:
    frame = json.loads(line)
    method = frame.get('method')
    if method == 'initialize': emit({'id': frame['id'], 'result': {}})
    elif method in ('thread/start', 'thread/resume'):
        assert frame['params']['sandbox'] == 'danger-full-access'
        assert frame['params']['approvalPolicy'] == 'never'
        emit({'id': frame['id'], 'result': {'thread': {'id': frame['params'].get('threadId', 'remote-session')}, 'approvalPolicy': 'never', 'sandbox': {'type': 'dangerFullAccess'}}})
    elif method == 'turn/start':
        assert frame['params']['sandboxPolicy'] == {'type': 'dangerFullAccess'}
        assert frame['params']['approvalPolicy'] == 'never'
        emit({'id': frame['id'], 'result': {}})
        req = urllib.request.Request(os.environ['CTHULHU_URL']+'/internal/call',
            data=json.dumps({'name':'list_tasks','arguments':{}}).encode(),
            headers={'Authorization':'Bearer '+os.environ['CTHULHU_TOKEN'],'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            assert json.load(response)['tasks']
        prompt = frame['params']['input'][0]['text']
        text = json.loads(prompt.split('\n',1)[1])['message']['text']
        if 'BLOCK' in text:
            emit({'method':'test/blocked','params':{}})
            time.sleep(60)
        emit({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'已收到新消息'}}})
        emit({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
