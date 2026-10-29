"""节点本机可用模型的发现：网页按节点显示真实可选的模型，而不是写死的一两个名字。

- Codex：app-server 的 model/list 只查目录、不调用模型，并带每个模型支持的推理强度。
- Claude：CLI 没有列模型的命令，只认别名（fable/opus/sonnet/haiku 指向各自最新版）或完整名。
  这里对每个别名启动一次 -p，读到 init 事件里解析出的完整模型名后立即结束进程组，
  不等模型回复；不落会话文件、不加载用户设置与 hooks，避免在主人的会话列表里留下探测记录。
不同机器的账号与 CLI 版本可能不同，所以每个节点各自发现、各自上报。
"""
import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path

CLAUDE_ALIASES = ('fable', 'opus', 'sonnet', 'haiku')
TIMEOUT = 60
REFRESH = 6 * 3600     # 模型目录变化很慢；失败时下一轮再试，保留上一次成功的结果


async def _kill(proc):
    try: os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    await proc.wait()


async def codex_models(env, timeout=TIMEOUT, cwd=None):
    with tempfile.TemporaryDirectory() as d:
        proc = await asyncio.create_subprocess_exec('codex', 'app-server', '--listen', 'stdio://', cwd=d, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=16 * 1024 * 1024)

        async def rpc(ident, method, params):
            proc.stdin.write((json.dumps({'id': ident, 'method': method, 'params': params}) + '\n').encode())
            await proc.stdin.drain()
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise RuntimeError('codex app-server 提前退出')
                frame = json.loads(line)
                if frame.get('id') == ident and 'method' not in frame:
                    if 'error' in frame:
                        raise RuntimeError(str(frame['error'])[:200])
                    return frame.get('result', {})

        async def talk():
            await rpc(1, 'initialize', {'clientInfo': {'name': 'cthulhu_v4', 'version': '0.1.0'},
                                        'capabilities': {'experimentalApi': True}})
            proc.stdin.write(b'{"method": "initialized"}\n')
            out, cursor, ident = [], None, 2
            while True:
                page = await rpc(ident, 'model/list', {'cursor': cursor} if cursor else {})
                out += page.get('data') or []
                cursor, ident = page.get('nextCursor'), ident + 1
                if not cursor:
                    return out
        try:
            data = await asyncio.wait_for(talk(), timeout)
        finally:
            await _kill(proc)
    return [{'id': m['id'], 'name': m.get('displayName') or m['id'], 'default': bool(m.get('isDefault')),
             'efforts': [e['reasoningEffort'] for e in m.get('supportedReasoningEfforts') or [] if e.get('reasoningEffort')],
             'default_effort': m.get('defaultReasoningEffort')}
            for m in data if m.get('id') and not m.get('hidden')]


async def claude_resolve(alias, env, timeout=TIMEOUT, cwd=None):
    # Claude 按 cwd 在 ~/.claude/projects 下建目录：用固定目录，否则每次探测都多留一个空目录。
    with tempfile.TemporaryDirectory() as tmp:
        d = cwd or tmp
        proc = await asyncio.create_subprocess_exec(
            'claude', '-p', '--model', alias, '--output-format', 'stream-json', '--verbose', '--max-turns', '1',
            '--no-session-persistence', '--setting-sources', '', '--strict-mcp-config', cwd=d, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True, limit=16 * 1024 * 1024)

        async def init():
            proc.stdin.write(b'ok'); proc.stdin.close()
            async for line in proc.stdout:
                try: ev = json.loads(line)
                except ValueError: continue
                if ev.get('type') == 'system' and ev.get('subtype') == 'init':
                    return ev.get('model')
            raise RuntimeError('claude 未输出 init 事件')
        try:
            return await asyncio.wait_for(init(), timeout)
        finally:
            # init 在请求模型之前输出：此时结束进程组，不等也不需要模型回复。
            await _kill(proc)


async def claude_models(env, timeout=TIMEOUT, cwd=None):
    found = await asyncio.gather(*(claude_resolve(a, env, timeout, cwd) for a in CLAUDE_ALIASES), return_exceptions=True)
    out = [{'id': m, 'name': f'{a} → {m}', 'alias': a} for a, m in zip(CLAUDE_ALIASES, found) if isinstance(m, str) and m]
    if not out:
        raise RuntimeError('没有解析出任何 Claude 别名：' + '; '.join(str(e)[:80] for e in found if isinstance(e, BaseException)))
    return out


DISCOVER = {'codex': codex_models, 'claude': claude_models}


async def discover(backends, env, previous=None, cwd=None):
    """返回 {backend: {'list': [...], 'at': 时间, 'error': 文字}}。某个后端失败时保留上次成功的列表。"""
    previous, out = previous or {}, {}
    if cwd:
        Path(cwd).mkdir(parents=True, exist_ok=True)
    for b in backends:
        if b not in DISCOVER:
            continue
        try:
            out[b] = {'list': await DISCOVER[b](env, cwd=cwd), 'at': time.time(), 'error': None}
        except Exception as e:
            old = previous.get(b) or {}
            out[b] = {'list': old.get('list') or [], 'at': old.get('at'), 'error': (str(e) or type(e).__name__)[:300]}
    return out


def clean(raw):
    """中心收到节点上报时的校验：只留已知字段与合理长度，节点文字不能撑大快照。"""
    out = {}
    for b, info in (raw or {}).items():
        if b not in DISCOVER or not isinstance(info, dict):
            continue
        items = []
        for m in (info.get('list') or [])[:64]:
            if not isinstance(m, dict) or not isinstance(m.get('id'), str) or not m['id'] or len(m['id']) > 100:
                continue
            item = {'id': m['id'], 'name': str(m.get('name') or m['id'])[:120]}
            if isinstance(m.get('efforts'), list):
                item['efforts'] = [e for e in m['efforts'] if isinstance(e, str) and len(e) <= 16][:10]
            for k in ('default_effort', 'alias'):
                if isinstance(m.get(k), str) and len(m[k]) <= 32:
                    item[k] = m[k]
            if m.get('default') is True:
                item['default'] = True
            items.append(item)
        at = info.get('at')
        out[b] = {'list': items, 'at': at if isinstance(at, (int, float)) else None,
                  'error': str(info['error'])[:300] if info.get('error') else None}
    return out
