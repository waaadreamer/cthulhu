# Cthulhu v3 设计文档（草案）

> 状态：讨论中。标 **【待定】** 的地方需要主人拍板；方括号里是当前默认方案。
>
> 已定：
> - D1 枢纽放在星系，old_one 的一切通信都经过枢纽；
> - D4 神殿每 10 分钟把检查点进度合成一条汇总发给阿撒托斯，`finish_task` 和 `report` 直达；
> - D5 阿撒托斯直接维护服务器和 harness；
> - D8 阿撒托斯的每条 sudo 命令都要主人在神殿网页上点按钮批准，由程序强制执行（§1.1.1b）；
> - 程序层服务（神殿、星系）跑在有 sudo 的账户下，主人确认没有风险。

## 0. 总纲

- **中央集权、极度扁平：** 只有一个常驻的决策者，即阿撒托斯。执行者是 old_one。中间层只有程序，加上若干次无状态的 AI 判断。
- **两层拓扑：** 阿撒托斯 ⇄ 神殿 ⇄ 枢纽（每个星系一个）⇄ old_one。old_one 只连本星系的枢纽，**永远不直接访问神殿**。神殿只服务阿撒托斯、主人和各个枢纽。
- **删除的角色：** 黑山羊、格赫罗斯、尤格索托斯。它们原来的职能拆分如下：
  - 派单和复用判断交给枢纽（Hub），由 Galaxy AI 辅助；
  - 进度检查交给枢纽的检查点；
  - 验收交给阿撒托斯。
- **纪律靠程序执行：** 计时器、打断、杀掉重建、限流都写在代码里。AI 只回答四个窄问题：
  1. 复用哪个 old_one；
  2. probe 是否合理；
  3. 进度是否正常；
  4. 当前进展总结。

  AI 的回答一律是结构化 JSON。AI 失败时，程序走固定的 fallback。
- **暂不考虑：** 延期、deadline、主人审批链、奈亚拉托提普（飞书入口）。

---

## 1. 类型

### 1.1 Agent（常驻的 LLM 进程）

所有 Agent 的公共字段：

| 字段 | 说明 |
|---|---|
| `id` / `name` | 唯一 id；显示名 |
| `role` | `azathoth` 或 `old_one` |
| `galaxy_id` | 运行在哪个星系 |
| `epoch` | 每次重建加 1，旧 token 随之作废 |
| `desired` | `running` / `stopped` / `deleted` |
| `thread` | 当前对话线程 id（见 §4） |
| `runtime` | 后端/模型配置（沿用 backends.py） |
| `status` | `idle` / `busy` / `stopped` / `crashed`（星系上报） |

所有 Agent 都有的公共接口（工具）：`receive(batch)` 由程序推送；还有 `read_file`、`list_tasks`、`task_view(task_id)`。

**连接对象不同：** 阿撒托斯连神殿；old_one 连本星系枢纽（本机 unix socket 或 localhost websocket），token 由枢纽签发。两者共用同一个 Agent 基类，只是 link 的目标不同。

#### 1.1.1 Azathoth（中央神殿，单例）

额外字段：`memory`（永久记忆，见 §4.1）。

| 工具 | 说明 |
|---|---|
| `new_task(galaxy, description, acceptance?)` | 下发任务，转给该星系枢纽的 `assign_task`。立即返回 `task_id`，绑定结果异步通知 |
| `send(to, text, priority=0..10)` | 可以发给任何 old_one，任意优先级 |
| `close_task(task_id, note)` | 验收通过，任务结束，old_one 被释放 |
| `continue_task(task_id, instruction)` | 验收不通过：重启检查点，并以 P9 把指令发给 old_one |
| `cancel_task(task_id, reason)` | 中止任务（任何状态都可以） |
| `stop_agent / restart_agent / delete_agent(id)` | 管理 old_one 进程 |
| `tell_owner(text)` | 通知主人 |
| `deploy(...)` | 沿用现有的部署能力 |
| 本机 shell | D5：阿撒托斯运行在专用星系，直接维护服务器和 harness（§1.1.1a） |
| `sudo_request(command, reason, cwd?, timeout?)` | 需要 root 的命令都走这个工具，主人在网页上批准后由程序以 root 执行，结果同步返回（§1.1.1b） |

#### 1.1.1a 阿撒托斯的运行环境（D5）

- **专用星系 `azathoth`：** 由原来的 `cthulhu-v2-ghroth` 服务改造。这个星系**只跑阿撒托斯一个 agent，不跑枢纽、不接任务**。`new_task` 不能指定这个星系。
- **两个系统用户分开：**
  - 星系进程（程序层）跑在有 sudo 的账户下（沿用 `ghroth`，或改名），它负责执行已批准的命令；
  - 阿撒托斯这个 agent 进程由星系以**一个没有任何 sudo 权限的用户**（新建 `azathoth`）拉起。
