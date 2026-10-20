"""十分钟增量审核：只有新信息才唤醒 Master，审核自身输出不构成下一轮的理由。"""
from ..util import TERMINAL, cut


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
        # 已归档任务不再返回：它们已结束，留在列表里只会占用 Master 的上下文。
        tasks = [t for t in s.all('task') if not t.get('archived')]
        live = {t['agent'] for t in tasks}
        keep = ('id', 'node', 'backend', 'model', 'reasoning_effort', 'cwd', 'session', 'status', 'paused')
        agents = [{k: a.get(k) for k in keep} for a in s.all('agent') if a['id'] == 'master' or a['id'] in live]
        return {'tasks': tasks, 'agents': agents, 'now': self.clock()}

    def read_task_events(self, p):
        """给模型读的任务活动：与侧栏同源，但去掉遥测与流式增量，并截断超长字段。

        原始事件全集仍完整保存在库里；这里只控制送进 Master 上下文的体积，
        单次读取不能因为一段大输出就吃掉几十万 token。
        """
        task = self.store.get('task', p['task_id'])
        after = int(p.get('after', 0))
        limit = max(1, min(int(p.get('limit', 60)), 200))
        events = self.store.events(after, limit, agent=task['agent'], ui=True, compact=True)
        return {'events': [slim(e) for e in events], 'next_after': events[-1]['seq'] if events else after,
                'highwater': self.store.highwater(),
                'note': '已省略遥测与流式增量；超长字段已截断，需要全文时请在执行节点上核对'}

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


FIELD = 1500


def slim(e):
    data = e['data']
    if e['kind'] == 'message':
        data = {k: data.get(k) for k in ('id', 'priority', 'state', 'reply_to') if k in data}  # 正文已在 text
    elif e['kind'] == 'harness_tool':
        data = {'arguments': data.get('arguments'), 'result': data.get('result')}
    return {'seq': e['seq'], 'at': e['at'], 'actor': e['actor'], 'target': e['target'], 'kind': e['kind'],
            'text': cut(e['text'], 4000), 'data': cut(data, FIELD)}
