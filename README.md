# Cthulhu v4 · 中心 + 星系

一个 Master 原生会话负责和你对齐目标；每个任务交给一个独立的 old_one（Codex、Claude Code 或 Antigravity 原生会话），运行在你选的执行机上。
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

## Antigravity（agy 1.5.3+）

节点在 PATH 找到 `agy` 后会上报 `antigravity`。中心的模型、节点默认值和新任务均可选择
Antigravity；选择的原生模型 slug 通过现有 WebSocket 派发到所属节点。
“刷新目录”让该节点重新发现模型，离线或旧版节点会明确提示。不会重启已有 Codex/Claude 回合。

认证默认在节点本机继承 Claude 的有效 `ANTHROPIC_AUTH_TOKEN`（或 `ANTHROPIC_API_KEY`）与
`ANTHROPIC_BASE_URL`。读取节点进程环境，再应用用户、任务目录 `.claude/settings.json`、
`.claude/settings.local.json` 和 managed settings 的 env 覆盖。节点目录探测不带任务目录覆盖，
实际运行时按任务 cwd 重读。不会执行交互 shell 函数、apiKeyHelper，也不会替用户修改认证文件。
若当前 Claude 依赖这些外部认证方式，需要先让节点能够读取等效设置；不能假定继承了交互终端的配置。

agy 子进程仅以同一 auth/base_url 设置 `GEMINI_API_KEY` / `GOOGLE_GEMINI_BASE_URL`，原生
`authenticate(gemini-api-key)` 只选择认证方式，**不证明网关支持 Gemini 协议或账户有额度**。
没有明确 base_url 时拒绝启动，不把继承的 key 发往默认 Google 地址。不实现协议转换代理。

模型页区分本机 `agy models` 目录与同地址 `/v1beta/models` 的渠道目录：前者是 CLI 的候选 slug，
后者也只证明列出模型，均不宣称推理成功。原生请求验证结果另行显示：已证实协议不支持与目录未列出/未经验证分别记录；
证据仅在节点私有状态按同一 auth/base_url 的摘要关联，刷新或重启保留，换凭据/地址不沿用。
渠道没列出候选模型时明确显示目录未列出，不能单凭缺项宣称协议不支持；认证缺失、查询失败、
自定义路径尚未验证时分别显示待配置或错误。目录查询不发送生成请求，不跟随 HTTP 重定向。
带路径的 base_url 不猜测改写；运行仍使用原地址，原生错误如实上报。

原生 ACP 会话、日志保存在该节点 `<state>/antigravity/<agent>/`，每轮新进程通过原生
`session/load` 恢复，不搬历史、不自建摘要。MCP 通过 new/load 的会话参数注入，每轮独立 token，
不写全局 MCP。模型必须存在于原生目录，且原生配置确认生效后才发 prompt。
agy 1.5.3 的 ACP 未暴露 headless 的 `customModelsConfig`：目前仅支持目录里的完整 slug，
目录外裸 ID 或自定义映射在中心拒绝保存，不会静默改成 `-medium`。headless 自定义模型方案不能当作 ACP 已支持的证据。

agy 1.5.3 存在一个失败边界：模型 HTTP 错误后 ACP 仍可能返回 `end_turn`。适配器识别其完整原生
致命通知，并提取本进程、本轮日志中的有限脱敏错误；日志缺失时报告原因未知的失败，不自动重放。
普通文本中的同句引用不算原生致命通知。后续 agy 版本若变更通知格式，需重新验证这一边界。

手动验证（不在默认 unittest 中）：

```bash
# 真实 agy + 回环 HTTP 400 夹具：MCP 发现、同 session 恢复、原始输入/系统指令保留、失败语义。
# 不访问真实模型；不能据此宣称模型推理成功。
python3 tests/native_agy_protocol.py
# 真实浏览器、隔离中心，回放 CLI 目录证据；不修改生产状态或调用模型。
PLAYWRIGHT_PATH=/path/to/playwright PLAYWRIGHT_EXECUTABLE=/path/to/chrome \
  AGY_MODELS_FILE=/path/to/agy-models.stdout node tests/ui_antigravity.cjs
```

发布继续使用既有网页更新入口：审核并合入 `origin/v4` 后，中心独立测试、local 排空后重启，
远端节点在各自空闲时更新。保留原节点身份与状态目录。若要回滚，以 revert 提交沿相同入口发布；
先暂停新增的 Antigravity 任务并恢复其节点默认后端，保留原生会话目录供以后恢复，不迁移至其他后端。

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
  agy_runtime.py Antigravity 原生 ACP 适配器，逐会话 MCP 与原生 session/load
  antigravity.py 节点本机 Claude 配置继承、agy 目录与渠道模型查询
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
- **并行**：每个节点同时运行的回合数在网页节点对话框里设置，默认 8。
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

独立多实验看板见 [Canvas 使用与部署](docs/canvas.md)；节点详情中的命令行与采集共用的执行接口见 [远程命令说明](docs/remote-execution.md)。Canvas 无需绑定 task，独立服务使用 `cthulhu canvas --center http://127.0.0.1:8767 --token-file /path/to/web-token` 启动。

```bash
.venv/bin/python -m unittest discover -s tests -v     # 不调用模型
for f in static/js/*.js; do node --check $f; done && node tests/test_model.mjs
PLAYWRIGHT_PATH=/path/to/playwright node tests/ui.cjs  # 可选浏览器验收，生成 preview.png
python3 tests/native_smoke.py codex|claude            # 手动：会使用模型额度
```

测试只验证调度与协议不变量，不代表真实模型的表现。
