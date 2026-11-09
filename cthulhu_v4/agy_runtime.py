"""原生 agy ACP v1 适配器：节点保存原生会话，中心只保存 session 指针。

选择 ACP 是为了在 new/load 中注入本回合 MCP，不改用户全局 MCP 配置，
也不让并行 agent 共享认证 token。默认继承 Claude 配置，但不转换模型协议。
"""
import asyncio
import json
import uuid
import re
from pathlib import Path

from .antigravity import claude_credentials, native_env, redact, credential_profile
from .runtime import ProcessRuntime, bounded, lines


class AntigravityRuntime(ProcessRuntime):
    def __init__(self, sink, agent):
        super().__init__(sink, agent)
        self.session = agent.get('session')
        self.pending, self.serial = {}, 0
        self.final, self.fatal = [], False
        self.loading = True
        self.key, self.base = None, None
        self.log_path = None
        self.log_offset = 0
        self.config = {}
        self.config_changed = asyncio.Event()

    def env(self):
        env = super().env()
        if self.key is None:
            self.key, self.base = claude_credentials(env, self.a['cwd'])
        return native_env(env, self.key, self.base)

    def safe(self, value):
        # agy 错误可能包含 HTTP 响应和 URL，先去掉已知凭据再进入中心事件库。
        if isinstance(value, str): return redact(value, self.key)
        if isinstance(value, list): return [self.safe(x) for x in value]
        if isinstance(value, dict): return {k: self.safe(v) for k, v in value.items()}
        return value

    async def request(self, method, params, timeout=60):
        self.serial += 1
        ident = self.serial
        fut = asyncio.get_running_loop().create_future()
        self.pending[ident] = fut
        try:
            await self.write({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f'Antigravity {method} 超过 {timeout} 秒未响应') from None
        finally:
            self.pending.pop(ident, None)

    async def start(self):
        self.env()  # 没有可继承配置时在创建原生进程前失败，不尝试 Google 默认登录。
        native_dir = Path(self.sink.native_dir)
        native_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_path = native_dir / ('acp-' + uuid.uuid4().hex + '.log')
        await self.spawn(['agy', 'acp', '--gemini-dir', str(native_dir), '--disable-telemetry',
                          '--log-file', str(self.log_path), '--dangerously-skip-permissions'])
        self.readers.append(asyncio.create_task(self.read()))
        hello = await self.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
            'clientInfo': {'name': 'cthulhu_v4', 'version': '0.1.0'}})
        caps = hello.get('agentCapabilities') or {}
        if hello.get('protocolVersion') != 1:
            raise RuntimeError('Antigravity 未协商 ACP v1，停止以免误读原生事件')
        await self.request('authenticate', {'methodId': 'gemini-api-key'})
        cfg = self.mcp()
        params = {'cwd': self.a['cwd'], 'mcpServers': [{'name': 'cthulhu', 'command': cfg['command'],
            'args': cfg['args'], 'env': [{'name': k, 'value': v} for k, v in cfg['env'].items()]}]}
        if self.session:
            if not caps.get('loadSession'):
                raise RuntimeError('该 agy 版本不支持恢复原生会话')
            params['sessionId'] = self.session
            # load 会重播历史：恢复期间不把旧文本再次写进中心，也不把它混入本轮答案。
            response = await self.request('session/load', params, timeout=300)
            if response.get('sessionId', self.session) != self.session:
                raise RuntimeError('Antigravity 恢复返回了不同 session，拒绝切换')
        else:
            if not caps.get('_meta', {}).get('antigravity', {}).get('systemPrompt', {}).get('append'):
                raise RuntimeError('该 agy 版本不支持原生 systemPrompt append，不能注入 harness 规则')
            params['_meta'] = {'antigravity': {'systemPrompt': {'append': self.instructions()}}}
            response = await self.request('session/new', params, timeout=120)
            self.session = response.get('sessionId')
            if not isinstance(self.session, str) or not self.session:
                raise RuntimeError('Antigravity 未返回原生 session ID')
            # 先保存原生指针再配置模型；之后任何失败都必须恢复它，不能偷偷换成空白会话。
            self.sink.session(self.session)
        self.config.update({o['id']: o.get('currentValue') for o in response.get('configOptions', []) if o.get('id')})
        if self.a.get('model'):
            available = (response.get('models') or {}).get('availableModels') or []
            if not any(m.get('modelId') == self.a['model'] for m in available):
                raise RuntimeError('所选模型不在该 agy 原生会话目录中，拒绝静默替换')
            self.config_changed.clear()
            await self.request('session/set_model', {'sessionId': self.session, 'modelId': self.a['model']})
            target = self.a['model']
            match = re.fullmatch(r'(.+)-(high|medium|low)', target)
            expected = {'model': match[1], 'thought_level': match[2]} if match else {'model': target}
            async def applied():
                while any(self.config.get(k) != v for k, v in expected.items()):
                    await self.config_changed.wait()
                    self.config_changed.clear()
            try: await asyncio.wait_for(applied(), 10)
            except asyncio.TimeoutError:
                raise RuntimeError('Antigravity 未确认所选模型/档位生效，停止以免静默替换') from None
        self.loading = False
        self.sink.native('runtime_config', {'backend': 'antigravity', 'session': self.session,
            'model': self.a.get('model') or (response.get('models') or {}).get('currentModelId'),
            'auth_source': 'claude', 'availability': 'unverified'})

    async def stderr(self):
        async for line in lines(self.proc.stderr):
            text = redact(line.decode(errors='replace'), self.key)
            self.stderr_tail = (self.stderr_tail + text)[-6000:]
            self.sink.native('stderr', {'text': text})

    async def read(self):
        try:
            async for line in lines(self.proc.stdout):
                frame = json.loads(line)
                if 'id' in frame and 'method' not in frame:
                    fut = self.pending.get(frame['id'])
                    if fut and not fut.done():
                        if 'error' in frame:
                            fut.set_exception(RuntimeError('Antigravity：' + str(self.safe(frame['error']))))
                        else:
                            fut.set_result(frame.get('result') or {})
                    continue
                method, p = frame.get('method'), frame.get('params') or {}
                if 'id' in frame:
                    # 正常工具由原生 CLI 本地执行；未声明 client 文件/终端能力，不能误接管执行。
                    if method == 'session/request_permission':
                        reply = {'result': {'outcome': {'outcome': 'cancelled'}}}
                    else:
                        reply = {'error': {'code': -32601, 'message': 'Unsupported client request'}}
                    await self.write({'jsonrpc': '2.0', 'id': frame['id'], **reply})
                    self.sink.native('declined_request', {'method': method})
                    continue
                if method != 'session/update':
                    continue
                update = p.get('update') or {}
                if p.get('sessionId') == self.session and update.get('sessionUpdate') == 'config_option_update':
                    self.config.update({o['id']: o.get('currentValue') for o in update.get('configOptions', []) if o.get('id')})
                    self.config_changed.set()
                if self.loading:
                    continue
                if p.get('sessionId') != self.session:
                    raise RuntimeError('Antigravity 输出了其他 session 的事件，停止以免串线')
                update = p.get('update') or {}
                if update.get('sessionUpdate') in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call', 'tool_call_update', 'plan'):
                    self.sink.native('agy_update', bounded(self.safe(update), len(line)))
                if update.get('sessionUpdate') == 'agent_message_chunk':
                    content = update.get('content') or {}
                    if content.get('type') == 'text':
                        text = content.get('text') or ''
                        # agy 1.5.3 的 ACP 即使推理 HTTP 400 也返回 end_turn；实测只在公开
                        # chunk 发出此终止通知。必须显式判失败，不能把空白/告警当模型成功回答。
                        if text == '\n⚠ Agent execution terminated due to error.\n':
                            self.fatal = True
                        self.final.append(text)
            raise RuntimeError('Antigravity ACP 已退出：' + self.stderr_tail)
        except asyncio.CancelledError:
            return
        except Exception as e:
            for fut in self.pending.values():
                if not fut.done(): fut.set_exception(RuntimeError(redact(str(e), self.key)))

    def native_error(self):
        # 仅查询本进程日志中的原生错误行，不读取/拼接会话历史；完整日志仍留所属节点。
        detail = ''
        try:
            with self.log_path.open('rb') as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(self.log_offset, size - 65536))
                for line in f.read().decode(errors='replace').splitlines():
                    if 'agent executor error:' in line:
                        detail = line.split('agent executor error:', 1)[1].strip()
        except OSError:
            pass
        return 'Antigravity 原生执行失败：' + (redact(detail, self.key)[-1500:] or 'CLI 已报告终止错误，详见所属节点原生日志')

    async def run(self, text):
        if not self.proc:
            await self.start()
        self.final, self.fatal = [], False
        # 同一进程可连续多轮：只看当前轮开始后的日志，不能把上轮失败带进新回合。
        try: self.log_offset = self.log_path.stat().st_size
        except OSError: self.log_offset = 0
        # prompt 没有固定超时；中心租约、节点 watchdog 和 close 管理进程组，不重放副作用。
        result = await self.request('session/prompt', {'sessionId': self.session,
            'prompt': [{'type': 'text', 'text': text}]}, timeout=None)
        self.sink.native('agy_result', self.safe(result))
        if self.fatal:
            error = self.native_error()
            # 只有本轮原生执行错误能生成验证结论，目录查询或普通模型文本不算协议证据。
            status = 'protocol_unsupported' if '不支持 Gemini 协议' in error else 'failed'
            if hasattr(self.sink, 'model_evidence'):
                self.sink.model_evidence(credential_profile(self.key, self.base), status, error, self.a.get('model'))
            raise RuntimeError(error)
        if result.get('stopReason') != 'end_turn':
            raise RuntimeError('Antigravity 回合未成功完成：' + str(result.get('stopReason')))
        if hasattr(self.sink, 'model_evidence'):
            self.sink.model_evidence(credential_profile(self.key, self.base), 'completed', '', self.a.get('model'))
        return redact(''.join(self.final), self.key)