- **原因：** 「每条 sudo 都要批准」必须在操作系统层面强制，否则阿撒托斯写个脚本、换个写法就能绕过。agent 进程本身没有 sudo 权限，root 操作就只剩 `sudo_request` 一条路。
- `azathoth` 用户需要能读写 harness 代码目录和自己的工作目录（加入相应的组），不需要 root 就能完成的事（改代码、跑测试、git）直接做。
- 运行时用无沙箱模式（codex `danger-full-access` / claude 对应权限）；沙箱的意义已被 OS 用户隔离取代。
- **自我修改：** 阿撒托斯可以改 harness 代码并重启服务，包括重启神殿和它自己所在的星系。要求：改代码在 worktree 里改，跑完测试再合并部署；重启前先写记忆，重启后由 handoff 接续。
- **自我修改中需要 root 的步骤**（重启 systemd 服务、改 nginx 等）同样走 `sudo_request`。

#### 1.1.1b sudo 审批（D8）

**流程：**

1. 阿撒托斯调用 `sudo_request(command, reason, cwd?, timeout?)`。
2. 星系把请求转给神殿，神殿建一条 `approvals` 记录（`pending`），推送给主人网页。
3. 主人网页**弹窗**，显示命令原文、理由、cwd、发起时间，以及阿撒托斯最近一个 turn 的摘要（帮助判断上下文）。按钮：**批准 / 拒绝**，拒绝时可以填一句理由。
4. 批准：神殿把审批结果发给阿撒托斯所在星系，星系以 root 执行**审批时显示的那条命令原文**（不重新取命令，防止审批后被替换），超时默认 10 分钟。
5. 退出码、stdout/stderr（截断）作为工具结果返回给阿撒托斯；全文写进审计记录。
6. 拒绝：工具结果返回「主人拒绝：理由」。

**调用期间：** 工具调用同步阻塞等审批。等待超过 30 分钟自动作废，返回「审批超时」，阿撒托斯这个 turn 可以继续做别的事或结束。等待期间来的新消息照常进队列。

**额外的 hook 层（体验层，不是安全边界）：** 在阿撒托斯的运行时上配置工具前置 hook（claude 的 PreToolUse / codex 对应机制）。shell 命令里出现 `sudo` 时，hook 不让它直接执行（反正也会失败），而是返回提示「请用 sudo_request」。真正的强制来自 §1.1.1a 的用户隔离：hook 被绕过也拿不到 root。

**审计：** 每条请求（包括被拒和超时的）都记录在 `approvals` 表：命令、理由、发起时间、审批人、审批时间、结果、输出摘要。网页上有单独的审批历史页。

**主人不在线时：** 请求挂起直到超时。【待定 D12】是否同时推送飞书或手机通知。

#### 1.1.2 OldOne（执行者，可以有多个）

额外字段：

| 字段 | 说明 |
|---|---|
| `current_task` | 当前绑定的 task_id，空闲时为空 |
| `task_history` | 做过的任务 id 列表，复用时参考 |
| `worktree` | 当前任务的工作目录（如果任务涉及代码仓库） |
| `rate` | 两个互不影响的限流桶：`peer: {receiver: ts}`、`report: ts`（由枢纽维护） |

| 工具 | 说明 |
|---|---|
| `register_task_probe(command, rationale, stage_note?)` | 登记 probe：一条只输出一个数字、随进展增长的命令。任务进入新阶段、旧 probe 不再反映进展时，用同一个工具**更新** probe（§2.4.1），`stage_note` 说明换阶段的理由 |
| `finish_task(evidence)` | 提交完成，附可核验的证据（路径、命令输出、commit） |
| `report(text, blocker=false)` | 向阿撒托斯汇报（经枢纽转发）。`blocker=true` 表示被卡住、需要决策。**独立限流桶**，见 §3.3 |
| `send(to=old_one, text, reply_to?)` | 只能发给其他 old_one（经枢纽，跨星系时再经神殿中转），**独立限流桶**，优先级 ≤ 8 |

以上所有工具调用都发给枢纽，由枢纽处理或转发。

### 1.2 Central Server（神殿 / temple）

神殿是**全局索引和阿撒托斯的服务端**，不再处理 old_one 的细粒度流量（D1，减少神殿压力）。

**数据归属（D1 带来的拆分）：**

| 数据 | 权威存储 | 说明 |
|---|---|---|
| 星系、阿撒托斯、记忆、主人会话、阿撒托斯收发的消息 | 神殿 | |
| Task 的**索引**：id、galaxy、description、acceptance、state、executor、最新一条总结、closed_at | 神殿 | 绑定表从这里生成。由枢纽在状态变化时推送 |
| Task 的**明细**：samples、checkpoint、events、submissions、AI 调用原文 | 枢纽本地 SQLite | 神殿要看时（`task_view`）实时向枢纽 RPC 拉取 |
| old_one 记录、old_one 之间的消息、turn_log | 枢纽本地 SQLite | 神殿只保留 old_one 的名字和状态，用于绑定表和 web 显示 |

