"""薄原生适配器：不实现模型循环、不手工压缩、不跨后端搬运上下文。

Codex 使用 app-server；Claude 使用持久 stream-json CLI。两者均挂同一个
MCP server，所有公开输出原样入事件库。运行时事件流包含工具与 hook 事件，
无需为采集额外执行 shell hooks。一个 agent 同时只有一个活动 turn（中心租约保证）。
"""
import asyncio
import json
import os
import signal
import sys
import uuid

from .util import ROOT, process_identity


CHUNK = 1 << 20
# 单个原生事件超过这个体积时截断其中的长字符串再记录：远端节点经 WebSocket 上传，单帧上限 20MB；
# 浏览器与 Master 也用不到几 MB 的单条事件。完整内容仍在原生 CLI 自己的会话文件里。
RECORD_LIMIT = 2 * 1024 * 1024


async def lines(stream):
    """按行读取子进程输出，不限制单行长度。

    asyncio 的 readline 受 StreamReader limit 约束：恢复一个很长的原生会话时，
    Codex 的 thread/resume 回执会把整段历史放在一行 JSON 里（实测超过 16MB），
    readline 直接抛出 “Separator is not found, and chunk exceed the limit”，回合失败。
    """
    buf, start = bytearray(), 0
    while True:
        data = await stream.read(CHUNK)
        if not data:
            if buf:
                yield bytes(buf)
            return
        buf += data
        while (i := buf.find(b'\n', start)) >= 0:
            yield bytes(buf[:i + 1])
            del buf[:i + 1]
            start = 0
        start = len(buf)  # 下次只从新数据开始找换行，超长行不会反复扫描


def bounded(payload, size):
    """体积过大的原生事件：截断长字符串，保留结构与原始长度说明。"""
    if size <= RECORD_LIMIT:
        return payload
    def cut(v):
        if isinstance(v, str) and len(v) > 20000:
            return v[:20000] + f'…[截断，共 {len(v)} 字符]'
        if isinstance(v, list):
            return [cut(x) for x in v[:200]] + ([f'…[另有 {len(v) - 200} 项]'] if len(v) > 200 else [])
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        return v
    out = cut(payload)
    if isinstance(out, dict):
        out['_truncated'] = f'原始事件 {size} 字节，已截断记录'
    return out


class Sink:
    """运行时与外界的窄接口；由节点实现。运行时不接触业务状态。

    native(kind, payload)  记录原生公开事件
    session(sid)           原生 session 指针（先落盘再上传）
    pid(pid, identity)     记录/清除子进程，用于重启后的孤儿检测
    tool(name, args)       经节点调用中心工具（demo 使用；真实后端走 MCP）
    url, token             本机 MCP 桥地址与本 agent 的 token
    """


class ProcessRuntime:
    def __init__(self, sink, agent):
        self.sink, self.a = sink, agent
        self.proc = None
        self.readers = []
        self.done = None
        self.stderr_tail = ''

    def env(self):
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)  # harness 启动独立会话，不嵌套当前交互客户端。
        env.update(CTHULHU_URL=self.sink.url, CTHULHU_AGENT=self.a['id'], CTHULHU_TOKEN=self.sink.token)
        return env

    def instructions(self):
        role = 'master' if self.a['id'] == 'master' else 'old_one'
        return (ROOT / 'skills' / (role + '.md')).read_text() + '\n你的 agent ID：' + self.a['id']

    def mcp(self):
        return {'command': sys.executable, 'args': [str(ROOT / 'cthulhu_v4' / 'mcp.py')],
                'env': {k: v for k, v in self.env().items() if k.startswith('CTHULHU_')}}

    async def spawn(self, argv):
        self.proc = await asyncio.create_subprocess_exec(*argv, cwd=self.a['cwd'], env=self.env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        self.sink.pid(self.proc.pid, process_identity(self.proc.pid))
        self.readers.append(asyncio.create_task(self.stderr()))

    async def stderr(self):
        async for line in lines(self.proc.stderr):
            text = line.decode(errors='replace')
            self.stderr_tail = (self.stderr_tail + text)[-6000:]
            self.sink.native('stderr', {'text': text})

    async def write(self, payload):
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode())
        await self.proc.stdin.drain()

    def fail(self, message):
        if self.done and not self.done.done():
            self.done.set_exception(RuntimeError(message))

    async def close(self):
        if self.proc and self.proc.returncode is None:
            # 只管理本适配器启动的进程组；不根据模糊名称杀其他会话。
            try: os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                try: os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await self.proc.wait()
        for reader in self.readers:
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        self.readers.clear()
        if self.proc:
            self.sink.pid(None, None)
        self.proc = None


