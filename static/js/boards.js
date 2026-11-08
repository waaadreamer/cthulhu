import { $, el, api as request, auth, toast } from './dom.js';
import { frameSnapshot, STATUS } from './board-data.js';

const cards = new Map();
const pending = new Set();
let nodes = [], editing = null, loading = false, saving = false, editorEpoch = 0;
const time = at => at == null ? '尚无' : new Date(at * 1000).toLocaleString();
const frameURL = new URL('../board-frame.html', import.meta.url).href;
const setText = (target, value) => { if (target.textContent !== value) target.textContent = value; };
const fields = ['name', 'node_id', 'cwd', 'interval', 'timeout', 'script', 'html_template', 'task_ids'];
const draftKey = 'cthulhu-canvas-drafts:' + new URL('api/canvas', document.baseURI).pathname;
let drafts = {};
try { drafts = JSON.parse(sessionStorage.getItem(draftKey) || '{}'); } catch { /* 禁用存储时仍可编辑 */ }
if (!drafts || typeof drafts !== 'object' || Array.isArray(drafts)) drafts = {};
const editorKey = () => editing?.id || 'new';

async function api(path, body) {
  // 半断开的连接不能永久占住 loading，让缓存一直带着旧的 fresh 标志。
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try { return await request(path, body, { signal: controller.signal }); }
  catch (error) {
    if (error.name === 'AbortError') throw Error('中心请求超时，请检查连接后重试');
    throw error;
  } finally { clearTimeout(timer); }
}

function persistDrafts() {
  try { sessionStorage.setItem(draftKey, JSON.stringify(drafts)); return true; }
  catch { return false; }
}
function rememberDraft() {
  drafts[editorKey()] = { revision: editing?.revision, values: Object.fromEntries(new FormData($('#board-form'))) };
  $('#board-draft-status').textContent = persistDrafts()
    ? '草稿已暂存于当前标签页，关闭编辑器、刷新或切换页面后可继续。'
    : '浏览器无法暂存草稿，请在离开页面前保存或复制输入。';
}
function clearDraft() { delete drafts[editorKey()]; persistDrafts(); }

function publish(card) {
  if (card.ready) card.frame.contentWindow.postMessage({ type: 'cthulhu:canvas:data', snapshot: frameSnapshot(card.data, card.summary) }, '*');
}

// 只接收指定 iframe 的 ready，不处理模板提出的请求。模板没有命令通道，也得不到页面凭证。
addEventListener('message', event => {
  if (event.data?.type !== 'cthulhu:canvas:ready') return;
  const card = [...cards.values()].find(c => c.frame.contentWindow === event.source);
  if (!card || card.ready) return;
  card.ready = true;
  event.source.postMessage({ type: 'cthulhu:canvas:init', template: card.template, snapshot: frameSnapshot(card.data, card.summary) }, '*');
});

async function control(id, action, acknowledge_unknown = false) {
  if (pending.has(id)) return;
  const focused = document.activeElement;
  pending.add(id);
  const card = cards.get(id);
  if (card) updateCard(card, card.summary);
  try { await api(`api/canvas/${id}/${action}`, { acknowledge_unknown }); await load(); }
  catch (error) { toast(error.message); }
  finally {
    pending.delete(id); if (card) updateCard(card, card.summary);
    if (focused?.isConnected && !focused.disabled && document.activeElement === document.body) focused.focus({ preventScroll: true });
  }
}

