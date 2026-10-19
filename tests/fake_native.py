"""协议夹具：只模拟进程协议，不模拟真实模型质量。"""
import json
import sys

backend = sys.argv[1]
def send(x): print(json.dumps(x), flush=True)
for line in sys.stdin:
    f = json.loads(line)
    if backend == 'codex':
        method = f.get('method')
        if method == 'initialize': send({'id': f['id'], 'result': {}})
        elif method in {'thread/start', 'thread/resume'}:
            assert f['params']['sandbox'] == 'danger-full-access'
            assert f['params']['approvalPolicy'] == 'never'
            # 恢复长会话时回执里带整段历史，实测单行可超过 16MB。
            history = [{'type': 'agentMessage', 'text': 'x' * 1000}] * (20000 if f['params'].get('threadId') else 0)
            send({'id': f['id'], 'result': {'history': history, 'thread': {'id': f['params'].get('threadId', 'native-codex-1')}, 'model': f['params'].get('model'), 'reasoningEffort': f['params'].get('config', {}).get('model_reasoning_effort'), 'approvalPolicy': 'never', 'sandbox': {'type': 'dangerFullAccess'}}})
        elif method == 'turn/start':
            assert f['params']['approvalPolicy'] == 'never'
            assert f['params']['sandboxPolicy'] == {'type': 'dangerFullAccess'}
            if f['params'].get('input', [{}])[0].get('text') == 'require-medium':
                assert f['params']['effort'] == 'medium'
            send({'id': f['id'], 'result': {'turn': {'id': 'turn-1'}}})
            send({'method': 'item/started', 'params': {'item': {'type': 'commandExecution', 'command': 'fixture only'}}})
            send({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'commentary', 'text': 'progress'}}})
            send({'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'native result'}}})
            send({'method': 'turn/completed', 'params': {'turn': {'id': 'turn-1', 'status': 'completed'}}})
    else:
        # 使用传入 session 标识，验证适配器确实持续使用一个原生会话。
        send({'type': 'system', 'subtype': 'init', 'session_id': f['session_id']})
        send({'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'text': 'progress'}}})
        send({'type': 'result', 'result': 'native result', 'is_error': False})
