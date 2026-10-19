"""中心侧 Galaxy 协议：出站 WebSocket、持久回合票据、事件去重、租约隔离。

只有中心拥有业务状态；节点拥有本机进程和原生 session 文件。断线不迁移
会话，租约过期暂停并报备，避免把已执行但未确认的外部操作再次执行。
"""
import asyncio
import json
import re
import secrets

from .core import TERMINAL, uid

LEASE = 30
MAX_FRAME = 20 * 1024 * 1024
EXECUTION_POLICY = 'full-access-v1'


class GalaxyHub:
    def __init__(self, core):
        self.c = core
        self.connections = {}

    def provision(self, ident):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', ident) or ident == 'local':
            raise ValueError('节点 ID 只能含字母、数字、下划线、短横线，不能为 local')
        existing = next((n for n in self.c.store.all('node') if n['id'] == ident), None)
        if not existing:
            existing = {'id': ident, 'token': secrets.token_urlsafe(32), 'seen': 0,
                        'ack': 0, 'instance': None, 'capacity': 1, 'backends': []}
            self.c.store.put('node', existing)
        path = self.c.state_dir / ('node-' + ident + '-token')
        path.write_text(existing['token']); path.chmod(0o600)
        return path

    def nodes(self):
        return [{k: v for k, v in n.items() if k != 'token'} |
                {'online': n['id'] in self.connections and self.c.clock() - n['seen'] < LEASE}
                for n in self.c.store.all('node')]

    def busy(self, aid):
        return any(j['agent'] == aid and j['state'] == 'running' for j in self.c.store.all('lease'))

    def runtime_issue(self, node, backend):
        if backend == 'demo': return None
        n = self.c.store.get('node', node)
        if n.get('execution_policy') != EXECUTION_POLICY:
            return (f'节点 {node} 仍在使用旧启动协议，不能确认已加载完整权限适配器。'
                    '请先停止星系进程，在原目录更新代码并重新连接，再恢复原任务；'
                    '仅更新中心或重启旧 connect.sh 不会修复远端权限。')
        return None

    def valid_job(self, node, ident):
        j = self.c.store.get('lease', ident)
        if j['node'] != node: raise PermissionError('回合不属于本节点')
        a = self.c.store.get('agent', j['agent'])
        if j['state'] != 'running' or j['until'] <= self.c.clock() or a['paused'] or j.get('stop_reason'):
            raise PermissionError('回合已失效或暂停，拒绝旧进程写入')
        return j

    def uncertain(self, j, reason):
        s = self.c.store
        j['state'] = 'unknown'; s.put('lease', j)
        m = s.get('message', j['message']); m['state'] = 'unknown'; s.put('message', m)
        a = s.get('agent', j['agent']); a.update(paused=True, status='paused'); s.put('agent', a)
        t = self.c.task_for(a['id'])
        if t['state'] not in TERMINAL:
            t['state'] = 'paused'; s.put('task', t)
        s.emit('node_attention', reason, target=a['id'], task=t['id'], visible=True)
        self.c.enqueue('system', 'master', reason + '；需检查节点及原会话、已有副作用后恢复，不能迁移重跑。', 8)

    def expire(self):
        with self.c.store.atomic():
            for j in self.c.store.all('lease'):
                if j['state'] == 'running' and j['until'] <= self.c.clock():
                    self.uncertain(j, f"节点 {j['node']} 回合租约到期：{j['agent']}，结果未知")

    def ingest(self, node, event):
        """事件与确认游标同一事务；响应丢失后重传不会重复显示或重复完成。"""
        s = self.c.store
        j = s.get('lease', event['job'])
        if j['node'] != node: raise PermissionError('事件不属于本节点')
        aid, kind, data = j['agent'], event['kind'], event['data']
        if kind == 'session':
            self.c.save_session(aid, data['session'])
        elif kind == 'finished':
            if j['state'] == 'running':
                if j.get('stop_reason') == 'priority10':
                    j['state'] = 'interrupted'; s.put('lease', j)
                    m = s.get('message', j['message']); m['state'] = 'unknown'; s.put('message', m)
                    a = s.get('agent', aid); a.update(status='idle', idle_at=self.c.clock()); s.put('agent', a)
                    s.emit('interrupted', '节点已确认停止旧回合，优先级 10 消息即将处理。',
                           target=aid, task=self.c.task_id(aid), visible=True)
                elif data.get('error'):
                    self.uncertain(j, f"节点 {node} 的 {aid} 回合停止：{data['error']}")
                else:
                    j['state'] = 'done'; s.put('lease', j)
                    m = s.get('message', j['message']); m['state'] = 'handled'; s.put('message', m)
                    a = s.get('agent', aid)
                    a.update(status='paused' if a['paused'] else 'idle', idle_at=self.c.clock())
                    s.put('agent', a)
            # 即使租约已失效也保留迟到结果供人工核验，但绝不据此重新激活任务。
            s.emit('remote_result', data.get('result', '') or data.get('error', ''), actor=aid,
                   task=self.c.task_id(aid), data={'node': node, 'job': j['id'], 'state': j['state']})
        else:
            s.emit(kind, event.get('text', ''), actor=aid, task=self.c.task_id(aid),
                   data=data | {'_galaxy': {'node': node, 'job': j['id'], 'occurred_at': event.get('at')}},
                   visible=bool(event.get('visible')))

    def sync(self, node, body):
        c, s = self.c, self.c.store
        with s.atomic():
            n = s.get('node', node)
            # instance 标识持久磁盘，而非连接；丢失节点磁盘不能伪装成原节点恢复。
            instance = body['instance']
            if n['instance'] and n['instance'] != instance:
                raise ValueError('节点状态目录已改变，请恢复原目录或注册新节点；不能替换原 session')
            n.update(instance=instance, seen=c.clock(), runtime_revision=body.get('runtime_revision', 'legacy'),
                     execution_policy=body.get('execution_policy'), capacity=max(1, min(int(body['capacity']), 32)),
                     backends=[b for b in body['backends'] if b in {'demo', 'codex', 'claude'}])
            s.put('node', n)
            for e in body.get('events', []):
                if e['seq'] <= n['ack']: continue
                if e['seq'] != n['ack'] + 1: raise ValueError('事件序号不连续，需从确认游标继续')
                self.ingest(node, e); n['ack'] = e['seq']
            jobs = []
            for j in s.all('lease'):
                if j['node'] != node or j['state'] != 'running': continue
                a = s.get('agent', j['agent'])
                issue = self.runtime_issue(node, a['backend'])
                if issue and not a['paused']:
                    # 已派发旧回合的结果必须标 unknown；不续租，让节点停止旧进程。
                    self.uncertain(j, issue)
                    continue
                if not a['paused'] and not j.get('stop_reason'):
                    j['until'] = c.clock() + LEASE; s.put('lease', j); jobs.append(j)
            free = n['capacity'] - sum(j['node'] == node and j['state'] == 'running' for j in s.all('lease'))
            for a in s.all('agent'):
                if free <= 0: break
                if a.get('node', 'local') != node or a['paused'] or self.busy(a['id']): continue
                if a['backend'] not in n['backends']: continue
                pending = [m for m in s.all('message') if m['target'] == a['id'] and m['state'] == 'queued']
                if not pending: continue
                issue = self.runtime_issue(node, a['backend'])
                if issue:
                    # 中心升级不等于执行机升级。派发前拦住旧节点，避免让模型用失败命令验证版本。
                    a.update(paused=True, status='paused'); s.put('agent', a)
                    t = c.task_for(a['id']); t['state'] = 'paused'; s.put('task', t)
                    s.emit('node_attention', issue, target=a['id'], task=t['id'], visible=True)
                    c.enqueue('system', 'master', issue, 8)
                    continue
                m = sorted(pending, key=lambda m: (-m['priority'], m['at']))[0]
                j = {'id': uid('turn'), 'node': node, 'agent': a['id'], 'message': m['id'],
                     'state': 'running', 'until': c.clock() + LEASE,
                     'agent_spec': c.public_agent(a), 'task': c.task_for(a['id']), 'prompt': c.prompt(a['id'], m)}
                s.put('lease', j)
                m['state'] = 'running'; s.put('message', m)
                a['status'] = 'running'; s.put('agent', a)
                s.emit('dispatch', '已派发至星系节点 ' + node, target=a['id'], task=c.task_id(a['id']),
                       data={'job': j['id'], 'node': node, 'message_id': m['id']})
                jobs.append(j); free -= 1
            s.put('node', n)
            return {'ack': n['ack'], 'lease_seconds': LEASE, 'jobs': jobs}

    def request(self, node, body):
        if body['op'] == 'sync':
            self.expire()
            return self.sync(node, body)
        if body['op'] == 'tool':
            job = self.valid_job(node, body['job'])
            # MCP 写请求有稳定 ID；响应丢失时允许重试同一调用，不重复提交副作用。
            key = job['id'] + ':' + body['call_id']
            prior = next((r for r in self.c.store.all('rpc') if r['id'] == key), None)
            if prior: return prior['result']
            with self.c.store.atomic():
                actor = job['agent']
                from .tools import tools_for
                if body['name'] not in {t['name'] for t in tools_for(actor)}:
                    raise ValueError('该角色没有此工具')
                result = self.c._tool(actor, body['name'], body.get('arguments', {}))
                self.c.store.emit('harness_tool', body['name'], actor=actor, task=self.c.task_id(actor),
                                  data={'arguments': body.get('arguments', {}), 'result': result})
                self.c.store.put('rpc', {'id': key, 'result': result})
                return result
        raise ValueError('未知节点操作')

    async def handle(self, ws):
        node = None
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            for n in self.c.store.all('node'):
                if n['id'] == hello.get('node') and secrets.compare_digest(n['token'], hello.get('token', '')):
                    node = n['id']; break
            if not node: raise PermissionError('节点凭证无效')
            if node in self.connections: raise PermissionError('此节点已有连接，拒绝并发接管')
            self.connections[node] = ws
            await ws.send(json.dumps({'ok': True}))
            async for raw in ws:
                try:
                    value = self.request(node, json.loads(raw))
                    response = {'result': value}
                except Exception as e:
                    response = {'error': str(e)}
                await ws.send(json.dumps(response, ensure_ascii=False))
        except Exception:
            await ws.close(code=1008, reason='节点认证失败或连接中断')
        finally:
            if node and self.connections.get(node) is ws:
                self.connections.pop(node, None)
