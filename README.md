# Cthulhu v4 · 中心 + 星系

一个 Master 原生会话负责和你对齐目标；每个任务交给一个独立的 old_one（Codex 或 Claude Code 原生会话），运行在你选的执行机上。
中心保存全部业务状态；执行机（星系节点）主动用 WebSocket 连中心，不需要 SSH、入站端口或共享文件系统。原生进程和会话文件留在执行机上。

Linux / Python 3.11+，唯一依赖 `websockets==15.0.1`，前端无构建。

## 快速试用（demo，不调用模型）

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/cthulhu center --state .state-demo
```

打开 http://127.0.0.1:8767，粘贴 `.state-demo/web-token` 的内容。新建任务后，点左侧任务即可在右侧查看该执行者的完整过程。

## 部署中心

```bash
cthulhu center --state .state --backend codex --model gpt-6-astra --reasoning-effort medium \
  --public-url https://你的域名或IP:8001/v4
```

- 网页只监听 `127.0.0.1:8767`，星系 WebSocket 监听 `127.0.0.1:8770`，由反向代理暴露：`/v4/` → 8767，`/v4/galaxy` → 8770（需支持 Upgrade）。
- 不指定 `--model` 时使用原生 CLI 的默认配置。已有真实会话的状态目录不能换后端或模型；要换就用新的 `--state`。
- 中心机本身就是一个执行节点（`local`），不用额外配置。
- 本服务器的启动方式见 `deploy/start-center.sh` 与 `deploy/cthulhu-v4.service`。

## 添加星系节点：一条命令

在网页左栏「节点 ＋」输入节点名，或在中心机执行 `cthulhu invite gpu1`，会得到：

```bash
curl -fsSL https://raw.githubusercontent.com/waaadreamer/cthulhu/v4/deploy/install.sh | bash -s -- <邀请>
```

在执行机上，用将要运行 agent 的普通用户执行。脚本会：
- 下载代码到 `~/.cthulhu/app` 并创建 venv。
- 用一次性邀请换取节点凭证，写入 `~/.cthulhu/node.json`（600 权限）。
- 用 `systemd --user` 常驻；没有 systemd 时用 nohup。
- 确认连上中心后才退出。

重跑同一条命令就是升级，原身份和会话保持不变。日志在 `~/.cthulhu/node.log`。

几点说明：
- 邀请 24 小时内有效，只能用于一台机器。命令里含邀请，按密码对待。
- 中心证书按 SHA-256 指纹校验，自签证书也无需分发 CA；证书更换后需要重新生成命令。
- 首次使用前，在执行机上自行登录 `codex login` 或 `claude`。节点会自动报告本机能找到的后端。
- 同一台机器想跑第二个节点：设置 `CTHULHU_HOME=~/.cthulhu-2` 再执行。
- Claude Code 拒绝在 root 下使用 `--dangerously-skip-permissions`，请用普通用户运行节点。

## 飞书（可选）

中心用官方 `lark-cli`（`@larksuite/cli`）订阅机器人「奈亚」收到的消息。只处理私聊奈亚、或群里 @ 奈亚的消息。

- 主人的消息直接交给 Master。
- 其他人按白名单处理：不在名单里的人不回复，网页上记一行；在名单里的人先由轻量模型（`codex exec`，默认 gpt-6.1-sol）按他的规则判断，不通过就回复「没有权限」，通过才交给 Master。Master 收到时附带此人的规则，二次核验后作答。
- 程序不替 Master 回复。Master 通过 `feishu` 工具调用 lark-cli（读消息记录、文档，回复，编辑等），中心执行，并在主聊天公开命令、结果，以及发往飞书的原文和回执。
- 白名单在左栏「飞书白名单」查看。修改靠对 Master 说话（例如「允许张三查看 S 集群训练状态」），只有主人发起的回合能改；其他人的消息单独成回合，改不了白名单。

```bash
cthulhu center ... --feishu-cli /path/to/lark-cli --feishu-owner ou_主人 --feishu-bot ou_奈亚 \
  [--feishu-config DIR] [--feishu-judge-model gpt-6.1-sol]   # 凭据目录默认 <state>/feishu/lark-cli
