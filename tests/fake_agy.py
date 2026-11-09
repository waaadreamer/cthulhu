"""agy ACP 协议夹具，不调用模型，不证明模型质量或渠道可用性。"""
import json
import os
import sys
from pathlib import Path

session = 'agy-session'
argv = sys.argv[1:]
mode = os.environ.get('FAKE_AGY_MODE', '')
if 'models' in argv:
    print('gemini-4-argon-high\tGemini protocol fixture'); sys.exit(0)


def send(value):
    print(json.dumps({'jsonrpc': '2.0', **value}), flush=True)


def update(value, sid=None):
    send({'method': 'session/update', 'params': {'sessionId': sid or session, 'update': value}})


models = {'availableModels': [{'modelId': 'gemini-4-argon-high', 'name': 'Gemini'}], 'currentModelId': 'gemini-4-argon-high'}
options = [{'id': 'model', 'currentValue': 'gemini-4-argon'}, {'id': 'thought_level', 'currentValue': 'high'}]
for line in sys.stdin:
    f = json.loads(line)
    method, p = f.get('method'), f.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True,
                  '_meta': {'antigravity': {'systemPrompt': {'append': True}}}}}
    elif method == 'session/new':
        assert p['_meta']['antigravity']['systemPrompt']['append']
        assert p['mcpServers'][0]['env'] and p['mcpServers'][0]['name'] == 'cthulhu'
        result = {'sessionId': session, 'models': models, 'configOptions': options}
    elif method == 'session/load':
        assert p['sessionId'] == session
        if mode == 'load-fails':
            send({'id': f['id'], 'error': {'code': -32000, 'message': 'session missing'}})
            continue
        update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'old history'}})
        result = {'models': models, 'configOptions': options}
    elif method == 'session/set_model':
        assert p['modelId'] == 'gemini-4-argon-high'
        update({'sessionUpdate': 'config_option_update', 'configOptions': options})
    elif method == 'session/prompt':
        text = p['prompt'][0]['text']
        if text == 'rpc-error':
            send({'id': f['id'], 'error': {'code': -32603, 'message': 'secret-key request failed'}})
            continue
        if text in ('fatal', 'fatal-nolog'):
            if text == 'fatal':
                with Path(argv[argv.index('--log-file') + 1]).open('a') as log:
                    log.write('agent executor error: HTTP 400 protocol unsupported secret-key\n')
            update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': '\n⚠ Agent execution terminated due to error.\n'}})
        else:
            update({'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'The CLI may report: ⚠ Agent execution terminated due to error.' if text == 'quote' else 'native result'}},
                   'wrong-session' if text == 'wrong-session' else None)
        result = {'stopReason': 'cancelled' if text == 'cancelled' else 'end_turn'}
    send({'id': f['id'], 'result': result})
