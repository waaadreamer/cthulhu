"""通用远程命令：中心持有业务记录，节点只持有进程/去重票据，复用原有 WS 与可靠事件流。"""
import asyncio
import codecs
from contextlib import nullcontext
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time

from .util import LEASE, LEASE_MARGIN, process_alive, process_identity

FEATURE = 'commands-v1'
OUTPUT_LIMIT = 1024 * 1024
SCRIPT_LIMIT = 64 * 1024
MAX_COMMANDS = 4
LIST_TIMEOUT = 5
ACTIVE = ('queued', 'dispatched', 'running', 'cancelling')
TERMINAL = ('completed', 'failed', 'cancelled', 'timed_out', 'unknown')


class ExecutionService:
    def __init__(self, center):
        self.c = center

    def active(self):
        return [x for state in ACTIVE for x in self.c.store.where('command', state)]

    def recover(self):
        # 中心重启不代表命令没有执行过。先封存 unknown，再让节点的同步撤销/生命线清理本机进程。
        for item in self.active():
            self.finish(item, 'unknown', '中心曾重启；未自动重放，须核验已有副作用')

    def finish(self, item, state, error=''):
        item.update(state=state, finished_at=self.c.clock(), received_at=self.c.clock(), error=error)
        self.c.store.put('command', item)

    def create(self, node_id, cwd, script, timeout=60, request_id=None):
        if not isinstance(node_id, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute() or '\0' in cwd:
            raise ValueError('node_id 与绝对路径 cwd 必填')
        if not isinstance(script, str) or not script.strip() or '\0' in script or len(script.encode()) > SCRIPT_LIMIT:
            raise ValueError('script 必须非空，且不超过 64KiB')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not .1 <= timeout <= 3600:
            raise ValueError('timeout 必须在 0.1–3600 秒之间')
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise ValueError('request_id 必须为 1–128 字符')
        spec = dict(node_id=node_id, cwd=cwd, script=script, timeout=timeout)
        with (nullcontext() if self.c.store.db.in_transaction else self.c.store.atomic()):
            if request_id is not None:
                known = self.c.store.find('command_request', request_id)
                if known:
                    item = self.get(known['command'])
                    if any(item[k] != v for k, v in spec.items()):
                        raise ValueError('request_id 已用于不同命令')
                    return item
            node = self.c.store.get('node', node_id)
            if not self.c.hub.online(node_id):
                raise ValueError('节点离线；命令未创建')
            if FEATURE not in node.get('features', []):
                raise ValueError('节点尚未支持远程命令，请先升级节点')
            if node.get('update', {}).get('state') in ('draining', 'ready') or (node_id == 'local' and self.c.hold_local):
                raise ValueError('节点正在准备更新，暂不接新命令')
            if sum(x['node_id'] == node_id for x in self.active()) >= MAX_COMMANDS:
                raise ValueError('节点已有 4 个未结束命令')
            item = dict(id='cmd_' + secrets.token_hex(16), **spec, state='queued', stdout='', stderr='', exit_code=None,
                        created_at=self.c.clock(), started_at=None, finished_at=None, received_at=None, truncated=False,
                        output_bytes=0, error='', request_id=request_id, until=self.c.clock() + LEASE)
            self.c.store.put('command', item)
            if request_id is not None:
                self.c.store.put('command_request', {'id': request_id, 'command': item['id']})
        self.c.hub.wake_node(node_id)
        return item

    def get(self, ident):
        return self.c.store.get('command', ident)

    def list(self, node_id=None, limit=20):
        return self._list(self.c.store.db, node_id, limit)

    def list_snapshot(self, node_id=None, limit=20):
        """在线程里读命令列表；独立只读连接不占中心事务，也不跨线程复用主连接。"""
        db = sqlite3.connect(f'file:{self.c.store.path}?mode=ro', uri=True)
        deadline = time.monotonic() + LIST_TIMEOUT
        # 冷门/没有命令的节点仍可能要读很多历史输出。HTTP 超时并不会取消 SQLite，
        # 因而必须在数据库层限制工作量，避免废弃查询长期占线程、磁盘和 WAL 快照。
        db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        try:
            return self._list(db, node_id, limit)
        except sqlite3.OperationalError as e:
            if str(e) == 'interrupted' and time.monotonic() >= deadline:
                raise ValueError('命令历史查询超时；请稍后重试') from e
            raise
        finally:
            db.close()

    @staticmethod
    def _list(db, node_id, limit):
        # SQL 先取最近的小页，不能把所有历史 stdout 拉进内存后才切片。
        limit = max(1, min(100, int(limit)))
        # NOT INDEXED 让 SQLite 从表尾沿 rowid 倒序，找到 limit 条就停。
        # objects_state 只按 kind 命中时会扫描全部状态再临时排序；线上两万多条历史
        # 命令的 stdout 因而被全量解析，主循环曾停顿百秒。直接倒序取页还避免上线时建大索引。
        sql = ("SELECT json_remove(data, '$.stdout', '$.stderr', '$.script', '$.late_result') "
               "FROM objects NOT INDEXED WHERE kind='command'")
        args = []
        if node_id:
            sql += " AND json_extract(data, '$.node_id')=?"; args.append(node_id)
        sql += ' ORDER BY rowid DESC LIMIT ?'; args.append(limit)
        return [json.loads(row[0]) for row in db.execute(sql, args)]

    def cancel(self, ident):
        with (nullcontext() if self.c.store.db.in_transaction else self.c.store.atomic()):
            item = self.get(ident)
            if item['state'] == 'queued':
                self.finish(item, 'cancelled', '派发前取消，未启动命令')
            elif item['state'] in ACTIVE:
                # 这里只表示取消请求。节点可能已正常退出，以节点看守观察到的结局为准。
                item['state'] = 'cancelling'; self.c.store.put('command', item)
        self.c.hub.wake_node(item['node_id'])
        return item

    def requests_for(self, node_id, have=()):
        items, have = [], set(have)
        for item in self.active():
            if item['node_id'] != node_id:
                continue
            if item['state'] == 'queued':
                item['state'] = 'dispatched'
            item['until'] = self.c.clock() + LEASE
            self.c.store.put('command', item)
            # 脚本最多 64KiB；节点已持有票据时只续状态，不能每秒重传全文挤占控制心跳。
            keys = ('id', 'state') if item['id'] in have else ('id', 'cwd', 'script', 'timeout', 'state')
            items.append({k: item[k] for k in keys})
        return items

    def expire(self):
        for item in self.active():
            if item['until'] <= self.c.clock():
                self.finish(item, 'unknown', '节点租约到期；未自动重放，须核验已有副作用')

    def ingest(self, node, event):
        item = self.get(event['job'])
        if item['node_id'] != node:
            raise PermissionError('命令事件不属于本节点')
        kind, data = event['kind'], event.get('data') or {}
        if kind == 'command:output':
            stream, value = data['stream'], data['text']
            if stream not in ('stdout', 'stderr') or not isinstance(value, str):
                raise ValueError('命令输出无效')
            # 也在中心设防，损坏/恶意节点不能通过事件绕过输出上限。源端仍负责排空管道。
            encoded = value.encode()
            room = OUTPUT_LIMIT - item['output_bytes']
            kept = encoded[:room].decode('utf-8', errors='ignore')
            item[stream] += kept; item['output_bytes'] += len(kept.encode())
            item['truncated'] |= len(encoded) > room
        elif kind == 'command:truncated':
            item['truncated'] = True
        elif kind == 'command:started':
            if item['state'] in ACTIVE:
                if item['state'] != 'cancelling': item['state'] = 'running'
                item['started_at'] = data['started_at']
        elif kind == 'command:finished':
            state = data.get('state')
            if state not in TERMINAL:
                raise ValueError('未知命令终态')
            item['truncated'] |= bool(data.get('truncated'))
            if item['state'] in ACTIVE:
                item.update({k: data.get(k) for k in ('state', 'exit_code', 'started_at', 'finished_at', 'error')})
                # 节点墙钟可能偏移，中心接收时刻单列；不能把它冒充实际采样时刻。
                item['received_at'] = self.c.clock()
            else:
                # unknown/取消后的迟到回执仅留档；不能因为连接恢复便把已封存的未知执行当成成功重放。
                item['late_result'] = data
        else:
            raise ValueError('未知命令事件')
        self.c.store.put('command', item)


class NodeCommands:
    def __init__(self, node):
        self.n = node
        self.active, self.expires, self.reasons = {}, {}, {}
        self.known = set()
        self.root = node.state_dir / 'commands'
        self.root.mkdir(exist_ok=True, mode=0o700)
        for item in node.store.where('command', 'running'):
            # 生命线由原看守收 EOF 后清组。恢复只报 unknown；即便上一回命令已产生副作用也不重放。
            owner = {'pid': item.get('owner_pid'), 'pid_identity': item.get('owner_identity')}
            if process_alive(owner):
                # 第二个节点进程不能在 WS 拒绝重复身份之前，先把原节点的命令误停掉。
                raise RuntimeError('旧节点仍拥有命令看守进程，拒绝并发启动同一节点')
            self.finish(item, dict(state='unknown', error='节点曾重启；未重放，须核验已有副作用',
                                   exit_code=None, started_at=item.get('started_at'), finished_at=time.time()))

    def finish(self, item, result):
        result['truncated'] = item.get('truncated', False)
        with self.n.store.atomic():
            item['state'] = 'stopped'; self.n.store.put('command', item)
            self.n.queue(item['id'], 'command:finished', result)

    def accept(self, commands, started):
        wanted = {x['id']: x for x in commands}
        known = set()
        for ident in self.active:
            if ident not in wanted:
                self.reasons.setdefault(ident, 'unknown')
                self.expires[ident] = 0
        for spec in commands:
            ident = spec['id']
            if ident not in self.active and self.n.store.find('command', ident):
                known.add(ident)
                continue
            if ident not in self.active and not all(k in spec for k in ('cwd', 'script', 'timeout')):
                # 本地缺票据但中心以为已有全文：不猜脚本，下一次同步去掉 have 请求完整票据。
                continue
            known.add(ident)
            self.expires[ident] = started + LEASE - LEASE_MARGIN
            if spec['state'] == 'cancelling':
                self.reasons.setdefault(ident, 'cancelled')
            if ident in self.active or self.n.store.find('command', ident):
                continue
            # 先持久化去重票据，再调度异步启动；响应重发/重连都只会运行同一 ID 一次。
            item = dict(id=ident, state='running', truncated=False, started_at=None,
                        owner_pid=os.getpid(), owner_identity=process_identity(os.getpid()))
            self.n.store.put('command', item)
            if time.monotonic() >= self.expires[ident]:
                self.finish(item, dict(state='unknown', error='派发抵达时租约已过期，未启动命令',
                                       exit_code=None, started_at=None, finished_at=time.time()))
                self.expires.pop(ident, None); self.reasons.pop(ident, None)
                continue
            self.active[ident] = asyncio.create_task(self.execute(spec, item))
        self.known = known

    async def execute(self, spec, item):
        ident, proc, readers = spec['id'], None, []
        stage = '启动看守'
        result = dict(state='failed', error='', exit_code=None, started_at=None, finished_at=None)
        result_path = self.root / (ident + '.json')

        async def wait_guard_exit():
            # Process.wait() 若先于退出进入等待，还会等继承管道的后代 EOF；取消路径尤其
            # 容易踩中。先观察子进程已回收的 returncode，再 wait，执行结局与输出预算分开。
            while proc.returncode is None:
                await asyncio.sleep(.05)
            return await proc.wait()

        try:
            if ident in self.reasons:
                result.update(state=self.reasons[ident], error='启动前已要求停止')
                return
            if not Path(spec['cwd']).is_dir():
                raise ValueError('节点上的工作目录不存在：' + spec['cwd'])
            proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('command_worker.py')),
                         stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                         start_new_session=True)
            if ident in self.reasons:
                proc.stdin.close(); await proc.wait()
                result.update(state=self.reasons[ident], error='交付脚本前已要求停止')
                return
            item.update(pid=proc.pid, pid_identity=process_identity(proc.pid), started_at=time.time())
            self.n.store.put('command', item)
            self.n.queue(ident, 'command:started', {'started_at': item['started_at']})
            stage = '交付脚本'
            proc.stdin.write((json.dumps(spec | {'result': str(result_path)}) + '\n').encode())
            await proc.stdin.drain()
            used = 0

            async def pump(stream, name):
                nonlocal used
                decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
                while True:
                    raw = await stream.read(16384)
                    value = decoder.decode(raw, final=not raw)
                    encoded = value.encode()
                    kept = encoded[:OUTPUT_LIMIT - used].decode('utf-8', errors='ignore')
                    # 两条流共享预算；更新到排队之间无 await，因此不会各自多保留一份上限。
                    used += len(kept.encode())
                    if len(kept.encode()) < len(encoded) and not item['truncated']:
                        item['truncated'] = True
                        self.n.store.put('command', item)
                        self.n.queue(ident, 'command:truncated', {})
                    if kept:
                        self.n.queue(ident, 'command:output', {'stream': name, 'text': kept})
                    if not raw: break
                    # 即使输出超过上限也继续读；否则满管道会让命令卡死，无法等到退出/超时。
                    await asyncio.sleep(0)

            readers = [asyncio.create_task(pump(proc.stdout, 'stdout')), asyncio.create_task(pump(proc.stderr, 'stderr'))]
            deadline = time.monotonic() + spec['timeout'] + 1
            while proc.returncode is None:
                if time.monotonic() >= deadline:
                    self.reasons.setdefault(ident, 'timed_out')
                if time.monotonic() >= self.expires.get(ident, 0):
                    self.reasons.setdefault(ident, 'unknown')
                if ident in self.reasons:
                    # 通过仅本节点持有的生命线传取消，避免按数字 PID 发信号的复用竞态。
                    proc.stdin.write(b'C')
                    break
                await asyncio.sleep(.05)
            stage = '等待看守退出'
            await asyncio.wait_for(wait_guard_exit(), spec['timeout'] + 3)
            # 恶意脚本可能 setsid 后保留管道；它已离开本次进程组，不让这类 FD 无限阻塞节点。
            stage = '排空输出管道'
            incomplete = False
            try:
                await asyncio.wait_for(asyncio.gather(*readers), 1)
            except asyncio.TimeoutError:
                # 执行结局须由下面读取的看守原子回执确认；后代持有 FD 只证明输出不完整，
                # 不能抹掉已知退出码。仍标截断，Canvas 不会把残缺数据提升为成功样本。
                incomplete = item['truncated'] = True
                self.n.queue(ident, 'command:truncated', {})
            stage = '读取看守回执'
            if result_path.is_file():
                result.update(json.loads(result_path.read_text()))
            else:
                result.update(state='unknown', error='命令看守退出但没有结局回执；须核验副作用')
            reason = self.reasons.get(ident)
            if reason == 'unknown' or (reason and result['state'] == 'cancelled'):
                result.update(state=reason, error='中心撤销/失联或节点关闭；须核验已有副作用' if reason == 'unknown' else '已停止命令进程组')
            if incomplete:
                result['error'] = '；'.join(filter(None, [result.get('error'), '输出管道排空超时，输出可能不完整']))
        except Exception as exc:
            # 不能仅留一个空 TimeoutError：命令期限、管道收尾和回执失败有不同的恢复边界。
            result.update(state='unknown' if proc else 'failed', error=f'{stage}失败：{type(exc).__name__}: {exc}')
        finally:
            if proc:
                if proc.returncode is None:
                    proc.stdin.close()
                    try: await asyncio.wait_for(wait_guard_exit(), 3)
                    except asyncio.TimeoutError:
                        proc.kill(); await wait_guard_exit()
                proc.stdin.close()
                for reader in readers:
                    if not reader.done(): reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                # asyncio 没有公开的 Process.close；此时看守已退出，仅关闭其遗留管道 transport。
                proc._transport.close()
            result['finished_at'] = result.get('finished_at') or time.time()
            result['started_at'] = result.get('started_at') or item.get('started_at')
            self.finish(item, result)
            result_path.unlink(missing_ok=True)
            self.active.pop(ident, None); self.expires.pop(ident, None); self.reasons.pop(ident, None)

    async def close(self):
        for ident in self.active:
            self.reasons.setdefault(ident, 'unknown')
        await asyncio.gather(*list(self.active.values()), return_exceptions=True)