> 星系离线时，`task_view` 只能返回神殿索引里的内容，并注明「明细不可用」。

成员：

| 成员 | 说明 |
|---|---|
| `store` | SQLite：galaxies、阿撒托斯的 messages/memory/turn_log、task 索引、old_one 名册 |
| `conns` | 在线的 agent 和星系 websocket |
| `router` | 只处理：阿撒托斯/主人 → 枢纽、枢纽 → 阿撒托斯、枢纽 → 枢纽（跨星系 old_one 消息中转，神殿不解析内容） |
| `bindings` | 任务↔old_one 绑定表，由 tasks 表推导出来，不单独存一份 |
| `waker` | 阿撒托斯的唤醒策略：直达事件立即投递，进度事件进 10 分钟汇总（§3.4） |
| `approvals` | sudo 审批队列和审计记录（§1.1.1b） |

接口：

| 接口 | 调用方 | 说明 |
|---|---|---|
| `new_task(galaxy, description, acceptance)` | 阿撒托斯 | 建 Task（`assigning` 状态），然后 RPC 调用星系的 `assign_task` |
| `bound(task_id, agent_id, reused, reason)` | 星系枢纽 | 枢纽已在本地完成绑定，神殿更新索引并通知阿撒托斯 |
| `task_index_update(task_id, patch)` | 星系枢纽 | 状态、executor、最新总结变化时推送 |
| `roster_update(galaxy, old_ones)` | 星系枢纽 | old_one 名册变化（新建、删除、状态） |
| `report(task_id, kind, text)` | 星系枢纽 | 上报阿撒托斯。神殿根据 `kind` 决定直达还是进汇总（§3.4） |
| `sudo_request / approve / reject` | 阿撒托斯所在星系 / 主人网页 | §1.1.1b |
| `relay(to_galaxy, envelope)` | 星系枢纽 | 跨星系 old_one 消息，原样转发给目标枢纽 |
| `send(to, text, priority)` | 阿撒托斯、主人 | 发给 old_one 时转给对应枢纽 |
| `close / continue / cancel` | 阿撒托斯、主人 | 转给星系枢纽执行 |
| `on_galaxy_connect(gid)` | 系统 | 下发该星系所有未结束任务的完整状态 |
| owner web API | 主人 | 查看、发消息、强停、加急 |

### 1.3 Galaxy Server（星系，含枢纽 Hub）

星系是一台机器上的守护进程，在原来的进程托管和 probe 执行之外，新增 **Hub** 模块。

**枢纽是本星系的权威（D1）：** 任务明细、old_one、old_one 消息、计时器都存在星系本地 SQLite（`<state>/hub.db`）。神殿重启或断线都不影响本星系的检查点，断线期间产生的上报和索引更新在本地排队，重连后补发。

成员：

| 成员 | 说明 |
|---|---|
| `procs` | 本机 agent 进程（沿用） |
| `db` | 本地 SQLite：tasks（明细）、old_ones、messages、turn_log、outbox（待发神殿的事件） |
| `old_one_conns` | 本星系 old_one 的连接（old_one 只连这里） |
| `timers` | 每个任务的下一个检查点（绝对时间，存在本地 db） |
| `assign_lock` | 每个星系一把锁，派单串行执行。因为绑定只在本地发生，这把锁就足以防止两个任务抢同一个 old_one，不需要神殿再做 CAS |
| `bindings_cache` | 神殿推送的全局绑定表，用于 old_one 的上下文头部和跨星系寻址 |
| `ai` | Galaxy AI 客户端（§4.3） |

Hub 接口：

| 接口 | 调用方 | 说明 |
|---|---|---|
| `assign_task(task)` | 神殿 | 决定复用还是新建（§2.2） |
| `register_task_probe(task_id, command, rationale)` | old_one（直连枢纽） | 试跑命令并判定 probe（§2.4） |
| `finish_task(task_id, evidence)` | old_one（直连枢纽） | 暂停检查点，提交审核 |
| `close_task(task_id)` | 神殿 | 停止计时器，释放 old_one |
| `continue_task(task_id, instruction)` | 神殿 | 恢复检查点，下发指令 |
| `cancel_task(task_id)` | 神殿 | 停止计时器，中断 old_one 当前 turn |
| `checkpoint(task_id)` | 计时器 | §2.5 |
| `run_probe(command, cwd)` | 内部 | 沿用现有实现 |
| `deliver(envelope)` | 神殿 | 阿撒托斯/主人发来的消息，或其他星系 old_one 的消息 |
| old_one 的 `send` / `report` | old_one | 鉴权、限流（§3.3），然后本地投递或经神殿转发 |
| `task_view(task_id)` | 神殿 | 返回任务明细 |

> **D1 已定：** 枢纽放在星系，old_one 的所有通信都经过枢纽。
>
> 代价：星系断线时，阿撒托斯只能看到神殿索引里的旧状态。但检查点、催办、old_one 互相通信照常运行，只是上报延后到达。