```

- 应用凭据：`LARKSUITE_CLI_CONFIG_DIR=DIR lark-cli config init --app-id … --app-secret-stdin`。读主人的消息与文档需要主人本人完成一次 `lark-cli auth login`（用户身份）。
- 去重键是平台 message_id，先落盘再处理。订阅断线后会退避重启，平台重投的消息不会重复处理；平台不再重投的会丢失。重启时还在鉴权的消息、还在执行的 lark-cli 调用会公开为「结果未知」，不会重放。
- 同一个应用只能有一个订阅者，启用前先停掉其他订阅同一应用的进程。
- 白名单靠模型把关，不是硬隔离：Master 以节点用户的完整权限运行，理论上可以绕过 `feishu` 工具。公开记录只覆盖经过工具的调用。

TODO：双 Master。网页一个、飞书一个，权限相同、可以互相通信，解决飞书消息排在网页长回合之后、上下文混用的问题。当前只有一个 Master。

## 架构

```
cthulhu_v4/
  center.py      中心：组装领域规则、启动修复、定时 tick
  domain/        纯同步状态机：messages / questions / tasks / review
  tools.py       唯一的工具目录与分发表（Master 与 old_one 权限不同）
  hub.py         唯一派发路径：回合租约、事件去重入库、停止确认、过期 → unknown
  node.py        执行节点（中心内嵌的 local 与远端星系共用）：发件箱、接单、watchdog
  runtime.py     Codex app-server / Claude stream-json / demo 适配器，只依赖窄接口 Sink
  transport.py   本机内存通道、WebSocket 客户端/服务端、邀请与证书指纹
  bridge.py      节点本机 MCP HTTP 桥；mcp.py 是原生 CLI 启动的 stdio 端
  web.py app.py cli.py
static/js/       model（事件 → 会话块，可单测）、render、chat、app
```

## 行为契约（审核重点）

- **消息**：先落盘再执行，状态为 `queued → running → handled`；异常为 `failed`，结果不明为 `unknown`。
  - handled 只表示回合结束，不代表任务完成，也不代表外部操作成功。
- **优先级**：0–9 只影响排序。10 是显式打断，仅主人和 Master 可用，old_one 最高为 8。
  - 打断时先撤销旧回合的工具写权限，等节点确认进程已停止，才派发新回合。
- **租约**：70 秒，节点每秒续租，按本机单调时钟在到期前 10 秒停止进程，即容忍与中心失联 60 秒。
  - 中心侧租约到期就标为 unknown、暂停任务并报备 Master。不迁移、不重放。
- **节点身份**：凭证加持久磁盘 instance。换了空目录不能接管原 session；同一节点不允许两条连接。
- **可靠性**：
  - 节点事件按连续序号上传，中心入库和确认游标在同一事务内。
  - 工具调用带 call_id，结果和业务变更同事务落盘，重试只返回原结果。
  - 这不保证任意 shell 命令的外部副作用 exactly-once。
- **问题**：`master → owner →（默认 5 分钟超时）master → answered`。
  - 每次变化 version 加一，旧版本的回答会被拒绝，历史答案保留。
- **活性**：每 60 秒检查一次。有活动回合或登记了等待就不打扰。
  - 意外空闲时按指数退避续办，三次后暂停并交给 Master。
- **审核**：每 10 分钟有新增信息才唤醒 Master；审核自身输出、遥测和心跳不算新增。
- **原生会话**：恢复原 session，不裁剪、不摘要、不跨后端迁移，上下文压缩交给 CLI。
  - 恢复失败如实报错，不悄悄新建空白会话。
- **权限**：Codex 使用 `--yolo`，并在 start/resume/每轮显式指定 `danger-full-access` 和 `approvalPolicy=never`，回执不符就停止；Claude 使用 `--dangerously-skip-permissions --permission-mode bypassPermissions`。
  - 运行时使用节点进程的 OS 用户身份，任务目录不是容器隔离。
- **网页**：只监听 loopback，用独立 token 访问并检查 Origin。浏览器凭证和节点凭证互不相通。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v     # 不调用模型
for f in static/js/*.js; do node --check $f; done && node tests/test_model.mjs
PLAYWRIGHT_PATH=/path/to/playwright node tests/ui.cjs  # 可选浏览器验收，生成 preview.png
python3 tests/native_smoke.py codex|claude            # 手动：会使用模型额度
```

测试只验证调度与协议不变量，不代表真实模型的表现。
