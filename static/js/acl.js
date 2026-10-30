// 飞书白名单页：左侧名单（可按人名、规则内容搜索），右侧选中者的规则全文。只读。
// 规则全文不在每秒轮询的快照里，打开页面或名单变化时才读 api/feishu/acl。
import { $, el, api, toast, stamp } from './dom.js';

// 规则里常见“主人授权来源：m_xxx / 飞书 om_xxx，日期；……”的前缀：拆出来单独显示，正文更好读。
const SOURCE = /^(主人授权(?:来源)?[：:]?\s*(?:m_[0-9a-f]+|[^；;，,]*?om_[0-9A-Za-z]+)[^；;]*?)[；;]\s*/;
export function splitRule(rule) {
  const m = rule.match(SOURCE);
  return m ? { source: m[1].trim(), text: rule.slice(m[0].length).trim() || rule } : { source: '', text: rule };
}

// 搜索：空格分词，每个词都要命中名字或任一规则。排序：名字完全相同 > 名字开头 > 名字包含 > 只在规则里。纯函数，便于测试。
export function filterAcl(list, query) {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return list;
  const hit = (a, w) => {
    const n = a.name.toLowerCase();
    return n === w ? 4 : n.startsWith(w) ? 3 : n.includes(w) ? 2 : a.rules.some(r => r.toLowerCase().includes(w)) ? 1 : 0;
  };
  return list.map(a => ({ a, s: words.map(w => hit(a, w)) }))
    .filter(x => x.s.every(Boolean))
    .sort((x, y) => Math.min(...y.s) - Math.min(...x.s) || x.a.name.localeCompare(y.a.name, 'zh'))
    .map(x => x.a);
}

// 规则完全相同的人归为一组：几十人共用同一套授权时，名单里标出“同组 N 人”，详情页列出同组成员。
export function groupOf(list) {
  const key = a => JSON.stringify(a.rules), size = new Map();
  for (const a of list) size.set(key(a), (size.get(key(a)) || 0) + 1);
  return a => size.get(key(a)) || 1;
}

const mark = (text, words) => {
  if (!words.length) return [text];
  const re = new RegExp('(' + words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  return text.split(re).map((part, i) => (i % 2 ? el('mark', { text: part }) : part));
};

let list = [], at = null, selected = null;

async function load() {
  try { list = await api('api/feishu/acl'); }
  catch (e) { toast(e.message); return; }
  render();
}

function render() {
  const q = $('#acl-q').value, words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const shown = filterAcl(list, q), same = groupOf(list);
  if (!shown.some(a => a.id === selected)) selected = shown[0]?.id ?? null;
  $('#acl-total').textContent = list.length;
  $('#acl-hint').textContent = q.trim() ? `${shown.length} / ${list.length} 人` : '';
  $('#acl-people').replaceChildren(...(shown.length ? shown.map(a => el('li', {
      role: 'option', tabindex: '0', class: 'acl-person', 'aria-selected': String(a.id === selected),
      onclick: () => { selected = a.id; render(); },
      onkeydown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selected = a.id; render(); } } },
    el('span', { class: 'acl-name' }, ...mark(a.name, words)),
    el('span', { class: 'acl-meta', text: `${a.rules.length} 条` + (same(a) > 1 ? ` · 同组 ${same(a)} 人` : '') })))
    : [el('li', { class: 'acl-none muted', text: list.length ? '没有匹配的人' : '暂无。对 Master 说「允许某人做什么」即可添加。' })]));
  const a = list.find(x => x.id === selected);
  if (!a) { $('#acl-detail').replaceChildren(); return; }
  const peers = list.filter(x => x.id !== a.id && JSON.stringify(x.rules) === JSON.stringify(a.rules));
  $('#acl-detail').replaceChildren(
    el('header', { class: 'acl-dh' }, el('h3', {}, ...mark(a.name, words)),
      el('span', { class: 'muted', text: [a.updated_at && '更新于 ' + stamp(a.updated_at), a.id].filter(Boolean).join(' · ') })),
    el('ol', { class: 'acl-rules' }, ...a.rules.map(r => {
      const { source, text } = splitRule(r);
      return el('li', {}, el('div', { class: 'acl-rule' }, ...mark(text, words)),
        source ? el('div', { class: 'acl-src' }, ...mark(source, words)) : null);
    })),
    peers.length ? el('div', { class: 'acl-peers' }, el('span', { class: 'muted', text: `规则相同的另外 ${peers.length} 人：` }),
      ...peers.map(p => el('button', { type: 'button', class: 'tag', text: p.name, onclick: () => { selected = p.id; render(); } }))) : null);
  $('#acl-people [aria-selected=true]')?.scrollIntoView({ block: 'nearest' });
}

export function initAcl() {
  const d = $('#acl-dialog');
  $('#acl-open').onclick = () => { d.showModal(); $('#acl-q').focus(); load(); };
  $('#acl-close').onclick = () => d.close();
  $('#acl-q').oninput = render;
  // 列表里上下键切换选中的人，搜索框里按下键直接进入列表。
  $('#acl-people').onkeydown = e => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    const items = [...$('#acl-people').querySelectorAll('.acl-person')], i = items.indexOf(document.activeElement);
    items[Math.max(0, Math.min(items.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))]?.focus();
  };
  $('#acl-q').onkeydown = e => { if (e.key === 'ArrowDown') { e.preventDefault(); $('#acl-people .acl-person')?.focus(); } };
}

// 侧栏入口：快照只带人数与最近修改时间；页面开着时名单有变化就重新读取。
export function renderAclEntry(f) {
  $('#acl-count').textContent = f.acl_count ? `${f.acl_count} 人` : '暂无';
  $('#acl-dot').dataset.state = f.state;
  $('#acl-open').title = f.state === 'running' ? '订阅中' : `订阅状态：${f.state}${f.error ? '，' + f.error : ''}`;
  if (at !== null && at !== f.acl_at && $('#acl-dialog').open) load();
  at = f.acl_at;
}
