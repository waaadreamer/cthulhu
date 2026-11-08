// DOM 小工具、安全的 Markdown 渲染与 API 访问。全程不使用 innerHTML：命令输出里的 HTML 只会显示为文本。
import { parseBlocks } from './markdown.js';

export const $ = s => document.querySelector(s);

export function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) n.append(k instanceof Node ? k : String(k));
  return n;
}

export const clean = t => String(t ?? '').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');

// ---- 文件链接 ----
// 固定写法是 `节点::绝对路径`。没写节点的绝对路径按上下文推测：任务过程里用任务所在节点，
// 其他地方用正文里最近提到的节点名；推测不出就留空，点开后在查看器里选择。
export const fileCtx = { nodes: [] };
export const fileHref = ref => '#file=' + encodeURIComponent(ref);
// 路径里不含冒号：`/a.py:12` 的 `:12` 是行号；`节点::路径` 的前缀由 REF 单独匹配。
const P = "[^\\s/`'\"<>()（）\\[\\]{}，。；、：:,;|]";
const REF = `[A-Za-z0-9_-]{1,64}::\\/(?:${P}+\\/)*${P}*`;
const PATH = `\\/(?:${P}+\\/)+${P}*`;
const BEFORE = "(?<=^|[\\s(（\\[{'\"`：:=，,])";
// 分组序号有含义（scan 按序号分派）：1 代码 2 粗体 3 链接 4 网址 5/6 文件 7–9 公式 10 删除线 11 斜体 12 转义。
// 行内公式 $…$ 按 pandoc 规则：开头 $ 后、结尾 $ 前不能是空白，结尾 $ 后不能紧跟数字或字母，
// 这样 “$5 到 $10” 这类金额不会被当成公式。斜体只认 *…*，不认 _…_，免得把 snake_case 拆开。
const INLINE = new RegExp([
  '`[^`\\n]+`', '\\*\\*[^*\\n]+\\*\\*', '\\[[^\\]\\n]+\\]\\([^\\s)]+\\)', 'https?:\\/\\/[^\\s)<>]+',
  `(?<![A-Za-z0-9_-])${REF}`, BEFORE + PATH,
  '\\$\\$[^$\\n]+?\\$\\$', '\\\\\\(.+?\\\\\\)', '(?<![\\\\$\\w])\\$(?=[^\\s$])[^$\\n]*?[^\\s\\\\$]\\$(?![\\w$])',
  '~~[^~\\n]+~~', '(?<![*\\w])\\*(?![\\s*])[^*\\n]+?(?<![\\s*])\\*(?![*\\w])',
  '\\\\[\\\\`*_$|~\\[\\]()#+\\-.!<>]'].map(x => `(${x})`).join('|'), 'g');
const FILES = new RegExp(`((?<![A-Za-z0-9_-])${REF})|(${BEFORE}${PATH})`, 'g');
const IS_REF = new RegExp(`^${REF}$`), IS_PATH = new RegExp(`^${PATH}$`);

let mentionKey = '', mentionRe = null;
function lastMention(text) {
  const names = fileCtx.nodes;
  if (!names.length) return null;
  if (mentionKey !== names.join('\n')) {
    mentionKey = names.join('\n');
    const alt = [...names].sort((a, b) => b.length - a.length).map(n => n.replace(/[-]/g, '\\-')).join('|');
    mentionRe = new RegExp(`(?<![A-Za-z0-9_-])(${alt})(?![A-Za-z0-9_-])`, 'g');
  }
  let last = null;
  for (const m of text.matchAll(mentionRe)) last = m[1];
  return last;
}

function fileSeg(raw, ctx, prefix, label, code) {
  const ref = raw.match(/^([A-Za-z0-9_-]{1,64})::(\/.*)$/);
  const node = ref ? ref[1] : ctx.node || lastMention(prefix) || ctx.last || null;
  return { type: 'file', node, path: ref ? ref[2] : raw, label: label ?? raw, code };
}

