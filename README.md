# Cthulhu v4 · 中心 + 多机星系试用版

从空目录重写。Linux / Python 3.11+（进程恢复核验使用 `/proc`），WebSocket 使用固定版本 websockets；无前端构建，不使用旧 harness 的代码、状态库或服务。没有每分钟的便宜模型。

## 先试用

```bash
cd /root/cthulhu-v4
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate
python -m cthulhu_v4
```

打开 **http://127.0.0.1:8767**，点击「访问凭证」，填写 `.state-demo/web-token` 文件内容。
默认 **demo 模式**不调用模型、不执行业务命令：新建任务 → old_one 提问 → 你回答 → 任务完成。可以点击「模拟：请我回答并计时」验证五分钟超时交回 master 的流程。模拟 master 不会自主推理，超时后仍可由你回答。

跨机执行不依赖 SSH，见下面的 WebSocket 部署。网页仍默认绑定中心机 loopback，可通过现有认证反向代理访问。

真实 Codex（使用本机已登录的 CLI，模型留空使用其默认配置）：

```bash
python3 -m cthulhu_v4 --backend codex --state .state-codex --port 8768
```

真实 Claude Code：

```bash
python3 -m cthulhu_v4 --backend claude --state .state-claude --port 8769
```

可以加 `--model 模型名`，Codex Master 可加 `--reasoning-effort medium`。master 的配置会作为同后端新任务的默认值；创建任务时可选后端/模型。已存在的 master 使用已保存配置，不静默改动原生会话。更换后端使用新的状态目录。

服务按 Ctrl+C 停止，会结束自身启动的运行时进程组。它不会卸载原生会话，也不能自动撤销文件变更或已脱离进程组的外部作业。

## 当前服务器的试用入口

网页：`https://47.115.202.248:8001/v4/`；星系：`wss://47.115.202.248:8001/v4/galaxy`。
两者使用同一公网端口；8001 根路径保留旧版，不能在旧版页面查看 v4 节点。
当前 Master 已配置为 Codex / `gpt-6-astra` / `medium`。启动命令保存在 `deploy/start-center.sh`；状态目录仍沿用 `.state-demo` 以保留已有节点身份，它的名字不代表当前模型模式。
模型和推理强度在原生 start/resume 及每轮请求中明确设置；原生回执不匹配时停止并报错，不静默替换配置。
配置参数参考：[OpenAI 官方 Codex 配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)。本机原生配置回执已验证为 `gpt-6-astra` / `medium`，并完成一次只读 `list_nodes` 调用。
网页地址和凭证保存在服务器 `/root/cthulhu-web.txt`（600 权限），连接命令在 `/root/connect-galaxy.txt`。
反向代理部署时，中心增加 `--public-origin https://47.115.202.248:8001`，显式允许网页的请求来源。

## 多机 WebSocket 部署（无需 SSH）

中心运行 Master、任务状态和消息队列；星系运行 old_one 和它的原生 session。星系只需主动访问中心 WSS 地址，不需要任何入站端口，也不需要共享文件系统。模型账户、CLI 配置、仓库和工作目录都位于实际执行节点上。**同一任务固定在一个节点，不能把原生 session ID 当成可跨机搬迁的会话。**

每台机器通过你现有的文件分发方式获取本目录，在该机安装 `requirements.txt`。以下命令均在各机器本地终端执行。

中心注册两个节点并启用 WebSocket：

```bash
source .venv/bin/activate
python -m cthulhu_v4 --backend codex --state .state-cluster \
  --node galaxy-a --node galaxy-b --ws-host 0.0.0.0 --ws-port 8770 \
  --tls-cert /path/to/fullchain.pem --tls-key /path/to/privkey.pem
```

如果已有 WSS 反向代理，可省略证书参数，将代理的 WebSocket 上游指向本服务 `ws://127.0.0.1:8770`。代理需要支持 Upgrade；中心浏览器 UI 仍在 8767。试用 demo 时改为 `--backend demo`。

中心分别生成 `.state-cluster/node-galaxy-a-token`、`node-galaxy-b-token`。通过现有的可信文件分发方式，将各节点对应的凭证交给该节点；**不要发送 web-token 或其他节点凭证**。节点目录必须持久保留，重连不能更换空目录。

在执行机 A 上：

```bash
source .venv/bin/activate
python -m cthulhu_v4.node --id galaxy-a \
  --center wss://中心域名:8770 --token-file /path/to/node-galaxy-a-token \
  --state /path/to/persistent-galaxy-a --capacity 2
```

