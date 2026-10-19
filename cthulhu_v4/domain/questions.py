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
        self.enqueue(actor, 'master', f"问题 {q['id']} v1：{q['text']}\n执行者建议：{q['recommendation']}\n请回答或转请主人。", 8, copy=True)
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
            self.enqueue(actor, q['agent'], f"问题 {q['id']} 的回答/修正（版本 {q['version']}）：{text}\n若已按旧答案执行，先核验副作用再调整。", 8, copy=True)
        if actor == 'owner':
            self.enqueue('owner', 'master', f"主人回答/修正问题 {q['id']}：{text}。已通知执行者，请核对是否需要进一步调整。", 9, copy=True)
        return q

    def master_decides(self):
        return bool((self.store.find('settings', 'prefs') or {}).get('master_decides'))

    def ask_owner(self, actor, p):
        s = self.store
        q = s.get('question', p['question_id'])
        if self.master_decides():
            # 主人明确授权 Master 全权回答：拒绝转问，并让模型看到原因，而不是静默吞掉。
            raise ValueError('主人已开启「Master 全权回答」：请自行 answer_question 并公开理由，不要转给主人')
        if q['version'] != int(p['version']) or q['state'] != 'master':
            raise ValueError('问题版本或状态已变化')
        q.update(state='owner', version=q['version'] + 1, deadline=self.clock() + self.question_timeout)
        q['history'].append({'by': 'master', 'text': p['recommendation'], 'at': self.clock(), 'kind': 'ask_owner'})
        s.put('question', q)
        s.emit('ask_owner', f"{q['text']}\nMaster 建议：{p['recommendation']}", actor=actor, task=q['task'], data=q, visible=True)
        return q

    def delegate(self, p, reason='主人交给 Master 决定'):
        """主人把等待自己的问题交回 Master。版本加一，之后 Master 用新版本作答；主人仍可随时修正。"""
        s = self.store
        q = s.get('question', p['question_id'])
        if q['state'] != 'owner':
            raise ValueError('问题当前不在等你回答，无需转交')
        q.update(state='master', deadline=None, version=q['version'] + 1)
        q['history'].append({'by': 'owner', 'kind': 'delegate', 'at': self.clock(), 'version': q['version']})
        s.put('question', q)
        s.emit('question_delegated', reason, actor='owner', task=q['task'], data=q, visible=True)
        self.enqueue('owner', 'master', f"主人把问题 {q['id']} 交给你决定（当前版本 {q['version']}）。请核对任务与证据后 answer_question，并公开你的判断。", 9, copy=True)
        return q

    def set_master_decides(self, on):
        s = self.store
        prefs = s.find('settings', 'prefs') or {'id': 'prefs'}
        prefs['master_decides'] = bool(on)
        s.put('settings', prefs)
        moved = 0
        if on:
            # 开启时正在等主人的问题一并交回，避免它们还要等超时。
            for q in s.all('question'):
                if q['state'] == 'owner':
                    self.delegate({'question_id': q['id']}, reason='已开启 Master 全权回答，问题交回 Master'); moved += 1
        s.emit('settings', 'Master 全权回答：' + ('开启' if on else '关闭'), actor='owner', data=prefs, visible=True)
        self.enqueue('owner', 'master', '主人已开启「Master 全权回答」：之后执行者的问题请自行判断并 answer_question，不要 ask_owner；回答与理由仍会公开，主人可能事后修正。'
                     if on else '主人已关闭「Master 全权回答」：需要主人决定的问题可以再用 ask_owner 转交。', 8, copy=True)
        return {'master_decides': bool(on), 'delegated': moved}

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
