#!/usr/bin/env python3
"""codex / claude 模型目录夹具：只模拟 model/list 与 init 事件，不模拟模型本身。

以 PATH 上的 codex、claude 名字被调用；FAKE_MODELS_MODE 控制失败场景。
"""
import json
import os
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
mode = os.environ.get('FAKE_MODELS_MODE', '')
log = os.environ.get('FAKE_MODELS_LOG')
if log:
    with open(log, 'a') as f:
        f.write(json.dumps({'cli': name, 'args': sys.argv[1:], 'cwd': os.getcwd()}) + '\n')

if name == 'codex':
    if 'codex-fail' in mode:
        sys.exit(2)
    pages = [[{'id': 'gpt-a', 'displayName': 'GPT-A', 'isDefault': True, 'hidden': False, 'defaultReasoningEffort': 'low',
               'supportedReasoningEfforts': [{'reasoningEffort': e} for e in ('low', 'medium', 'ultra')]},
              {'id': 'gpt-hidden', 'hidden': True, 'supportedReasoningEfforts': []}],
             [{'id': 'gpt-b', 'displayName': 'GPT-B', 'hidden': False,
               'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'xhigh'}]}]]
    for line in sys.stdin:
        f = json.loads(line)
        if 'id' not in f:
            continue
        if f['method'] == 'initialize':
            print(json.dumps({'id': f['id'], 'result': {}}), flush=True)
        elif f['method'] == 'model/list':
            # 先发一条通知，确认适配器按 id 取回执而不是取第一行。
            print(json.dumps({'method': 'thread/started', 'params': {}}), flush=True)
            page = 1 if f['params'].get('cursor') == 'p2' else 0
            print(json.dumps({'id': f['id'], 'result': {'data': pages[page], 'nextCursor': 'p2' if page == 0 else None}}), flush=True)
else:
    alias = sys.argv[sys.argv.index('--model') + 1]
    assert '--no-session-persistence' in sys.argv, '探测不能在主人的会话列表里留下记录'
    sys.stdin.read()
    if 'claude-partial' in mode and alias == 'haiku':
        sys.exit(1)
    print(json.dumps({'type': 'system', 'subtype': 'hook_started'}), flush=True)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': f'claude-{alias}-9-9'}), flush=True)
    # 真 CLI 在 init 之后才请求模型；探测应当在这里结束进程，不等回复。
    time.sleep(30)
    print(json.dumps({'type': 'assistant', 'message': 'should not be read'}), flush=True)
