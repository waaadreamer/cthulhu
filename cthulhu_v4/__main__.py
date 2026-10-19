"""入口：单进程锁防止两个服务同时调度同一状态库。"""
import argparse
import asyncio
import fcntl
import os
import signal
import ssl
import threading
from pathlib import Path

from .core import Core
from .web import Web


async def main(args):
    os.umask(0o077)
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'server.lock').open('w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('该状态目录已有服务运行')
    core = Core(state, args.backend, args.model, heartbeat=args.heartbeat,
                review_interval=args.review_interval, question_timeout=args.question_timeout, reasoning_effort=args.reasoning_effort)
    if args.activate_demo_master:
        core.activate_codex_master(args.model, args.reasoning_effort)
    if core.store.get('agent', 'master')['backend'] != args.backend:
        raise SystemExit('已有会话后端与 --backend 不同。请使用独立 --state，不能静默切换原生会话。')
    for ident in args.node:
        path = core.galaxies.provision(ident)
        print(f'节点 {ident} 凭证文件：{path}', flush=True)
    ws_server = None
    if args.ws_port is not None:
        from websockets.asyncio.server import serve
        from .galaxy import MAX_FRAME
        tls = None
        if args.tls_cert:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(args.tls_cert, args.tls_key)
        ws_server = await serve(core.galaxies.handle, args.ws_host, args.ws_port,
                                ssl=tls, max_size=MAX_FRAME, origins=[None])
        print(f'Galaxy WebSocket: {args.ws_host}:{ws_server.sockets[0].getsockname()[1]}', flush=True)
    loop = asyncio.get_running_loop()
    web = Web(core, loop, args.port, args.public_origin)
    thread = threading.Thread(target=web.server.serve_forever, daemon=True); thread.start()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(sig, stop.set)
    print(f'Cthulhu v4 ({args.backend}): {core.url}\n访问 token 文件：{state / "web-token"}', flush=True)
    scheduler = asyncio.create_task(core.serve())
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([scheduler, stopper], return_when=asyncio.FIRST_COMPLETED)
        if scheduler in done: scheduler.result()
    finally:
        scheduler.cancel(); stopper.cancel()
        await core.close()
        if ws_server:
            ws_server.close(); await ws_server.wait_closed()
        await asyncio.gather(scheduler, stopper, return_exceptions=True)
        await asyncio.to_thread(web.server.shutdown)
        web.server.server_close()
        core.store.db.close()
        lock.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Cthulhu v4 中心服务（默认模拟，可启用星系 WebSocket）')
    p.add_argument('--backend', choices=['demo', 'codex', 'claude'], default='demo')
    p.add_argument('--node', action='append', default=[], help='注册节点 ID，可重复；凭证写入状态目录')
    p.add_argument('--ws-port', type=int, help='启用星系 WebSocket 监听，例如 8770')
    p.add_argument('--public-origin', action='append', default=[], help='允许的网页公网来源，例如 https://47.115.202.248:8001')
    p.add_argument('--ws-host', default='127.0.0.1')
    p.add_argument('--tls-cert')
    p.add_argument('--tls-key')
    p.add_argument('--activate-demo-master', action='store_true', help='明确将已有模拟 Master 切为 Codex，保留节点和业务状态')
    p.add_argument('--reasoning-effort', choices=['low','medium','high','xhigh','max','ultra'], help='Codex 推理强度，显式覆盖原生默认值')
    p.add_argument('--model', help='不指定时使用原生 CLI 配置的模型')
    p.add_argument('--state', default='.state-demo')
    p.add_argument('--port', type=int, default=8767)
    p.add_argument('--heartbeat', type=int, default=60)
    p.add_argument('--review-interval', type=int, default=600)
    p.add_argument('--question-timeout', type=int, default=300)
    args = p.parse_args()
    if min(args.heartbeat, args.review_interval, args.question_timeout) <= 0: p.error('时间间隔必须大于零')
    if bool(args.tls_cert) != bool(args.tls_key): p.error('TLS 证书与私钥必须一起提供')
    if args.activate_demo_master and (args.backend != 'codex' or not args.model or not args.reasoning_effort):
        p.error('--activate-demo-master 需要 --backend codex、--model 和 --reasoning-effort')
    asyncio.run(main(args))
