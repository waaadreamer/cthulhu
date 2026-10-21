"""唯一的执行派发路径：中心机（local）与远端星系使用同一套租约协议。

中心持有业务状态；节点持有本机进程和原生 session 文件。
- 中心先持久化回合票据（lease），再派发；同一票据重发不会再执行。
- 节点按连续序号上传事件，中心与确认游标同事务入库，断线补传去重。
- 租约到期 → 结果 unknown，暂停并报备 Master；不迁移、不重放。
- 停止（P10/暂停/取消）先撤销旧回合的写权限，节点确认进程已停止后才派新回合。
"""
import re
import secrets

from .util import LEASE, LOCAL, PROTOCOL, TERMINAL
from .tools import FEISHU_TOOLS


class Hub:
    def __init__(self, center):
        self.c = center
        self.links = {}  # node id → 活动连接（本机为进程内通道，远端为 WebSocket）

    # ---- 节点登记 ----
    def register(self, name):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name) or name == LOCAL:
            raise ValueError('节点名只能含字母、数字、下划线、短横线，且不能为 local')
        if self.c.store.find('node', name):
            raise ValueError(f'节点 {name} 已存在；升级请在原机器重跑安装命令，不能用新身份接管原会话')
        node = {'id': name, 'token': secrets.token_urlsafe(32), 'seen': 0, 'ack': 0,
                'instance': None, 'capacity': 2, 'backends': []}
        self.c.store.put('node', node)
        return node

    def online(self, ident):
        return ident in self.links

    def nodes(self):
        out, running = [], self.c.store.where('lease', 'running')
        for n in self.c.store.all('node'):
            public = {k: v for k, v in n.items() if k != 'token'}
            public['online'] = self.online(n['id'])
            public['running'] = sum(j['node'] == n['id'] for j in running)
            out.append(public)
        return out

    def attach(self, ident, link):
        if ident in self.links:
            raise PermissionError('此节点已有连接，拒绝并发接管')
        self.links[ident] = link

    def detach(self, ident, link):
        if self.links.get(ident) is link:
            self.links.pop(ident, None)

    def check_hello(self, hello):
        """校验节点身份；返回 node id。token 用常量时间比较。"""
        if hello.get('protocol') != PROTOCOL:
            raise PermissionError(f"节点协议 {hello.get('protocol')} 与中心 {PROTOCOL} 不一致：请在该机器重跑安装命令升级")
        n = self.c.store.find('node', str(hello.get('node')))
        if not n or not n.get('token') or not secrets.compare_digest(n['token'], str(hello.get('token', ''))):
            raise PermissionError('节点凭证无效')
        return n['id']

    # ---- 回合状态 ----
    def busy(self, aid):
        return any(j['agent'] == aid and j['state'] == 'running' for j in self.c.store.where('lease', 'running'))

    def stop(self, aid, reason):
        """撤销当前回合：此后它的工具写入被拒绝，节点下一次同步时停止进程。"""
        for j in self.c.store.where('lease', 'running'):
            if j['agent'] == aid and j['state'] == 'running' and not j.get('stop_reason'):
                j['stop_reason'] = reason; self.c.store.put('lease', j)
        self.wake(aid)

    def wake(self, aid):
        a = self.c.store.find('agent', aid)
        link = a and self.links.get(a.get('node') or LOCAL)
        if link:
            link.wake()

    def wake_node(self, ident):
        link = self.links.get(ident)
        if link:
            link.wake()

    def valid_job(self, node, ident):
        j = self.c.store.get('lease', ident)
        if j['node'] != node:
            raise PermissionError('回合不属于本节点')
        a = self.c.store.get('agent', j['agent'])
        if j['state'] != 'running' or j['until'] <= self.c.clock() or a['paused'] or j.get('stop_reason'):
            raise PermissionError('回合已失效或暂停，拒绝旧进程写入')
        return j

    def uncertain(self, j, reason):
        """结果未知：暂停任务、报备 Master。已执行的外部操作可能存在，不能自动重放。"""
        s = self.c.store
        j['state'] = 'unknown'; s.put('lease', j)
        self.c.set_messages(j.get('messages') or [j['message']], 'unknown')
        a = s.get('agent', j['agent']); a.update(paused=True, status='paused'); s.put('agent', a)
        t = self.c.task_for(a['id'])
        if t and t['state'] not in TERMINAL:
            t['state'] = 'paused'; s.put('task', t)
        s.emit('node_attention', reason, target=a['id'], task=t and t['id'], visible=True)
        if a['id'] != 'master':
            self.c.enqueue('system', 'master', reason + '；需检查节点及原会话、已有副作用后恢复，不能迁移重跑。', 8)

    def expire(self):
        now = self.c.clock()
        with self.c.store.atomic():
            for j in self.c.store.where('lease', 'running'):
                if j['state'] == 'running' and j['until'] <= now:
                    self.uncertain(j, f"节点 {j['node']} 回合租约到期：{j['agent']}，结果未知")

    # ---- 事件入库 ----
    def finished(self, node, j, data):
        s, c = self.c.store, self.c
        aid = j['agent']
        outcome = data.get('outcome')
        if j['state'] != 'running':
            return  # 迟到结果只作为 remote_result 留档，绝不据此重新激活任务。
        if outcome == 'stopped' and j.get('stop_reason'):
            j['state'] = 'interrupted'; s.put('lease', j)
            c.set_messages(j.get('messages') or [j['message']], 'unknown')
            a = s.get('agent', aid); a.update(status='paused' if a['paused'] else 'idle', idle_at=c.clock()); s.put('agent', a)
            s.emit('interrupted', '节点已确认停止旧回合；已有文件与外部作业不会自动回滚。',
                   target=aid, task=c.task_id(aid), visible=True)
        elif outcome == 'completed':
            j['state'] = 'done'; s.put('lease', j)
            c.set_messages(j.get('messages') or [j['message']], 'handled')
            a = s.get('agent', aid)
            a.update(status='paused' if a['paused'] else 'idle', idle_at=c.clock(), failures=0); s.put('agent', a)
            if data.get('result'):
                s.emit('chat' if aid == 'master' else 'reply', data['result'], actor=aid,
                       task=c.task_id(aid), visible=aid == 'master')
        elif outcome == 'failed':
            j['state'] = 'failed'; s.put('lease', j)
            c.set_messages(j.get('messages') or [j['message']], 'failed')
            a = s.get('agent', aid); a['failures'] = a.get('failures', 0) + 1
            a.update(status='idle', idle_at=c.clock())
            s.emit('runtime_error', data.get('error', ''), target=aid, task=c.task_id(aid), visible=True)
            if aid == 'master':
                # Master 出错等待主人明确恢复，避免错误循环消耗模型。
                a.update(paused=True, status='paused')
                s.emit('master_attention', 'Master 失败，已暂停。检查错误后点击恢复；原会话保留。', visible=True)
            else:
                c.notify_failure(aid, data.get('error'))
            s.put('agent', a)
        else:
            # stopped 但中心没要求停（节点本地租约到期/重启）或 unknown：一律当作结果未知。
            self.uncertain(j, f"节点 {node} 的 {aid} 回合结果未知：{data.get('error') or outcome}")

    def ingest(self, node, event):
        s, c = self.c.store, self.c
        j = s.get('lease', event['job'])
        if j['node'] != node:
            raise PermissionError('事件不属于本节点')
        aid, kind, data = j['agent'], event['kind'], event.get('data') or {}
        if kind == 'session':
            c.save_session(aid, data['session'])
        elif kind == 'finished':
            self.finished(node, j, data)
            s.emit('remote_result', data.get('result') or data.get('error') or '', actor=aid,
                   task=c.task_id(aid), data={'node': node, 'job': j['id'], 'outcome': data.get('outcome')})
        else:
            s.emit(kind, event.get('text', ''), actor=aid, target=None, task=c.task_id(aid),
                   data=data, visible=bool(event.get('visible')))

    # ---- 节点请求 ----
    def sync(self, node, body):
        c, s = self.c, self.c.store
        with s.atomic():
            n = s.get('node', node)
            # instance 标识持久磁盘而非连接；换了空目录不能伪装成原节点接管旧 session。
            if n['instance'] and n['instance'] != body['instance']:
                raise PermissionError(f'节点 {node} 的状态目录已改变：请恢复原目录；不能用新目录接管原 session')
            n.update(instance=body['instance'], seen=c.clock(), version=body.get('version'),
                     capacity=max(1, min(int(body.get('capacity', 1)), 32)),
                     backends=[b for b in body.get('backends', []) if b in {'demo', 'codex', 'claude'}],
                     host=body.get('host'), home=body.get('home'), features=list(body.get('features') or []),
                     code=body.get('code'), managed=bool(body.get('managed')))
            for e in body.get('events', []):
                if e['seq'] <= n['ack']:
                    continue
                if e['seq'] != n['ack'] + 1:
                    raise ValueError('事件序号不连续，需从确认游标继续')
                self.ingest(node, e); n['ack'] = e['seq']
            s.put('node', n)
            jobs, running = [], 0
            for j in s.where('lease', 'running'):
                if j['node'] != node or j['state'] != 'running':
                    continue
                running += 1
                # 被要求停止的回合不再续租，也不发给节点；节点据此停止进程并回报 stopped。
                if not j.get('stop_reason') and not s.get('agent', j['agent'])['paused']:
                    j['until'] = c.clock() + LEASE; s.put('lease', j); jobs.append(j)
            plan = c.update_plan(n, running)
            s.put('node', n)
            # 等待更新时不派发新回合：消息留在队列里，更新完成（或失败）后照常派发。
            free = 0 if plan in ('draining', 'ready') else n['capacity'] - running
            if node == LOCAL and c.hold_local:
                free = 0  # 中心即将重启：本机节点不接新回合，等已派发的回合结束
            for a in s.all('agent'):
                if free <= 0:
                    break
                if (a.get('node') or LOCAL) != node or a['paused'] or self.busy(a['id']):
                    continue
                if a['backend'] not in n['backends']:
                    continue
                batch = c.next_batch(a['id'])
                if not batch:
                    continue
                m = batch[0]
                t = c.task_for(a['id'])
                j = {'id': c.uid('turn'), 'node': node, 'agent': a['id'], 'message': m['id'], 'messages': [x['id'] for x in batch],
                     'state': 'running', 'until': c.clock() + LEASE, 'started': c.clock(), 'agent_spec': a, 'feishu': a['id'] == 'master' and bool(c.feishu),
                     'task': t, 'prompt': c.prompt(a['id'], batch)}
                s.put('lease', j)
                c.set_messages(j['messages'], 'running')
                a['status'] = 'running'; s.put('agent', a)
                s.emit('dispatch', '已派发至节点 ' + node, target=a['id'], task=t and t['id'],
                       data={'job': j['id'], 'node': node, 'message_id': m['id'], 'message_ids': j['messages']})
                jobs.append(j); free -= 1
            # 文件读取请求不是业务状态，不进事务、不落盘；随同步响应捎给节点即可。
            reply = {'ack': n['ack'], 'lease_seconds': LEASE, 'jobs': jobs, 'files': c.files.requests_for(node)}
            if plan == 'ready':
                reply['update'] = c.release.offer()
            return reply

    def tool(self, node, body):
        job = self.valid_job(node, body['job'])
        # MCP 写请求带稳定 call_id；响应丢失后重试同一调用，返回原结果而不重复提交副作用。
        dedupe = job['id'] + ':' + str(body['call_id'])
        if body['name'] in FEISHU_TOOLS:
            # 飞书工具要知道本回合的来源（白名单只能在主人的回合里改），lark-cli 调用是异步子进程；
            # 返回协程，由链路 await，不在同步事务里等外部网络。
            return self.c.feishu_tool(job, body['name'], body.get('arguments') or {}, dedupe)
        return self.c.tool(job['agent'], body['name'], body.get('arguments') or {}, dedupe=dedupe)

    def request(self, node, body):
        op = body.get('op')
        if op == 'sync':
            self.expire()
            return self.sync(node, body)
        if op == 'tool':
            return self.tool(node, body)
        if op == 'file':
            return self.c.files.deliver(node, body)
        if op == 'release':
            u = self.c.store.get('node', node).get('update') or {}
            if not self.c.release or u.get('target') != self.c.release.code:
                raise ValueError('没有待下发的更新')
            return self.c.release.download()
        if op == 'update_failed':
            with self.c.store.atomic():
                return self.c.update_failed(node, body.get('error', '未知错误'))
        raise ValueError('未知节点操作')
