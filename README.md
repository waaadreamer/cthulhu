# Cthulhu Harness v2

多 agent harness。中心神殿负责路由、鉴权、权限和存储，星系负责托管 agent 进程。所有约束都在程序层实现，skill 只负责逐层继承意志。

## 架构

```
 主人(浏览器) ──https:8000──► nginx ──► Temple 中心神殿 (127.0.0.1:7500)
                                         ▲ websocket /ws（只走 loopback）
             ┌───────────────────────────┼──────────────────────────┐
     Galaxy "temple"（用户 cthulhu）                         Galaxy "temple-ghroth"（用户 ghroth，有 sudo）
      ├─ 阿撒托斯  azathoth                                    └─ 格赫罗斯 ghroth
      ├─ 黑山羊    shub
      ├─ 尤格      yog
      └─ 旧日支配者 old_* （由黑山羊创建）
```

- **Temple**（`cthulhu/temple.py`）：唯一的消息路由。保存 agent 注册表、消息、永久上下文和主人聊天，执行权限矩阵，并提供网页和 API。
- **Galaxy**（`cthulhu/galaxy.py`）：以某个 OS 用户运行，主动连接中心，按中心下发的期望集合启动、停止、重启 agent 进程（崩溃后指数退避）。agent 继承该 OS 用户，所以"用户"只用来区分格赫罗斯和其他 agent。
- **Agent**（`cthulhu/agent.py`）：一个 agent 对应一个进程，包含一个 `codex app-server` 子进程和一条连到中心的 websocket。接口是 `send`、`receive`、`latest_result`。消息按优先级排队，同一批消息合并成一轮处理。
- **角色子类**（`cthulhu/gods.py`）：决定哪些内容进永久上下文、持有哪些工具、被什么唤醒。
- **运行时**（`cthulhu/runtime.py`）：Codex app-server 的 stdio JSON-RPC 适配。以后接 Claude Code 只需实现同一组方法。

身份由中心签发：agent token 是 `HMAC(secret, id:epoch)`，由星系通过环境变量传给 agent 进程，不进入模型的 shell 环境。终止或重启会递增 epoch，旧 token 随即失效。

## 权限矩阵（`cthulhu/roles.py`）

| 发送 → 接收 | 阿撒托斯 | 黑山羊 | 尤格 | 格赫罗斯 | old_one |
|---|---|---|---|---|---|
| 主人 | ○ | | | ○ | |
| 阿撒托斯 | | ○ | ○ | ○ | |
| 黑山羊 | ○ | | | | ○ |
| 尤格 | ○ | | | | ○（催办） |
| 格赫罗斯 | | | | | |
| old_one | | ○ | | | ○ |

其他能力：
- 创建、终止、重启 old_one：只有黑山羊。
- `latest_result`：只有尤格。
- 读取全部消息和注册表：阿撒托斯和黑山羊。
- 主动发消息给主人（`tell_owner`）：只有阿撒托斯。
- `temple-ghroth` 星系不允许创建 old_one。

## 上下文

| 角色 | 永久上下文（程序注入 developer instructions） | 临时上下文 |
|---|---|---|
| 阿撒托斯 | 每次和主人对话后，在 ephemeral fork 上额外调用一次模型，提炼要新增或作废的条目 | 原生线程：主人对话、收发的所有消息 |
| 黑山羊 | 和阿撒托斯的交流提炼；外加 old_one 名册（初始化参数），变化时自动刷新 | 原生线程 |
| 尤格 | 和阿撒托斯的交流提炼 | 每次巡检新开 ephemeral 线程，不累积 |
| 格赫罗斯 | 和主人对话的提炼 | 原生线程 |
| old_one | 黑山羊写的职责和任务 | 原生线程（可以使用 Codex 子 agent） |

临时线程的上下文占用超过 80% 时，程序会换一个新线程，新线程只带永久上下文。

尤格每 5 分钟巡检一次所有 old_one 和格赫罗斯的 `latest_result`。状态没变化时不调用模型。