### 1.4 Task

| 字段 | 说明 |
|---|---|
| `id` | `t_xxx` |
| `galaxy_id` | 所属星系 |
| `description` | 阿撒托斯写的任务描述（原文，不改写） |
| `acceptance` | 验收标准，可选，阿撒托斯写 |
| `state` | 见下方状态机 |
| `executor` | 当前 old_one 的 id |
| `executors` | 历任 old_one 列表：`[{agent_id, from, to, reason}]`，重建时追加 |
| `reused` | 是否复用了已有 old_one，附 AI 给出的理由 |
| `started_at` | old_one 第一个 turn 实际开始的时间，是检查点时钟的起点 |
| `probe` | 当前 probe：`{stage, command, rationale, verdict: approved/rejected/unreviewed, reason, registered_at, baseline}` |
| `probe_history` | 历任 probe：`[{stage, command, rationale, stage_note, from, to, last_value}]` |
| `samples` | `[{ts, stage, value, error?}]`，按 stage 分段，不同 stage 的数值不做比较 |
| `checkpoint` | `{n, next_at, paused, no_probe_hits, stall_streak}` |
| `respawns` | 因未注册 probe 被杀掉重建的次数 |
| `blocker` | `{text, at}`，或为空 |
| `submissions` | `[{at, evidence, verdict, note}]` |
| `events` | 时间线：所有事件、AI 判定原文、上报内容 |
| `created_at / updated_at / closed_at` | |

**状态机：**

```
assigning ──bind──▶ awaiting_probe ──probe approved──▶ running ◀──continue_task── review
    │                    │  ▲                            │  │                       │
    │                    │  └── rejected / P10 ──────────┘  └──finish_task─────────▶│
    │                    └─ 2nd checkpoint without probe: respawn (≤1) ─▶ awaiting_probe
    │                    └─ over the respawn cap ─▶ suspended
    ▼                                                                               ▼
assign_failed                                                       closed (close_task)
任何非终态 ──cancel_task──▶ cancelled
```

- **在途：** `assigning / awaiting_probe / running / review / suspended`，处于这些状态的任务所绑定的 old_one 不可复用。
- **终态：** `closed / cancelled / assign_failed`。

---

## 2. 任务完整生命周期

### 2.1 下发

1. 阿撒托斯调用 `new_task(galaxy, description, acceptance?)`。
2. 神殿校验星系存在且在线。
   - 不在线时：**直接返回错误**给阿撒托斯，不排队。阿撒托斯可以换星系，或稍后重试。
3. 建 Task，状态为 `assigning`，RPC 调用 `assign_task`，超时 60 秒。
   - RPC 超时或失败：任务转 `assign_failed`，上报阿撒托斯（§3.4，D13）。

### 2.2 派单（复用还是新建）

1. 拿到 `assign_lock`。
2. **程序先筛候选：** 本星系中 `desired=running`、状态不是 crashed、并且**没有任何在途任务**的 old_one。
3. 候选为空：直接新建。
4. 候选不为空：调用 Galaxy AI `choose_executor`。
   - 输入：任务描述、每个候选最近的 `task_history`（标题和总结）、候选的上下文用量。
   - 输出：`{"reuse": agent_id | null, "reason": "..."}`。
   - AI 失败、超时、输出不合法，或返回的 id 不在候选里：**新建**。
   - 候选的上下文用量超过阈值【默认 70%】时，程序直接把它剔除。
5. 在本地 db 写入绑定（仍在锁内，不会被抢），释放锁。
6. 调用神殿的 `bound(...)`；神殿离线时进 outbox，重连后补发。

### 2.3 启动执行

- **新建：** `spawn_old_one`，初始 prompt 是「任务简报」，内容包括：
  - 任务描述和验收标准；
  - 绑定表；
  - 要求先调用 `register_task_probe`。
- **复用：** 给该 old_one 发一条 P9 的任务简报。
  - 如果任务涉及代码仓库：枢纽先切换或创建新的 worktree/分支，再更新 `worktree` 字段。**不在上一个任务的分支上继续改。**
- 神殿收到 `bound` 后通知阿撒托斯：「任务 t_x 已绑定 old_one Y（新建/复用：理由）」。
- **检查点时钟从 old_one 第一个 turn 实际开始时算起**（`started_at`）。
  - 如果 5 分钟内 turn 一直没开始（进程起不来、后端故障）：转 §2.8 的进程故障分支。

### 2.4 注册 probe

old_one 调用 `register_task_probe(command, rationale)` 后：

1. **程序校验：** 在 cwd 下试跑命令（超时 30 秒），要求退出码为 0，且 stdout 只有一个数字。
   - 不通过：直接返回错误和原因，**不调用 AI**，也不打断（这次调用本身就是同步返回的）。
