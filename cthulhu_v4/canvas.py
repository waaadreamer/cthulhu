"""独立 Canvas 的中心状态。只有外部 Canvas 服务的 tick 会推动采集。

Canvas 不属于 task；task_ids 只是可选元数据，采集脚本自行决定汇总哪些实验。
不在这里运行进程：采集与节点命令行共用 executions 服务及 WebSocket 节点。
"""
import json
import math
import posixpath

from .util import uid

TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'unknown'}
CONFIG = ('name', 'html_template', 'node_id', 'cwd', 'script', 'interval', 'timeout', 'task_ids')
WORKER_GRACE = 15
TEMPLATE_LIMIT = 256 * 1024
BOARD_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
             "img-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
             "frame-src 'none'; frame-ancestors 'self'")


def json_number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('JSON 数字必须有限')
    return value


def invalid_constant(value):
    # Python 默认接受 NaN/Infinity，浏览器 JSON.parse 不接受；不能让一个脚本破坏整个数据 API。
    raise ValueError('不是标准 JSON: ' + value)


class Canvases:
    def __init__(self, center):
        self.c, self.s = center, center.store

    def validate(self, body):
        out = {}
        for field, limit in (('name', 200), ('html_template', TEMPLATE_LIMIT),
                             ('node_id', 64), ('cwd', 4096), ('script', 64000)):
            value = body.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit or '\0' in value:
                raise ValueError(f'{field} 必须是非空字符串，最多 {limit} 字节且不能含 NUL')
            out[field] = value
        self.s.get('node', out['node_id'])
        if not posixpath.isabs(out['cwd']):
            raise ValueError('cwd 必须是执行节点上的绝对路径')
        for field, low, high in (('interval', 1, 86400), ('timeout', 0.1, 3600)):
            value = body.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'{field} 必须在 {low}–{high} 秒之间')
            out[field] = float(value)
        refs = body.get('task_ids', [])
        if not isinstance(refs, list) or len(refs) > 100 or any(not isinstance(x, str) or not x or len(x) > 128 for x in refs):
            raise ValueError('task_ids 必须是至多 100 项的字符串列表（可为空）')
        out['task_ids'] = list(dict.fromkeys(refs))
        return out

    def create(self, body):
        now = self.c.clock()
        board = self.validate(body) | {'id': uid('canvas'), 'state': 'active', 'revision': 1,
            'created_at': now, 'updated_at': now, 'next_at': now, 'inflight': None,
            'last_success': None, 'last_attempt': None, 'blocked_unknown': False, 'sequence': 0}
        self.s.put('canvas', board)
        self.s.emit('canvas_created', board['name'], actor='owner', data={'canvas': board['id']}, visible=True)
        return self.view(board)

    def edit(self, ident, body):
        board = self.s.get('canvas', ident)
        if board['state'] == 'closed':
            raise ValueError('Canvas 已关闭；请新建面板')
        if body.get('revision') != board['revision']:
            raise ValueError('Canvas 已被修改，请刷新后重试')
        config = self.validate({k: body.get(k, board[k]) for k in CONFIG})
        # 编辑只换配置版本，不能清掉在途票据：否则旧命令还没结束就会启动新采集。
        # 旧配置的结果可以回收，但不能覆盖新模板/数据语义。
        board.update(config, revision=board['revision'] + 1, updated_at=self.c.clock(), next_at=self.c.clock())
        self.s.put('canvas', board)
        return self.view(board)

    def control(self, ident, action, acknowledge=False):
        board = self.s.get('canvas', ident)
        if action not in ('pause', 'resume', 'close'):
            raise ValueError('未知 Canvas 操作')
        if board['state'] == 'closed':
            if action == 'close':
                return self.view(board)
            raise ValueError('Canvas 已关闭；请新建面板')
        if action == 'resume':
            if board['blocked_unknown'] and acknowledge is not True:
                raise ValueError('上次命令结果未知；先核验节点上的命令已结束，再显式确认恢复')
            board.update(state='active', blocked_unknown=False, next_at=self.c.clock())
        elif action == 'pause':
            board['state'] = 'paused'
        else:
            # 关闭仅停止本面板采集，不向其展示的训练/评测发停止命令。
            # 仍保留在途票据供回收，但版本递增使迟到结果永远不能成为最后成功数据。
            board.update(state='closed', revision=board['revision'] + 1)
        board['updated_at'] = self.c.clock()
        self.s.put('canvas', board)
        self.s.emit('canvas_control', f"{board['name']}: {action}", actor='owner', data={'canvas': ident}, visible=True)
        return self.view(board)

    def reconcile(self, board, now):
        flight = board['inflight']
        if not flight:
            return False
        try:
            command = self.c.executions.get(flight['command_id'])
        except ValueError:
            # 正常情况下两条记录同事务存在；若数据库被人工修过，不能把丢失票据当作没执行。
            command = {'state': 'unknown', 'error': '中心命令记录缺失，结果未知'}
        if command['state'] not in TERMINAL:
            return True
        attempt = {k: command.get(k) for k in ('state', 'stdout', 'stderr', 'exit_code', 'error', 'truncated', 'started_at', 'finished_at')}
        attempt.update(command_id=flight['command_id'], collected_at=now,
                       revision=flight['revision'], discarded=flight['revision'] != board['revision'] or board['state'] == 'closed')
        board['last_attempt'] = attempt
        board['inflight'] = None
        # unknown 与失败不同：程序是否仍有外部副作用尚不清楚，必须暂停，不能到下个 tick 盲发新命令。
        if command['state'] == 'unknown':
            board['blocked_unknown'] = True
            if board['state'] != 'closed':
                board['state'] = 'paused'
        elif command['state'] == 'completed' and command.get('exit_code') == 0 and not command.get('truncated') and not attempt['discarded']:
            stdout = command.get('stdout') or ''
            try:
                data = json.loads(stdout, parse_float=json_number, parse_constant=invalid_constant)
                # 转义的孤立 surrogate 可被 json.loads 接受，却无法写入 SQLite UTF-8。
                # 此类输出按原始文本展示，不能让一个面板的结果回滚所有面板的 tick。
                json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
                kind = 'json'
            except (ValueError, RecursionError):
                data, kind = stdout, 'text'
            # 节点时钟可能领先，回执也可能在断线后迟到。鲜度从中心创建命令的时间保守计算，
            # 展示中心收到回执的时间；不能用节点 finished_at 或 worker 恢复时间刷新旧数据。
            board['last_success'] = {'data': data, 'format': kind,
                'sampled_at': command.get('received_at') or command['created_at'],
                'freshness_at': command['created_at'], 'source_finished_at': command.get('finished_at'),
                'command_id': flight['command_id'], 'revision': flight['revision']}
        # 从本次回收时刻重新计时：长采集错过的 tick 全部跳过，不补跑积压。
        board['next_at'] = now + board['interval']
        self.s.put('canvas', board)
        return True

    def tick(self, worker):
        if not isinstance(worker, str) or not 1 <= len(worker) <= 128:
            raise ValueError('worker 必须是 1–128 字符的实例标识')
        now = self.c.clock()
        self.s.put('settings', {'id': 'canvas_worker', 'seen_at': now, 'worker': worker})
        started = []
        for board in self.s.all('canvas'):
            if self.reconcile(board, now):
                continue
            if board['state'] != 'active' or now < board['next_at']:
                continue
            if not self.c.hub.online(board['node_id']):
                continue
            # 票据与命令创建在中心同一同步事务内提交。多个 worker 同时 tick、HTTP 响应丢失、
            # worker/中心重启都不能留下“已发命令但没有在途记录”的窗口；不把租约存在 worker 磁盘。
            sequence = board['sequence'] + 1
            try:
                command = self.c.executions.create(node_id=board['node_id'], cwd=board['cwd'], script=board['script'],
                    timeout=board['timeout'], request_id=f"canvas:{board['id']}:{sequence}")
            except ValueError as exc:
                # 容量满、升级或旧节点不支持命令时，此次没有创建命令。只延后本面板，不能回滚
                # 其他面板已经回收的结果/票据，也不能用错误采集覆盖最后成功数据。
                board.update(next_at=now + board['interval'], last_attempt={'state': 'failed', 'error': str(exc),
                    'stdout': '', 'stderr': '', 'exit_code': None, 'collected_at': now, 'command_id': None,
                    'revision': board['revision'], 'discarded': False})
                self.s.put('canvas', board)
                continue
            board.update(sequence=sequence, inflight={'command_id': command['id'], 'revision': board['revision'], 'started_at': now})
            self.s.put('canvas', board)
            started.append(command['id'])
        return {'started': started, 'now': now}

    def view(self, board, *, detail=True):
        now = self.c.clock()
        worker = self.s.find('settings', 'canvas_worker') or {}
        worker_online = bool(worker) and now - worker.get('seen_at', 0) <= WORKER_GRACE
        node_online = self.c.hub.online(board['node_id'])
        success = board['last_success']
        stale = not success or success['revision'] != board['revision'] or now - success.get('freshness_at', success['sampled_at']) > max(2 * board['interval'], board['interval'] + board['timeout'])
        attempt = board['last_attempt']
        failed = attempt and not attempt['discarded'] and (attempt['state'] != 'completed' or attempt.get('exit_code') != 0 or attempt.get('truncated'))
        # 关闭是终态；迟到的 unknown 仍留作回执，但不能把已关闭面板重新显示成“已暂停可恢复”。
        status = ('closed' if board['state'] == 'closed' else 'unknown' if board['blocked_unknown'] else board['state'] if board['state'] != 'active'
                  else 'node_offline' if not node_online else 'collector_offline' if not worker_online
                  else 'failed' if failed else 'stale' if stale and success else 'collecting' if board['inflight']
                  else 'waiting' if not success else 'fresh')
        out = {k: v for k, v in board.items() if detail or k not in ('html_template', 'script', 'last_success', 'last_attempt')}
        out.update(status=status, stale=stale, fresh=bool(success and not stale and status in ('fresh', 'collecting')),
                   node_online=node_online, collector_online=worker_online, now=now,
                   sampled_at=success['sampled_at'] if success else None,
                   data_stamp=[success['command_id'] if success else None,
                               attempt.get('collected_at') if attempt else None])
        return out

    def data(self, board, *, compact=False):
        view = self.view(board)
        success, attempt = board['last_success'], board['last_attempt']
        result = {k: view[k] for k in ('id', 'name', 'state', 'status', 'stale', 'fresh', 'node_online', 'collector_online', 'sampled_at', 'now', 'revision', 'blocked_unknown')} | {
            'data': success['data'] if success else None, 'format': success['format'] if success else None,
            'data_revision': success['revision'] if success else None, 'attempt': attempt}
        # 同一成功票据的 data 是 stdout 的确定解析结果。只传一次原文，浏览器可精确恢复两者；
        # 失败/丢弃/不同票据必须保留独立的最后成功数据，不能用新失败 stdout 覆盖它。
        if compact and success and attempt and success['command_id'] == attempt['command_id']:
            raw = attempt.get('stdout', '')
            equivalent = result['format'] == 'text' and success['data'] == raw
            if result['format'] == 'json':
                # 只接受与既有 JSON 响应相同的数字/字符串词法；如原文 -0 被 Python 规范成 0，
                # 或任意自定义格式不匹配，保守退回完整 data，不能改变浏览器看到的数值语义。
                equivalent = raw.strip() in (json.dumps(success['data'], ensure_ascii=False),
                    json.dumps(success['data'], ensure_ascii=False, separators=(',', ':')))
            if equivalent:
                result.pop('data')
                result['data_from_stdout'] = result['format']
        return result

    async def route(self, method, path, query, body):
        parts = path.removeprefix('/api/canvas').strip('/').split('/')
        with self.s.atomic():
            if method == 'GET' and parts == ['']:
                return {'canvases': [self.view(b, detail=False) for b in self.s.all('canvas')],
                        'nodes': [{'id': n['id'], 'online': self.c.hub.online(n['id'])} for n in self.s.all('node')]}
            if method == 'POST' and parts == ['']:
                return self.create(body)
            if method == 'POST' and parts == ['tick']:
                return self.tick(body.get('worker'))
            if len(parts) == 1 and parts[0]:
                if method == 'GET':
                    board = self.s.get('canvas', parts[0])
                    result = self.view(board)
                    if query.get('view') == ['config']:
                        result = {k: v for k, v in result.items() if k not in ('last_success', 'last_attempt')}
                    return result
                if method == 'POST':
                    return self.edit(parts[0], body)
            if len(parts) == 2:
                ident, action = parts
                if method == 'GET' and action == 'data':
                    return self.data(self.s.get('canvas', ident), compact=query.get('compact') == ['1'])
                if method == 'POST':
                    return self.control(ident, action, body.get('acknowledge_unknown', False))
        raise ValueError('未知 Canvas 接口')