function makeCard(board, data, previous = null) {
  const frame = el('iframe', { sandbox: 'allow-scripts', src: frameURL, title: board.name,
    referrerpolicy: 'no-referrer', class: 'board-frame' });
  if (previous) {
    // 配置版本变化不等于模板变化。保留卡片、键盘焦点、详情展开和全屏状态，避免轮询打断阅读。
    if (previous.template !== board.html_template) {
      previous.frame.replaceWith(frame); previous.frame = frame; previous.ready = false;
    }
    Object.assign(previous, { template: board.html_template, revision: board.revision, data,
      stamp: JSON.stringify(board.data_stamp) });
    return previous;
  }
  const title = el('h2', { id: 'board-title-' + board.id, text: board.name });
  const badge = el('span', { class: 'board-status', role: 'status' });
  const meta = el('div', { class: 'board-meta muted' }), errors = el('pre', { class: 'board-error' });
  const pause = el('button', { class: 'btn ghost', type: 'button' });
  const edit = el('button', { class: 'btn ghost', text: '编辑', type: 'button', onclick: () => openEditor(board.id) });
  const close = el('button', { class: 'btn ghost danger', text: '关闭采集', type: 'button', onclick: () => {
    if (confirm('关闭这个 Canvas 的后续采集？正在展示的训练和评测会继续运行。已发出的本次采集由命令超时收尾，结果将丢弃。')) control(board.id, 'close');
  } });
  const box = el('section', { class: 'board-card', 'data-canvas-id': board.id, 'aria-labelledby': title.id },
    el('header', {}, title, badge), meta, frame,
    el('details', {}, el('summary', { text: '最近采集详情' }), errors),
    el('div', { class: 'board-actions' }, edit, pause, close,
      el('button', { class: 'btn ghost', type: 'button', text: '全屏', 'aria-pressed': 'false', onclick: event => {
        const full = !box.classList.contains('fullscreen'); exitFullscreen();
        box.classList.toggle('fullscreen', full); event.target.textContent = full ? '退出全屏' : '全屏';
        event.target.setAttribute('aria-pressed', String(full));
      } })));
  const card = { box, frame, title, badge, meta, errors, pause, edit, close, template: board.html_template,
    revision: board.revision, ready: false, summary: board, data, stamp: JSON.stringify(board.data_stamp) };
  cards.set(board.id, card);
  return card;
}

function exitFullscreen() {
  for (const card of cards.values()) if (card.box.classList.contains('fullscreen')) {
    card.box.classList.remove('fullscreen');
    const button = card.box.querySelector('[aria-pressed]'); button.textContent = '全屏'; button.setAttribute('aria-pressed', 'false');
  }
}
addEventListener('keydown', event => { if (event.key === 'Escape' && !document.querySelector('dialog[open]')) exitFullscreen(); });

function updateCard(card, summary) {
  card.summary = summary;
  const snapshot = frameSnapshot(card.data, summary);
  card.title.textContent = summary.name;
  card.frame.title = summary.name;
  setText(card.badge, STATUS[snapshot.status] || snapshot.status);
  card.badge.classList.toggle('fresh', snapshot.fresh);
  card.meta.textContent = `${summary.node_id} · 最近成功采集：${time(snapshot.sampled_at)} · 间隔 ${summary.interval}s / 超时 ${summary.timeout}s`
    + (snapshot.stale && snapshot.sampled_at ? ' · 保留的数据已过期' : '')
    + (summary.task_ids.length ? ` · 关联：${summary.task_ids.join(', ')}` : ' · 独立面板');
  const attempt = card.data?.attempt;
  card.errors.dataset.failed = String(Boolean(attempt && (attempt.state !== 'completed' || attempt.exit_code !== 0 || attempt.truncated)));
  card.errors.textContent = attempt ? `状态：${attempt.state} · exit_code：${attempt.exit_code ?? '无'} · 采集回执：${time(attempt.collected_at)}`
    + (attempt.truncated ? '\n输出超过 1MiB 合计限制，已截断' : '') + (attempt.discarded ? '\n配置已变更或面板已关闭，本次结果已丢弃' : '')
    + `\n${attempt.error || ''}\nstderr:\n${attempt.stderr || ''}\nstdout:\n${attempt.stdout || ''}` : '尚无采集结果';
  card.pause.textContent = summary.state === 'active' ? '暂停' : '恢复';
  card.pause.onclick = () => {
    if (summary.blocked_unknown) {
      if (confirm('上次命令结果未知。请先在节点核验旧采集命令及其子进程已结束。确认已核验，并允许启动新的采集？')) control(summary.id, 'resume', true);
    } else control(summary.id, summary.state === 'active' ? 'pause' : 'resume');
  };
  for (const button of [card.pause, card.edit, card.close]) button.disabled = summary.state === 'closed' || pending.has(summary.id);
  publish(card);
}