2. **AI 判定 `judge_probe`：**
   - 输入：任务描述、命令、rationale、试跑得到的数值。
   - 输出：`{"ok": bool, "reason": "..."}`。
   - 判定为合理：`verdict=approved`，状态转 `running`，记下第一个样本。
   - 判定为不合理：`verdict=rejected`，**以 P10 打断**，要求它按原因重新注册。状态保持 `awaiting_probe`，原来的检查点计时继续。
   - AI 失败：按 `unreviewed` 先接受，状态转 `running`，下一个检查点再审一次。

#### 2.4.1 更新 probe（任务换阶段）

任务做到下一阶段时，旧 probe 可能不再增长（例如从「下载文件数」换到「训练步数」）。这时 old_one 在 `running` 状态下再调用一次 `register_task_probe`，必须填写 `stage_note`，说明上一阶段完成到哪、为什么要换。

1. **程序校验：** 和首次注册相同。
2. **AI 判定 `judge_probe`（更新模式）：** 比首次注册多三项输入：旧 probe 及其样本曲线、`stage_note`、本期 turn 摘要。AI 要同时判断两件事：
   - 新 probe 是否合理；
   - **换阶段是否真实**：上一阶段是确实做完或确实已转向，还是旧 probe 不涨了、想换一个来逃避催办。
3. **通过：**
   - `stage` 加 1，旧 probe 归档进 `probe_history`；
   - 试跑得到的值记为新阶段的 `baseline`，下一个检查点以它为起点判断增长；
   - **`stall_streak` 不清零**。只有真实的增长才能把它清零，所以换 probe 不能用来重置催办；
   - 在时间线上记一条「换阶段」事件，进入下一次 10 分钟汇总。
4. **驳回：** 同步返回驳回原因，**旧 probe 继续生效**，状态不变，不打断（和首次注册不同，因为此时手上仍有一个可用的 probe）。
5. **AI 失败：** 先接受（`unreviewed`），下一个检查点再审一次。
6. **频率：** 不限次数。但如果同一任务 1 小时内换了 2 次以上，汇总时给阿撒托斯加一条提醒。

### 2.5 检查点（每 10 分钟）

**状态为 `awaiting_probe` 时：**

| 检查点 | 动作 |
|---|---|
| 第 1 个 | **P10 打断**，强令它注册 probe（如果之前有被驳回的原因，一并附上） |
| 第 2 个 | 杀掉当前 old_one，拉起新的，把旧任务上下文交接给它（§4.2.3），并强调必须先注册 probe。`respawns` 加 1，**上报阿撒托斯**（§3.4，D13），时钟重新开始 |
| 第 2 个，且已经重建过 1 次【默认上限 1】 | 不再重建：状态转 `suspended`，停止计时，**上报阿撒托斯**（§3.4，D13），由它决定 send、cancel 或手动重启 |

**状态为 `running` 时：**

1. 采样（`run_probe`）。
   - 命令执行失败：记一个错误样本，本次按「无增长」处理，并在上报中注明「probe 失败：stderr 摘要」。
   - 连续 2 次失败：P10 打断，要求修复或重新注册 probe。
   - 增长只在**同一 stage 内**比较。换阶段后的第一个检查点和 `baseline` 比。
2. 调用 AI `judge_progress`。
   - 输入：任务描述、probe 含义、最近 N 个样本、上一次总结、old_one 自上一个检查点以来的 turn 摘要（turn_log 截断）。
   - 输出：`{"pace": "none|slow|ok", "summary": "...", "note_to_azathoth": "...", "probe_stale": bool}`。
   - `probe_stale`：AI 从 turn 摘要看出 old_one 实际已在做下一阶段，当前 probe 已不能反映进展。
   - 程序覆盖规则：样本没有增长时强制 `pace=none`，AI 不能把它判成 ok。
   - AI 失败：`pace` 由程序按是否增长决定，`summary` 改为原始样本加最近 turn 的标题。
3. 根据 `pace` 处理：
   - `none` 或 `slow`：给 old_one 发一条 **P9 催办**（附 summary），`stall_streak` 加 1。
   - `probe_stale=true` 时，催办中明确写「看起来你已进入新阶段，请用 `register_task_probe` 更新 probe」，并在上报中告诉阿撒托斯。
   - `ok`：`stall_streak` 清零。
4. **上报神殿**：内容是 summary、pace、样本。`none/slow` 时带上提醒标记。神殿把它放进下一次的 10 分钟汇总，不单独唤醒阿撒托斯（§3.4）。
5. 如果 `blocker` 不为空：本次**跳过催办**，只上报，并提醒阿撒托斯「仍在等你处理阻塞」。

> **【待定 D2】** `stall_streak` 达到 3 次时要不要做程序级动作（例如自动转 `suspended`）？目前默认只在上报中加重提醒，不自动动作。

### 2.6 完成与审核

