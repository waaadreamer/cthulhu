# 任务短名与独立过程汇报

开发基线：`fb48835320ef4e6095b41bf9e8446534e5bd792c`。独立分支：`task/t_424119a69db5b030`。未修改运行发布目录、未重启或部署。

## 已证实的链路问题

- `Chat.tsx` 的 `group()` 按 `task_id` 创建唯一 `TaskView`，其中“过程 · N 步”就是现有任务过程容器。旧 `send_digest` 把所有任务拼成一个没有 `task_id` 的文本，先调用 `note_owner`；后者每次生成随机 `note` ID，新建聊天 turn。阿撒托斯再 `tell_owner` 时又新建一次，因此两次都可能绕过任务过程容器。旧 `tests/test_owner_digest.py` 甚至断言两个任务只生成一个混合 turn。
- 旧 `Azathoth.internal` 忽略空 ID 后计算唯一任务。如果批次是“任务 A 的 report + 无 ID 的混合 digest/全局事件”，整个输出会被标成 A；两个非空任务 ID 则全部失去归属。新混批回归以 A、无 ID digest、B、全局消息同批复现并验证独立来源。
- 旧标题分别截取描述 40/60/100 字，消息侧栏直接显示长描述；消息执行者名字取当前目录，复用后历史显示可能漂移。
- 旧 `Hub.turn_digest` 只读取已结束回合的 input/reply，以及 `status.current` 的短输入提示。恢复中回合的实时工具活动存在 `Agent.latest.live`、`activity_task`、`last_activity_at`，却没有被读取。普通 `report_up` 直接转发，也没有进入持久任务事件。因而新 commit/report 可以实际存在，摘要输入仍主要是旧失败回合。这里的结论由代码与模拟恢复测试证实；没有越权读取生产数据库或 owner-digest 文件。

## 实现

复用 `digest`、`messages`、`chat_turns` 和 `TaskView`。每期每任务一个 `digest_<period>_<task_id>`；全局事件用独立 global 项。队列消息、初始过程行、消耗的 digest 事件在同一 SQLite 事务提交，不再拼接旧 mixed digest，也不额外发送第二条事实聊天。

管理回合逐来源消息处理。`tell_owner` 带原始消息 ID，服务端验证该消息所属任务，再补全同一时期的 turn。已核验综合摘要成为主行；旧检查点快照及实际操作参数、结果、错误保留在“依据与操作”展开区。管理者可以纠正旧 403/0 步判断，主行不再同时断言被纠正的结论。尚无综合摘要时明确标注“待核实”。

管理操作继续走原有处理器；成功的同来源、同参数重放复用已记录结果，避免再次操作和重复追加。失败后允许真实重试。指令投递只记“已投递，待执行者回报”，不伪造任务已经执行完毕。异任务操作结果归属于实际目标任务。

任务短名只来自新任务工具的 `short_name` 或管理者批准的一次性迁移映射；应用逻辑不从任意 prompt 抽取短句，也没有针对当前任务的正则。缺失时用稳定的“任务 <ID尾部>”占位，并标记可补名；确认后不可改名。完整描述继续保存在详情中。目录随当前绑定显示任务名，历史标题按历史任务 ID 查找。

检查点继续使用原有 `GalaxyAI.judge_progress`，没有新增总结队列或后端。普通 report 保存到现有任务事件；检查点通过现有 `latest_result` 读取未结束回合活动。回合日志增加任务 ID；输入严格按任务、绑定时间和本期时间窗过滤，不混入复用执行者的旧任务。失败回合仅计为失败事实，不能等同没有产物。AI 失败只给探针变化及总结不可用提示，不回填原始 prompt/last_reply。

前端按 turn ID 和更新时间合并实时数据，重连同时补最新页和已加载的历史 ID，防止迟到判断在最新页之外丢失。晚到期次更新不会覆盖下一期；摘要之后的新进展仍在同一过程容器内。

## 迁移与部署准备

`task-names-20261015.json` 是阿撒托斯核验全部 13 项历史/在途任务后提供的映射，不含任务 prompt 或凭据。此次开发仅在临时测试数据库验证，未应用到生产。

审核、合并后由部署负责人整合源码并重新构建 static。部署审批时先备份中心数据库，在停止写入的发布窗口执行：

```sh
venv/bin/python scripts/task_short_names.py --db /path/to/temple.db --mapping docs/task-progress/task-names-20261015.json
venv/bin/python scripts/task_short_names.py --db /path/to/temple.db --mapping docs/task-progress/task-names-20261015.json --apply
```

首条命令只读预检；第二条只有全部未命名任务均被映射覆盖、所有 ID 存在、长度有效且不冲突时才应用。映射幂等；已确认名称不可覆盖。如果发布前又新增旧客户端任务，先由管理者补齐映射。数据库迁移只增加列/索引，保留历史消息、原摘要、任务描述及操作记录。

发布后验收两个任务各自唯一过程容器、连续两期各一行、故障降级、迟到更正、重连与历史标题。应用与数据库按同一备份点回滚；旧版本不应继续生成混合摘要。

## 验证入口

```sh
venv/bin/python -m unittest discover -s tests -p test_owner_digest.py -v
venv/bin/python -m unittest discover -s tests -p test_checkpoint_evidence.py -v
venv/bin/python -m unittest discover -s tests -v
npm --prefix web run build
PLAYWRIGHT_BROWSERS_PATH=/tmp/task-report-browsers venv/bin/python tests/ui_task_progress.py
```

浏览器脚本启动临时数据库与仅监听 localhost 的真实 Temple，使用构建后的页面与真实 HTTP/WebSocket；不调用生产 API。最终测试结果与截图在交付时追加。

## 当前核验结果

- owner digest 专项：25 项通过；checkpoint 恢复证据专项：4 项通过，含 RPC 期间快照更新的确定性时钟边界。
- 短名迁移 CLI：只读预检 13/13、首次应用 13 项、重复应用 0 项；全程使用临时 fixture 数据库。
- 前端构建成功，Vite 2m39s；已核验产物包含重连历史 ID 刷新与“依据与操作”抽屉。只有既有的大 chunk 提示。
- 真实 HTTP/WebSocket 浏览器断言通过：任务隔离、连续期次、迟到更正、操作追溯、重复事件、重连、刷新、侧栏短名；截图 `chat.png`、`sidebar.png`。
- 全量命令已执行：194 项 / 824.466s，3 项失败、2 项跳过。两项 `test_priority.Preemption` 分别在等待 urgent done 10s、原 job handled 40s 超时；同样两项在 `fb48835` 基线独立重跑 50.557s 全通过，候选独立重跑 49.347s 全通过，没有改超时或抢占实现，初次失败的时序原因未证实。第三项 v3 生命周期旧断言依赖 digest 中的 `⚠` 字符；改为核对任务 ID、稳定 turn ID 与事实归属并保留完整生命周期覆盖，专项结果见后续追加。

探针语义严格由 `probe_meaning` 决定：字节/行数增长不等于训练 step 大于零或实现完成，必须有独立证据。摘要不会把旧失败回合正文冒充当前状态，也不会把实时工具命令冒充完成证明。
