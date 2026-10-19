"""手动原生接入检查：只读列任务，两轮恢复同一会话；会使用已登录模型额度。
运行 python3 tests/native_smoke.py codex|claude。
不在默认测试中运行，避免把模型服务可用性混为代码正确性。
"""
import asyncio
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cthulhu_v4.core import Core
from cthulhu_v4.runtime import make_runtime
from cthulhu_v4.web import Web


async def main(backend):
    with tempfile.TemporaryDirectory(prefix='cthulhu-v4-smoke-') as tmp:
        c = Core(tmp, backend)
        web = Web(c, asyncio.get_running_loop(), 0)
        thread = threading.Thread(target=web.server.serve_forever, daemon=True); thread.start()
        rt = None
        try:
            for attempt in range(2):
                rt = make_runtime(c, c.store.get('agent', 'master'))
                text = ('This is a read-only harness integration test. Discover and call the cthulhu MCP server list_tasks tool, then report the task count. '
                        'Tool discovery is allowed. Do not create tasks, modify files, execute shell commands, or use unrelated tools. '
                        'Include CTHULHU_OK and the actual task count in your reply. Remember the word silverfish.' if attempt == 0 else
                        'This is the second read-only integration turn after process restart. '
                        'Do not use any tools. Reply with the word you were asked to remember in the previous turn.')
                result = await asyncio.wait_for(rt.run(text), 120)
                print(backend, 'turn', attempt + 1, repr(result), flush=True)
                session = c.store.get('agent', 'master')['session']
                if attempt == 0:
                    first = session
                    calls = c.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='harness_tool' AND text='list_tasks'").fetchone()[0]
                    if not calls:
                        print('Tool/debug events:', [(r[0], r[1][:800]) for r in c.store.db.execute("SELECT kind,data FROM events WHERE kind LIKE '%mcp%' OR kind='native:stderr'")], flush=True)
                    if not calls:
                        print('Completed items:', [(r[0], r[1][:1500]) for r in c.store.db.execute("SELECT kind,data FROM events WHERE kind='native:item/completed'")], flush=True)
                    assert calls, 'MCP tool not called'
                else:
                    assert session == first, 'session changed'
                    assert 'silverfish' in result.lower(), 'native history not restored'
                await rt.close(); rt = None
            print('PASS: MCP roundtrip + same-session recovery', flush=True)
        finally:
            if rt: await rt.close()
            await asyncio.to_thread(web.server.shutdown); web.server.server_close(); thread.join()
            c.store.db.close()


if __name__ == '__main__': asyncio.run(main(sys.argv[1]))
