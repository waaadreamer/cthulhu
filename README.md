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
- **Agent**（`cthulhu/agent.py`）：一个 agent 对应一个进程，包含一个 `codex app-server` 子进程和一条连到中心的 websocket。接口是 `send`、`receive`、`latest_result`。消息按优先级（0–10，高先）排队，同一批消息合并成一轮处理；优先级 10 立即打断当前回合，被打断的消息排在它之后续办。主人消息默认 9，界面“加急”为 10；旧日支配者不能发 10（`roles.py`）。
- **角色子类**（`cthulhu/gods.py`）：决定哪些内容进永久上下文、持有哪些工具、被什么唤醒。
- **运行时**（`cthulhu/runtime.py`）：Codex app-server 的 stdio JSON-RPC 适配。以后接 Claude Code 只需实现同一组方法。

身份由中心签发：agent token 是 `HMAC(secret, id:epoch)`，由星系通过环境变量传给 agent 进程，不进入模型的 shell 环境。终止或重启会递增 epoch，旧 token 随即失效。

## 权限矩阵（`cthulhu/roles.py`）

| 发送 → 接收 | 阿撒托斯 | 黑山羊 | 尤格 | 格赫罗斯 | old_one |
|---|---|---|---|---|---|
| 主人 | ○ | | | ○ | |
| 阿撒托斯 | | ○ | ○ | ○ | |
| 黑山羊 | ○ | | | | ○ |
| 尤格 | ○ | ○ | | | ○ |
| 格赫罗斯 | ○ | | | | ○（只限自己的） |
| old_one | | ○ | | ○ | ○ |

管理者（黑山羊、格赫罗斯）和 old_one 之间只能在自己的团队内互发消息。

其他能力：
- 创建、终止、重启、删除 old_one：两位管理者（黑山羊、格赫罗斯），各自只管自己创建的。格赫罗斯的 old_one 在格赫罗斯的星系上、在 harness 仓库各自的 git worktree（分支 `old/<ID>`，禁止推送）里工作。删除只针对已停止的 agent，历史保留。
- 强制停止 / 恢复任意 agent（含三柱神和格赫罗斯）：只有主人（网页上每个 agent 的「强制停止」按钮）和本机维护（`python -m cthulhu stop|resume <agent_id> [--config /etc/cthulhu/temple.json]`）。停止立即中断当前回合并结束其模型进程，排队中的消息作废（`cancelled`，恢复后不执行），停止期间发给它的消息直接拒收；状态持久化，重启后仍然停止。被强制停止的 old_one 黑山羊不能重启。
- `latest_result`：只有尤格。
- 读取全部消息和注册表：阿撒托斯和黑山羊。
- 主动发消息给主人（`tell_owner`）：阿撒托斯，以及神殿的任务军纪（程序写好的通知）。阿撒托斯处理下属汇报时每次都必须调用 `tell_owner`（漏调时程序先提醒，仍然漏调就直接投递最终回复）；军纪通知不需要。
- 任务的创建、探针、延期、验收见下面“任务军纪”。

## 上下文

| 角色 | 永久上下文（程序注入 developer instructions） | 临时上下文 |
|---|---|---|
| 阿撒托斯 | 只从和主人的对话中提炼（新增 / 改写 / 删除）；下属汇报只能更正或删除已有条目 | 原生线程：主人对话、收发的所有消息 |
| 黑山羊 | 只提炼阿撒托斯定下的长期规则和稳定事实，不记具体任务；外加 old_one 名册（初始化参数），变化时自动刷新 | 原生线程 |
| 尤格 | 和阿撒托斯的交流提炼 | 原生线程：探针审查、验收、诊断 |
| 格赫罗斯 | 和主人对话的提炼 | 原生线程 |
| old_one | 黑山羊写的职责和任务 | 原生线程（可以使用 Codex 子 agent） |

永久上下文只记高层次的意志和规则（目标与关注点、偏好、决定与授权、约束、世界的稳定事实），单条不超过 120 字，超长的条目会被程序拒收。数据、路径、调查结论这类细节留在消息历史里，阿撒托斯和黑山羊用 `temple_messages`（按任务或关键词搜索）和 `temple_tasks` 查阅。总量超过 50k tokens 时，程序会做一次整体整理。每个条目带编号、创建时间和最后更新时间（`updated_at`），改写保留编号。条目变动后，正在使用的线程在下一轮开头收到 `<memory_update>` 增量，不必重开线程。主人也可以在网页的永久上下文面板里直接编辑或删除条目。

临时线程的上下文占用超过 80% 时，程序会换一个新线程，新线程只带永久上下文。

## 任务军纪（`cthulhu/tasks.py`）

任务是一等对象（`tasks` 表：creator / manager / executor / deadline / acceptance / probe，检查点、延期、提交、事件在 `data` JSON 里），状态 `active → (blocked_on_owner) → submitted → accepted | failed`。