执行机 B 使用 `galaxy-b` 和它的凭证。自建 CA 时加 `--ca /path/to/ca.pem`；证书验证保持开启。节点自动报告本机可找到的 codex/claude，demo 始终可用。

页面会显示节点在线状态，新建任务可选执行节点，`cwd` 填执行机路径。Master 通过 `list_nodes` 查询节点，再在 `create_task` 中填写 `node`。注册过节点后，漏填 node 会拒绝，而不是静默在中心机执行。仍可显式选择 `local`。

### 传输与故障规则（审核重点）

- 所有星系业务通信都经过同一条出站 WebSocket，包括 MCP 工具调用；节点只有本机 loopback MCP 桥，不需要从执行机另连中心 HTTP。
- 节点 ID / 独立 token 认证，同一节点不允许两个连接同时接管；持久磁盘 instance 改变也不能接管旧会话。
- 中心先持久化回合票据，再发任务；节点先记录收到票据，再启动原生进程。同一票据重传不会再执行。
- 原生活动在节点 SQLite 发件箱持久化，中心按连续序号确认，断线补传去重；session 指针先在本机落盘再上传。
- 30 秒执行租约，节点按本机单调时钟在到期前 10 秒停止进程，中心租约到期则暂停、标记 unknown 并通知 Master。短断线可续租；过期不自动迁移、不重放原输入。
- P10 先撤销旧回合的工具写权限并请求停止；收到节点的进程停止确认后，才派新回合。失联时无法保证立即停止，租约到期进入 unknown，不能假装已经打断成功。
- 节点进程异常重启时，未完成票据只报备 unknown。如果持久记录的原生 PID 仍活着，节点拒绝启动第二份，须在执行机本地核验并结束旧进程后重启。
- MCP 请求携带调用 ID，业务变更和结果回执同事务，重复请求返回同一结果。这不保证任意 shell 命令的外部副作用 exactly-once。
- 中心业务库保持单写者；中心没有高可用副本。暂不支持节点自动迁移、远端目录同步和文件下载。

### 本机模拟两个独立星系（协议联调）

三个终端启动：

```bash
# 中心
.venv/bin/python -m cthulhu_v4 --state .state-demo --node alpha --node beta --ws-port 8770
# 星系 alpha
.venv/bin/python -m cthulhu_v4.node --id alpha --center ws://127.0.0.1:8770 \
  --token-file .state-demo/node-alpha-token --state .state-node-alpha
# 星系 beta
.venv/bin/python -m cthulhu_v4.node --id beta --center ws://127.0.0.1:8770 \
  --token-file .state-demo/node-beta-token --state .state-node-beta
```

这验证独立进程、独立状态目录和真实 WebSocket 传输，不等于验证了你的实际跨机网络。执行机的连接地址与证书仍需按部署环境填写。

