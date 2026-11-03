// 对话历史页：主人与 Master 的往来（含飞书、定期审核），新的在前，可按关键词搜索、逐页往前翻。
// 主聊天里混着任务动态和工作过程，消息一多就难找；这里只列对话本身，按需从中心读取，不进每秒轮询。
import { $, el, api, toast, markdown, stamp } from './dom.js';
import { canvas } from './canvas.js';

const PAGE = 50;
const LABEL = { chat: 'Master', review: 'Master 定期审核', reply: 'Master · 侧栏', feishu_in: '飞书来信', feishu_out: 'Master → 飞书', message: '你' };

// 纯函数，便于测试：谁说的。
export function speaker(e) {
  if (e.kind === 'feishu_in') return e.data?.owner ? '你 · 飞书' : (e.data?.name || '飞书用户');
  return LABEL[e.kind] || e.actor;
}

let before = null, token = 0;

async function load(reset) {
  const q = $('#history-q').value.trim(), mine = ++token;
  if (reset) { before = null; $('#history-list').replaceChildren(); }
  $('#history-hint').textContent = '加载中';
  try {
    const r = await api('api/history?' + new URLSearchParams({ q, limit: PAGE, aside: $('#history-aside').checked ? '1' : '0', ...(before ? { before } : {}) }));
    if (mine !== token) return; // 输入变了，旧请求的结果丢掉
    for (const e of r.events) {
      before = e.seq;
      $('#history-list').append(el('li', { class: 'h-item ' + e.kind },
        el('div', { class: 'h-head' }, el('strong', { text: speaker(e) }), el('time', { text: stamp(e.at) }),
          el('span', { class: 'muted mono', text: '#' + e.seq })),
        el('div', { class: 'h-body' }, e.data?.format === 'html' ? canvas(e.text) : markdown(e.text))));
    }
    $('#history-more').hidden = !r.more;
    const n = $('#history-list').children.length;
    $('#history-hint').textContent = n ? `${n} 条${r.more ? '，可继续往前翻' : ''}` : (q ? '没有匹配的对话' : '还没有对话');
  } catch (e) { if (mine === token) { $('#history-hint').textContent = ''; toast(e.message); } }
}

export function initHistory() {
  const dlg = $('#history-dialog');
  let timer = null;
  $('#history-open').onclick = () => { dlg.showModal(); $('#history-q').focus(); load(true); };
  $('#history-close').onclick = () => dlg.close();
  $('#history-q').oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(true), 300); };
  $('#history-aside').onchange = () => load(true);
  $('#history-more').onclick = () => load(false);
}
