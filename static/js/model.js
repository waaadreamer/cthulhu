// 事件 → 会话块。纯数据模型，不接触 DOM，可在 node 下单独测试。
// 只呈现 CLI 已公开的内容：不生成、推测或补写思考；同一原生 item 的 start/delta/complete 合并为一块。
const TASK_PREFIX = '你的唯一任务：\n';
const NOTICE = {
  interrupted: 'warn', control: 'info', recovery: 'warn', node_attention: 'error', runtime_error: 'error',
  urgent: 'warn', deadline: 'info', overdue: 'warn', orphan_process: 'error', recovery_exhausted: 'error',
  master_attention: 'error', question_timeout: 'warn', ask_owner: 'info',
};
export const STEP_TYPES = new Set(['command', 'tool', 'patch', 'text', 'reasoning', 'plan']);

export function toText(v) {
  if (v == null) return '';
  if (typeof v === 'string') {
    const s = v.trim();
    if (/^[[{]/.test(s) && /[\]}]$/.test(s)) { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { /* 原样 */ } }
    return v;
  }
  if (Array.isArray(v)) return v.map(toText).filter(Boolean).join('\n');
  if (typeof v === 'object') {
    if (v.type === 'text' && typeof v.text === 'string') return toText(v.text);
    if (Array.isArray(v.content)) return toText(v.content);
    return JSON.stringify(v, null, 2);
  }
  return String(v);
}

