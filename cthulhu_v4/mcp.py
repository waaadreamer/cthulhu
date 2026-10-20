"""最小 MCP stdio bridge。只负责协议与 HTTP 转发，无业务状态。

Codex/Claude 都启动此脚本；stdout 只允许 JSON-RPC。身份由服务端 token 映射，
不接受工具参数自报身份。单机同 OS 用户不是恶意 agent 的安全隔离边界。
"""
import json
import os
import sys
import urllib.request
import urllib.error


def request(path, data=None):
    req = urllib.request.Request(os.environ['CTHULHU_URL'] + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['CTHULHU_TOKEN']})
    try:
        with urllib.request.urlopen(req, timeout=160) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        # 让模型看到版本冲突/权限等业务错误，不只看到不透明的 HTTP 400。
        result = json.loads(e.read())
    if 'error' in result: raise ValueError(result['error'])
    return result


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
            if 'id' not in msg: continue
            method = msg.get('method')
            if method == 'initialize':
                result = {'protocolVersion': msg.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'cthulhu-v4', 'version': '0.1.0'}}
            elif method == 'ping': result = {}
            elif method == 'tools/list': result = request('/internal/tools')
            elif method == 'tools/call':
                try:
                    value = request('/internal/call', msg['params'])
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except Exception as e:
                    result = {'content': [{'type': 'text', 'text': str(e)}], 'isError': True}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(str(e), file=sys.stderr, flush=True)


if __name__ == '__main__': main()