// 纯函数：把一行文字切成片段，便于在 node 下单测，也让渲染与识别规则只有一处。
export function scan(text, ctx = {}, re = INLINE) {
  const out = [];
  let pos = 0;
  re.lastIndex = 0;
  for (const m of text.matchAll(re)) {
    let s = m[0];
    const isFile = re === FILES || m[5] || m[6];
    // 句末的点号不属于路径。
    if (isFile) s = s.replace(/\.+$/, '');
    if (!s) continue;
    if (m.index > pos) out.push({ type: 'text', text: text.slice(pos, m.index) });
    const prefix = text.slice(0, m.index);
    if (isFile) out.push(fileSeg(s, ctx, prefix));
    else if (s[0] === '`') {
      const inner = s.slice(1, -1);
      out.push(IS_REF.test(inner) || IS_PATH.test(inner) ? fileSeg(inner, ctx, prefix, inner, true) : { type: 'code', text: inner });
    } else if (m[2]) out.push({ type: 'bold', text: s.slice(2, -2) });
    else if (m[4]) out.push({ type: 'url', href: s, text: s });
    else if (m[7] || m[8]) out.push({ type: 'math', text: s.slice(2, -2) });
    else if (m[9]) out.push({ type: 'math', text: s.slice(1, -1) });
    else if (m[10]) out.push({ type: 'del', text: s.slice(2, -2) });
    else if (m[11]) out.push({ type: 'em', text: s.slice(1, -1) });
    else if (m[12]) out.push({ type: 'text', text: s[1] });
    else {
      const [, label, target] = s.match(/^\[([^\]]+)\]\((.+)\)$/);
      // 只允许 http(s) 与文件链接，避免 javascript: 等协议。
      if (/^https?:\/\//i.test(target)) out.push({ type: 'url', href: target, text: label });
      else if (IS_REF.test(target) || IS_PATH.test(target)) out.push(fileSeg(target, ctx, prefix, label));
      else out.push({ type: 'text', text: `${label} (${target})` });
    }
    pos = m.index + s.length;
  }
  if (pos < text.length) out.push({ type: 'text', text: text.slice(pos) });
  return out;
}

export function fileLink(node, path, label, code = false) {
  const ref = (node || '') + '::' + path;
  return el('a', { class: 'file-link' + (node ? '' : ' unresolved'), href: fileHref(ref), 'data-ref': ref,
    title: node ? ref : path + '（未标明节点，点开后选择）' }, code ? el('code', { text: label }) : label);
}

function put(parent, segs, ctx) {
  for (const g of segs) {
    if (g.type === 'text') parent.append(g.text);
    else if (g.type === 'code') parent.append(el('code', { text: g.text }));
    else if (g.type === 'bold') { const b = el('strong'); put(b, scan(g.text, ctx), ctx); parent.append(b); }
    else if (g.type === 'em' || g.type === 'del') { const b = el(g.type); put(b, scan(g.text, ctx), ctx); parent.append(b); }
    else if (g.type === 'math') parent.append(math(g.text, false));
    else if (g.type === 'url') parent.append(el('a', { href: g.href, target: '_blank', rel: 'noopener noreferrer', text: g.text }));
    else parent.append(fileLink(g.node, g.path, g.label, g.code));
  }
}

function inline(parent, text, ctx = {}) { put(parent, scan(text, ctx), ctx); }

// 命令输出等纯文本：只识别文件链接，不做 Markdown。
export function linkPaths(parent, text, ctx = {}) { put(parent, scan(text, ctx, FILES), ctx); return parent; }

// 单行富文本（事件行等）。
export function rich(text, ctx = {}) { const n = el('span'); inline(n, text, ctx); return n; }

// ---- 公式 ----
// KaTeX（static/vendor/katex，MIT）约 270KB，只在第一次遇到公式时加载。加载前先显示 TeX 原文，
// 加载完再原地替换：流式输出时节点会被频繁替换，已脱离文档的占位就跳过。
let katexLoad = null;
const pendingMath = new Set();
function typeset(n) {
  try { globalThis.katex.render(n.dataset.tex, n, { displayMode: n.dataset.display === '1', throwOnError: false, output: 'html' }); }
  catch { /* 渲染失败时保留原文 */ }
}
export function math(tex, display) {
  const n = el(display ? 'div' : 'span', { class: display ? 'math math-block' : 'math', 'data-tex': tex, 'data-display': display ? '1' : '0', text: tex });
  if (globalThis.katex) { typeset(n); return n; }
  pendingMath.add(n);
  // UMD 包在模块里执行时挂到 self.katex 上；样式表同时加载，字体按需由样式表引用。
  katexLoad ||= (document.head.append(el('link', { rel: 'stylesheet', href: new URL('../vendor/katex/katex.min.css', import.meta.url).href })),
    import('../vendor/katex/katex.min.js')).then(() => { for (const p of pendingMath) p.isConnected !== false && typeset(p); pendingMath.clear(); },
    () => { katexLoad = null; pendingMath.clear(); });
  return n;
}

