"""手动原生接入检查：真实 MCP 调用 + 关闭进程后按同一 session 恢复；会使用已登录模型额度。
运行 python3 tests/native_smoke.py codex|claude。不在默认测试中运行。
"""
import asyncio
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from helpers import Harness

TURNS = ['This is a read-only harness integration test. Call the cthulhu MCP list_tasks tool once, then reply with CTHULHU_OK. '
         'Do not create tasks, modify files, or run shell commands. Remember the word silverfish.',
         'Second read-only turn after a process restart. Do not use tools. Reply with the word you were asked to remember.']


async def main(backend):
    with tempfile.TemporaryDirectory(prefix='cthulhu-smoke-') as tmp:
        h = Harness(Path(tmp), backend=backend)
        c = h.c
        try:
            for i, text in enumerate(TURNS):
                with c.store.atomic(): c.enqueue('owner', 'master', text)
                await h.settle(seconds=180)
                reply = [e['text'] for e in c.store.events(limit=500) if e['kind'] == 'chat'][-1]
                print(backend, 'turn', i + 1, repr(reply), flush=True)
                session = c.store.get('agent', 'master')['session']
                if i == 0:
                    first = session
                    assert c.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='harness_tool' AND text='list_tasks'").fetchone()[0], 'MCP 工具未被调用'
                else:
                    assert session == first, 'session 变化'
                    assert 'silverfish' in reply.lower(), '原生历史未恢复'
            print('PASS: MCP 往返 + 同一 session 恢复', flush=True)
        finally:
            await h.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