export function unwrapShell(command) {
  const c = Array.isArray(command) ? command.join(' ') : String(command ?? '');
  const m = c.match(/^\/(?:usr\/)?bin\/(?:ba|z)?sh -lc (['"])([\s\S]*)\1$/);
  return m ? m[2].replace(/'\\''/g, "'") : c;
}

export function diffStat(diff) {
  let add = 0, del = 0;
  for (const l of String(diff || '').split('\n')) {
    if (l.startsWith('+') && !l.startsWith('+++')) add++;
    else if (l.startsWith('-') && !l.startsWith('---')) del++;
  }
  return { add, del };
}

function editDiff(input) {
  // Claude 的 Edit/Write 只给新旧片段；转成统一的 +/- 行，与 Codex diff 同样展示。
  if (input.content != null) return String(input.content).split('\n').map(l => '+' + l).join('\n');
  const edits = input.edits || [{ old_string: input.old_string, new_string: input.new_string }];
  return edits.map(e => [...String(e.old_string ?? '').split('\n').map(l => '-' + l),
                         ...String(e.new_string ?? '').split('\n').map(l => '+' + l)].join('\n')).join('\n@@\n');
}

export function preview(input) {
  if (input == null) return '';
  if (typeof input !== 'object') return String(input);
  return Object.values(input).filter(v => v != null && typeof v !== 'object' && String(v).trim())
    .map(v => String(v).replace(/\s+/g, ' ')).join(', ');
}

export class Model {
  // mode: drawer = 某个 old_one 的完整过程；process = 主聊天里 Master 一个回合的过程（最终回复另行展示）。
  // harnessTools: demo 没有原生工具事件，用中心的 harness_tool 记录代替；真实后端用原生事件，顺序更准。
  constructor(agent, mode = 'drawer', { harnessTools = false } = {}) {
    Object.assign(this, { agent, mode, harnessTools });
    this.blocks = []; this.byKey = new Map(); this.changed = new Set(); this.skip = new Set();
    this.seen = new Set(); this.toolKeys = new Map(); this.claudeMsg = ''; this.turn = 't0';
  }

  put(key, e, fields) {
    if (this.skip.has(key)) return null;
    let b = this.byKey.get(key);
    if (!b) { b = { key, seq: e.seq, at: e.at }; this.byKey.set(key, b); this.blocks.push(b); }
    Object.assign(b, fields);
    this.changed.add(key);
    return b;
  }

  append(key, e, type, delta) {
    const b = this.byKey.get(key) || this.put(key, e, { type, text: '' });
    if (!b) return;
    b.text = (b.text || '') + delta;
    this.changed.add(key);
  }

  remove(key) { const b = this.byKey.get(key); if (b && !b.removed) { b.removed = true; this.changed.add(key); } }
  live() { return this.blocks.filter(b => !b.removed); }
  hasText(t) { t = String(t || '').trim(); return !!t && this.live().some(b => b.type === 'text' && String(b.text || '').trim() === t); }
  dropText(t) {
    t = String(t || '').trim();
    const b = t && this.live().find(b => b.type === 'text' && String(b.text || '').trim() === t);
    if (b) this.remove(b.key);
    return !!b;
  }
  // 当前在做什么：给“思考中”指示用。只描述已公开的动作，不猜测模型内部状态。
  activity() {
    const b = this.live().filter(b => !['turn', 'log'].includes(b.type) && (b.type !== 'notice' || b.retry)).at(-1);
    if (!b) return { kind: 'think', text: '思考中' };
    if (b.retry) return { kind: 'retry', text: b.text };
    if (b.status === 'running') {
      if (b.type === 'command') return { kind: 'command', text: '正在执行', code: String(b.command || '').split('\n')[0] };
      if (b.type === 'tool') return { kind: 'tool', text: '正在调用 ' + b.name };
      if (b.type === 'patch') return { kind: 'patch', text: '正在编辑 ' + (b.files || []).map(f => String(f.path).split('/').pop()).join(', ') };
    }
    if (b.type === 'text') return { kind: 'text', text: '正在回复' };
    return { kind: 'think', text: '思考中' };
  }

  take() { const out = this.blocks.filter(b => this.changed.has(b.key)); this.changed.clear(); return out; }
  steps() { return this.live().filter(b => STEP_TYPES.has(b.type)).length; }

  apply(e) {
    if (this.seen.has(e.seq)) return;
    this.seen.add(e.seq);
    const d = e.data || {};
    if (e.kind.startsWith('native:')) this.native(e.kind.slice(7), d, e);
    else this.harness(e, d);
  }

  harness(e, d) {
    const k = 'ev:' + e.seq;
    switch (e.kind) {
      case 'dispatch':
        if (e.target === this.agent) { this.turn = d.job || k; this.put('turn:' + this.turn, e, { type: 'turn' }); }
        return;
      case 'message':
        if (this.mode !== 'drawer' || d.copy) return; // 路由副本已以问题/回答/结果卡片展示
        if (e.text.startsWith(TASK_PREFIX)) {
          try { this.put(k, e, { type: 'task', task: JSON.parse(e.text.slice(TASK_PREFIX.length)) }); return; } catch { /* 当普通消息 */ }
        }
        this.put(k, e, { type: 'message', from: e.actor, to: e.target, text: e.text, priority: d.priority });
        return;
      case 'harness_tool':
        if (this.harnessTools) this.put(k, e, { type: 'tool', name: e.text, input: d.arguments, output: d.result, status: 'completed' });
        return;
      case 'review_requested': this.put(k, e, { type: 'result', pending: true, text: e.text, evidence: d.evidence, round: d.round }); return;
      case 'completed': this.put(k, e, { type: 'result', text: e.text, evidence: d.evidence, reason: d.reason }); return;
      case 'task_failed': this.put(k, e, { type: 'notice', level: 'error', text: '判定未达成：' + e.text }); return;
      case 'review_returned': this.put(k, e, { type: 'notice', level: 'warn', text: '审核退回：' + e.text }); return;
      case 'task_reopened': this.put(k, e, { type: 'notice', level: 'info', text: '重新打开：' + e.text }); return;
      case 'reply': if (!this.hasText(e.text)) this.put(k, e, { type: 'text', text: e.text }); return;
      case 'question': this.put(k, e, { type: 'question', text: e.text, recommendation: d.recommendation, blocking: d.blocking }); return;
      case 'answer': this.put(k, e, { type: 'notice', level: 'ok', text: `${e.actor === 'owner' ? '你' : 'Master'}的回答：${e.text}` }); return;
      default: if (NOTICE[e.kind]) this.put(k, e, { type: 'notice', level: NOTICE[e.kind], text: e.text });
    }
  }

  native(m, d, e) {
    const k = 'ev:' + e.seq, id = 'i:' + d.itemId;
    if (m === 'item/started' || m === 'item/completed') return this.item(d.item || {}, m === 'item/completed', e);
    if (m === 'item/agentMessage/delta') return this.append(id, e, 'text', d.delta || '');
    if (m === 'item/reasoning/summaryTextDelta') return this.append(id, e, 'reasoning', d.delta || '');
    if (m === 'item/reasoning/summaryPartAdded') return this.byKey.has(id) && this.append(id, e, 'reasoning', '\n\n');
    if (m === 'item/reasoning/textDelta') return this.byKey.has(id) || this.append(id + ':raw', e, 'reasoning', d.delta || '');
    if (m === 'item/commandExecution/outputDelta') {
      const b = this.byKey.get(id) || this.put(id, e, { type: 'command', command: '', status: 'running', output: '' });
      if (b) { b.output = (b.output || '') + (d.delta || ''); this.changed.add(id); }
      return;
    }
    if (m === 'turn/completed') {
      const t = d.turn || {};
      if (t.error || t.status === 'failed') this.put(k, e, { type: 'notice', level: 'error', text: '回合失败：' + toText(t.error || t.status) });
      return;
    }
    if (m === 'turn/plan/updated') {
      return this.put('plan:' + this.turn, e, { type: 'plan', explanation: d.explanation,
        steps: (d.plan || []).map(s => ({ text: s.step, status: s.status })) });
    }
    if (m === 'stderr') {
      const key = 'log:' + this.turn, b = this.byKey.get(key) || this.put(key, e, { type: 'log', lines: [] });
      if (b) { b.lines.push(String(d.text || '').replace(/\n$/, '')); this.changed.add(key); }
      return;
    }
    if (m === 'error') {
      const msg = toText(d.error?.message || d.error || d.message || d);
      // 原生 CLI 的自动重连是同一件事的进度，只保留最新一条，不刷屏。
      const retry = msg.match(/Reconnecting\.*\s*(\d+\/\d+)/);
      if (retry) return this.put('retry:' + this.turn, e, { type: 'notice', level: 'warn', retry: true, text: '模型连接中断，正在重连（' + retry[1] + '）' });
      return this.put(k, e, { type: 'notice', level: 'error', text: msg });
    }
    if (m === 'declined_request') return this.put(k, e, { type: 'notice', level: 'warn', text: '原生审批/输入请求未执行：' + d.method });
    if (m === 'demo') return this.put(k, e, { type: 'reasoning', text: d.text });
    if (m === 'stream_event') return this.claudeStream(d.event || {}, e);
    if (m === 'assistant') {
      const msg = d.message || {};
      (msg.content || []).forEach((b, i) => this.claudeBlock(b, i, msg.id || this.claudeMsg, e, true));
      return;
    }
    if (m === 'user') { for (const b of d.message?.content || []) if (b?.type === 'tool_result') this.toolResult(b, e); return; }
    if (m === 'result' && d.is_error) return this.put(k, e, { type: 'notice', level: 'error', text: toText(d.result || d.errors) });
    // 其余（token 用量、限速、握手、hook 生命周期等）是传输遥测，不是会话正文。
  }

  item(it, done, e) {
    const key = 'i:' + it.id, prev = this.byKey.get(key);
    const status = !done ? 'running' : ['failed', 'declined'].includes(it.status) ? 'failed' : 'completed';
    switch (it.type) {
      case 'userMessage': return; // 路由输入已有带来源的 message 事件，不重复展示包装 JSON。
      case 'agentMessage':
        if (this.mode === 'process' && it.phase === 'final_answer') { this.skip.add(key); return; }
        this.put(key, e, { type: 'text', text: it.text || prev?.text || '', final: it.phase === 'final_answer' });
        return;
      case 'reasoning': {
        const text = toText(it.summary?.length ? it.summary : it.content);
        if (text) { this.put(key, e, { type: 'reasoning', text }); this.remove(key + ':raw'); }
        return;
      }
      case 'commandExecution': {
        const failed = it.exitCode != null && it.exitCode !== 0;
        this.put(key, e, { type: 'command', command: unwrapShell(it.command), cwd: it.cwd, output: it.aggregatedOutput ?? prev?.output ?? '',
          exitCode: it.exitCode, duration: it.durationMs, status: failed ? 'failed' : status });
        return;
      }
      case 'mcpToolCall': case 'dynamicToolCall': case 'collabToolCall':
        this.put(key, e, { type: 'tool', name: it.tool || it.type, server: it.server,
          input: it.arguments, output: it.error || it.result, status: it.error ? 'failed' : status, duration: it.durationMs });
        return;
      case 'fileChange':
        this.put(key, e, { type: 'patch', status, files: (it.changes || []).map(c => ({ path: c.path, kind: c.kind?.type || c.kind, diff: c.diff || '' })) });
        return;
      case 'webSearch': this.put(key, e, { type: 'tool', name: '网页搜索', input: it.query || it.action, status }); return;
      case 'plan': this.put(key, e, { type: 'text', text: it.text }); return;
      case 'contextCompaction': this.put(key, e, { type: 'notice', level: 'info', text: '原生 CLI 已整理会话上下文' }); return;
      default: this.put(key, e, { type: 'tool', name: it.type || '原生活动', input: it, status });
    }
  }

  claudeStream(ev, e) {
    if (ev.type === 'message_start') { this.claudeMsg = ev.message?.id || 'm' + e.seq; return; }
    if (ev.type === 'content_block_start') return this.claudeBlock(ev.content_block || {}, ev.index, this.claudeMsg, e, false);
    if (ev.type === 'content_block_delta') {
      const dl = ev.delta || {}, key = `c:${this.claudeMsg}:${ev.index}`;
      if (dl.type === 'text_delta') this.append(key, e, 'text', dl.text || '');
      else if (dl.type === 'thinking_delta') this.append(key, e, 'reasoning', dl.thinking || '');
    }
  }

  claudeBlock(b, i, msg, e, full) {
    const key = `c:${msg}:${i}`, prev = this.byKey.get(key);
    if (b.type === 'text') { this.put(key, e, { type: 'text', text: b.text || prev?.text || '' }); return; }
    if (b.type === 'thinking') { if (b.thinking || !full) this.put(key, e, { type: 'reasoning', text: b.thinking || prev?.text || '' }); return; }
    if (b.type !== 'tool_use') return;
    const tk = 'c:' + b.id, input = b.input || {}, name = b.name || 'tool';
    const status = this.byKey.get(tk)?.status || 'running';
    this.toolKeys.set(b.id, tk);
    if (name === 'Bash') this.put(tk, e, { type: 'command', command: input.command || '', description: input.description, status });
    else if (['Edit', 'Write', 'MultiEdit'].includes(name) && input.file_path) this.put(tk, e, { type: 'patch', status, files: [{ path: input.file_path, diff: editDiff(input) }] });
    else if (name.startsWith('mcp__')) { const [, server, ...rest] = name.split('__'); this.put(tk, e, { type: 'tool', name: rest.join('__'), server, input, status }); }
    else this.put(tk, e, { type: 'tool', name, input, status });
  }

  toolResult(b, e) {
    const tk = this.toolKeys.get(b.tool_use_id) || 'c:' + b.tool_use_id, cur = this.byKey.get(tk);
    const status = b.is_error ? 'failed' : 'completed';
    if (!cur) { this.put(tk, e, { type: 'tool', name: '工具结果', output: b.content, status }); return; }
    this.put(tk, e, { status, output: cur.type === 'tool' ? b.content : toText(b.content) });
  }
}
