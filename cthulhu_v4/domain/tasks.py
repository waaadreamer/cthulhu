"""任务状态机：running / waiting / paused / completed / cancelled。

期限不随等待自动顺延；等待必须有显式原因和句柄；暂停与取消是控制操作，
与消息优先级无关。所有方法都在调用方的事务内执行，不跨 await。
"""
import json
from pathlib import Path

from ..util import LOCAL, TERMINAL, deadline


class Tasks:
    def create_task(self, p, initiator='master'):
        s = self.store
        node = p.get('node') or ''
        remote = [n for n in s.all('node') if n['id'] != LOCAL]
        if not node:
            # 有远端节点时漏填 node 直接拒绝，不能静默落到中心机执行。
            if remote:
                raise ValueError('已有星系节点，create_task 必须明确指定 node（中心机为 local）')
            node = LOCAL
        n = s.get('node', node)
        defaults = n.get('defaults') or {}
        # 工作目录决定原生 CLI 读到哪个 AGENTS.md / CLAUDE.md：显式 > 节点默认，绝不静默用 / 或家目录。
        raw = p.get('cwd') or defaults.get('cwd')
        if not raw:
            raise ValueError(f'节点 {node} 没有默认工作目录，必须提供 cwd（该节点上的项目绝对路径）')
        cwd = Path(str(raw)).expanduser() if node == LOCAL else Path(str(raw))
        if not cwd.is_absolute() or (node == LOCAL and not cwd.is_dir()):
            raise ValueError('cwd 必须是该节点上存在的绝对目录')
        due = deadline(p['deadline'])
        master = s.get('agent', 'master')
        backend = p.get('backend') or defaults.get('backend') or master['backend']
        if backend not in {'codex', 'claude', 'demo'}:
            raise ValueError('不支持的 backend')
        if n.get('backends') and backend not in n['backends']:
            raise ValueError(f"节点 {node} 未报告可用的 {backend}；可用：{', '.join(n['backends'])}")

        def inherit(key):
            # 模型名只对同一后端有意义：显式 > 节点默认（同后端）> Master（同后端）。
            if p.get(key):
                return p[key]
            if defaults.get(key) and defaults.get('backend') == backend:
                return defaults[key]
            return master.get(key) if backend == master['backend'] else None

        for key in ('title', 'description', 'acceptance'):
            if not str(p.get(key, '')).strip():
                raise ValueError(f'{key} 不能为空')
        ident, aid = self.uid('t'), self.uid('old')
        task = {'id': ident, 'agent': aid, 'title': p['title'], 'description': p['description'],
                'acceptance': p['acceptance'], 'node': node, 'deadline': due, 'original_deadline': due,
                'state': 'running', 'wait': {}, 'created_at': self.clock(), 'review_cursor': 0,
                'recoveries': 0, 'overdue_notified': False}
        s.put('task', task)
        s.put('agent', {'id': aid, 'node': node, 'backend': backend, 'model': inherit('model'),
                        'reasoning_effort': inherit('reasoning_effort') if backend == 'codex' else None,
                        'cwd': str(cwd.resolve()) if node == LOCAL else str(cwd), 'session': None,
                        'status': 'idle', 'failures': 0, 'idle_at': self.clock(), 'paused': False})
        s.emit('task_created', f"创建任务：{task['title']}（{node}）", actor=initiator,
               task=ident, data=task, visible=True)
        self.enqueue(initiator, aid, '你的唯一任务：\n' + json.dumps(task, ensure_ascii=False), 9)
        return task

    def clear_wait(self, task):
        paused = self.store.get('agent', task['agent'])['paused']
        task.update(state='paused' if paused else 'running', wait={})
        self.store.put('task', task)

    def control(self, p, actor='master'):
        s = self.store
        task = s.get('task', p['task_id'])
        a = s.get('agent', task['agent'])
        action = p['action']
        if task['state'] in TERMINAL:
            raise ValueError('任务已结束')
        if action == 'resume':
            if self.hub.busy(a['id']):
                raise ValueError('上一个回合正在结束，请等待节点确认或租约到期后恢复')
            a.update(paused=False, status='idle', failures=0, idle_at=self.clock())
            task.update(state='waiting' if task.get('wait') else 'running', recoveries=0)
            s.put('agent', a); s.put('task', task)
            self.enqueue('master', a['id'], '恢复任务。先核验已有产物、旧进程和外部作业，避免重复执行。', 9)
        elif action in {'pause', 'cancel'}:
            a['paused'] = True
            task['state'] = 'paused' if action == 'pause' else 'cancelled'
            if action == 'cancel':
                task['wait'] = {}
                self.close_questions(task['id'])
            s.put('agent', a); s.put('task', task)
            self.cancel_queued(a['id'])
            # 控制信号独立于消息优先级；停止当前进程，但保留原生 session 以便恢复。
            self.stop_turn(a['id'], action)
        else:
            raise ValueError('action 应为 pause/resume/cancel')
        s.emit('control', f"{action}: {p.get('reason', '')}", actor=actor, target=a['id'],
               task=task['id'], visible=True)
        return task

    def archive_task(self, p):
        """归档只是收起：任务、事件、原生会话都保留，随时可取消归档。只允许已结束的任务。"""
        s = self.store
        t = s.get('task', p['task_id'])
        archived = bool(p.get('archived', True))
        if archived and t['state'] not in TERMINAL:
            raise ValueError('只能归档已完成或已取消的任务；进行中的任务请先取消')
        t['archived'] = archived
        s.put('task', t)
        s.emit('archive', ('归档' if archived else '取消归档') + '：' + t['title'], actor='owner', task=t['id'])
        return t

    def active_task(self, actor):
        t = self.task_for(actor)
        if not t or t['state'] in TERMINAL or t['state'] == 'paused':
            raise ValueError('无可操作的在途任务')
        return t

    def finish_task(self, actor, p):
        s = self.store
        t = self.active_task(actor)
        if not str(p.get('evidence', '')).strip():
            raise ValueError('完成必须附证据')
        t.update(state='completed', result=p['result'], evidence=p['evidence'], wait={},
                 completed_at=self.clock())
        s.put('task', t)
        self.close_questions(t['id'])
        s.emit('completed', p['result'], actor=actor, task=t['id'], data={'evidence': p['evidence']}, visible=True)
        self.enqueue(actor, 'master', f"任务 {t['id']} 完成，请核验并告诉主人：{p['result']}\n证据：{p['evidence']}", 5, copy=True)
        self.cancel_queued(actor)
        return t

    def wait(self, actor, name, p):
        s = self.store
        t = self.active_task(actor)
        seconds = max(1, min(int(p.get('seconds', 300)), 86400))
        if t.get('wait'):
            raise ValueError('已有等待事项，不能覆盖')
        if name == 'wait_for_reply':
            m = s.get('message', p['message_id'])
            if m['sender'] != actor:
                raise ValueError('只能等待自己发出的消息')
            if any(r.get('reply_to') == m['id'] for r in s.all('message')):
                return {'already_replied': True}
            wait = {'kind': 'reply', 'message_id': m['id'], 'until': self.clock() + seconds}
        else:
            wait = {'kind': 'operation', 'reason': p['reason'], 'operation': p['operation'],
                    'until': self.clock() + seconds}
        t.update(state='waiting', wait=wait, recoveries=0)
        s.put('task', t)
        return t

    def set_deadline(self, actor, p):
        s = self.store
        t = s.get('task', p['task_id'])
        old = t['deadline']
        t.update(deadline=deadline(p['deadline']), overdue_notified=False)
        s.put('task', t)
        s.emit('deadline', p['reason'], actor=actor, task=t['id'], data={'old': old, 'new': t['deadline']}, visible=True)
        self.enqueue(actor, t['agent'], f"DDL 调整：{p['deadline']}；理由：{p['reason']}", 8)
        return t

    def tick_tasks(self, now):
        """活性检查：只判断有没有活动回合或显式等待，不把“没有输出”误判为停工。"""
        s = self.store
        for t in s.all('task'):
            if t['state'] in TERMINAL or t['state'] == 'paused':
                continue
            a = s.get('agent', t['agent'])
            if t['deadline'] <= now and not t['overdue_notified']:
                t['overdue_notified'] = True; s.put('task', t)
                s.emit('overdue', f"任务超出 DDL：{t['title']}", task=t['id'], visible=True)
                self.enqueue('system', 'master', f"任务 {t['id']} 超出 DDL，请评估并公开决定。", 8)
            wait = t.get('wait', {})
            if wait:
                if wait.get('until', float('inf')) <= now:
                    t.update(state='running', wait={}); s.put('task', t)
                    self.enqueue('system', a['id'], '等待到期，请检查回复或外部作业状态，再决定续办或报备 master。', 8)
                    self.enqueue('system', 'master', f"任务 {t['id']} 等待到期，已唤醒核验。", 8)
                continue
            if a['paused'] or self.hub.busy(a['id']) or self.has_queued(a['id']):
                continue
            # 指数退避：同一任务反复空闲时拉长间隔，三次后暂停交 master，防止空转耗费模型。
            if now - a['idle_at'] < self.heartbeat * 2 ** min(t['recoveries'], 4):
                continue
            if t['recoveries'] >= 3:
                a['paused'] = True; t['state'] = 'paused'
                s.put('agent', a); s.put('task', t)
                self.enqueue('system', 'master', f"任务 {t['id']} 连续三次续办仍未完成/进入有效等待，已暂停，需检查后恢复。", 8)
                s.emit('recovery_exhausted', '自动续办已达三次，暂停并交 master 处理。', task=t['id'], visible=True)
                continue
            t['recoveries'] += 1; s.put('task', t)
            self.enqueue('system', a['id'], '任务尚未结束，但当前没有活动回合或等待事项。先检查现有产物和外部进程，再继续；完成用 finish_task，阻塞用 ask_master。', 8, key='recover:' + t['id'])
            self.enqueue('system', 'master', f"任务 {t['id']} 非预期空闲，已发第 {t['recoveries']} 次续办，请关注。", 8)
            s.emit('recovery', f"第 {t['recoveries']} 次续办（保留原 session）", target=a['id'], task=t['id'], visible=True)