1. old_one 调用 `finish_task(evidence)`：状态转 `review`，**暂停检查点**，再采一次最终样本。
2. 直达阿撒托斯（P8），内容包括：任务描述、验收标准、evidence、最终 probe 值、AI 最后一次总结。
3. 阿撒托斯审核：
   - `close_task(note)`：状态转 `closed`，old_one 被释放、可以复用。old_one 收到一条 P0 通知「已验收」，不会唤醒它跑新 turn【或者干脆不通知，待定】。
   - `continue_task(instruction)`：状态转 `running`，检查点从现在起重新计时 10 分钟，以 P9 把指令发给 old_one，并在 `submissions` 里记下驳回原因。
4. **审核超时【待定 D3】：** 默认 30 分钟后再提醒一次阿撒托斯；60 分钟后 `tell_owner`。不自动通过。

### 2.7 主动汇报与阻塞

- old_one 调用 `report(text)`：经枢纽、神殿**直达**阿撒托斯，不进汇总。
- old_one 调用 `report(text, blocker=true)`：写入 `blocker`，直达阿撒托斯（P8）。在阻塞期间，检查点**只上报、不催办**。
- 两者共用 report 限流桶，和 old_one 之间通信的限流**互不影响**（§3.3）。
- 阿撒托斯给该 old_one 发任何消息，或调用 `continue_task`，都会清空 `blocker`。

### 2.8 异常分支汇总

| 情况 | 处理 |
|---|---|
| old_one 进程崩溃 | 星系 supervise 自动拉起（沿用），任务不变 |
| 10 分钟内连续崩溃 3 次 | 视作一次重建（共用 `respawns` 计数），超过上限转 `suspended` |
| 后端/模型故障 | 沿用 backends 的 fallback；全部失败按进程故障处理 |
| 星系与神殿断线 | 枢纽照常执行检查点、催办和本星系通信，上报进 outbox。神殿在绑定表上标记 `galaxy_offline`，断线超过 10 分钟上报阿撒托斯（D13） |
| 星系重连 | 枢纽补发 outbox，并推送本星系所有在途任务的索引全量，神殿以此为准覆盖 |
| 星系进程重启 | 枢纽从本地 db 恢复计时器，`next_at = max(原 next_at, now + 2min)`；old_one 重新连上枢纽 |
| 神殿重启 | 不影响各星系的检查点；各枢纽重连后推送全量索引 |
| 星系机器彻底丢失 | 本地明细丢失。神殿索引里的任务转 `suspended`（galaxy_lost），由阿撒托斯决定在别的星系重新 `new_task` |
| Galaxy AI 不可用 | 按 §2.2、§2.4、§2.5 各自的 fallback 处理；**任何 AI 失败都不能让任务卡住** |
| cancel_task | 中断 old_one 当前 turn，状态转 `cancelled`，old_one 被释放（不杀进程） |
| 主人强停 old_one | 神殿转给枢纽执行；任务转 `suspended`，上报阿撒托斯（D13） |

---

## 3. 通信

### 3.1 权限矩阵（写在 roles.py）

| 发送方 → 接收方 | 方式 | 优先级 | 限流 |
|---|---|---|---|
| 主人 → 阿撒托斯 | web | 默认 9，加急为 10 | 无 |
| 主人 → old_one | web | 默认 9，加急为 10 | 无 |
| 阿撒托斯 → old_one | `send` | 0–10 | 无 |
| old_one → old_one | `send`（枢纽；跨星系经神殿中转） | 0–8 | peer 桶，见 §3.3 |
| old_one → 阿撒托斯 | `report`（枢纽 → 神殿） | 由程序决定 | report 桶，见 §3.3 |
| old_one → 阿撒托斯 | `finish_task`（枢纽 → 神殿） | 由程序决定 | 不限流（状态机保证不会重复：只有 running 状态能提交） |
| 枢纽（system）→ old_one | 任务简报 P9、probe 强令 P10、催办 P9、continue 指令 P9 | | |
| 枢纽（system）→ 阿撒托斯 | 绑定、检查点上报、告警、审核请求 | 见 §3.4 | |

### 3.1a probe 提醒页脚

枢纽在投递以下消息给 old_one 时，会**在末尾自动追加一段程序生成的页脚**：
- 检查点催办（P9）；
- 阿撒托斯或主人发来的任何消息；
- `continue_task` 的指令。

页脚内容：

```
[probe] 当前 stage 2：`<command>`，最近值 120 → 120 → 135（CP7）
如果你的工作已进入新阶段、这个 probe 不再反映进展，请调用 register_task_probe 更新。
```

页脚由程序拼接，不经过 AI，也不占阿撒托斯的 token。`continue_task` 往往意味着工作方向变了，所以页脚改为更强的措辞：「新指令可能改变了工作内容，请确认 probe 是否仍然适用」。

### 3.2 优先级语义

- **P10：** 打断接收方正在跑的 turn，立即处理。
- **P9：** 如果接收方的运行时支持，就 steer 进当前 turn；否则在下一个 turn 最先处理。
- **P0–8：** 进队列，按优先级从高到低合批处理。