## 运维

```bash
systemctl status cthulhu-v2-temple cthulhu-v2-galaxy cthulhu-v2-ghroth
journalctl -u cthulhu-v2-galaxy -f
tail -f /var/lib/cthulhu/galaxy-temple/<agent-id>/agent.log        # agent 进程日志
sudo -u cthulhu /opt/cthulhu/venv/bin/python -m cthulhu passwd --config /etc/cthulhu/temple.json owner   # 改密码（需 PYTHONPATH=/opt/cthulhu）
```

- 配置：`/etc/cthulhu/*.json`（0600，含密钥）
- 数据库：`/var/lib/cthulhu-temple/temple.db`（temple 以 ghroth 用户运行，cthulhu 用户无法读写）
- Codex 登录态：`/var/lib/cthulhu-harness/.codex`（cthulhu）、`/var/lib/cthulhu-ghroth/.codex`（ghroth）

新增星系（集群节点）见下一节。

## 部署到其他星系

星系主动用 `wss://<神殿IP>:8000/ws` 连接神殿，节点只需要能出站访问这个端口，自己不需要开放任何端口，也不需要 root 权限。
- 神殿的证书是自签名的，星系会按 provision 时下发的 SHA-256 指纹校验证书（certificate pinning），所以不需要 CA。
- 星系连上后，必须通过 token 鉴权才能做任何事。

两步完成：

```bash
# 1. 神殿上（root）：登记星系，并输出节点安装命令
new-galaxy gpu1 "GPU 集群"

# 2. 节点上（agent 要用的那个用户）：粘贴上一步输出的那一行
curl -fsSL https://raw.githubusercontent.com/waaadreamer/cthulhu/main/deploy/install-galaxy.sh | bash -s -- <blob>
```

blob 里包含星系 token，按密码对待。安装脚本会做这些事：
- 克隆代码。
- 建 venv（Python ≥3.11，也可以用 uv）。
- 找到 codex，没有就用 npm 安装。
- 写入 `~/.cthulhu/galaxy.json`。
- 以 `systemd --user` 或 nohup 方式常驻，最后确认已连上神殿。

节点首次部署还要手动做两件事：
- 执行一次 `codex login`。
- 如果这个星系要跑 Claude agent，在 `~/.cthulhu/env` 里写上 `ANTHROPIC_*`。

重新执行同一条命令即可升级。日志在 `~/.cthulhu/galaxy.log`。

证书续期（当前证书有效期到 2026-11-09）后，指纹会变。这时需要给每个星系更新 `galaxy.json` 里的 `tls_pin`，或者重新 provision。

## 模型后端

每个角色可以单独选择 Codex 或 Claude Code（Claude Agent SDK）作为后端，并配置模型、effort、记忆提炼模型和后备。
- 默认值写在星系配置的 `runtimes` 里；网页"模型设置"里的修改保存在中心，覆盖默认值，并在 agent 当前回合结束后生效。
- 主后端某一回合失败时（比如中转站限流），这一回合改由后备重跑，接下来 30 分钟都走后备（`backends.py: FALLBACK_HOLD`）。
- 两种后端的会话不互通。每个 agent 的每一轮都会记进中心的 `turn_log`（与后端无关）。换到新会话时，程序把本会话没见过的最近 12 轮放进 `<handoff>` 块带过去，并注明这些请求已经处理过，不要重新执行。
- Claude 后端的凭证在 `/etc/cthulhu/claude.env`（0600，root），由两个星系服务通过 systemd 的 `EnvironmentFile` 读取。

## 开发

```bash
python3 -m unittest discover -s tests -v      # 端到端：真实 temple/galaxy/agent 进程 + 假 codex
cd web && npm install && npm run build        # 前端构建到 cthulhu/static
```

## TODO

- 优先级消息插队（steer 进正在运行的回合），目前是排队后合并处理。
- 永久上下文超过约 100k tokens 时自动重写整理。
- 奈亚拉托提普（飞书入口）。