class CodexRuntime(ProcessRuntime):
    def __init__(self, sink, agent):
        super().__init__(sink, agent)
        self.pending, self.serial, self.final = {}, 0, []
        self.session = agent['session']

    async def request(self, method, params):
        self.serial += 1
        ident = self.serial
        fut = asyncio.get_running_loop().create_future()
        self.pending[ident] = fut
        try:
            await self.write({'id': ident, 'method': method, 'params': params})
            return await asyncio.wait_for(fut, 60)
        finally:
            self.pending.pop(ident, None)

    async def start(self):
        cfg = self.mcp()
        argv = ['codex', '--yolo', '-c', 'mcp_servers.cthulhu.command=' + json.dumps(cfg['command']),
                '-c', 'mcp_servers.cthulhu.args=' + json.dumps(cfg['args']),
                '-c', 'mcp_servers.cthulhu.env_vars=' + json.dumps(list(cfg['env'])),
                # 仅本地 harness 工具由角色权限检查后直接执行；不修改其他 MCP 的审批。
                '-c', 'mcp_servers.cthulhu.default_tools_approval_mode="approve"',
                'app-server', '--listen', 'stdio://']
        await self.spawn(argv)
        self.readers.append(asyncio.create_task(self.read()))
        await self.request('initialize', {'clientInfo': {'name': 'cthulhu_v4', 'version': '0.1.0'},
                                          'capabilities': {'experimentalApi': True}})
        await self.write({'method': 'initialized'})
        # 用户明确要求完整执行权限。app-server 不能只依赖 CLI 的 --yolo：
        # start/resume 均传同样的策略，覆盖旧 session 持久化的沙箱限制。
        params = {'cwd': self.a['cwd'], 'approvalPolicy': 'never', 'sandbox': 'danger-full-access'}
        if self.a.get('model'): params['model'] = self.a['model']
        # 在原生线程层显式配置，避免继承本机配置里不同的推理强度。
        if self.a.get('reasoning_effort'):
            params['config'] = {'model_reasoning_effort': self.a['reasoning_effort']}
        if self.session:
            params['threadId'] = self.session
            # 恢复失败必须抛错；不能偷偷 start 新线程“修好”错误。
            response = await self.request('thread/resume', params)
        else:
            params['developerInstructions'] = self.instructions()
            response = await self.request('thread/start', params)
            self.session = response['thread']['id']
            self.sink.session(self.session)
        self.sink.native('runtime_config', {
            'model': response.get('model'), 'reasoning_effort': response.get('reasoningEffort'),
            'session': self.session, 'approval_policy': response.get('approvalPolicy'),
            'sandbox': response.get('sandbox')})
        if response.get('approvalPolicy') != 'never' or response.get('sandbox', {}).get('type') != 'dangerFullAccess':
            raise RuntimeError('原生后端未应用完整执行权限；停止，避免在旧沙箱中继续运行')
        if self.a.get('reasoning_effort') and response.get('reasoningEffort') != self.a['reasoning_effort']:
            raise RuntimeError('原生后端未应用请求的 reasoning_effort，停止以免用错配置')
        if self.a.get('model') and response.get('model') != self.a['model']:
            raise RuntimeError('原生后端返回的 model 与请求不一致，停止以免静默替换模型')

    async def read(self):
        try:
            async for line in lines(self.proc.stdout):
                frame = json.loads(line)
                if 'id' in frame and 'method' not in frame:
                    fut = self.pending.get(frame['id'])
                    if fut and not fut.done():
                        if 'error' in frame: fut.set_exception(RuntimeError(str(frame['error'])))
                        else: fut.set_result(frame.get('result', {}))
                    continue
                method, p = frame.get('method', ''), frame.get('params', {})
                self.sink.native(method, bounded(p, len(line)))
                if 'id' in frame:
                    # 首版无原生审批 UI。明确拒绝而非挂起；模型可通过 ask_master 说明需求。
                    if method.endswith('requestApproval'):
                        await self.write({'id': frame['id'], 'result': {'decision': 'decline'}})
                    elif method.endswith('requestUserInput'):
                        await self.write({'id': frame['id'], 'result': {'answers': {}}})
                    else:
                        await self.write({'id': frame['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}})
                    self.sink.native('declined_request', {'method': method})
                if method == 'item/completed':
                    item = p.get('item', {})
                    if item.get('type') == 'agentMessage' and item.get('phase') != 'commentary':
                        self.final.append(item.get('text', ''))
                if method == 'turn/completed' and self.done and not self.done.done():
                    turn = p.get('turn', {})
                    if turn.get('status') == 'completed': self.done.set_result('\n'.join(self.final))
                    else: self.fail(str(turn.get('error') or turn.get('status')))
            raise RuntimeError('Codex app-server 已退出：' + self.stderr_tail)
        except asyncio.CancelledError:
            return
        except Exception as e:
            self.fail(str(e))
            for fut in self.pending.values():
                if not fut.done(): fut.set_exception(RuntimeError(str(e)))

    async def run(self, text):
        if not self.proc: await self.start()
        self.final = []
        self.done = asyncio.get_running_loop().create_future()
        # 每轮也明确策略，防止恢复配置或其他回合覆盖为 workspace-write。
        params = {'threadId': self.session, 'input': [{'type': 'text', 'text': text}],
                  'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'}}
        if self.a.get('reasoning_effort'): params['effort'] = self.a['reasoning_effort']
        # 每轮显式带上模型：主人在网页切换模型后，沿用同一线程也立即按新模型执行。
        if self.a.get('model'): params['model'] = self.a['model']
        await self.request('turn/start', params)
        return await self.done


class ClaudeRuntime(ProcessRuntime):
    def __init__(self, sink, agent):
        super().__init__(sink, agent)
        self.session = agent['session'] or str(uuid.uuid4())

    async def start(self):
        argv = ['claude', '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--verbose', '--include-partial-messages', '--include-hook-events', '--forward-subagent-text',
                '--replay-user-messages', '--append-system-prompt', self.instructions(),
                # 新建和 --resume 均使用同一组参数，不能恢复到旧的 acceptEdits 模式。
                '--dangerously-skip-permissions', '--permission-mode', 'bypassPermissions',
                '--mcp-config', json.dumps({'mcpServers': {'cthulhu': self.mcp()}})]
        argv += ['--resume' if self.a['session'] else '--session-id', self.session]
        if self.a.get('model'): argv += ['--model', self.a['model']]
        await self.spawn(argv)
        self.readers.append(asyncio.create_task(self.read()))

    async def read(self):
        try:
            async for line in lines(self.proc.stdout):
                frame = json.loads(line)
                self.sink.native(frame.get('type', 'unknown'), bounded(frame, len(line)))
                if frame.get('type') == 'system' and frame.get('subtype') == 'init':
                    self.sink.session(frame['session_id'])
                if frame.get('type') == 'result' and self.done and not self.done.done():
                    if frame.get('is_error'):
                        self.fail(frame.get('result') or str(frame.get('errors', frame)))
                    else: self.done.set_result(frame.get('result', ''))
            raise RuntimeError('Claude CLI 已退出：' + self.stderr_tail)
        except asyncio.CancelledError:
            return
        except Exception as e: self.fail(str(e))

    async def run(self, text):
        self.done = asyncio.get_running_loop().create_future()
        if not self.proc: await self.start()
        await self.write({'type': 'user', 'message': {'role': 'user', 'content': text},
                          'session_id': self.session, 'parent_tool_use_id': None})
        return await self.done


class DemoRuntime:
    """可预测的本地演示：不联网、不调用模型、不执行命令，也不伪装成真实模型结果。

    与真实后端走同一条节点 → 中心工具路径，用来验证调度与协议，而不是模型质量。
    """
    def __init__(self, sink, agent):
        self.sink, self.a = sink, agent

    async def run(self, text):
        sink, aid = self.sink, self.a['id']
        if not self.a.get('session'):
            self.a['session'] = 'demo-' + aid
            sink.session(self.a['session'])
        payload = json.loads(text.split('\n', 1)[1])
        message, task = payload['message'], payload['current_state']['task']
        sink.native('demo', {'text': '模拟模式：正在处理输入，不调用模型或执行命令。'})
        await asyncio.sleep(0.15)
        if aid == 'master':
            if message.get('key') == 'review':
                tasks = (await sink.tool('list_tasks', {}))['tasks']
                await sink.tool('finish_review', {
                    'text': '[模拟审核] ' + '；'.join(t['title'] + '：' + t['state'] for t in tasks),
                    'cursors': {t['id']: t['review_cursor'] for t in tasks}})
                return ''
            return '[模拟 Master] 已收到。请用「新建任务」试用完整任务、提问与侧栏流程；实际模型请选择 codex/claude 模式。'
        if task['state'] == 'completed':
            return '[模拟] 原任务已经完成，记录在任务事件中。'
        if '回答/修正' in message['text']:
            sink.native('item/completed', {'item': {'id': 'demo-cmd-' + message['id'], 'type': 'commandExecution',
                        'command': 'echo 模拟验证', 'status': 'completed', 'exitCode': 0,
                        'aggregatedOutput': '模拟验证'}})
            await sink.tool('finish_task', {'result': '[模拟] 任务流程演示完成。',
                                            'evidence': '本地事件记录；未执行真实业务。'})
            return '[模拟] 完成。'
        if task['state'] == 'waiting':
            return '[模拟] 等待问题答复。'
        await sink.tool('ask_master', {'text': '这是模拟问题：是否按任务验收条件继续？',
                                       'recommendation': '建议继续；本演示不会修改业务文件。', 'blocking': True})
        return '[模拟] 已提出问题，等待回答。'

    async def close(self):
        pass


def make_runtime(sink, agent):
    return {'codex': CodexRuntime, 'claude': ClaudeRuntime, 'demo': DemoRuntime}[agent['backend']](sink, agent)