WebSocket 库接口参考：[官方 asyncio 文档](https://websockets.readthedocs.io/en/15.0.1/reference/asyncio/client.html)。

## 已实现

- 一个 master 原生会话；每个 task 一个独立 old_one 和原生会话 ID。
- Codex app-server、Claude stream-json CLI；统一 MCP 工具桥。恢复原 session，不裁剪或重组其历史、不跨后端迁移、不自设压缩阈值。
- SQLite 事务保存任务状态与事件；消息先入队再处理。同一 agent 串行，不同 agent 并行。
- 优先级 0–9 排队，10 显式打断当前回合，确认进程停止后处理加急消息。网页加急勾选、`POST /api/chat` 的 `priority: 10`、Master 的 `send_message(priority=10)` 均支持。old_one 仍最高 8。暂停/取消是单独控制操作。
- 所有收发消息、原生公开输出、工具与生命周期事件入库；CoT/活动侧栏独立滚动、显示来源、展开原始事件，流式文字合并展示。
- 每 60 秒检查意外空闲；有活动回合、明确等待或用户暂停时不续办。指数退避，三次续办后仍空闲则暂停并通知 master。
- 每 600 秒向 master 投递一次合并审核请求，检查所有未结束任务及 DDL。没有额外摘要模型。
- old_one 提问立即公开；master 回答/转问主人均公开；等待主人默认 300 秒，超时交回 master。版本校验防止旧答案覆盖晚到回复，历史答案保留。
- master 给执行者的纠正消息自动抄送主聊天，并明确仅已入队；DDL 修改、控制操作、问题和回答也自动公开。其余判断由最终回复 / tell_owner / finish_review 展示。
- 明确/推测偏好记忆分开标记，有来源、更新记录；不训练模型。
- 服务进程锁；异常重启后的 inflight 消息标记 unknown，不直接重放。旧运行时仍活着时暂停，拒绝并发恢复同一会话。

## 推荐审核顺序

| 顺序 | 文件 | 重点 |
| --- | --- | --- |
| 1 | `cthulhu_v4/tools.py`、`skills/` | agent 能做什么、哪些仅由 skill 约束 |
| 2 | `cthulhu_v4/core.py` | 状态、提问/超时/晚到回答、心跳、优先级、恢复 |
| 3 | `cthulhu_v4/store.py` | 状态与事件事务、事件读取、原始记录 |
| 4 | `cthulhu_v4/runtime.py`、`mcp.py` | 原生会话恢复与工具接入，不自行处理上下文 |
| 5 | `cthulhu_v4/galaxy.py`、`node.py` | 节点凭证、租约、断线恢复、去重、P10 停止确认 |
| 6 | `cthulhu_v4/web.py`、`static/` | 本地接口、侧栏与主聊天分离 |
| 7 | `tests/` | 不变量及真实接入验收 |

关键逻辑有中文注释。代码是待你审核的首版，不是已经替换旧服务的生产版本。

## 状态与调度边界

**Task**：`running / waiting / paused / completed / cancelled`。第一次发送任务就进入 running，不等模型回复后才登记。agent 的 `running/idle/paused` 单独表示运行时状态。

**Message**：`queued → running → handled`；异常为 `failed`，中断或服务异常退出为 `unknown`。handled 只表示回合完成，不等于任务完成，也不表示指令造成的外部操作成功。

**Question**：`master → owner → master（超时）→ answered`。每次状态/答案变化增加 version。主人可以修正已回答问题，旧答案不删除。任务取消/完成关闭未处理问题。

定时器以持久化的绝对时间为依据，检测精度为一个心跳周期。DDL 不随等待自动顺延，到期只告警、不杀任务。等待外部作业必须登记句柄与复查时间；程序不从自然语言猜测它是否在等。

master 审核使用 `list_tasks`、`read_task_events` 和 `finish_review`。每个任务保存已读事件游标；原文与侧栏同源、分页可回查。第一版依赖 master skill 认真核验，不宣称程序能证明模型确实理解了全部证据。

十分钟到点先由程序判断增量：主人消息/回答、执行者原生活动、任务及故障状态变化才触发审核；无增量不调用模型、不重复汇报。Master 自身审核输出、token 遥测和心跳不触发下一轮。已暂停但无变化的任务保持安静；新消息仍正常处理。`list_tasks` 固定本轮审核边界，`finish_review` 持久化已报告边界，审核期间后到的信息留待下一轮，重启保留去重状态。DDL 首次到期和问题超时仍走原有通知路径。

界面中 Master 的原生文字、命令、工具调用与最终回复直接进入主会话；任务的 `CoT` 按钮打开对应 old_one 的独立侧栏。两处使用同一 `static/transcript.js` 解析器，按原生 item 合并 start/delta/complete，隐藏握手、token 用量等遥测，展示命令与输出、工具参数与结果、文件 diff 及 CLI 已提供的公开思考摘要。没有公开文字的 reasoning 事件不补写内容。大段输出可展开，消息保留发送来源；不会用模型二次生成过程。主会话使用 `view=conversation` 在同一事件序列中读取 Master 原生活动与公开汇报，执行者的原生活动留在各任务侧栏。

输入区通过「普通发送 / 加急打断」分段按钮选择优先级。普通为 9，加急为 10；加急选中后输入框显示暖色并提示立即打断当前回合，成功发送后恢复普通。

## 原生工具与权限

- Codex：按用户要求使用 `--yolo`；thread/start 与 thread/resume 显式指定 `danger-full-access` / `approvalPolicy=never`，每轮也指定完整权限；校验原生回执，策略不匹配则报错。中心和星系共用该适配器。
- Claude：按用户要求，新建与恢复均传 `--dangerously-skip-permissions --permission-mode bypassPermissions`。操作系统和平台本身的权限限制仍由原生进程返回，不伪造成功。
- 所有运行时使用启动服务的 OS 用户、认证与原生配置；在 `/root` 启动就是 root 身份。任务目录不是独立容器。同 OS 用户之间的 token 不构成恶意 agent 隔离。

中心与执行节点必须分别更新。节点同步时上报 `execution_policy=full-access-v1`；缺失此协议的旧节点不能领取真实 Codex/Claude 回合，中心暂停任务并一次报备 Master，禁止通过重复 shell 失败来判断版本。更新原目录并重连后，明确恢复任务，沿用原 session；适配器继续校验原生回执的完整权限。`runtime_revision` 仅用于定位代码版本，不能代替实际权限回执。
- Web 仅监听 127.0.0.1，以本地 token 访问并检查 Origin。浏览器凭证与星系凭证独立；星系 WebSocket 单独监听，支持原生 TLS 或现有代理终止 TLS。
- 活动展示的是后端公开的内容，不承诺读取不可见的内部推理。stdout 事件与 Claude hook lifecycle 一起收集；没有重复安装一套执行 shell 的采集 hooks。

## 已知的首版取舍

1. 支持多个远端星系节点主动连接中心；节点安装、模型登录、业务仓库准备仍由用户完成。未加入自动部署、自更新、容器和自动 worktree 管理。
2. P10 采用停止当前原生进程、恢复原 session 后处理新输入的方式；不是向正在运行的模型注入 steer。节点每轮结束会关闭 CLI，下轮 resume，同一节点保留原生历史；存在 CLI 启动开销。
3. 原 session 恢复失败会如实报错并有限重试，不自动建立空白 session。孤儿进程存活时需先核验并结束，再恢复。
4. 外部后台作业由 agent 登记和复查；服务不能保证任意脱离进程组的进程都能自动识别或撤销。
5. 原始事件完整存储，小规模试用没有事件保留清理、压缩或大型日志索引。前端每秒拉取增量；星系通过 WebSocket 每秒同步，积压事件加速补传。
6. 故障期间请求是否已经产生外部副作用可能未知。保留原 session、提示核验；不声称做到任意 shell 操作 exactly-once。
7. 模型是否正确完成任务仍需你审核；原生接口通了，不等于质量与延迟已完成基准评测。

## 测试

不访问模型、不收费的回归测试：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
node --check static/app.js
```

真实接入检查（会使用本机模型额度；只调用只读 list_tasks，不改业务文件）：

```bash
python3 tests/native_smoke.py codex
python3 tests/native_smoke.py claude
```

检查：真实 MCP 调用成功 → 关闭运行时 → 按同一 session ID 恢复 → 记住上一轮测试词。

可选浏览器测试使用 Playwright 与 Node 20+；应用本身不依赖它们：

```bash
PLAYWRIGHT_PATH=/tmp/cthulhu-v4-browser/node_modules/playwright \
  /tmp/cthulhu-v4-browser/node_modules/node/bin/node tests/ui.cjs
```

浏览器测试在临时独立状态目录运行，缩短计时以检查聊天、任务创建、问题超时、回答、任务完成、侧栏来源与独立滚动，并生成 `preview.png`。

2026-10-17 已验证本机 Codex 0.163.0、Claude Code 2.1.300 的只读 MCP 与原生恢复；更换 CLI 版本请重跑检查。

参考接口：[Codex App Server](https://developers.openai.com/codex/app-server/)、[Codex MCP 配置](https://developers.openai.com/codex/config-reference/)、[Claude CLI](https://code.claude.com/docs/en/cli-reference)。

## 更新执行节点启动策略

中心和星系共用 `runtime.py`，已按用户要求启用 Codex `--yolo` 与 Claude `--dangerously-skip-permissions`。
中心原 session 的恢复回执已确认 `dangerFullAccess / never`；远端节点必须更新自己的文件并重启才生效。
服务器 `/root/update-galaxy.txt` 保存了在原执行机目录内更新的完整命令。先停止旧节点，更新包只覆盖程序和原凭证，保留 `.state-node` 和原生会话文件。中心 `list_nodes` 的 `runtime_revision` 可确认是否已加载新代码。

若更新后出现“节点状态目录已改变”，不要重置中心 instance。服务器 `/root/recover-galaxy.txt` 提供恢复命令：重新下载代码，运行 `recover-state.py` 查找匹配原 UUID、session 和已确认事件的数据库，唯一匹配后将绝对路径保存到 `node-state-path`。`connect.sh` 优先使用该路径，也可显式设置 `CTHULHU_NODE_STATE`。恢复不复制或覆盖数据库；多份匹配、记录缺失或旧节点未停止时拒绝自动选择。搜索默认覆盖当前代码目录的上层与用户主目录；其他位置用 `--root /原安装目录` 指定。
Claude Code 在 root/sudo 下会直接拒绝 `--dangerously-skip-permissions`，需由普通用户运行；不通过伪造环境变量绕过该检查。
