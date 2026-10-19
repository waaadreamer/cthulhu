"""消息队列：先落盘再执行；优先级只排序，10 是显式打断。"""


class Messages:
    def enqueue(self, sender, target, text, priority=5, *, reply_to=None, key=None, copy=False):
        """copy=True：程序为已公开事件（提问、回答、完成）生成的路由副本，界面不再重复展示。"""
        s = self.store
        s.get('agent', target)
        task = self.task_for(target)
        if task and task['state'] == 'cancelled':
            raise ValueError('任务已取消，不能继续投递执行消息')
        if key:
            existing = next((m for m in s.all('message') if m.get('key') == key
                             and m['state'] in {'queued', 'running'}), None)
            if existing:
                return existing
        if not isinstance(text, str) or not text.strip():
            raise ValueError('消息不能为空')
        priority = int(priority)
        if not 0 <= priority <= 10:
            raise ValueError('priority 应为 0..10')
        if sender not in {'owner', 'system', 'master'} and priority > 8:
            raise ValueError('old_one 最高优先级为 8')
        if reply_to:
            original = s.get('message', reply_to)
            if original['sender'] != target or original['target'] != sender:
                raise ValueError('reply_to 必须对应双方之间的来信')
        m = {'id': self.uid('m'), 'sender': sender, 'target': target, 'text': text, 'priority': priority,
             'state': 'queued', 'at': self.clock(), 'reply_to': reply_to, 'key': key, 'copy': copy}
        s.put('message', m)
        s.emit('message', text, actor=sender, target=target, task=self.task_id(target), data=m,
               visible=sender == 'owner' and target == 'master' and not copy)
        if priority == 10:
            # 10 是显式打断，不只是排序。新消息已先持久化；旧回合的写权限立刻撤销，
            # 但只有节点确认进程停止后才会派发新回合，不能假装已经打断成功。
            self.stop_turn(target, 'priority10')
            s.emit('urgent', '优先级 10：已请求打断当前回合，等待停止确认后处理新消息。',
                   actor=sender, target=target, task=self.task_id(target), visible=True)
        if reply_to and task and task.get('wait', {}).get('message_id') == reply_to:
            self.clear_wait(task)
        self.wake(target)
        return m

    def next_message(self, aid):
        """选出下一条待处理消息；排队期间已被覆盖的审核请求直接取消，避免空模型调用。"""
        s = self.store
        pending = sorted((m for m in s.all('message') if m['target'] == aid and m['state'] == 'queued'),
                         key=lambda m: (-m['priority'], m['at']))
        for m in pending:
            if aid == 'master' and m.get('key') == 'review' and not self.review_due():
                m['state'] = 'cancelled'; s.put('message', m)
                continue
            return m
        return None

    def has_queued(self, aid):
        return any(m['target'] == aid and m['state'] == 'queued' for m in self.store.all('message'))

    def cancel_queued(self, aid):
        for m in self.store.all('message'):
            if m['target'] == aid and m['state'] == 'queued':
                m['state'] = 'cancelled'; self.store.put('message', m)

    def read_messages(self, actor, limit=30):
        limit = max(1, min(int(limit), 100))
        return [m for m in self.store.all('message') if actor in {m['sender'], m['target']}][-limit:]

    def set_message(self, mid, state):
        m = self.store.get('message', mid); m['state'] = state; self.store.put('message', m)
        return m
