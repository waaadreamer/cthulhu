import { $, el, api as request, auth, toast } from './dom.js';
import { frameSnapshot, STATUS } from './board-data.js';

const cards = new Map();
const pending = new Set();
const reads = new Map(), navItems = new Map();
let summaries = [], selectedId = '', selectionEpoch = 0, reloadQueued = false;
const selectionKey = 'cthulhu-canvas-selected:' + new URL('api/canvas', document.baseURI).pathname;
try { selectedId = new URLSearchParams(location.hash.slice(1)).get('canvas') || localStorage.getItem(selectionKey) || ''; } catch { /* 存储不可用仍可从列表选择 */ }
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
    el('header', {}, title, badge, el('div', { class: 'board-tools' },
      el('details', { class: 'board-management' }, el('summary', { class: 'btn ghost', text: '管理', 'aria-label': '管理 Canvas' }),
        el('div', { class: 'board-menu' }, el('div', { class: 'board-actions' }, edit, pause, close),
          el('details', { class: 'board-receipt' }, el('summary', { text: '最近采集详情' }), errors))),
      el('button', { class: 'btn ghost', type: 'button', text: '全屏', 'aria-pressed': 'false', onclick: event => {
        const full = !box.classList.contains('fullscreen'); exitFullscreen();
        box.classList.toggle('fullscreen', full); event.target.textContent = full ? '退出全屏' : '全屏';
        event.target.setAttribute('aria-pressed', String(full));
      } }))), meta, frame);
  title.tabIndex = -1;
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
addEventListener('keydown', event => {
  if (event.key !== 'Escape' || document.querySelector('dialog[open]')) return;
  exitFullscreen(); closeMenus(); sidebar(false);
});
function closeMenus() { for (const menu of document.querySelectorAll('.board-management[open]')) menu.open = false; }
addEventListener('click', event => { if (!event.target.closest('.board-management')) closeMenus(); });

const narrow = matchMedia('(max-width: 760px)');
function sidebar(open, focus = true) {
  const visible = narrow.matches && open;
  document.body.classList.toggle('boards-list-open', visible);
  $('#boards-mask').hidden = !visible;
  $('#boards-sidebar').inert = narrow.matches && !visible;
  $('#boards-main').inert = visible;
  $('#boards-list-toggle').setAttribute('aria-expanded', String(visible));
  if (visible && focus) $('#boards-list-close').focus();
  else if (narrow.matches && focus) $('#boards-list-toggle').focus();
}
$('#boards-list-toggle').onclick = () => sidebar(!document.body.classList.contains('boards-list-open'));
$('#boards-list-close').onclick = $('#boards-mask').onclick = () => sidebar(false);
narrow.addEventListener('change', () => sidebar(false, false));
sidebar(false, false);
addEventListener('keydown', event => {
  if (event.key !== 'Tab' || !document.body.classList.contains('boards-list-open')) return;
  const stops = [...$('#boards-sidebar').querySelectorAll('a,button:not(:disabled),summary')].filter(e => e.getClientRects().length);
  if (event.shiftKey && document.activeElement === stops[0]) { event.preventDefault(); stops.at(-1).focus(); }
  else if (!event.shiftKey && document.activeElement === stops.at(-1)) { event.preventDefault(); stops[0].focus(); }
});

function rememberSelection(id, push = false) {
  try { localStorage.setItem(selectionKey, id); } catch { /* URL 仍然记录当前选择 */ }
  const url = new URL(location.href); url.hash = new URLSearchParams({ canvas: id }).toString();
  if (url.href !== location.href) history[push ? 'pushState' : 'replaceState'](null, '', url);
}