- 创建：`task_create`（阿撒托斯 → 管理者；管理者 → 自己的 old_one）或管理者的 `spawn`。执行者、验收标准必填，截止默认 120 分钟。old_one 必须绑定一个任务。
- 探针：执行者用 `task_probe` 登记一条 shell 命令，输出单个数字。执行者所在星系以它的 OS 用户、在它的工作目录运行（超时 30 秒）。数字比上一检查点严格增大才算进展，非数字算无进展。
- 检查点：神殿自己的时钟，每个进行中的任务每 `checkpoint_interval`（默认 600 秒）一次，不依赖任何模型。第一个检查点没有探针 → 作废（任务 failed，old_one 执行者被停止，通知主人、阿撒托斯、管理者）；第一个检查点请尤格审查探针，判为无效同样作废。连续 1 次无进展 → 提醒执行者；2 次 → 通知直接上级（old_one 的上级是其管理者，管理者的上级是阿撒托斯）并请尤格诊断；3 次或超过截止 → 程序写好的消息直接进主人聊天、并通知阿撒托斯，之后每 3 个检查点重复，主人可暂停。程序消息优先级 9。排空中或星系离线的任务顺延，不算停滞。
- 延期 `task_extend`：第 1 次直接上级批准，第 2 次阿撒托斯，第 3 次起只有主人（网页按钮）；单次不超过原时长；上个检查点无进展直接拒绝；执行者不能自批。全部记录。
- `task_block`：暂停计时、立即通知主人；解除后截止顺延暂停的时长。
- 验收：`task_submit` 冻结证据。old_one 的任务由尤格 `task_verdict`；一个检查点内未裁决就转给主人。管理者的任务由阿撒托斯或主人裁决。驳回回到 active，计时不重置。汇报不能关闭任务。
- 阿撒托斯的 `task_unblock` / `task_replace` / `task_extension` 每次都由程序抄送主人。
- 上线迁移：旧的 open 任务（交给管理者）得到 `task_grace`（默认 600 秒）宽限期登记探针，否则作废；done 记为 accepted。迁移只在第一次启动时改写，重启幂等。

尤格是独立质检，只被这三类军纪消息唤醒：探针审查、验收、诊断。每轮最终回复的中文结论记入巡检记录。

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

## 一键部署（更新已有星系）

中心神殿可以把 GitHub `main` 上的某个版本部署到自己和所有星系，有三个入口：

- 网页"总览"里的"部署"面板
- 阿撒托斯的 `temple_deploy` 工具
- 格赫罗斯的 `temple_deploy_request`：需要阿撒托斯用 `temple_deploy(request_id=...)` 批准后才会执行

执行流程（`cthulhu/deploy.py`）：

1. 共用同一份代码（同一台机器、同一个代码目录）的星系分成一组。中心神殿所在的那组先部署。
2. 每组先排空：沿用 maintenance/drain 机制，等正在进行的回合和记忆整理结束，最多等 30 分钟。
3. 更新代码，要求：
   - 目标版本必须在 `origin/main` 上
   - 只允许快进
   - 工作区干净
   - `pyproject.toml` 有变化时重装依赖
   - 导入检查失败就回滚
4. 让星系退出，由 systemd 或 `run.sh` 用新代码重新拉起，然后等它以新版本连回神殿。
5. 解除排空。中心神殿最后自己重启；重启后从数据库里接着把部署任务跑完，并把结果发给阿撒托斯。

失败时的回滚：

- 本地组由神殿回滚。
- 远程星系由启动保护（`deploy.guard_startup`）回滚：新版本连续 3 次没能连上神殿，就把代码退回旧版本。

还没有 `deploy_v1` 能力的旧星系需要现场升级一次。
异常重启接续、回滚重试入口及保留既有配置的首次升级步骤见 [部署恢复](docs/deploy-recovery.md)。

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
- 维护者修复已有旧日支配者的模型配置可在中心本机运行 `venv/bin/python -m cthulhu.ops --config /etc/cthulhu/temple.json runtime --agent-id old_xxxx --runtime-json '{"backend":"codex","model":"gpt-6-astra"}'`。复用相同校验与热切换，不重启或重新投递任务；`null` 恢复默认。返回的是已保存配置，实际生效状态需再用 `status` 核验。
- 可恢复的 API/后端失败（比如中转站限流）默认只重试 1 次：首次尝试和这次重试都失败后，沿现有上下文核验与交接流程切到已配置后备；后备也最多重试 1 次。从切到后备的时刻起保留固定 30 分钟冷却，后续成功回合不延长截止时间；到期时正在执行的后备回合安全完成，下一回合再尝试主源（`backends.py: RETRY_DELAYS`、`FALLBACK_HOLD`）。鉴权或模型不支持等不可恢复错误直接如实报错，没有后备或后备不可用时不会宣称恢复成功。运行状态的 `recovery_policy` 给出实际默认重试数与冷却秒数。
- 两种后端的会话不互通。每个 agent 的每一轮都会记进中心的 `turn_log`（与后端无关）。换到新会话时，程序把本会话没见过的最近 12 轮放进 `<handoff>` 块带过去，并注明这些请求已经处理过，不要重新执行。
- Claude 后端的凭证在 `/etc/cthulhu/claude.env`（0600，root），由两个星系服务通过 systemd 的 `EnvironmentFile` 读取。

## 开发

```bash
python3 -m unittest discover -s tests -v      # 端到端：真实 temple/galaxy/agent 进程 + 假 codex
cd web && npm install && npm run build        # 前端构建到 cthulhu/static；线上检出目录有 .served-checkout 标记时拒绝构建，须在另一份 clone 构建后走托管部署
```

## TODO

- 同任务消息 steer 进正在运行的回合（优先级 10 已实现为打断当前回合，见 agent.py: preempt）。
- 奈亚拉托提普（飞书入口）。
