"""消息队列：先落盘再执行；优先级只排序，10 是显式打断。"""

BATCH_COUNT = 30
BATCH_CHARS = 60000


class Messages:
    def enqueue(self, sender, target, text, priority=5, *, reply_to=None, key=None, copy=False, solo=False):
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
             'state': 'queued', 'at': self.clock(), 'reply_to': reply_to, 'key': key, 'copy': copy, 'solo': solo}
        s.put('message', m)
        s.emit('message', text, actor=sender, target=target, task=self.task_id(target), data=m,
               visible=sender == 'owner' and target == 'master' and not copy)
        if priority == 10:
            # 10 是显式打断，不只是排序。新消息已先持久化；旧回合的写权限立刻撤销，
            # 但只有节点确认进程停止后才会派发新回合，不能假装已经打断成功。
            self.stop_turn(target, 'priority10')
            s.emit('urgent', '优先级 10：已请求打断当前回合，等待停止确认后处理新消息。',
                   actor=sender, target=target, task=self.task_id(target), visible=True)
        if task and task['state'] == 'review' and sender in ('master', 'owner'):
            # 审核期间 Master/主人给执行者发消息 = 退回继续做：任务回到进行中，申请记录留档。
            self.return_review(task, sender, text)
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

    def next_batch(self, aid):
        """Master 一个回合处理队列里的全部普通来信（按优先级与时间排序），而不是一条一个回合。

        执行者汇报很密时，一条一回合会让队列越积越长：每个 Master 回合都要读完整上下文，
        处理速度追不上来信速度，任务结束后还在逐条回应过时的进度。合并后 Master 一次看全，
        也能直接忽略已被后续消息覆盖的内容。审核请求单独成回合（它有自己的读取流程）。
        执行者仍是一条一回合：它只有一个任务，来信很少，逐条处理更利于核对 reply_to。
        """
        first = self.next_message(aid)
        # 审核请求、结束申请等 solo 消息单独成回合：它们需要 Master 专门核验，不能淹没在一批进度里。
        if not first or aid != 'master' or first.get('key') == 'review' or first.get('solo'):
            return [first] if first else []
        batch, size = [first], len(first['text'])
        for m in sorted((m for m in self.store.all('message') if m['target'] == aid and m['state'] == 'queued'
                         and m['id'] != first['id'] and m.get('key') != 'review' and not m.get('solo')),
                        key=lambda m: (-m['priority'], m['at'])):
            # 单回合输入有上限：剩下的留到下一回合，不截断任何一条来信。
            if len(batch) >= BATCH_COUNT or size + len(m['text']) > BATCH_CHARS:
                break
            batch.append(m); size += len(m['text'])
        return batch

    def set_messages(self, ids, state):
        for mid in ids:
            self.set_message(mid, state)

    def supersede_progress(self, aid):
        """任务结束时，执行者此前发给 Master 而尚未读到的普通进度消息不再需要逐条处理：
        完成通知（或取消记录）与 read_task_events 已覆盖它们。标记为 superseded 而不是删除，可回查。
        提问、完成等程序生成的消息（copy=True）不受影响。"""
        n = 0
        for m in self.store.all('message'):
            if m['sender'] == aid and m['target'] == 'master' and m['state'] == 'queued' and not m.get('copy'):
                m['state'] = 'superseded'; self.store.put('message', m); n += 1
        return n

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
