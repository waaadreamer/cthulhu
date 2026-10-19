"""十分钟增量审核：只有新信息才唤醒 Master，审核自身输出不构成下一轮的理由。"""
from ..util import TERMINAL


class Review:
    def schedule(self):
        return self.store.get('settings', 'schedule')

    def review_due(self):
        covered = self.schedule()['reported_through']
        return self.store.review_highwater(covered) > covered

    def list_tasks(self, actor):
        s = self.store
        if actor == 'master':
            # 固定本轮读取开始时的边界。finish_review 不能吞掉审核期间刚到的输入。
            schedule = self.schedule()
            schedule['review_snapshot'] = s.review_highwater(schedule['reported_through'])
            s.put('settings', schedule)
        return {'tasks': s.all('task'), 'agents': s.all('agent'), 'now': self.clock()}

    def read_task_events(self, p):
        task = self.store.get('task', p['task_id'])
        after = int(p.get('after', 0))
        events = self.store.events(after, p.get('limit', 100), agent=task['agent'])
        return {'events': events, 'next_after': events[-1]['seq'] if events else after,
                'highwater': self.store.highwater()}

    def finish_review(self, p):
        s = self.store
        schedule = self.schedule()
        through = schedule.get('review_snapshot', schedule['reported_through'])
        if through <= schedule['reported_through']:
            return {'skipped': True, 'reason': '没有尚未汇报的增量；审核须先 list_tasks'}
        for tid, cursor in (p.get('cursors') or {}).items():
            t = s.get('task', tid)
            t['review_cursor'] = max(t['review_cursor'], min(int(cursor), s.highwater()))
            s.put('task', t)
        schedule['reported_through'] = through
        s.put('settings', schedule)
        return {'seq': s.emit('review', p['text'], actor='master', visible=True)}

    def tick_review(self, now):
        s = self.store
        schedule = self.schedule()
        if now < schedule['next_review']:
            return
        if any(t['state'] not in TERMINAL for t in s.all('task')) and self.review_due():
            self.enqueue('system', 'master', '十分钟增量审核：list_tasks 查看任务；按 review_cursor 读取 read_task_events 的新增原始活动，并核对主人的新消息。只汇报新增进展、判断或措施；没有增量不重复汇报。最后 finish_review 记录本轮审核。', 6, key='review')
        schedule['next_review'] = now + self.review_interval
        s.put('settings', schedule)
