# 通用节点命令执行（v1）

远程命令与节点详情命令行共用 `Center.executions`，Canvas 仅作为调用方。
不依赖 SSH；节点主动连接中心既有 WebSocket。中心保存业务记录，节点保存本地去重票据、进程看守和可靠发件箱。
这里的隔离测试不代表线上节点已升级，也不调用真实模型。

## 中心 Python 接口

```python
item = center.executions.create(
    node_id='gpu1', cwd='/home/user/project', script='python collect.py',
    timeout=60, request_id='canvas:panel_id:sequence',
)
item = center.executions.get(item['id'])
item = center.executions.cancel(item['id'])
recent = center.executions.list(node_id='gpu1', limit=20)
```

调用在中心 asyncio 线程同步执行，不跨 `await` 持有数据库事务。
`create`/`cancel` 可独立调用，也可复用调用方已有事务；Canvas 可把命令与自己的 inflight 引用一并提交。
`request_id` 可选，1–128 字符，全中心唯一。同 ID、同参数返回原记录（包括终态），参数冲突报错；不传则每次创建新命令。
HTTP 响应丢失时应用原请求号查询/重试，不能生成新 ID 盲重跑副作用。

节点必须在线且已上报 `commands-v1`；否则拒绝创建，不离线积压。每节点最多 4 条未结束命令，独立于原生 agent 并发上限。
正在排空更新的节点不接新命令。`cwd` 必须为节点上的绝对目录，目录不存在在节点返回 `failed`。
`script` 非空、最多 64KiB UTF-8；用 `/bin/sh -c`，stdin 关闭。超时范围 0.1–3600 秒，默认 60 秒。
不提供 PTY、交互程序或常驻服务；shell 返回后也会清理本次进程组留下的后台进程。

## HTTP 与结果

使用现有浏览器 Bearer token 和 Origin 检查，不接受节点凭证替代浏览器凭证。

- `POST /api/executions`：JSON 参数与 `create` 相同，返回完整记录。
- `GET /api/executions?id=cmd_...`：返回完整记录；每秒轮询可读取实时输出。
- `GET /api/executions?node_id=gpu1&limit=20`：最近记录元数据；limit 为 1–100，不含 stdout/stderr/script/late_result。
- `POST /api/executions/cancel`：`{"id":"cmd_..."}`，返回当前完整记录。

记录包含 `id/node_id/cwd/script/timeout/request_id/state`，`stdout/stderr/exit_code`，
`created_at/started_at/finished_at/received_at`（Unix 秒），`truncated/output_bytes/error`。
`started_at/finished_at/exit_code` 在尚未知晓时为 null。开始和结束来自节点时钟，创建和 received_at 来自中心时钟。received_at 是首次获知终态或中心封存终态的时间，迟到回执不覆盖；它不是数据实际采样时间。
`completed` 表示 shell 已退出；退出码可以非零，调用方应另查 `exit_code == 0`。
受信号终止的 exit_code 可以为负，unknown 不推测退出码。
有效 HTTP 200 记录的 `error` 可以非空（取消、unknown 原因）；前端使用 `api(path, body, {resultError:true})` 读取，不能把这个字段当 HTTP 错误。

stdout 与 stderr 合计最多保留 **1MiB UTF-8**，无效字节替换后也计入预算。
限制首先在节点源端生效：16KiB 读取块，超限输出继续排空并丢弃，第一次丢弃立即上传 `truncated=true`。
中心再次限制预算。两条流各保留顺序，不保证 stdout 与 stderr 之间的全局顺序。
命令数据复用可靠事件游标去重，远端通过 data 连接上传；小批上传避免挤占控制心跳。节点已持有的命令只续 ID/状态，不每秒重发脚本全文。
节点详情先轮询元数据，变化才读取全文，避免长命令反复传输已满的 1MiB 输出。

## 状态、取消及恢复

`queued → dispatched → running → completed/failed/cancelled/timed_out/unknown`。
已派发的取消先进入 `cancelling`；真正终态以节点看守观察到的结果为准。

- 派发前取消直接 `cancelled`，没有启动进程。重复取消终态不改写原结果。
- 命令已正常结束而取消才抵达，保留 `completed`；取消先被看守观察到才返回 `cancelled`。
- 超时或取消清理本次 shell 的独立进程组：先 TERM，0.2 秒后 KILL，清理后才回报终态。
- 每条命令有独立看守，节点以专用管道交付脚本/取消；节点 SIGKILL 后管道 EOF 仍触发清组。
  清组前保留 shell 的 PID（waitid WNOWAIT），避免 PID/PGID 重用误杀无关进程。
  不杀节点原生 agent 或其他命令；主动脱离该进程组的程序不属于这一版的常驻作业管理能力。
- 节点最长容忍失联约 60 秒，按本地单调时钟停组；中心 70 秒租约到期封存 `unknown`。
  重新连接、重复派发和同磁盘重启均凭持久票据去重，未知命令不自动重放。
- 中心重启将所有未结束命令封存 `unknown`，恢复同步后撤销旧命令。节点重启将本地未结束票据报为 `unknown`。
  迟到的输出仍可补入，迟到结局放入 `late_result`，不改写已封存的终态。
- `unknown` 不证明副作用不存在，也不证明外部作业已结束。须核验节点与原执行结果后，由调用方显式创建新命令。

## 验证与部署接入

```bash
PATH="$PWD/.venv/bin:$PATH" python3 -m unittest discover -s tests -v
for f in static/js/*.js; do node --check "$f"; done
node tests/test_model.mjs
# 可选真实浏览器；临时中心和 shell，不调用模型
PLAYWRIGHT_PATH=/path/to/playwright CHROMIUM_PATH=/path/to/chrome node tests/ui_execution.cjs
```

`tests/test_execution.py` 使用真实子进程和真实 WS，覆盖实时输出、合计截断/排空、UTF-8、取消/超时与无关进程存活、
真实节点 SIGKILL 后清组和重启去重、慢数据上传时续租、重复上传、离线/并发、中心重启和迟到回执、现有 HTTP 鉴权。
`tests/test_deploy.py` 验证中心更新必须等待本地命令，避免只等 agent 空闲便重启。

本改动不自行重启线上中心/节点。中心内嵌 local 节点与中心同进程；部署前必须等当前 local 原生回合及命令结束，
交 Master 决定重启窗口。已有远端节点须升级后上报能力才能执行；旧节点会明确被拒绝。
远端自动更新同时等待原生回合和命令收尾，保留原节点身份、session 与去重数据库。

回滚前先停止新命令入口/Canvas 调度，等待活动命令和节点发件箱结局确认；不能把未知命令直接重跑。
旧版本不认识 command 事件，未确认的命令事件不能在回滚后盲发给旧中心。
保留中心与节点原状态目录，不用旧数据库快照覆盖新去重票据。若进程已异常中断，按 unknown 核验后再恢复。