function renderNavigation() {
  let closed = $('#boards-list .board-nav-closed');
  if (!closed) {
    closed = el('details', { class: 'board-nav-closed' }, el('summary', { text: '已关闭' }));
    $('#boards-list').append(closed);
  }
  for (const summary of summaries) {
    let item = navItems.get(summary.id);
    if (!item) {
      item = el('a', { class: 'board-nav-item', href: '#canvas=' + encodeURIComponent(summary.id), 'data-canvas-id': summary.id,
        onclick: event => { event.preventDefault(); select(summary.id, true); } },
        el('span', { class: 'board-nav-name' }), el('span', { class: 'board-nav-meta' }));
      navItems.set(summary.id, item);
    }
    setText(item.querySelector('.board-nav-name'), summary.name);
    const meta = item.querySelector('.board-nav-meta');
    setText(meta, `${summary.node_id} · ${STATUS[summary.status] || summary.status}`);
    meta.dataset.fresh = String(summary.fresh);
    const wasSelected = item.getAttribute('aria-current') === 'page', previousParent = item.parentElement;
    if (summary.id === selectedId) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    const parent = summary.state === 'closed' ? closed : $('#boards-list');
    if (item.parentElement !== parent) parent === closed ? parent.append(item) : parent.insertBefore(item, closed);
    if (summary.state === 'closed' && summary.id === selectedId && (!wasSelected || previousParent !== closed)) closed.open = true;
  }
  for (const [id, item] of navItems) if (!summaries.some(b => b.id === id)) { item.remove(); navItems.delete(id); }
  closed.hidden = !summaries.some(b => b.state === 'closed');
}

function showCard(card) {
  for (const other of cards.values()) other.box.hidden = other !== card;
  if (!card.box.isConnected) $('#boards').append(card.box);
  $('#boards-stage-state').hidden = true;
  $('#boards').removeAttribute('aria-busy');
}

async function readCard(summary) {
  const key = JSON.stringify([summary.id, summary.revision, summary.data_stamp]);
  if (reads.has(key)) return reads.get(key);
  const work = (async () => {
    let card = cards.get(summary.id);
    if (!card || card.revision !== summary.revision) {
      const [board, data] = await Promise.all([api(`api/canvas/${summary.id}`), api(`api/canvas/${summary.id}/data`)]);
      card = makeCard(board, data, card);
    } else if (card.stamp !== JSON.stringify(summary.data_stamp)) {
      card.data = await api(`api/canvas/${summary.id}/data`); card.stamp = JSON.stringify(summary.data_stamp);
    }
    updateCard(card, summary);
    return card;
  })();
  reads.set(key, work);
  try { return await work; } finally { reads.delete(key); }
}

async function displaySelected(focus = false) {
  const summary = summaries.find(b => b.id === selectedId), epoch = selectionEpoch;
  if (!summary) return;
  try {
    const card = await readCard(summary);
    // 切换是纯展示行为。迟到的 A 请求只能更新 A 的缓存，不能把用户已选的 B 再切回去。
    if (epoch !== selectionEpoch || selectedId !== summary.id) return;
    showCard(card);
    if (focus) card.title.focus({ preventScroll: true });
  } catch (error) {
    if (epoch !== selectionEpoch) return;
    const cached = cards.get(summary.id);
    if (cached?.box.isConnected) showCard(cached);
    else { $('#boards-stage-state').hidden = false; setText($('#boards-stage-state'), '无法打开 Canvas：' + error.message + '。可重试或从左侧选择其他面板。'); }
    throw error;
  }
}

function select(id, push = false) {
  if (!summaries.some(b => b.id === id)) return;
  const changed = selectedId !== id;
  selectedId = id; selectionEpoch++; rememberSelection(id, push);
  if (changed) {
    exitFullscreen(); closeMenus();
    for (const card of cards.values()) card.box.hidden = true;
    $('#boards-stage-state').hidden = false; setText($('#boards-stage-state'), '正在打开 Canvas…');
    $('#boards').setAttribute('aria-busy', 'true');
  }
  renderNavigation(); sidebar(false, false);
  displaySelected(push).catch(error => { markOffline(error); });
}
addEventListener('hashchange', () => {
  const id = new URLSearchParams(location.hash.slice(1)).get('canvas');
  select(summaries.some(b => b.id === id) ? id : summaries.find(b => b.state !== 'closed')?.id || summaries[0]?.id);
});

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

