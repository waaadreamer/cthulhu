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

function inline(parent, text) {
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\)|https?:\/\/[^\s)<>]+)/g;
  let pos = 0;
  for (const m of text.matchAll(re)) {
    parent.append(text.slice(pos, m.index));
    const s = m[0];
    if (s[0] === '`') parent.append(el('code', { text: s.slice(1, -1) }));
    else if (s.startsWith('**')) parent.append(el('strong', { text: s.slice(2, -2) }));
    else {
      const p = s.match(/^\[([^\]]+)\]\((.+)\)$/) || [s, s, s];
      // 只允许 http(s) 链接，避免 javascript: 等协议。
      if (/^https?:\/\//i.test(p[2])) parent.append(el('a', { href: p[2], target: '_blank', rel: 'noopener noreferrer', text: p[1] }));
      else parent.append(el('span', { text: `${p[1]} (${p[2]})` }));
    }
    pos = m.index + s.length;
  }
  parent.append(text.slice(pos));
}

export function markdown(text) {
  const root = el('div', { class: 'md' });
  let code = null, list = null, para = null;
  for (const line of clean(text).split('\n')) {
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
      const li = el('li'); inline(li, item[2]); list.append(li); para = null; continue;
    }
    list = null;
    const h = line.match(/^(#{1,4})\s+(.+)/);
    if (h) { const n = el('h' + Math.min(6, h[1].length + 2)); inline(n, h[2]); root.append(n); para = null; continue; }
    const quote = line.match(/^>\s?(.*)/);
    if (quote) { const q = el('blockquote'); inline(q, quote[1]); root.append(q); para = null; continue; }
    if (para) para.append(el('br')); else { para = el('p'); root.append(para); }
    inline(para, line);
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
