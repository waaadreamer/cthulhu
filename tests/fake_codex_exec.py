"""codex exec 协议夹具：只模拟命令行与产物格式，不模拟真实模型的提炼质量。"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
prompt = sys.stdin.read()
opt = lambda name: args[args.index(name) + 1] if name in args else None
log = os.environ.get('FAKE_CODEX_LOG')
mode = os.environ.get('FAKE_CODEX_MODE', '')
if log:
    with open(log, 'a') as f:
        f.write(json.dumps({'args': args, 'prompt': prompt}, ensure_ascii=False) + '\n')
if '--output-schema' in args:
    if 'stage1-fail' in mode and '失败段' in prompt:
        sys.exit(3)
    Path(opt('-o')).write_text(json.dumps({'raw_memory': '记住：' + prompt.split('filtered response items):', 1)[-1].split('IMPORTANT:', 1)[0].strip()[-200:], 'rollout_summary': '# 摘要',
                                           'rollout_slug': 'demo slug'}, ensure_ascii=False))
else:
    root = Path(opt('-C'))
    assert (root / 'phase2_workspace_diff.md').exists(), '阶段 2 应先看到增量 diff'
    (root / 'MEMORY.md').write_text('# Task Group: demo\n' + (root / 'raw_memories.md').read_text()[-200:])
    first = 'bad' if 'phase2-bad' in mode else 'v1'
    (root / 'memory_summary.md').write_text(first + '\n\n## User preferences\n- 先给结论\n')