async function load() {
  if (loading || !auth.token) return;
  loading = true;
  try {
    const value = await api('api/canvas');
    nodes = value.nodes;
    $('#board-new').disabled = !nodes.length;
    for (const child of $('#boards').querySelectorAll('.board-empty')) child.remove();
    const results = await Promise.allSettled(value.canvases.map(async summary => {
      let card = cards.get(summary.id);
      if (!card || card.revision !== summary.revision) {
        const [board, data] = await Promise.all([api(`api/canvas/${summary.id}`), api(`api/canvas/${summary.id}/data`)]);
        card = makeCard(board, data, card);
      } else if (card.stamp !== JSON.stringify(summary.data_stamp)) {
        card.data = await api(`api/canvas/${summary.id}/data`);
        card.stamp = JSON.stringify(summary.data_stamp);
      }
      updateCard(card, summary);
    }));
    // 等所有读取结束后再统一报离线，避免一个失败后另一个迟到成功又把缓存标为 fresh。
    const failed = results.find(result => result.status === 'rejected');
    if (failed) throw failed.reason;
    for (const [index, summary] of value.canvases.entries()) {
      const box = cards.get(summary.id).box;
      // 首次详情并发加载的响应顺序不稳定，按中心列表排列；不移动已就位 iframe，以免重新加载。
      if ($('#boards').children[index] !== box) $('#boards').insertBefore(box, $('#boards').children[index] || null);
    }
    if (!value.canvases.length) $('#boards').append(el('p', { class: 'board-empty muted', text: '还没有 Canvas。新建独立面板，选择节点、脚本和模板即可；不需要已有任务。' }));
    setText($('#boards-status'), `${value.canvases.length} 个面板 · 数据与配置保存在中心 · 关闭采集不会停止训练或评测`);
    $('#board-login-error').textContent = '';
  } catch (error) {
    setText($('#boards-status'), '无法读取中心：' + error.message + '。当前画面为缓存，不能视为实时数据。');
    // 网络中断时不能让 iframe 继续保留 fresh=true；即使模板只看数据面，也会收到离线更新。
    for (const card of cards.values()) {
      card.summary = { ...card.summary, status: 'collector_offline', fresh: false, collector_online: false };
      setText(card.badge, '中心连接中断 · 缓存画面'); card.badge.classList.remove('fresh'); publish(card);
    }
    if ($('#board-login').open || error.status === 401) {
      $('#board-login-error').textContent = error.status === 401 ? '凭证无效，请检查中心 web-token 后重试。' : error.message;
      if (!$('#board-login').open) $('#board-login').showModal();
    }
  } finally { loading = false; }
}

async function openEditor(id = null) {
  const epoch = ++editorEpoch;
  try {
    const board = id ? await api(`api/canvas/${id}`) : null;
    if (epoch !== editorEpoch) return;
    editing = board;
    const form = $('#board-form'); form.reset(); $('#board-form-error').textContent = '';
    for (const field of fields) form.elements[field].setCustomValidity('');
    form.elements.node_id.replaceChildren(...nodes.map(n => el('option', { value: n.id, text: n.id + (n.online ? '' : '（离线）') })));
    if (editing) for (const key of fields) {
      form.elements[key].value = key === 'task_ids' ? editing[key].join(', ') : editing[key];
    }
    const draft = drafts[editorKey()];
    if (draft?.values) {
      for (const key of fields) if (key in draft.values) form.elements[key].value = draft.values[key];
      // 恢复编辑草稿必须保留原 revision；否则刷新页面会悄悄覆盖另一个窗口的新配置。
      if (editing) editing.revision = draft.revision;
    }
    $('#board-draft-status').textContent = draft ? '已恢复当前标签页的草稿，尚未保存到中心。'
      : '输入会在当前标签页暂存；保存或丢弃后清除草稿。';
    $('#board-editor-title').textContent = editing ? '编辑 Canvas' : '新建 Canvas';
    $('#board-editor').showModal();
  } catch (error) { toast(error.message); }
}