### 3.3 限流（枢纽执行）

每个 old_one 有**两个独立的桶，互不影响**：发了 report 不影响找同伴，找了同伴也不影响 report。

**report 桶（old_one → 阿撒托斯）：**
- 每个 old_one 每 10 分钟 1 次 `report`（不论是否 blocker）。
- `finish_task` 不占这个桶。

**peer 桶（old_one → old_one）：**
- 规则：按**发送方→接收方**这一对计，每 10 分钟 1 条。
- 回复豁免：带 `reply_to`、回复别人发给自己的消息时，**每条来信可以免限流回复 1 次**。
- 被拒时返回 `{"error": "rate_limited", "next_allowed_at": ...}`，程序不排队。
- skill 中明确写：「问题一次写完整，包括背景、你已经试过什么、你具体要什么答案、期望的格式。」

### 3.4 阿撒托斯的消息策略（D4）

阿撒托斯收到的系统消息分两类：**直达**和**10 分钟汇总**。目的是检查点数量再多，阿撒托斯的消息量也不会随任务数线性增长，不被进度消息堵住。

**直达（立即投递）：**

| 事件 | 优先级 |
|---|---|
| `finish_task`（审核请求） | P8 |
| `report(blocker=true)` | P8 |
| `report`（非阻塞） | P5 |
| 主人的消息 | 默认 P9，加急 P10 |

**进汇总（神殿每 10 分钟发一条）：**

- 所有检查点上报：`ok`、`none/slow`、probe 失败、probe 被驳回、`probe_stale`、换阶段。
- 任务已绑定（新建/复用及理由）。
- 异常事件【待定 D13】：杀掉重建、`suspended`、`assign_failed`、星系断线、审核超时提醒。默认也进汇总，但在汇总里置顶；如果你希望这些直达，改成 P7 直达即可。

**汇总的格式：** 一条消息，P6，程序拼接，不调 AI（各任务的总结已由枢纽的 Galaxy AI 写好）：

```
[进度汇总 14:20–14:30] 在途 7 · 需关注 2
⚠ t_ab12 old_one 甲 · S2 · probe 120→120 · 无增长（连续 2 次）· 摘要…
⚠ t_cd34 old_one 乙 · 已被重建（未注册 probe）
✓ t_ef56 old_one 丙 · S1 · 37→52 · 摘要…
· 新绑定：t_gh78 → old_one 丁（复用：之前做过同仓库的部署脚本）
```

- 「需关注」的条目排在前面，其余按任务排。
- **空汇总不发：** 这 10 分钟里没有任何事件时跳过。
- 汇总的发出时间按神殿的固定 10 分钟节拍，和各任务自己的检查点节拍无关，所以一个任务的检查点结果最多晚 10 分钟到达阿撒托斯。
- 汇总消息若在队列中还没被处理，下一条汇总**合并进同一条**（替换内容），不堆积。

---

## 4. 上下文管理

### 4.1 阿撒托斯

- **system prompt（稳定，利于缓存）：** 角色 skill、权限说明、工具说明。不放任何会变化的内容。
- **永久记忆：** `memory` 表。达到约 100k token 时：
  1. 让阿撒托斯写一份压缩记忆；
  2. 开新 thread，把压缩记忆注入。
- **每轮动态头部**（拼在输入最前面，由程序生成）：
  - **绑定表**：所有在途任务，加上最近 24 小时结束的任务，每行一条：

    ```
    t_ab12 | gx-main | old_one 甲(o_33) | running | CP4 · S2 · probe 37→52 · ok | 修复部署回滚脚本…
    ```

  - 汇总不放进动态头部，它以独立消息的形式到达（§3.4）。
- **按需拉取：** `task_view(task_id)` 返回完整时间线、所有总结和 submissions。`list_tasks(filter)` 用来查历史。

### 4.2 old_one

#### 4.2.1 线程

- 每个 old_one 有一个线程；**复用时沿用**，因为保留之前的上下文正是复用的意义。
- 复用前，如果上下文用量超过阈值【70%】，枢纽不会选它（§2.2）。
- 运行中上下文超过 85%：用运行时的 compact 能力（或 handoff 新 thread）压缩，任务不中断。

#### 4.2.2 每轮动态头部

- 自己的当前任务：描述、验收标准、当前 stage 和 probe、本 stage 最近 5 个样本、当前是第几个检查点、上次催办的内容。
- 绑定表，只保留在途任务的精简版，用于知道该找谁问问题。
- 限流状态：对每个对象下一次可以发消息的时间（只列最近通信过的）。

#### 4.2.3 交接（杀掉重建时）

- 交接内容：
  - 任务简报原文；
  - `prepare_handover` 生成的交接摘要：已经做了什么、产物在哪、卡在哪；
  - 原始 turn_log 的文件路径，需要时自己读；
  - 醒目的要求：「你的前任因为 20 分钟未注册 probe 被替换，第一件事是 `register_task_probe`」。