function markOffline(error) {
  setText($('#boards-status'), '无法读取中心：' + error.message + '。当前画面为缓存，不能视为实时数据。');
  // 网络中断时同时撤销当前模板和列表中的 fresh，不能让缓存继续看起来像实时数据。
  summaries = summaries.map(b => ({ ...b, fresh: false, collector_online: false,
    status: b.state === 'active' ? 'collector_offline' : b.status }));
  renderNavigation();
  for (const card of cards.values()) {
    card.summary = { ...card.summary, status: 'collector_offline', fresh: false, collector_online: false };
    setText(card.badge, '中心连接中断 · 缓存画面'); card.badge.classList.remove('fresh'); publish(card);
  }
  $('#boards').removeAttribute('aria-busy');
  if (![...cards.values()].some(c => !c.box.hidden && c.box.isConnected)) {
    $('#boards-stage-state').hidden = false; setText($('#boards-stage-state'), '无法连接中心。请检查连接，或通过左侧访问凭证重新登录。');
  }
  if ($('#board-login').open || error.status === 401) {
    $('#board-login-error').textContent = error.status === 401 ? '凭证无效，请检查中心 web-token 后重试。' : error.message;
    if (!$('#board-login').open) $('#board-login').showModal();
  }
}

async function load() {
  if (!auth.token) return;
  if (loading) { reloadQueued = true; return; }
  loading = true;
  try {
    const value = await api('api/canvas');
    nodes = value.nodes; summaries = value.canvases;
    $('#board-new').disabled = !nodes.length;
    const chosen = summaries.find(b => b.id === selectedId)?.id
      || summaries.find(b => b.state !== 'closed')?.id || summaries[0]?.id;
    if (chosen && chosen !== selectedId) { selectedId = chosen; selectionEpoch++; }
    if (chosen) rememberSelection(chosen);
    renderNavigation();
    setText($('#boards-status'), `${summaries.filter(b => b.state !== 'closed').length} 个面板 · ${summaries.filter(b => b.state === 'closed').length} 个已关闭`);
    $('#board-login-error').textContent = '';
    if (chosen) await displaySelected();
    else {
      for (const card of cards.values()) card.box.hidden = true;
      $('#boards-stage-state').hidden = false;
      setText($('#boards-stage-state'), '还没有 Canvas。点击左侧 ＋ 新建面板，选择节点、脚本和模板即可；不需要已有任务。');
    }
  } catch (error) { markOffline(error); }
  finally { loading = false; if (reloadQueued) { reloadQueued = false; load(); } }
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
    sidebar(false, false); closeMenus();
    $('#board-editor').showModal();
  } catch (error) { toast(error.message); }
}

$('#board-new').onclick = () => openEditor();
$('#board-editor-close').onclick = () => $('#board-editor').close();
$('#board-editor').oncancel = event => { if (saving) event.preventDefault(); };
$('#board-editor').onclose = () => {
  if ($('#board-editor').open) return;
  editorEpoch++;
  const card = cards.get(selectedId);
  if (narrow.matches) (card && !card.box.hidden ? card.title : $('#boards-list-toggle')).focus({ preventScroll: true });
  else if (card) card.box.querySelector('.board-management > summary').focus({ preventScroll: true });
};
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
    const saved = await api('api/canvas' + (editing ? '/' + editing.id : ''), values);
    if (!editing) {
      selectedId = saved.id; selectionEpoch++; rememberSelection(saved.id, true);
      // 创建成功后立即撤下旧面板，不能让新选择的管理操作暂时落到之前的 Canvas 上。
      for (const card of cards.values()) card.box.hidden = true;
      $('#boards-stage-state').hidden = false; setText($('#boards-stage-state'), '正在打开新 Canvas…');
    }
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
