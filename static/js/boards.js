import { $, el, api, auth, toast } from './dom.js';
import { frameSnapshot, STATUS } from './board-data.js';

const cards = new Map();
let nodes = [], editing = null, loading = false;
const time = at => at == null ? '尚无' : new Date(at * 1000).toLocaleString();
const frameURL = new URL('../board-frame.html', import.meta.url).href;

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
  try { await api(`api/canvas/${id}/${action}`, { acknowledge_unknown }); await load(); }
  catch (error) { toast(error.message); }
}

function makeCard(board, data, previous = null) {
  const frame = el('iframe', { sandbox: 'allow-scripts', src: frameURL, title: board.name,
    referrerpolicy: 'no-referrer', class: 'board-frame' });
  const title = el('h2', { text: board.name }), badge = el('span', { class: 'board-status' });
  const meta = el('div', { class: 'board-meta muted' }), errors = el('pre', { class: 'board-error' });
  const pause = el('button', { class: 'btn ghost', type: 'button' });
  const edit = el('button', { class: 'btn ghost', text: '编辑', type: 'button', onclick: () => openEditor(board.id) });
  const close = el('button', { class: 'btn ghost danger', text: '关闭采集', type: 'button', onclick: () => {
    if (confirm('关闭这个 Canvas 的后续采集？正在展示的训练和评测会继续运行。已发出的本次采集由命令超时收尾，结果将丢弃。')) control(board.id, 'close');
  } });
  const box = el('section', { class: 'board-card', 'data-canvas-id': board.id },
    el('header', {}, title, badge), meta, frame,
    el('details', {}, el('summary', { text: '最近采集详情' }), errors),
    el('div', { class: 'board-actions' }, edit, pause, close,
      el('button', { class: 'btn ghost', type: 'button', text: '全屏', onclick: event => {
        const full = box.classList.toggle('fullscreen'); event.target.textContent = full ? '退出全屏' : '全屏';
      } })));
  const card = { box, frame, title, badge, meta, errors, pause, edit, close, template: board.html_template,
    revision: board.revision, ready: false, summary: board, data, stamp: JSON.stringify(board.data_stamp) };
  cards.set(board.id, card);
  if (previous) previous.box.replaceWith(box); else $('#boards').append(box);
  return card;
}

function updateCard(card, summary) {
  card.summary = summary;
  card.title.textContent = summary.name;
  card.badge.textContent = STATUS[summary.status] || summary.status;
  card.badge.classList.toggle('fresh', summary.fresh);
  card.meta.textContent = `${summary.node_id} · 最近成功采集：${time(summary.sampled_at)} · 间隔 ${summary.interval}s / 超时 ${summary.timeout}s`
    + (summary.stale && summary.sampled_at ? ' · 保留的数据已过期' : '')
    + (summary.task_ids.length ? ` · 关联：${summary.task_ids.join(', ')}` : ' · 独立面板');
  const attempt = card.data?.attempt;
  card.errors.textContent = attempt ? `状态：${attempt.state} · exit_code：${attempt.exit_code ?? '无'} · 采集回执：${time(attempt.collected_at)}`
    + (attempt.truncated ? '\n输出超过 1MiB 合计限制，已截断' : '') + (attempt.discarded ? '\n配置已变更或面板已关闭，本次结果已丢弃' : '')
    + `\n${attempt.error || ''}\nstderr:\n${attempt.stderr || ''}\nstdout:\n${attempt.stdout || ''}` : '尚无采集结果';
  card.pause.textContent = summary.state === 'active' ? '暂停' : '恢复';
  card.pause.onclick = () => {
    if (summary.blocked_unknown) {
      if (confirm('上次命令结果未知。请先在节点核验旧采集命令及其子进程已结束。确认已核验，并允许启动新的采集？')) control(summary.id, 'resume', true);
    } else control(summary.id, summary.state === 'active' ? 'pause' : 'resume');
  };
  for (const button of [card.pause, card.edit, card.close]) button.disabled = summary.state === 'closed';
  publish(card);
}

