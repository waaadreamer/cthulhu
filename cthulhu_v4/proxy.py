"""出站代理：只给真正需要的连接用，而不是整个进程。

- 模型 CLI（Codex/Claude）访问模型服务需要代理；它运行的命令（agent 执行的 curl/pip/git）不需要，
  走代理反而会让内网访问变慢或失败。
- 中心检查 GitHub 更新时按需走代理。
- 中心与节点之间的 WebSocket、网页、本机 MCP 桥一律直连。

代理只通过显式参数配置（--model-proxy / --git-proxy），不读取进程继承的 *_PROXY：
那样任何从外层环境漏进来的代理都会被“自动”用上，又回到全进程走代理的状态。
"""
import json
import os

LOOPBACK = 'localhost,127.0.0.1,::1'
VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
# Codex 运行命令时不继承的变量：模型 CLI 自己的代理只用于访问模型服务。
CODEX_EXCLUDE = ['*proxy*', '*PROXY*']


def strip(env):
    """去掉继承来的代理变量，返回新字典。"""
    return {k: v for k, v in env.items() if k not in VARS and k.lower() != 'no_proxy'}


def proxy_env(url):
    """把 url 作为代理的环境变量；loopback 始终直连（本机 MCP 桥不能被转发出去）。"""
    if not url:
        return {}
    return {k: url for k in VARS} | {'NO_PROXY': LOOPBACK, 'no_proxy': LOOPBACK}


def codex_args(url):
    """有模型代理时，让 Codex 运行的命令看不到代理变量。没有代理时不改 Codex 的默认行为。"""
    if not url:
        return []
    return ['-c', 'shell_environment_policy.exclude=' + json.dumps(CODEX_EXCLUDE)]


def git_env(url):
    """git 访问远端的环境：https 远端用 HTTPS_PROXY，ssh 远端经 HTTP CONNECT 隧道。"""
    env = strip(os.environ)
    if not url:
        return env
    host = url.split('://', 1)[-1].rstrip('/')
    return env | proxy_env(url) | {'GIT_SSH_COMMAND': f'ssh -o ProxyCommand="nc -X connect -x {host} %h %p"'}
