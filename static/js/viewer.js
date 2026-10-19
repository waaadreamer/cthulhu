// 文件查看器：打开 `节点::路径`。内容由节点读取、经中心转发与缓存；只做展示，不能修改文件。
import { $, el, api, markdown, stamp, fileHref, fileCtx } from './dom.js';

const TEXT_LIMIT = 400000;  // 超长文本只渲染前面部分，避免一次塞进几 MB 的 DOM
const IMAGE = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp', svg: 'image/svg+xml' };
const human = n => n == null ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const ext = p => (p.match(/\.([A-Za-z0-9]+)$/)?.[1] || '').toLowerCase();
const parent = p => p.replace(/\/[^/]*\/?$/, '') || '/';
const join = (dir, name) => (dir.endsWith('/') ? dir : dir + '/') + name;

let current = null, raw = false, returnHash = () => '';

function bytes(b64) { const s = atob(b64), a = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); return a; }

function decode(u8) {
  // 含 NUL 视为二进制；否则按 UTF-8 解码，非法字节以替换符显示而不是报错。
  if (u8.subarray(0, 8192).includes(0)) return null;
  return new TextDecoder('utf-8', { fatal: false }).decode(u8);
}

function crumbs(node, path) {
  const box = el('nav', { class: 'crumbs', 'aria-label': '路径' }, el('span', { class: 'crumb-node', text: node }));
  const parts = path.split('/').filter(Boolean);
  parts.forEach((part, i) => {
    const p = '/' + parts.slice(0, i + 1).join('/');
    box.append(el('span', { class: 'sep', text: '/' }),
      i === parts.length - 1 ? el('span', { class: 'here', text: part }) : el('a', { href: fileHref(node + '::' + p), text: part }));
  });
  return box;
}

function body(r) {
  if (r.kind === 'dir') {
    const list = el('ul', { class: 'dir' });
    if (r.path !== '/') list.append(el('li', {}, el('a', { href: fileHref(r.node + '::' + parent(r.path)), text: '..' })));
    for (const e of r.entries) list.append(el('li', { 'data-dir': String(e.dir) },
      el('a', { href: fileHref(r.node + '::' + join(r.path, e.name)), text: e.name + (e.dir ? '/' : '') }),
      el('span', { class: 'muted', text: e.dir ? '' : human(e.size) }),
      el('time', { text: e.mtime ? stamp(e.mtime) : '' })));
    if (r.truncated) list.append(el('li', { class: 'muted', text: `只列出前 ${r.entries.length} 项，共 ${r.total} 项` }));
    return list;
  }
  const u8 = bytes(r.content || ''), type = IMAGE[ext(r.path)];
  if (type) {
    // 图片走 data: URL：CSP 只放行 img-src 'self' data:，blob: 会被拦截；SVG 作为 <img> 加载不会执行脚本。
    return el('div', { class: 'img-box' }, el('img', { class: 'img', src: `data:${type};base64,${r.content}`, alt: r.path }));
  }
  const text = decode(u8);
  if (text == null) return el('p', { class: 'muted pad', text: `二进制文件（${human(r.size)}），无法预览。` });
  const shown = text.length > TEXT_LIMIT ? text.slice(0, TEXT_LIMIT) : text;
  const note = r.truncated || text.length > TEXT_LIMIT ? el('p', { class: 'muted pad', text: `文件 ${human(r.size)}，只显示前面部分。` }) : null;
  const isMd = ['md', 'markdown'].includes(ext(r.path));
  if (isMd && !raw) return el('div', {}, note, el('article', { class: 'doc' }, markdown(shown, { node: r.node })));
  const lines = shown.split('\n'), pre = el('pre', { class: 'code-view' });
  lines.forEach((l, i) => pre.append(el('span', { class: 'ln', 'data-n': i + 1 }, l + '\n')));
  return el('div', {}, note, pre);
}

