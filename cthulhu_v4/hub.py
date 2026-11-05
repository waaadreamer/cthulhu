"""唯一的执行派发路径：中心机（local）与远端星系使用同一套租约协议。

中心持有业务状态；节点持有本机进程和原生 session 文件。
- 中心先持久化回合票据（lease），再派发；同一票据重发不会再执行。
- 节点按连续序号上传事件，中心与确认游标同事务入库，断线补传去重。
- 租约到期 → 结果 unknown，暂停并报备 Master；不迁移、不重放。
- 停止（P10/暂停/取消）先撤销旧回合的写权限，节点确认进程已停止后才派新回合。
"""
import re
import secrets

from .models import clean as clean_models
from .util import LEASE, LOCAL, MAX_TURNS, PROTOCOL, TERMINAL
from .tools import FEISHU_TOOLS


class Hub:
    def __init__(self, center):
        self.c = center
        self.links = {}  # node id → 活动连接（本机为进程内通道，远端为 WebSocket）
        self.data_links = {}  # node id → 远端节点的 data 连接（工具、事件、文件）

    # ---- 节点登记 ----
    def register(self, name):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name) or name == LOCAL:
            raise ValueError('节点名只能含字母、数字、下划线、短横线，且不能为 local')
        if self.c.store.find('node', name):
            raise ValueError(f'节点 {name} 已存在；升级请在原机器重跑安装命令，不能用新身份接管原会话')
        node = {'id': name, 'token': secrets.token_urlsafe(32), 'seen': 0, 'ack': 0,
                'instance': None, 'backends': []}
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

    def attach(self, ident, link, channel=None):
        # 每个节点每个通道只允许一条连接：控制连接（channel=None，同步与续租）决定在线状态，
        # data 连接只承载工具调用、事件上传与文件，不影响在线判定。
        links = self.links if channel is None else self.data_links
        if ident in links:
            raise PermissionError('此节点已有连接，拒绝并发接管')
        links[ident] = link

    def detach(self, ident, link, channel=None):
        links = self.links if channel is None else self.data_links
        if links.get(ident) is link:
            links.pop(ident, None)

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
        j['state'] = 'unknown'
        self.c.settle(j, 'unknown'); s.put('lease', j)
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
            j['state'] = 'interrupted'
            c.settle(j, 'unknown'); s.put('lease', j)
            a = s.get('agent', aid); a.update(status='paused' if a['paused'] else 'idle', idle_at=c.clock()); s.put('agent', a)
            s.emit('interrupted', '节点已确认停止旧回合；已有文件与外部作业不会自动回滚。',
                   target=aid, task=c.task_id(aid), visible=True)
        elif outcome == 'completed':
            j['state'] = 'done'
            c.settle(j, 'handled'); s.put('lease', j)
            a = s.get('agent', aid)
            a.update(status='paused' if a['paused'] else 'idle', idle_at=c.clock(), failures=0); s.put('agent', a)
            if data.get('result'):
                public = aid == 'master' and c.reply_is_public(j)
                s.emit('chat' if public else 'reply', data['result'], actor=aid,
                       task=c.task_id(aid), visible=public)
        elif outcome == 'failed':
            j['state'] = 'failed'
            c.settle(j, 'failed'); s.put('lease', j)
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

    def ingest(self, node, event, batch=None):
        """batch：同一批事件共用的缓存 {'owner': 回合→(节点, 执行者), 'task': 执行者→任务 id}。
        一批最多 64 条、几乎都属于同一个回合；每条都重新读租约（带完整 prompt）和查任务是纯浪费。"""
        s, c = self.c.store, self.c
        batch = {'owner': {}, 'task': {}} if batch is None else batch
        kind, job = event['kind'], event['job']
        # steer/finished 会读写租约的其余字段并改状态，必须读最新；其余事件只需要回合的归属，
        # 归属在回合存续期间不会变，可以整批复用。
        j = None
        if kind in ('steer', 'finished') or job not in batch['owner']:
            j = s.get('lease', job)
            batch['owner'][job] = (j['node'], j['agent'])
        owner, aid = batch['owner'][job]
        if owner != node:
            raise PermissionError('事件不属于本节点')
        if aid not in batch['task']:
            batch['task'][aid] = c.task_id(aid)
        task, data = batch['task'][aid], event.get('data') or {}
        if kind == 'session':
            c.save_session(aid, data['session'])
        elif kind == 'steer':
            self.steered(j, data)
        elif kind == 'finished':
            self.finished(node, j, data)
            s.emit('remote_result', data.get('result') or data.get('error') or '', actor=aid,
                   task=task, data={'node': node, 'job': j['id'], 'outcome': data.get('outcome')})
        else:
            s.emit(kind, event.get('text', ''), actor=aid, target=None, task=task,
                   data=data, visible=bool(event.get('visible')))

    def steered(self, j, data):
        """节点报告插入结果。成功：消息留在本回合，回合结束时一起标记；失败：退回队列，下一回合先处理。"""
        mid = data.get('message')
        if j['state'] != 'running' or mid not in (j.get('steer') or []):
            return  # 回合已结束（settle 已退回队列）或重复报告
        if data.get('ok'):
            j['steer'] = [x for x in j['steer'] if x != mid]
        else:
            self.c.unsteer(j, mid)
            self.c.store.emit('steer_failed', '插入当前回合失败，已排到下一回合最前：' + (data.get('error') or ''),
                              target=j['agent'], data={'message': mid, 'job': j['id']})
        self.c.store.put('lease', j)

    # ---- 节点请求 ----
    def sync(self, node, body):
        c, s = self.c, self.c.store
        with s.atomic():
            n = s.get('node', node)
            # instance 标识持久磁盘而非连接；换了空目录不能伪装成原节点接管旧 session。
            if n['instance'] and n['instance'] != body['instance']:
                raise PermissionError(f'节点 {node} 的状态目录已改变：请恢复原目录；不能用新目录接管原 session')
            n.update(instance=body['instance'], seen=c.clock(), version=body.get('version'),
                     backends=[b for b in body.get('backends', []) if b in {'demo', 'codex', 'claude'}],
                     host=body.get('host'), home=body.get('home'), features=list(body.get('features') or []),
                     code=body.get('code'), managed=bool(body.get('managed')), base=body.get('base'))
            if 'models' in body:
                # 节点只在列表变化时上报；未上报时沿用中心保存的上一份。
                n['models'] = clean_models(body['models'])
            self.ingest_batch(node, n, body.get('events', []))
            s.put('node', n)
            jobs, running = [], 0
            # 全中心正在跑的回合只取一次：下面既续租本节点的回合，也据此判断哪些执行者忙。
            # 以前每个执行者各调一次 busy()，每次都重新反序列化全部运行中的租约（带完整 prompt）。
            live = s.where('lease', 'running')
            busy_agents = {j['agent'] for j in live if j['state'] == 'running'}
            # 节点已持有完整回合（prompt、agent_spec、task 可达数十 KB）时，续租只回 id。
            # 为什么：心跳每秒一次，原先每次重发全文，是中心出站流量的大头。
            # 旧节点不报 have，按原样拿全文，保持兼容。
            have = set(body.get('have') or ())
            for j in live:
                if j['node'] != node or j['state'] != 'running':
                    continue
                running += 1
                # 被要求停止的回合不再续租，也不发给节点；节点据此停止进程并回报 stopped。
                if not j.get('stop_reason') and not s.get('agent', j['agent'])['paused']:
                    j['until'] = c.clock() + LEASE; s.put('lease', j)
                    item = {'id': j['id']} if j['id'] in have else dict(j)
                    if j.get('steer'):
                        # 待插入的消息随续租下发，直到节点报告结果；节点按消息 id 去重，重复下发不会重复插入。
                        item['steer'] = [{'id': mid, 'text': c.steer_text(mid)} for mid in j['steer']]
                    jobs.append(item)
            plan = c.update_plan(n, running)
            s.put('node', n)
            # 等待更新时不派发新回合：消息留在队列里，更新完成（或失败）后照常派发。
            # 主人调低上限时不打断已在跑的回合，只是暂不派发新的，直到 running 降到上限以下。
            free = 0 if plan in ('draining', 'ready') else (n.get('max_turns') or MAX_TURNS) - running
            if node == LOCAL and c.hold_local:
                free = 0  # 中心即将重启：本机节点不接新回合，等已派发的回合结束
            # 只看有排队消息的执行者：以前遍历全部 321 个执行者，再对每个逐个查租约和队列，
            # 每个节点每秒一次。没有消息的执行者本来就派发不了，不必取出来。
            # get_many 保持执行者创建顺序，名额不够时谁先派发与原先一致。
            for a in s.get_many('agent', s.queued_targets()):
                if free <= 0:
                    break
                if (a.get('node') or LOCAL) != node or a['paused'] or a['id'] in busy_agents:
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
                # 记下派发事件的序号：回合结束时据此判断本回合是否已用 tell_owner 公开过。
                j['seq'] = s.emit('dispatch', '已派发至节点 ' + node, target=a['id'], task=t and t['id'],
                                  data={'job': j['id'], 'node': node, 'message_id': m['id'], 'message_ids': j['messages'],
                                        # 网页据此决定这个回合的工作过程放主聊天还是只放 Master 侧栏。
                                        'owner': any(x['sender'] in ('owner', 'feishu') for x in batch)})
                s.put('lease', j)
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

    def ingest_batch(self, node, n, events):
        batch = {'owner': {}, 'task': {}}
        for e in events:
            if e['seq'] <= n['ack']:
                continue   # 重发的批次：已入库的跳过
            if e['seq'] != n['ack'] + 1:
                raise ValueError('事件序号不连续，需从确认游标继续')
            self.ingest(node, e, batch); n['ack'] = e['seq']

    def upload(self, node, body):
        """data 连接上传事件。为什么和 sync 分开：大批事件（MB 级工具输出）在 sync 里会拖慢续租回复，
        节点那边续租超时就会停进程。分开后续租只传几百字节，事件慢慢传不影响租约。"""
        s = self.c.store
        with s.atomic():
            n = s.get('node', node)
            if n['instance'] and n['instance'] != body.get('instance'):
                raise PermissionError(f'节点 {node} 的状态目录已改变：请恢复原目录；不能用新目录接管原 session')
            self.ingest_batch(node, n, body.get('events', []))
            s.put('node', n)
        return {'ack': n['ack']}

    def request(self, node, body):
        op = body.get('op')
        if op == 'sync':
            self.expire()
            return self.sync(node, body)
        if op == 'events':
            return self.upload(node, body)
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
