"""问题状态机：master → owner →（超时）master → answered。

每次状态或答案变化都增加 version；提交时必须带读取时的版本，
避免 master 的旧判断覆盖主人晚到的回答。历史答案只追加不删除。
"""
from ..util import TERMINAL


class Questions:
    def question(self, actor, p):
        s = self.store
        task = self.task_for(actor)
        if not task or task['state'] in TERMINAL:
            raise ValueError('没有进行中的任务')
        q = {'id': self.uid('q'), 'task': task['id'], 'agent': actor, 'text': p['text'],
             'recommendation': p['recommendation'], 'blocking': bool(p['blocking']),
             'state': 'master', 'version': 1, 'deadline': None, 'history': [], 'created_at': self.clock()}
        if q['blocking']:
            if task.get('wait'):
                raise ValueError('已有等待事项，请先处理，不能覆盖')
            task.update(state='waiting', wait={'kind': 'question', 'question_id': q['id']}, recoveries=0)
            s.put('task', task)
        s.put('question', q)
        s.emit('question', p['text'], actor=actor, target='master', task=task['id'], data=q, visible=True)
        self.enqueue(actor, 'master', f"问题 {q['id']} v1：{q['text']}\n执行者建议：{q['recommendation']}\n请回答或转请主人。", 8)
        return q

    def answer(self, actor, p):
        s = self.store
        q = s.get('question', p['question_id'])
        if q['state'] == 'closed':
            raise ValueError('问题已随任务结束而关闭')
        if int(p['version']) != q['version']:
            raise ValueError('问题版本已变化，请重新读取；不能覆盖更新的答案')
        if actor != 'owner' and q['state'] != 'master':
            raise ValueError('问题正等主人或已经回答；请重新读取')
        text = str(p['answer']).strip()
        if not text:
            raise ValueError('回答不能为空')
        q['version'] += 1
        q.update(state='answered', deadline=None)
        q['history'].append({'by': actor, 'text': text, 'at': self.clock(), 'version': q['version']})
        s.put('question', q)
        task = s.get('task', q['task'])
        if task.get('wait', {}).get('question_id') == q['id']:
            self.clear_wait(task)
        s.emit('answer', text, actor=actor, target=q['agent'], task=q['task'], data=q, visible=True)
        if task['state'] not in TERMINAL:
            self.enqueue(actor, q['agent'], f"问题 {q['id']} 的回答/修正（版本 {q['version']}）：{text}\n若已按旧答案执行，先核验副作用再调整。", 8)
        if actor == 'owner':
            self.enqueue('owner', 'master', f"主人回答/修正问题 {q['id']}：{text}。已通知执行者，请核对是否需要进一步调整。", 9)
        return q

    def ask_owner(self, actor, p):
        s = self.store
        q = s.get('question', p['question_id'])
        if q['version'] != int(p['version']) or q['state'] != 'master':
            raise ValueError('问题版本或状态已变化')
        q.update(state='owner', version=q['version'] + 1, deadline=self.clock() + self.question_timeout)
        q['history'].append({'by': 'master', 'text': p['recommendation'], 'at': self.clock(), 'kind': 'ask_owner'})
        s.put('question', q)
        s.emit('ask_owner', f"{q['text']}\nMaster 建议：{p['recommendation']}", actor=actor, task=q['task'], data=q, visible=True)
        return q

    def close_questions(self, task_id):
        for q in self.store.all('question'):
            if q['task'] == task_id and q['state'] not in {'answered', 'closed'}:
                q.update(state='closed', deadline=None, version=q['version'] + 1)
                self.store.put('question', q)

    def expire_questions(self, now):
        s = self.store
        for q in s.all('question'):
            if q['state'] == 'owner' and q['deadline'] <= now:
                q.update(state='master', deadline=None, version=q['version'] + 1)
                s.put('question', q)
                s.emit('question_timeout', '等待主人超时，已交回 master 决定。', task=q['task'], data=q, visible=True)
                self.enqueue('system', 'master', f"问题 {q['id']} 等待主人超时，当前版本 {q['version']}。请核对后自行决定，不能推定新的授权。", 8)