async function load(ref, refresh = false) {
  const box = $('#viewer-body'), m = ref.match(/^([A-Za-z0-9_-]{0,64})::(\/.*)$/);
  current = ref; raw = false;
  $('#viewer-dialog').open || $('#viewer-dialog').showModal();
  if (!m) { box.replaceChildren(el('p', { class: 'bad pad', text: '链接格式应为 节点::绝对路径' })); return; }
  const [, node, path] = m;
  $('#viewer-crumbs').replaceChildren(crumbs(node || '?', path));
  $('#viewer-meta').textContent = ''; $('#viewer-raw').hidden = true;
  if (!node) {
    // 没标明节点：让主人选，选完写回固定写法。
    box.replaceChildren(el('div', { class: 'pick pad' }, el('p', { text: '这个路径没有标明在哪个节点上：' }),
      el('div', { class: 'pick-row' }, ...fileCtx.nodes.map(n => el('a', { class: 'btn', href: fileHref(n + '::' + path), text: n })))));
    return;
  }
  box.replaceChildren(el('p', { class: 'muted pad loading', text: '正在从节点 ' + node + ' 读取' }));
  try {
    const r = await api('api/file?ref=' + encodeURIComponent(ref) + (refresh ? '&refresh=1' : ''));
    if (current !== ref) return;
    const render = () => box.replaceChildren(body(r));
    render();
    $('#viewer-meta').replaceChildren(
      r.kind === 'file' ? el('span', { text: human(r.size) }) : el('span', { text: `${r.total} 项` }),
      r.mtime ? el('span', { text: '修改于 ' + stamp(r.mtime) }) : '',
      r.stale ? el('span', { class: 'warn', text: r.stale }) : r.cached ? el('span', { class: 'muted', text: '来自中心缓存' }) : '');
    const md = r.kind === 'file' && ['md', 'markdown'].includes(ext(r.path));
    $('#viewer-raw').hidden = !md; $('#viewer-raw').textContent = '查看源码';
    $('#viewer-raw').onclick = () => { raw = !raw; $('#viewer-raw').textContent = raw ? '查看渲染' : '查看源码'; render(); };
  } catch (e) {
    if (current === ref) box.replaceChildren(el('p', { class: 'bad pad', text: e.message }));
  }
}

export function openFile(ref) { load(ref); }

// returnHash：关闭查看器时应回到的地址片段（例如当前打开的任务）。
// 直接在地址栏输入 #file= 时 history.state 里没有来处，只能问页面当前状态。
export function initViewer(opts = {}) {
  if (opts.returnHash) returnHash = opts.returnHash;
  const d = $('#viewer-dialog');
  // 关闭时去掉地址里的 #file=，回到打开前的地址（通常是 #task=）。dialog 的 close 事件是异步的，
  // 关闭按钮直接同步还原；Esc 等其他关闭方式由 close 事件兜底，重复执行无害。
  const restore = () => {
    current = null;
    if (location.hash.startsWith('#file=')) history.replaceState(null, '', history.state?.back || location.pathname + location.search + returnHash());
  };
  $('#viewer-close').onclick = () => { restore(); d.close(); };
  $('#viewer-refresh').onclick = () => current && load(current, true);
  $('#viewer-copy').onclick = async () => {
    try { await navigator.clipboard.writeText(current); $('#viewer-copy').textContent = '已复制'; } catch { /* 无剪贴板权限 */ }
    setTimeout(() => ($('#viewer-copy').textContent = '复制链接'), 1500);
  };
  d.addEventListener('close', restore);
  // 普通点击就地打开；中键/Ctrl 点击仍走浏览器默认，在新标签打开同一个固定链接。
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#file="]');
    if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    const back = location.hash.startsWith('#file=') ? history.state?.back : location.pathname + location.search + location.hash;
    history.replaceState({ back }, '', a.getAttribute('href'));
    load(decodeURIComponent(a.getAttribute('href').slice(6)));
  });
}

export function fileFromHash() {
  const m = location.hash.match(/^#file=(.+)$/);
  if (m) load(decodeURIComponent(m[1]));
  return !!m;
}
