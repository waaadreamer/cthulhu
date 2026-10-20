"""命令行入口。

  cthulhu center [--backend codex --model … --public-url https://host/v4]
  cthulhu invite <节点名>            在中心机生成一行接入命令
  cthulhu node join <邀请>          在执行机兑换邀请（install.sh 调用）
  cthulhu node run                  运行执行节点（systemd --user 调用）
  cthulhu node status
"""
import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path

from .util import PROTOCOL


def lock_dir(path, name):
    """单进程锁：同一状态目录只能有一个调度者，避免并发恢复同一原生会话。"""
    path.mkdir(parents=True, exist_ok=True)
    handle = (path / name).open('w')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f'{path} 已有进程在运行')
    return handle


async def until_signal():
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


# ---- center ----
async def run_center(args):
    from .app import App
    os.umask(0o077)
    state = Path(args.state).resolve()
    lock = lock_dir(state, 'server.lock')
    try:
        app = App(state, backend=args.backend, model=args.model, reasoning_effort=args.reasoning_effort,
                  port=args.port, ws_host=args.ws_host, ws_port=args.ws_port, public_url=args.public_url,
                  galaxy_url=args.galaxy_url, tls_cert=args.tls_cert, tls_key=args.tls_key,
                  heartbeat=args.heartbeat, review_interval=args.review_interval,
                  question_timeout=args.question_timeout,
                  feishu_cli=args.feishu_cli, feishu_config=args.feishu_config, feishu_owner=args.feishu_owner,
                  model_proxy=args.model_proxy, git_proxy=args.git_proxy,
                  **({'install_url': args.install_url} if args.install_url else {}))
    except ValueError as e:
        raise SystemExit(str(e))
    await app.start()
    if app.notice:
        print(app.notice, flush=True)
    print(f'Cthulhu 中心已启动（{args.backend}）\n  网页：{app.public_url or app.web.url}\n'
          f'  访问凭证：{state / "web-token"}\n  星系：{app.center_ws_url()}', flush=True)
    try:
        await until_signal()
    finally:
        await app.stop(); lock.close()
    if getattr(app, 'deployer', None) and app.deployer.restart:
        # 已切换到新代码：用非零退出码让 systemd（Restart=on-failure）重新拉起。
        raise SystemExit(75)


def invite(args):
    state = Path(args.state).resolve()
    try:
        port = json.loads((state / 'center.json').read_text())['port']
        token = (state / 'web-token').read_text().strip()
    except OSError:
        raise SystemExit(f'没有找到运行中的中心（{state}）。先启动 cthulhu center')
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/invite', data=json.dumps({'name': args.name}).encode(),
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(json.load(e).get('error'))
    print('在执行机上，用将要运行 agent 的用户执行（24 小时内有效，只能用于一台机器）：\n')
    print(result['command'])


# ---- node ----
def home(args):
    return Path(args.home or os.environ.get('CTHULHU_HOME') or Path.home() / '.cthulhu').expanduser().resolve()


async def join(args):
    from .node import Node
    from .transport import RemoteLink, decode_invite
    os.umask(0o077)
    base = home(args)
    invite = decode_invite(args.invite)
    config_path = base / 'node.json'
    if config_path.exists():
        config = json.loads(config_path.read_text())
        if config['node'] == invite['node'] and config['center'] == invite['center']:
            # 重跑安装命令 = 升级；保留原身份、凭证和状态目录，不再消耗邀请。
            print(f"节点 {config['node']} 已加入，保留原身份与会话。", flush=True)
            return
        raise SystemExit(f"{base} 已属于节点 {config['node']}（{config['center']}）。"
                         '同一目录不能换身份；如需在本机再加一个节点，设置 CTHULHU_HOME 到新目录。')
    lock = lock_dir(base / 'state', 'node.lock')
    try:
        node = Node(base / 'state')
        instance = node.instance
        node.store.close()
    finally:
        lock.close()
    link = RemoteLink(invite['center'], invite['node'], pin=invite.get('pin'), hello={'instance': instance})
    try:
        token = await link.redeem(invite['code'])
    except Exception as e:
        raise SystemExit(f'接入失败：{e}')
    config = {'center': invite['center'], 'node': invite['node'], 'token': token, 'pin': invite.get('pin')}
    config_path.write_text(json.dumps(config, indent=2)); config_path.chmod(0o600)
    print(f"节点 {invite['node']} 已加入中心 {invite['center']}", flush=True)