- 不直接塞入全量原始上下文。

- 动态头部由**枢纽**生成（它有本地任务明细和神殿推送的绑定表），不需要访问神殿。

#### 4.2.4 skill（old_one.md）要点，保持简短

1. 第一件事：注册 probe（什么样的 probe 合格、给出示例）。工作进入新阶段、probe 不再增长时，立刻用同一个工具更新 probe 并说明理由。换 probe 不会清零催办计数，不要用它来逃避催办。
2. 每 10 分钟有检查点；收到 P9 催办，要么加快，要么用 `report(blocker=true)` 说明阻塞。
3. 两条限流规则（找阿撒托斯、找同伴各自独立，都是 10 分钟一次），以及「一次写完整」的要求。
4. 完成后调用 `finish_task`，附可核验的证据。

### 4.3 Galaxy AI（无状态）

- **在枢纽进程里调用**，不经过神殿。
- **不是 Agent：** 没有线程，没有记忆。每次调用都是一次独立的 Messages API 请求，所需上下文全部由程序传入。
- 四个函数，每个有固定的 system prompt 和 JSON schema：

| 函数 | 输入 | 输出 |
|---|---|---|
| `choose_executor` | 任务描述，候选的 `{id, 最近 3 个任务的标题和总结, 上下文用量}` | `{reuse, reason}` |
| `judge_probe` | 任务描述、命令、rationale、试跑数值；更新模式另加旧 probe 及样本、`stage_note`、本期 turn 摘要 | `{ok, reason}`；更新模式另加 `{stage_change_genuine}` |
| `judge_progress` | 任务描述、probe 含义、当前 stage 最近 N 个样本、上一次总结、本期 turn 摘要（截断到约 4k token） | `{pace, summary, note_to_azathoth, probe_stale}` |
| `summarize_for_handover` | 任务简报和截断后的 turn_log | `{done, artifacts, stuck_on, next_steps}` |

- **模型：** 默认用便宜的模型【待定，例如 Haiku 档】，可以在 web 的「模型设置」页按星系配置。
- **调用约束：**
  - 超时 60 秒，重试 1 次，然后走 fallback；
  - 每次调用的输入输出原文写进任务 `events`，用于审计。
- **跨调用的「记忆」只有一种：** 程序把上一次的 summary 传进下一次调用。

---

## 附录 A：从 v2 删除或迁移的内容

- 角色：shub、ghroth、yog，相关 skill 和 roles.py 条目。
- 任务机制：延期（extend / decide_extension）、deadline、三级升级阶梯、尤格的 verdict 和 probe_verdict、主人兜底验收。
- 现有在途任务：迁移时一律转 `suspended`，交给阿撒托斯逐个处理。
- 格赫罗斯原来的 sudo 和 harness 维护职能：转给阿撒托斯（D5，§1.1.1a）。`cthulhu-v2-ghroth` 服务改造成阿撒托斯的专用星系。
- old_one 记录、消息、turn_log：从神殿迁到对应星系的 hub.db。离线星系的 old_one 等它上线时再迁。
- agent.py 的连接目标：old_one 从神殿改为本星系枢纽。

## 附录 B：待定事项

| # | 问题 | 默认方案 |
|---|---|---|
| D1 | 枢纽逻辑放在星系还是神殿 | **已定**：放在星系，任务明细和 old_one 通信都归枢纽，神殿只存索引 |
| D2 | `stall_streak` 是否触发程序动作 | 只加重提醒 |
| D3 | 审核超时的处理 | 30 分钟提醒，60 分钟 tell_owner |
| D4 | 检查点进度如何到达阿撒托斯 | **已定**：神殿每 10 分钟发一条汇总；`finish_task`、`report` 直达 |
| D5 | harness 自身由谁维护 | **已定**：阿撒托斯拿 sudo 直接维护，运行在只跑它一个 agent 的专用星系 |
| D6 | `close_task` 后是否通知 old_one | 发 P0 通知，不触发新 turn |
| D7 | 重建上限、上下文阈值 | 重建 1 次；复用阈值 70%，压缩阈值 85% |
| D8 | 阿撒托斯拿 root 后的防护 | **已定**：每条 sudo 命令由主人在神殿网页弹窗批准；靠 OS 用户隔离强制，hook 只做体验层 |
| D9 | old_one 能否 `send` 给其他星系的 old_one | 能，经神殿 `relay` 中转，神殿不解析内容 |
| D10 | 主人在 web 上看 old_one 对话/明细 | 神殿实时向枢纽 RPC 拉取，不在神殿留副本 |
| D11 | 换 probe 是否限次数 | 不限次数；1 小时内换 2 次以上提醒阿撒托斯；换 probe 不清零 `stall_streak` |
| D12 | 主人不在网页时，sudo 审批是否另外推送通知 | 不推送，挂起 30 分钟超时 |
| D13 | 重建、suspended、assign_failed、星系断线是否直达 | 进汇总并置顶 |