// 块树 → DOM。文件路径的节点推测沿用“正文里最近提到的节点名”：按阅读顺序逐段更新 ctx.last。
function blocks(parent, list, ctx) {
  const run = (n, t) => { inline(n, t, ctx); ctx.last = lastMention(t) || ctx.last; return n; };
  for (const b of list) {
    if (b.type === 'para') {
      const p = el('p');
      b.lines.forEach((l, i) => { if (i) p.append(el('br')); run(p, l); });
      parent.append(p);
    } else if (b.type === 'heading') parent.append(run(el('h' + Math.min(6, b.level + 2)), b.text));
    else if (b.type === 'code') parent.append(el('pre', { class: 'code', 'data-lang': b.lang || null, text: b.text }));
    else if (b.type === 'math') parent.append(math(b.text, true));
    else if (b.type === 'hr') parent.append(el('hr'));
    else if (b.type === 'quote') parent.append(blocks(el('blockquote'), b.blocks, ctx));
    else if (b.type === 'list') {
      const n = el(b.ordered ? 'ol' : 'ul', { start: b.ordered && b.start !== 1 ? b.start : null });
      for (const it of b.items) {
        const li = el('li', { class: it.check == null ? null : 'task-item' });
        if (it.check != null) li.append(el('input', { type: 'checkbox', checked: it.check, disabled: true, 'aria-label': it.check ? '已完成' : '未完成' }));
        // 只有一段文字的项不包 <p>，保持紧凑列表的行距。
        const [head, ...rest] = it.blocks;
        if (head?.type === 'para') { head.lines.forEach((l, i) => { if (i) li.append(el('br')); run(li, l); }); blocks(li, rest, ctx); }
        else blocks(li, it.blocks, ctx);
        n.append(li);
      }
      parent.append(n);
    } else if (b.type === 'table') {
      const cell = (tag, t, k) => run(el(tag, { 'data-align': b.align[k] }), t);
      // 宽表在窄屏上横向滚动，而不是把整页撑宽。
      parent.append(el('div', { class: 'table-wrap' }, el('table', {},
        el('thead', {}, el('tr', {}, ...b.head.map((t, k) => cell('th', t, k)))),
        el('tbody', {}, ...b.rows.map(r => el('tr', {}, ...r.map((t, k) => cell('td', t, k))))))));
    }
  }
  return parent;
}

export function markdown(text, opts = {}) {
  return blocks(el('div', { class: 'md' }), parseBlocks(clean(text)), { node: opts.node, last: null });
}

const pad = n => String(n).padStart(2, '0');
export function clock(ts) { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; }
export function stamp(ts) { const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()} ${clock(ts)}`; }
export function span(seconds) {
  const s = Math.abs(seconds);
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' 分钟';
  if (s < 86400 * 2) return Math.round(s / 3600) + ' 小时';
  return Math.round(s / 86400) + ' 天';
}

let toastTimer;
export function toast(message) {
  const t = $('#toast'); t.textContent = message; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 6000);
}

export const auth = {
  get token() { return localStorage.getItem('cthulhu-token') || ''; },
  set(value) { localStorage.setItem('cthulhu-token', value.trim()); },
};

export async function api(path, body, { resultError = false, signal } = {}) {
  const r = await fetch(new URL(path, document.baseURI), {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  let value = {};
  try { value = await r.json(); } catch { /* 非 JSON 响应 */ }
  // 执行结果本身含 error 字段（如取消/unknown原因），HTTP 200 的这类记录仍是有效响应。
  if (!r.ok || (!resultError && value.error)) { const e = Error(value.error || r.statusText); e.status = r.status; throw e; }
  return value;
}
