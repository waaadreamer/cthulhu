"""薄原生适配器：不实现模型循环、不手工压缩、不跨后端搬运上下文。

Codex 使用 app-server；Claude 使用持久 stream-json CLI。两者均挂同一个
MCP server，所有公开输出原样入事件库。运行时事件流包含工具与 hook 事件，
无需为采集额外执行 shell hooks。一次只有一个活动 turn（Core 负责串行）。
"""
import asyncio
import json
import os
import signal
import sys
import uuid

from .core import ROOT, process_identity


class ProcessRuntime:
    def __init__(self, core, agent):
        self.core, self.a = core, agent
        self.proc = None
        self.readers = []
        self.done = None
        self.stderr_tail = ''

    def env(self):
        env = os.environ.copy()
        env.pop('CLAUDECODE', None)  # harness 启动独立会话，不嵌套当前交互客户端。
        env.update(CTHULHU_URL=self.core.url, CTHULHU_AGENT=self.a['id'], CTHULHU_TOKEN=self.a['token'])
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
            limit=16 * 1024 * 1024, start_new_session=True)
        a = self.core.store.get('agent', self.a['id'])
        a.update(pid=self.proc.pid, pid_identity=process_identity(self.proc.pid))
        self.core.store.put('agent', a)
        self.readers.append(asyncio.create_task(self.stderr()))

    async def stderr(self):
        while line := await self.proc.stderr.readline():
            text = line.decode(errors='replace')
            self.stderr_tail = (self.stderr_tail + text)[-6000:]
            self.core.record_native(self.a['id'], 'stderr', {'text': text})

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
        a = self.core.store.get('agent', self.a['id'])
        if self.proc and a.get('pid') == self.proc.pid:
            a.update(pid=None, pid_identity=None)
            self.core.store.put('agent', a)
        self.proc = None


class CodexRuntime(ProcessRuntime):
    def __init__(self, core, agent):
        super().__init__(core, agent)
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
            self.core.save_session(self.a['id'], self.session)
        self.core.record_native(self.a['id'], 'runtime_config', {
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
            while line := await self.proc.stdout.readline():
                frame = json.loads(line)
                if 'id' in frame and 'method' not in frame:
                    fut = self.pending.get(frame['id'])
                    if fut and not fut.done():
                        if 'error' in frame: fut.set_exception(RuntimeError(str(frame['error'])))
                        else: fut.set_result(frame.get('result', {}))
                    continue
                method, p = frame.get('method', ''), frame.get('params', {})
                self.core.record_native(self.a['id'], method, p)
                if 'id' in frame:
                    # 首版无原生审批 UI。明确拒绝而非挂起；模型可通过 ask_master 说明需求。
                    if method.endswith('requestApproval'):
                        await self.write({'id': frame['id'], 'result': {'decision': 'decline'}})
                    elif method.endswith('requestUserInput'):
                        await self.write({'id': frame['id'], 'result': {'answers': {}}})
                    else:
                        await self.write({'id': frame['id'], 'error': {'code': -32601, 'message': 'Unsupported request'}})
                    self.core.store.emit('native_request', '原生审批/输入请求未执行，请通过 harness 问题工具处理：' + method,
                                         target=self.a['id'], task=self.core.task_id(self.a['id']), visible=True)
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
        await self.request('turn/start', params)
        return await self.done


class ClaudeRuntime(ProcessRuntime):
    def __init__(self, core, agent):
        super().__init__(core, agent)
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
            while line := await self.proc.stdout.readline():
                frame = json.loads(line)
                self.core.record_native(self.a['id'], frame.get('type', 'unknown'), frame)
                if frame.get('type') == 'system' and frame.get('subtype') == 'init':
                    self.core.save_session(self.a['id'], frame['session_id'])
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
    """可预测的本地演示，不联网、不伪装成真实模型结果。"""
    def __init__(self, core, agent):
        self.core, self.a = core, agent

    async def run(self, text):
        c, aid = self.core, self.a['id']
        if not self.a['session']:
            self.a['session'] = 'demo-' + aid
            c.save_session(aid, self.a['session'])
        c.record_native(aid, 'demo', {'text': '模拟模式：正在处理输入，不调用模型或执行命令。'})
        await asyncio.sleep(0.15)
        message = json.loads(text.split('\n', 1)[1])['message']
        if aid == 'master':
            if message.get('key') == 'review':
                tasks = c.tool(aid, 'list_tasks', {})['tasks']
                c.tool(aid, 'finish_review', {'text': '[模拟审核] ' + '；'.join(t['title'] + '：' + t['state'] for t in tasks),
                                             'cursors': {t['id']: c.store.highwater() for t in tasks}})
                return ''
            return '[模拟 Master] 已收到。请用「新建任务」试用完整任务、提问与侧栏流程；实际模型请选择 codex/claude 模式。'
        task = c.task_for(aid)
        if task['state'] == 'completed': return '[模拟] 原任务已经完成，记录在任务事件中。'
        if task['state'] == 'waiting': return '[模拟] 等待问题答复。'
        if not any(q['task'] == task['id'] for q in c.store.all('question')):
            c.tool(aid, 'ask_master', {'text': '这是模拟问题：是否按任务验收条件继续？',
                   'recommendation': '建议继续；本演示不会修改业务文件。', 'blocking': True})
            return '[模拟] 已提出问题，等待回答。'
        c.tool(aid, 'finish_task', {'result': '[模拟] 任务流程演示完成。', 'evidence': '本地事件记录；未执行真实业务。'})
        return '[模拟] 完成。'

    async def close(self): pass


def make_runtime(core, agent):
    return {'codex': CodexRuntime, 'claude': ClaudeRuntime, 'demo': DemoRuntime}[agent['backend']](core, agent)