async def run_node(args):
    from .bridge import Bridge
    from .node import Node
    from .transport import RemoteLink
    os.umask(0o077)
    base = home(args)
    try:
        config = json.loads((base / 'node.json').read_text())
    except OSError:
        raise SystemExit(f'{base}/node.json 不存在：先用网页生成的命令安装/加入')
    from . import update
    app = Path(__file__).resolve().parent.parent
    managed = app == (base / 'app').resolve()
    if managed and update.check_rollback(base, app):
        # 新代码连续多次没能连上中心：已换回旧代码，用旧代码重新启动。
        print('更新后的代码多次启动失败，已回滚到上一版本', flush=True)
        update.reexec(app)
    lock = lock_dir(base / 'state', 'node.lock')
    try:
        node = Node(base / 'state', capacity=args.capacity, protected=[base], app=app, managed=managed,
                    model_proxy=args.model_proxy or config.get('model_proxy'))
    except RuntimeError as e:
        raise SystemExit(str(e))
    node.link = RemoteLink(config['center'], config['node'], token=config['token'], pin=config.get('pin'),
                           hello={'instance': node.instance})
    # 第一次与中心同步成功才算新版本可用：此时清理旧代码与更新标记。
    node.on_synced = lambda: (info := update.confirm(base)) and print(f"更新已生效：{info['from']} → {info['to']}", flush=True)
    bridge = Bridge(node, asyncio.get_running_loop()); bridge.start()
    print(f"节点 {config['node']} 启动 · 后端 {', '.join(node.backends)} · 状态 {node.state_dir}", flush=True)
    runner = asyncio.create_task(node.run())
    stopper = asyncio.create_task(until_signal())
    try:
        done, _ = await asyncio.wait([runner, stopper], return_when=asyncio.FIRST_COMPLETED)
        if runner in done: runner.result()
    finally:
        runner.cancel(); stopper.cancel()
        await asyncio.gather(runner, stopper, return_exceptions=True)
        await node.close(); await node.link.close()
        await asyncio.to_thread(bridge.stop)
        node.store.close(); lock.close()
    if node.restart:
        update.reexec(app)


def status(args):
    base = home(args)
    path = base / 'node.json'
    if not path.exists():
        raise SystemExit('尚未加入任何中心')
    config = json.loads(path.read_text())
    print(f"节点 {config['node']} → {config['center']}\n状态目录 {base / 'state'}\n协议 {PROTOCOL}")


def main(argv=None):
    p = argparse.ArgumentParser(prog='cthulhu', description='Cthulhu v4：中心 + 星系节点')
    sub = p.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('center', help='运行中心（含本机执行节点）')
    c.add_argument('--state', default='.state')
    c.add_argument('--backend', choices=['demo', 'codex', 'claude'], default='demo')
    c.add_argument('--model', help='不指定时使用原生 CLI 配置的模型')
    c.add_argument('--reasoning-effort', choices=['low', 'medium', 'high', 'xhigh', 'max', 'ultra'])
    c.add_argument('--port', type=int, default=8767, help='网页端口，只绑定 127.0.0.1')
    c.add_argument('--ws-host', default='127.0.0.1')
    c.add_argument('--ws-port', type=int, default=8770)
    c.add_argument('--public-url', help='反向代理后的网页地址，如 https://host:8001/v4；星系地址默认为其 /galaxy')
    c.add_argument('--galaxy-url', help='显式指定节点连接地址（wss://…）')
    c.add_argument('--install-url', help='安装脚本地址（默认 GitHub v4 分支）')
    c.add_argument('--tls-cert'); c.add_argument('--tls-key')
    c.add_argument('--heartbeat', type=int, default=60)
    c.add_argument('--review-interval', type=int, default=600)
    c.add_argument('--question-timeout', type=int, default=300)
    c.add_argument('--model-proxy', help='模型 CLI 访问模型服务的代理，如 http://127.0.0.1:7897；agent 运行的命令不经过它')
    c.add_argument('--git-proxy', help='中心检查/拉取 GitHub 更新时使用的代理')
    c.add_argument('--feishu-cli', help='lark-cli 可执行文件；指定后中心订阅飞书消息并交给 Master')
    c.add_argument('--feishu-config', help='lark-cli 凭据目录（默认 <state>/feishu/lark-cli）')
    c.add_argument('--feishu-owner', help='主人的飞书 open_id（ou_…）；其他发送者按非主人处理')

    i = sub.add_parser('invite', help='生成节点接入命令（需中心在运行）')
    i.add_argument('name'); i.add_argument('--state', default='.state')

    n = sub.add_parser('node', help='执行节点')
    nsub = n.add_subparsers(dest='node_cmd', required=True)
    j = nsub.add_parser('join'); j.add_argument('invite'); j.add_argument('--home')
    r = nsub.add_parser('run'); r.add_argument('--home'); r.add_argument('--capacity', type=int, default=2)
    r.add_argument('--model-proxy', help='该节点上模型 CLI 的代理；也可写在 node.json 的 model_proxy')
    st = nsub.add_parser('status'); st.add_argument('--home')

    args = p.parse_args(argv)
    if args.cmd == 'center':
        if bool(args.tls_cert) != bool(args.tls_key): p.error('TLS 证书与私钥必须一起提供')
        if (args.feishu_config or args.feishu_owner) and not args.feishu_cli: p.error('飞书参数需要同时指定 --feishu-cli')
        if min(args.heartbeat, args.review_interval, args.question_timeout) <= 0: p.error('时间间隔必须大于零')
        asyncio.run(run_center(args))
    elif args.cmd == 'invite':
        invite(args)
    elif args.node_cmd == 'join':
        asyncio.run(join(args))
    elif args.node_cmd == 'run':
        if not 1 <= args.capacity <= 32: p.error('capacity 必须为 1..32')
        asyncio.run(run_node(args))
    else:
        status(args)


if __name__ == '__main__':
    main(sys.argv[1:])
