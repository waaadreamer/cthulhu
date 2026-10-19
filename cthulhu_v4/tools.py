"""唯一的工具目录，Codex 与 Claude 均通过同一个 MCP bridge 使用。"""
S = {'type': 'string'}
I = {'type': 'integer'}
B = {'type': 'boolean'}


def spec(name, description, props=None, required=()):
    return {'name': name, 'description': description,
            'annotations': {'readOnlyHint': name in {'list_tasks', 'read_task_events', 'list_questions', 'read_messages', 'list_nodes'},
                            'destructiveHint': name in {'control_task', 'memory'}, 'openWorldHint': False},
            'inputSchema': {'type': 'object', 'properties': props or {},
                            'required': list(required), 'additionalProperties': False}}


COMMON = [
    spec('list_nodes', '查看星系节点在线状态、容量与原生后端；任务必须绑定可执行目标的节点。'),
    spec('list_tasks', '查看任务、DDL、执行者与等待状态。'),
    spec('read_messages', '读取自己最近的收发消息和处理状态，恢复时核对 failed/unknown 输入，不能直接重放副作用。', {'limit': I}),
    spec('read_task_events', '读取与侧边栏完全相同的原始事件；按 next_after 翻页，不把截断当作没有进展。',
         {'task_id': S, 'after': I, 'limit': I}, ['task_id']),
    spec('send_message', '异步发送消息；10 打断当前回合并优先执行（仅 Master 可用），其余只排序；返回只代表入队。无需对无信息增量的消息回复。',
         {'to': S, 'text': S, 'priority': I, 'reply_to': S}, ['to', 'text']),
    spec('wait_for_reply', '当前无独立工作可做时，登记等待某条已发送消息。不会阻塞工具调用。',
         {'message_id': S, 'seconds': I}, ['message_id']),
]
MASTER = [
    spec('create_task', '创建任务与唯一 old_one。deadline 为带时区 ISO 时间；node 选择星系节点 ID（local 表示中心机），cwd 必须是该节点上的现有绝对目录。',
         {'title': S, 'description': S, 'acceptance': S, 'deadline': S, 'cwd': S, 'node': S,
          'backend': {'type': 'string', 'enum': ['codex', 'claude', 'demo']}, 'model': S},
         ['title', 'description', 'acceptance', 'deadline', 'cwd']),
    spec('tell_owner', '主动向主人主聊天框汇报判断、操作结果和风险。', {'text': S}, ['text']),
    spec('list_questions', '读取问题、当前版本与等待期限。'),
    spec('answer_question', '回答问题；必须提交读取时的 version，旧版本拒绝，防止覆盖主人晚到的回答。',
         {'question_id': S, 'version': I, 'answer': S}, ['question_id', 'version', 'answer']),
    spec('ask_owner', '将执行者的问题转请主人回答；默认五分钟后退回 master 自行决定。',
         {'question_id': S, 'version': I, 'recommendation': S}, ['question_id', 'version', 'recommendation']),
    spec('control_task', '暂停/恢复/取消；暂停会中断原生进程，恢复前应检查外部作业。',
         {'task_id': S, 'action': {'type': 'string', 'enum': ['pause', 'resume', 'cancel']}, 'reason': S},
         ['task_id', 'action', 'reason']),
    spec('set_deadline', '修改期限并公开理由；必须符合主人授权，不可偷偷延期。',
         {'task_id': S, 'deadline': S, 'reason': S}, ['task_id', 'deadline', 'reason']),
    spec('finish_review', '先 list_tasks 再读取增量并提交报告；无新增信息返回 skipped，不补发重复报告。记录事件阅读位置。',
         {'text': S, 'cursors': {'type': 'object', 'additionalProperties': I}}, ['text', 'cursors']),
    spec('memory', '读取或维护偏好。推测不得当成授权；source 写明依据。读取不传 key。',
         {'key': S, 'text': S, 'source': S, 'inferred': B, 'delete': B}),
]
WORKER = [
    spec('ask_master', '提交问题并立即对主人公开。blocking=true 时登记等待；仍可做独立工作。',
         {'text': S, 'recommendation': S, 'blocking': B}, ['text', 'recommendation', 'blocking']),
    spec('finish_task', '只有实际交付并验证后才能完成，证据为路径/命令结果/结论。',
         {'result': S, 'evidence': S}, ['result', 'evidence']),
    spec('wait_until', '等待外部作业，记录原因和句柄；到时唤醒核验，不自动认定作业结束。',
         {'reason': S, 'seconds': I, 'operation': S}, ['reason', 'seconds', 'operation']),
]


def tools_for(agent):
    return ([t for t in COMMON if t['name'] != 'wait_for_reply'] + MASTER
            if agent == 'master' else COMMON + WORKER)