$('#board-new').onclick = () => openEditor();
$('#board-editor-close').onclick = () => $('#board-editor').close();
$('#board-editor').oncancel = event => { if (saving) event.preventDefault(); };
$('#board-editor').onclose = () => { if (!$('#board-editor').open) editorEpoch++; };
$('#board-form').oninput = event => {
  event.target.setCustomValidity?.(''); $('#board-form-error').textContent = ''; rememberDraft();
};
$('#board-draft-discard').onclick = () => {
  if (drafts[editorKey()] && !confirm('丢弃此面板尚未保存的草稿？')) return;
  clearDraft(); $('#board-editor').close();
};
$('#board-form').onsubmit = async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  values.interval = Number(values.interval); values.timeout = Number(values.timeout);
  values.task_ids = values.task_ids.split(/[,，\s]+/).filter(Boolean);
  if (editing) values.revision = editing.revision;
  for (const [key, label, limit] of [['name', '名称', 200], ['cwd', '工作目录', 4096], ['script', '采集脚本', 64000], ['html_template', 'HTML 模板', 256 * 1024]]) {
    let message = !values[key].trim() ? `${label}不能为空。` : new TextEncoder().encode(values[key]).length > limit ? `${label}最多 ${limit} UTF-8 字节，请缩短后重试。` : '';
    if (key === 'cwd' && !values[key].startsWith('/')) message = '工作目录必须是执行节点上的绝对路径，以 / 开头。';
    if (message) {
      event.target.elements[key].setCustomValidity(message); event.target.elements[key].reportValidity();
      $('#board-form-error').textContent = message; return;
    }
  }
  const submit = event.target.querySelector('[type=submit]');
  // 保存期间冻结这一份输入和关闭动作，防止迟到响应清掉下一份草稿或覆盖刚输入的修改。
  saving = true; event.target.setAttribute('aria-busy', 'true'); submit.textContent = '正在保存…';
  for (const field of event.target.elements) field.disabled = true;
  try {
    await api('api/canvas' + (editing ? '/' + editing.id : ''), values);
    clearDraft();
    $('#board-editor').close(); load();
  } catch (error) {
    $('#board-form-error').textContent = error.message + (error.message.includes('已被修改') ? '。输入已保留；请复制所需内容，丢弃草稿后重新打开最新配置。' : '');
    $('#board-form-error').scrollIntoView({ block: 'nearest' });
  }
  finally {
    saving = false; event.target.removeAttribute('aria-busy'); submit.textContent = '保存';
    for (const field of event.target.elements) field.disabled = false;
  }
};

$('#board-example').onclick = async () => {
  const form = $('#board-form');
  if ((form.elements.script.value || form.elements.html_template.value) && !confirm('用双实验演示替换当前名称、工作目录、脚本和模板？')) return;
  const epoch = editorEpoch;
  try {
    const response = await fetch(new URL('../examples/canvas-summary.html', import.meta.url));
    if (!response.ok) throw Error('无法读取示例模板');
    const template = await response.text();
    if (epoch !== editorEpoch || !$('#board-editor').open || saving) return;
    form.elements.html_template.value = template;
    form.elements.name.value = '双实验汇总（演示数据）'; form.elements.cwd.value = '/tmp';
    form.elements.script.value = "python3 - <<'PY'\nimport json, time\nprint(json.dumps({'demo': True, 'source': '内置演示脚本，无真实训练数据', 'observed_at': time.time(), 'experiments': [{'name': 'RoboTwin 示例 A', 'status': '演示，未运行'}, {'name': 'RoboTwin 示例 B', 'status': '演示，未运行'}]}, ensure_ascii=False))\nPY";
    for (const field of fields) form.elements[field].setCustomValidity('');
    $('#board-form-error').textContent = '';
    rememberDraft();
  } catch (error) { $('#board-form-error').textContent = error.message; }
};

$('#board-auth').onclick = () => $('#board-login').showModal();
$('#board-login-form').onsubmit = async event => {
  event.preventDefault(); auth.set($('#board-token').value); $('#board-token').value = '';
  const submit = event.target.querySelector('button'); submit.disabled = true; $('#board-login-error').textContent = '正在连接中心…';
  // 先校验凭证再关弹窗，错误信息必须留在可见、可访问的对话框里。
  try { await api('api/canvas'); $('#board-login').close(); await load(); }
  catch (error) { $('#board-login-error').textContent = error.status === 401 ? '凭证无效，请检查中心 web-token 后重试。' : error.message; }
  finally { submit.disabled = false; }
};
if (!auth.token) $('#board-login').showModal(); else load();
setInterval(() => { if (!$('#board-login').open) load(); }, 2000);