async function load() {
  if (loading || !auth.token) return;
  loading = true;
  try {
    const value = await api('api/canvas');
    nodes = value.nodes;
    for (const child of $('#boards').querySelectorAll('.board-empty')) child.remove();
    await Promise.all(value.canvases.map(async summary => {
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
    if (!value.canvases.length) $('#boards').append(el('p', { class: 'board-empty muted', text: '还没有 Canvas。新建独立面板，选择节点、脚本和模板即可；不需要已有任务。' }));
    $('#boards-status').textContent = `${value.canvases.length} 个面板 · 数据与配置保存在中心 · 关闭采集不会停止训练或评测`;
  } catch (error) {
    $('#boards-status').textContent = '无法读取中心：' + error.message + '。当前画面为缓存，不能视为实时数据。';
    // 网络中断时不能让 iframe 继续保留 fresh=true；即使模板只看数据面，也会收到离线更新。
    for (const card of cards.values()) {
      card.summary = { ...card.summary, status: 'collector_offline', fresh: false, collector_online: false };
      card.badge.textContent = '中心连接中断 · 缓存画面'; card.badge.classList.remove('fresh'); publish(card);
    }
    if (error.status === 401 && !$('#board-login').open) $('#board-login').showModal();
  } finally { loading = false; }
}

async function openEditor(id = null) {
  try {
    editing = id ? await api(`api/canvas/${id}`) : null;
    const form = $('#board-form'); form.reset(); $('#board-form-error').textContent = '';
    form.elements.node_id.replaceChildren(...nodes.map(n => el('option', { value: n.id, text: n.id + (n.online ? '' : '（离线）') })));
    if (editing) for (const key of ['name', 'node_id', 'cwd', 'interval', 'timeout', 'script', 'html_template', 'task_ids']) {
      form.elements[key].value = key === 'task_ids' ? editing[key].join(', ') : editing[key];
    }
    $('#board-editor-title').textContent = editing ? '编辑 Canvas' : '新建 Canvas';
    $('#board-editor').showModal();
  } catch (error) { toast(error.message); }
}

$('#board-new').onclick = () => openEditor();
$('#board-editor-close').onclick = () => $('#board-editor').close();
$('#board-form').onsubmit = async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  values.interval = Number(values.interval); values.timeout = Number(values.timeout);
  values.task_ids = values.task_ids.split(/[,，\s]+/).filter(Boolean);
  if (editing) values.revision = editing.revision;
  const submit = event.target.querySelector('[type=submit]'); submit.disabled = true;
  try {
    await api('api/canvas' + (editing ? '/' + editing.id : ''), values);
    $('#board-editor').close(); await load();
  } catch (error) { $('#board-form-error').textContent = error.message; }
  finally { submit.disabled = false; }
};

$('#board-example').onclick = async () => {
  try {
    const response = await fetch(new URL('../examples/canvas-summary.html', import.meta.url));
    if (!response.ok) throw Error('无法读取示例模板');
    const form = $('#board-form');
    form.elements.html_template.value = await response.text();
    form.elements.name.value = '双实验汇总（演示数据）'; form.elements.cwd.value = '/tmp';
    form.elements.script.value = "python3 - <<'PY'\nimport json, time\nprint(json.dumps({'demo': True, 'source': '内置演示脚本，无真实训练数据', 'observed_at': time.time(), 'experiments': [{'name': 'RoboTwin 示例 A', 'status': '演示，未运行'}, {'name': 'RoboTwin 示例 B', 'status': '演示，未运行'}]}, ensure_ascii=False))\nPY";
  } catch (error) { $('#board-form-error').textContent = error.message; }
};

$('#board-auth').onclick = () => $('#board-login').showModal();
$('#board-login-form').onsubmit = event => {
  event.preventDefault(); auth.set($('#board-token').value); $('#board-token').value = ''; $('#board-login').close(); load();
};
if (!auth.token) $('#board-login').showModal(); else load();
setInterval(load, 2000);
