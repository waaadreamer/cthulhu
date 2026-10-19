// DOM 小工具、安全的 Markdown 渲染与 API 访问。全程不使用 innerHTML：命令输出里的 HTML 只会显示为文本。
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
const INLINE = new RegExp([
  '`[^`\\n]+`', '\\*\\*[^*\\n]+\\*\\*', '\\[[^\\]\\n]+\\]\\([^\\s)]+\\)', 'https?:\\/\\/[^\\s)<>]+',
  `(?<![A-Za-z0-9_-])${REF}`, BEFORE + PATH].map(x => `(${x})`).join('|'), 'g');
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
    } else if (s.startsWith('**')) out.push({ type: 'bold', text: s.slice(2, -2) });
    else if (/^https?:\/\//i.test(s)) out.push({ type: 'url', href: s, text: s });
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
    else if (g.type === 'url') parent.append(el('a', { href: g.href, target: '_blank', rel: 'noopener noreferrer', text: g.text }));
    else parent.append(fileLink(g.node, g.path, g.label, g.code));
  }
}

function inline(parent, text, ctx = {}) { put(parent, scan(text, ctx), ctx); }

// 命令输出等纯文本：只识别文件链接，不做 Markdown。
export function linkPaths(parent, text, ctx = {}) { put(parent, scan(text, ctx, FILES), ctx); return parent; }

// 单行富文本（事件行等）。
export function rich(text, ctx = {}) { const n = el('span'); inline(n, text, ctx); return n; }

export function markdown(text, opts = {}) {
  const root = el('div', { class: 'md' });
  const ctx = { node: opts.node, last: null };
  let mention = null;
  let code = null, list = null, para = null;
  for (const line of clean(text).split('\n')) {
    ctx.last = mention || ctx.last; mention = lastMention(line);
    const fence = line.match(/^\s*```(\S*)/);
    if (fence) {
      if (code) { root.append(code); code = null; }
      else { code = el('pre', { class: 'code', 'data-lang': fence[1] || null }); }
      list = para = null; continue;
    }
    if (code) { code.textContent += (code.textContent ? '\n' : '') + line; continue; }
    if (!line.trim()) { list = para = null; continue; }
    const item = line.match(/^\s*([-*]|\d+[.)])\s+(.*)/);
    if (item) {
      const ordered = /\d/.test(item[1]);
      if (!list || list.ordered !== ordered) { list = el(ordered ? 'ol' : 'ul'); list.ordered = ordered; root.append(list); }
      const li = el('li'); inline(li, item[2], ctx); list.append(li); para = null; continue;
    }
    list = null;
    const h = line.match(/^(#{1,4})\s+(.+)/);
    if (h) { const n = el('h' + Math.min(6, h[1].length + 2)); inline(n, h[2], ctx); root.append(n); para = null; continue; }
    const quote = line.match(/^>\s?(.*)/);
    if (quote) { const q = el('blockquote'); inline(q, quote[1], ctx); root.append(q); para = null; continue; }
    if (para) para.append(el('br')); else { para = el('p'); root.append(para); }
    inline(para, line, ctx);
  }
  if (code) root.append(code);
  return root;
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

export async function api(path, body) {
  const r = await fetch(new URL(path, document.baseURI), {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let value = {};
  try { value = await r.json(); } catch { /* 非 JSON 响应 */ }
  if (!r.ok || value.error) { const e = Error(value.error || r.statusText); e.status = r.status; throw e; }
  return value;
}
