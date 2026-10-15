"""Real HTTP/WS browser verification for task progress (no external API calls)."""
import asyncio
import json
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uvicorn
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cthulhu.temple import Temple, password_hash
from test_universe import free_port

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        t = Temple({'secret': secrets.token_hex(16), 'db': tmp + '/db',
            'galaxies': [{'id': 'test', 'name': '测试星系', 'token': 'test'}],
            'gods': [{'id': 'az', 'role': 'azathoth', 'galaxy': 'test'}]})
        salt = secrets.token_hex(16)
        t.store.set_account('owner', salt, password_hash('test-password', salt))
        for tid, name in [('a', '部署界面修复'), ('b', '28节点训练性能')]:
            t.store.index_task(tid, 'test', '完整任务说明仅详情可见。' * 20, name=name)
            t.store.set_task(tid, state='running', executor='worker-' + tid)
        t.store.set_roster('test', [{'id': 'worker-' + tid, 'name': '旧日支配者' + tid, 'task_id': tid, 'state': 'working'} for tid in ('a', 'b')])
        async def tick(stamp, summary):
            with patch('cthulhu.temple.time.time', return_value=stamp):
                t.store.add_digest('a', '原始prompt不能公开', False, details={'progress': {'summary': summary, 'pace': 'ok', 'at': stamp}})
                t.store.add_digest('b', '原始工具流水不能公开', True, details={'progress': {'summary': '仍403，训练0步' if stamp == 12000 else '等待新的有效产物', 'pace': 'none', 'at': stamp}})
                await t.send_digest()
        await tick(12000, '完成部署按钮验证')
        t.task_command = AsyncMock(return_value={'task_id': 'a', 'state': 'running'})
        await t.handle_agent_call(SimpleNamespace(id='az'), 'continue_task', {'task_id': 'a', 'instruction': '补充回归验证', 'report_source': 'digest_20_a'})
        await t.note_owner('验证通过，下一步补充回归', task_id='a', source_message_id='digest_20_a')
        await t.note_owner('已核验训练完成30步，连接恢复；保持配置继续采样', task_id='b', source_message_id='digest_20_b')
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(t.app(), host='127.0.0.1', port=port, log_level='error'))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(.05)
        proc = await asyncio.create_subprocess_exec('node', str(Path(__file__).with_suffix('.mjs')), str(port), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            async for line in proc.stdout:
                text = line.decode().strip()
                print(text, flush=True)
                if text == 'PERIOD_TWO':
                    await tick(12600, '第二期新增回归验证')
                    await t.note_owner('迟到核验：按钮验证通过；回归指令已投递，等待结果', task_id='a', source_message_id='digest_20_a')
                    await t.broadcast({'type': 'chat', 'turn': t.store.turn('digest_20_a')})
                if text == 'RECONNECT':
                    await tick(13200, '重连期间新增验证')
            if await proc.wait():
                raise RuntimeError('browser assertions failed')
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()
            server.should_exit = True
            await serve
            t.store.db.close()

if __name__ == '__main__':
    asyncio.run(main())
