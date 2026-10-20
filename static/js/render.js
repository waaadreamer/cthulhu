// 会话块 → DOM。工具调用、命令、文件修改默认折叠为一行摘要；文字与公开思考直接展示。
import { el, markdown, clean, clock, linkPaths, fileLink } from './dom.js';
import { toText, preview, diffStat } from './model.js';

const NAMES = { owner: '你', master: 'Master', system: '系统' };
export const who = id => NAMES[id] || id || '系统';
const trunc = (s, n) => { s = String(s || '').replace(/\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s; };
const seconds = ms => ms == null ? '' : ms < 1000 ? ms + 'ms' : (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + 's';
const base = p => String(p || '').split('/').pop();
const STATUS = { running: '进行中', failed: '失败', completed: '完成' };
const ICON = { command: '❯', tool: '◆', patch: '✎', log: '≡' };

function out(text, cls = 'out', ctx = {}) {
  text = clean(text).replace(/\n+$/, '');
  if (!text) return el('div', { class: 'none', text: '（无输出）' });
  const lines = text.split('\n');
  // 输出中的绝对路径同样可点开；超长输出先显示前 40 行。
  const pre = t => linkPaths(el('pre', { class: cls }), t, ctx);
  if (lines.length <= 60 && text.length <= 8000) return pre(text);
  const box = el('div', { class: 'long' }, pre(lines.slice(0, 40).join('\n')));
  box.append(el('button', { class: 'link', type: 'button', text: `显示全部 ${lines.length} 行`,
    onclick: () => box.replaceChildren(pre(text)) }));
  return box;
}

function diff(text) {
  const box = el('pre', { class: 'diff' });
  for (const line of String(text || '').split('\n')) {
    const c = /^(\+\+\+|---|@@)/.test(line) ? 'dh' : line[0] === '+' ? 'da' : line[0] === '-' ? 'dd' : '';
    box.append(el('span', { class: c, text: line + '\n' }));
  }
  return box;
}

function step(b, icon, title, meta, body) {
  return el('details', { class: 'step', 'data-kind': b.type, 'data-status': b.status || 'completed' },
    el('summary', {},
      el('span', { class: 'ic', 'aria-hidden': 'true', text: icon }),
      el('span', { class: 'ti' }, ...title),
      meta ? el('span', { class: 'meta' }, ...[].concat(meta)) : null,
      el('span', { class: 'st', title: STATUS[b.status] || '', 'aria-label': STATUS[b.status] || '' })),
    el('div', { class: 'step-body' }, ...body));
}

const label = t => el('div', { class: 'lbl', text: t });

export function render(b, ctx = {}) {
  const md = t => markdown(t, ctx);
  switch (b.type) {
    case 'turn': return el('div', { class: 'turn', role: 'separator' }, el('span', { text: '新回合 ' + clock(b.at) }));
    case 'task': {
      const t = b.task || {};
      return el('section', { class: 'card task-card' },
        el('div', { class: 'card-k', text: '任务' }), el('h3', { text: t.title }),
        el('dl', {}, el('dt', { text: '目标' }), el('dd', {}, md(t.description)),
          el('dt', { text: '验收' }), el('dd', {}, md(t.acceptance)),
          el('dt', { text: '节点' }), el('dd', { text: t.node || 'local' })));
    }
    case 'message':
      return el('div', { class: 'msg' + (b.priority >= 10 ? ' urgent' : '') },
        el('div', { class: 'msg-h' }, el('span', { text: `${who(b.from)} → ${who(b.to)}` }),
          b.priority >= 10 ? el('span', { class: 'tag warn', text: '加急' }) : null, el('time', { text: clock(b.at) })),
        md(b.text));
    case 'text': return el('div', { class: 'prose' + (b.final ? ' final' : '') }, md(b.text));
    case 'reasoning': return el('div', { class: 'thinking' }, el('span', { class: 'think-k', text: '思考' }), md(b.text));
    case 'plan':
      return el('div', { class: 'plan' }, el('div', { class: 'lbl', text: '计划' }),
        b.explanation ? el('p', { text: b.explanation }) : null,
        el('ul', {}, ...(b.steps || []).map(s => el('li', { 'data-status': s.status, text: s.text }))));
    case 'command': {
      const cmd = String(b.command || ''), first = cmd.split('\n')[0];
      const meta = [b.exitCode != null && b.exitCode !== 0 ? el('span', { class: 'bad', text: 'exit ' + b.exitCode }) : null,
        seconds(b.duration) || null].filter(Boolean);
      return step(b, ICON.command, [el('code', { text: trunc(first, 140) + (cmd.includes('\n') ? ' …' : '') })], meta,
        [b.description ? el('div', { class: 'desc', text: b.description }) : null,
         cmd.includes('\n') || cmd.length > 140 ? out(cmd, 'cmd', ctx) : null,
         b.cwd ? el('div', { class: 'desc', text: b.cwd }) : null,
         out(b.output, 'out', ctx)]);
    }
    case 'tool': {
      const arg = trunc(preview(b.input), 90);
      return step(b, ICON.tool, [el('span', { class: 'name', text: b.name }), b.server ? el('span', { class: 'srv', text: b.server }) : null,
        arg ? el('span', { class: 'arg', text: arg }) : null],
        seconds(b.duration), [label('参数'), out(toText(b.input) || '{}'), b.output != null ? label('结果') : null,
          b.output != null ? out(toText(b.output), 'out', ctx) : null]);
    }
    case 'patch': {
      const files = b.files || [];
      let add = 0, del = 0;
      for (const f of files) { const s = diffStat(f.diff); add += s.add; del += s.del; }
      return step(b, ICON.patch, [el('span', { class: 'name', text: '编辑' }), el('span', { class: 'arg', text: files.map(f => base(f.path)).join(', ') })],
        [el('span', { class: 'add', text: '+' + add }), el('span', { class: 'del', text: '−' + del })],
        files.map(f => el('div', { class: 'file' }, el('div', { class: 'file-h' }, f.path?.startsWith('/') && ctx.node ? fileLink(ctx.node, f.path, f.path) : f.path), diff(f.diff))));
    }
    case 'result':
      // 执行者的申请与 Master 的通过是两张卡：申请只是待审核，不能显示成已完成。
      return el('section', { class: 'card result' + (b.pending ? ' pending' : '') },
        el('div', { class: 'card-k', text: b.pending ? `申请结束${b.round > 1 ? `（第 ${b.round} 次）` : ''}，待审核` : '审核通过' + (b.reason ? '：' + b.reason : '') }), md(b.text),
        b.evidence ? el('details', { class: 'evidence', open: true }, el('summary', { text: '证据' }), md(b.evidence)) : null);
    case 'question':
      return el('section', { class: 'card question' }, el('div', { class: 'card-k', text: b.blocking ? '提问，等待回答' : '提问' }),
        md(b.text), b.recommendation ? el('p', { class: 'rec', text: '建议：' + b.recommendation }) : null);
    case 'notice': return el('div', { class: 'notice ' + (b.level || 'info') }, el('span', { text: b.text }), el('time', { text: clock(b.at) }));
    case 'log':
      return el('details', { class: 'step log' }, el('summary', {}, el('span', { class: 'ic', text: ICON.log }),
        el('span', { class: 'ti', text: `运行日志（${b.lines.length} 行）` })), el('div', { class: 'step-body' }, out(b.lines.join('\n'), 'out', ctx)));
    default: return el('div');
  }
}

// 把模型的增量变化应用到容器；替换时保留折叠状态，流式更新不会把用户展开的块收起来。
export class View {
  constructor(root, model, ctx = {}) { this.root = root; this.model = model; this.ctx = ctx; this.nodes = new Map(); }
  update() {
    const changed = this.model.take();
    for (const b of changed) {
      const old = this.nodes.get(b.key);
      if (b.removed) { old?.remove(); this.nodes.delete(b.key); continue; }
      const node = render(b, this.ctx);
      if (old) { if (old.tagName === 'DETAILS' && node.tagName === 'DETAILS') node.open = old.open; old.replaceWith(node); }
      else this.root.append(node);
      this.nodes.set(b.key, node);
    }
    return changed.length;
  }
}

// 工作中指示器：持续显示在会话末尾。状态来自中心的排队与租约，动作来自已公开的原生事件。
export function elapsed(s) {
  if (s == null) return '';
  return s < 60 ? s + 's' : Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}
export class Live {
  constructor(root) {
    this.root = root;
    this.text = el('span', { class: 'live-t' }); this.code = el('code', { class: 'live-c' }); this.time = el('time');
    this.node = el('div', { class: 'live', role: 'status' },
      el('span', { class: 'sigil', 'aria-hidden': 'true', text: '✳' }), this.text, this.code, this.time);
  }
  set(info) {
    if (!info) { this.node.remove(); return; }
    this.node.dataset.phase = info.phase;
    this.text.textContent = info.text;
    this.code.textContent = info.code || ''; this.code.hidden = !info.code;
    this.time.textContent = elapsed(info.elapsed);
    if (this.root.lastElementChild !== this.node) this.root.append(this.node);
  }
}
