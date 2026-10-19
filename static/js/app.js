// 页面主控：轮询状态与事件，左栏任务/节点，中间 Master 会话，右侧抽屉展示某个 old_one 的完整过程。
import { $, el, api, auth, toast, markdown, stamp, span } from './dom.js';
import { Model } from './model.js';
import { View, Live } from './render.js';
import { Chat } from './chat.js';

const STATE = { running: '进行中', waiting: '等待', paused: '已暂停', completed: '已完成', cancelled: '已取消' };
const DONE = new Set(['completed', 'cancelled']);
let snap = null, chatCursor = 0, polling = false, drawer = null, priority = 9, sentAt = null;

const chat = new Chat($('#chat'), { openTask: id => openTask(id), taskTitle: id => snap?.tasks.find(t => t.id === id)?.title });

// ---- 登录 ----
function showLogin(message) {
  $('#login').hidden = false; $('#app').inert = true;
  if (message) $('#login-error').textContent = message;
  $('#login-token').focus();
}
$('#login-form').onsubmit = e => {
  e.preventDefault(); auth.set($('#login-token').value);
  $('#login').hidden = true; $('#app').inert = false; $('#login-error').textContent = ''; poll();
};

// ---- 轮询 ----
async function poll() {
  if (polling || !auth.token) return;
  polling = true;
  try {
    snap = await api('api/state');
    chat.demo = snap.agents.find(a => a.id === 'master')?.backend === 'demo';
    renderSide(); renderQuestions(); renderMaster();
    const stick = nearBottom($('#chat'));
    for (let more = true; more;) {
      const { events } = await api(`api/events?view=conversation&after=${chatCursor}&limit=300`);
      for (const e of events) { chat.apply(e); chatCursor = e.seq; }
      more = events.length === 300;
    }
    chat.flush();
    // 刚发送、中心还没登记时也立即给出反馈，避免“发了没反应”。
    const info = liveInfo('master', chat.running()) || (sentAt && Date.now() - sentAt < 5000 ? { phase: 'wait', text: '已发送' } : null);
    if (snap.pending?.master) sentAt = null;
    chat.live.set(info);
    if (!$('#chat').children.length) $('#chat').append(el('div', { class: 'empty' }, el('h2', { text: '从一个目标开始' }),
      el('p', { text: '告诉 Master 你想完成什么。它会拆成任务，交给星系上的独立执行者，过程都能在任务里查看。' })));
    if (stick) scrollEnd($('#chat'));
    if (drawer) await drawer.poll();
    $('#conn').dataset.ok = 'true'; $('#conn').title = '已连接';
  } catch (e) {
    $('#conn').dataset.ok = 'false'; $('#conn').title = e.message;
    if (e.status === 401) showLogin('凭证无效，请重新填写');
  } finally { polling = false; }
}
const nearBottom = n => n.scrollHeight - n.scrollTop - n.clientHeight < 120;
const scrollEnd = n => (n.scrollTop = n.scrollHeight);

// ---- 工作中状态 ----
// 中心的 pending（排队数、租约开始时间）决定“是否在跑”；原生事件只决定“在做什么”。
function liveInfo(aid, model) {
  const a = snap.agents.find(a => a.id === aid);
  if (!a || a.paused) return null;
  const p = snap.pending?.[aid];
  if (p?.since) {
    const act = model?.activity() || { kind: 'think', text: '思考中' };
    return { phase: p.stopping ? 'stopping' : act.kind, text: p.stopping ? '正在停止当前回合' : act.text,
             code: act.code, elapsed: Math.max(0, Math.round(snap.now - p.since)) };
  }
  if (p?.queued) {
    const node = snap.nodes.find(n => n.id === (a.node || 'local'));
    return node && !node.online ? { phase: 'wait', text: `排队中，节点 ${node.id} 离线` } : { phase: 'wait', text: '已收到，正在启动' };
  }
  return null;
}
const isRunning = aid => !!snap.pending?.[aid]?.since;

// ---- 左栏 ----
function renderSide() {
  const list = $('#tasks'); list.replaceChildren();
  const tasks = [...snap.tasks].sort((a, b) => DONE.has(a.state) - DONE.has(b.state) || b.created_at - a.created_at);
  $('#task-count').textContent = tasks.filter(t => !DONE.has(t.state)).length || '';
  if (!tasks.length) list.append(el('p', { class: 'muted pad', text: '还没有任务' }));
  for (const t of tasks) {
    const q = snap.questions.some(q => q.task === t.id && ['master', 'owner'].includes(q.state));
    const overdue = !DONE.has(t.state) && t.deadline < snap.now;
    list.append(el('button', { type: 'button', class: 'task' + (drawer?.task === t.id ? ' on' : ''), 'data-state': t.state,
      'aria-pressed': String(drawer?.task === t.id), onclick: () => openTask(t.id) },
      el('span', { class: 'dot' + (isRunning(t.agent) ? ' busy' : ''), 'aria-hidden': 'true' }),
      el('span', { class: 'task-main' }, el('span', { class: 'task-t', text: t.title }),
        el('span', { class: 'task-m' }, el('span', { text: STATE[t.state] || t.state }), el('span', { text: t.node }),
          q ? el('span', { class: 'ask', text: '有提问' }) : null,
          overdue ? el('span', { class: 'bad', text: '超期' }) : null)),
      el('span', { class: 'chev', 'aria-hidden': 'true', text: '›' })));
  }
  const nodes = $('#nodes'); nodes.replaceChildren();
  // 固定顺序：local 在前，其余按名字排序；心跳只更新圆点状态，不改变位置。
  const ordered = [...snap.nodes].sort((a, b) => (b.id === 'local') - (a.id === 'local') || a.id.localeCompare(b.id));
  for (const n of ordered) {
    const d = n.defaults || {};
    const hint = [d.backend && [d.backend, d.model].filter(Boolean).join(' '), d.cwd].filter(Boolean).join('，');
    nodes.append(el('button', { type: 'button', class: 'node', 'data-online': String(n.online),
      title: `${n.host || ''}${hint ? '\n默认：' + hint : '\n未设置默认值'}`, onclick: () => openNode(n.id) },
      el('span', { class: 'dot', 'aria-hidden': 'true' }), el('span', { class: 'node-n', text: n.id === 'local' ? 'local（中心）' : n.id }),
      el('span', { class: 'muted', text: n.online ? (n.backends || []).filter(b => b !== 'demo').join(' ') || 'demo' : '离线' })));
  }
}

function renderMaster() {
  const a = snap.agents.find(a => a.id === 'master');
  $('#model').textContent = modelLabel(a);
  $('#model').onclick = () => openModel('master');
  $('#master-state').textContent = a.paused ? '已暂停' : a.status === 'running' ? '工作中' : '就绪';
  $('#master-state').dataset.state = a.paused ? 'paused' : a.status;
  $('#master-toggle').textContent = a.paused ? '恢复' : '暂停';
  const decides = !!snap.prefs?.master_decides, sw = $('#decides');
  sw.setAttribute('aria-checked', String(decides));
  sw.title = decides ? 'Master 会直接回答执行者的问题，不再转给你；你仍可事后修正' : '需要时 Master 会把问题转给你决定';
  sw.onclick = () => act(() => api('api/settings', { master_decides: !decides }));
  $('#master-toggle').onclick = () => act(() => api('api/master', { action: a.paused ? 'resume' : 'pause' }));
}

// ---- 待回答问题：显示在输入框上方 ----
// 每个问题一张卡片，原地更新：轮询每秒一次，整块重建会替换正在输入的文本框，导致失焦、草稿丢失。
const qCards = new Map();
function renderQuestions() {
  const box = $('#questions');
  const open = snap.questions.filter(q => ['master', 'owner'].includes(q.state));
  const ids = new Set(open.map(q => q.id));
  for (const [id, card] of qCards) if (!ids.has(id)) { card.root.remove(); qCards.delete(id); }
  for (const q of open) {
    let card = qCards.get(q.id);
    if (!card) {
      card = { head: el('div', { class: 'q-h' }), body: el('div'), extra: el('span'), key: '' };
      card.input = el('textarea', { rows: 1, 'aria-label': '回答' });
      // 提交时读取最新版本：输入期间问题可能已超时交回 Master，版本号随之变化。
      const send = () => {
        const cur = snap.questions.find(x => x.id === q.id), text = card.input.value.trim();
        if (!cur || !text) return;
        act(async () => { await api('api/answer', { question_id: cur.id, version: cur.version, answer: text }); card.input.value = ''; });
      };
      card.input.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } };
      card.root = el('div', { class: 'q' }, card.head, card.body,
        el('div', { class: 'q-a' }, card.input, el('button', { class: 'btn primary', type: 'button', text: '回答', onclick: send }), card.extra));
      qCards.set(q.id, card);
      box.append(card.root);
    }
    // 只有状态或版本变化才重建标题与正文；文本框始终是同一个元素。
    const key = q.state + ':' + q.version;
    if (card.key !== key) {
      card.key = key;
      const t = snap.tasks.find(t => t.id === q.task);
      card.root.dataset.state = q.state;
      card.countdown = q.deadline ? el('span', { class: 'muted' }) : null;
      card.head.replaceChildren(el('span', { class: 'tag ' + (q.state === 'owner' ? 'warn' : ''), text: q.state === 'owner' ? '需要你决定' : 'Master 处理中' }),
        el('button', { class: 'link', type: 'button', text: t?.title || q.agent, onclick: () => openTask(q.task) }), card.countdown);
      card.body.replaceChildren(markdown(q.text));
      card.input.placeholder = q.recommendation ? '建议：' + q.recommendation : '你的回答';
      card.extra.replaceChildren(
        q.state === 'owner' ? el('button', { class: 'btn ghost', type: 'button', text: '交给 Master', title: '不等超时，立即让 Master 决定',
          onclick: () => act(() => api('api/question/delegate', { question_id: q.id })) }) : '',
        chat.demo && q.state === 'master' ? el('button', { class: 'btn ghost', type: 'button', text: '模拟：转给我',
          onclick: () => act(() => api('api/demo/escalate', { question_id: q.id, version: q.version, recommendation: q.recommendation })) }) : '');
    }
    if (card.countdown) card.countdown.textContent = span(q.deadline - snap.now) + '后交回 Master';
  }
}

async function act(fn) { try { await fn(); await poll(); } catch (e) { toast(e.message); } }

// ---- 抽屉：某个 old_one 的完整过程 ----
class Drawer {
  constructor(taskId) {
    this.task = taskId; this.cursor = 0;
    const t = snap.tasks.find(t => t.id === taskId);
    this.agent = t.agent;
    const backend = snap.agents.find(a => a.id === t.agent)?.backend;
    this.model = new Model(t.agent, 'drawer', { harnessTools: backend === 'demo' });
    $('#timeline').replaceChildren();
    this.view = new View($('#timeline'), this.model);
    this.live = new Live($('#timeline'));
    $('#drawer').hidden = false; document.body.classList.add('drawer-open');
    history.replaceState(null, '', '#task=' + taskId);
  }
  head() {
    const t = snap.tasks.find(t => t.id === this.task); if (!t) return;
    const a = snap.agents.find(a => a.id === t.agent) || {};
    $('#d-title').textContent = t.title;
    $('#d-state').textContent = STATE[t.state] || t.state; $('#d-state').dataset.state = t.state;
    $('#d-meta').replaceChildren(el('span', { text: t.node }),
      DONE.has(t.state) ? el('span', { text: modelLabel(a) }) : el('button', { type: 'button', class: 'model-btn', text: modelLabel(a), title: '切换模型', onclick: () => openModel(a.id) }),
      el('span', { class: t.deadline < snap.now && !DONE.has(t.state) ? 'bad' : '', text: '截止 ' + stamp(t.deadline) }),
      el('span', { class: 'mono', title: '工作目录：原生 CLI 从这里读取 AGENTS.md / CLAUDE.md', text: a.cwd }),
      a.session ? el('span', { class: 'mono', title: '原生会话 ' + a.session, text: a.session.slice(0, 8) }) : null);
    const ctl = $('#d-controls'); ctl.replaceChildren();
    if (!DONE.has(t.state)) for (const action of t.state === 'paused' ? ['resume', 'cancel'] : ['pause', 'cancel'])
      ctl.append(el('button', { type: 'button', class: 'btn' + (action === 'cancel' ? ' danger' : ''), text: { pause: '暂停', resume: '恢复', cancel: '取消' }[action],
        onclick: () => (action !== 'cancel' || confirm('取消任务？已有文件与外部作业不会回滚。')) &&
          act(() => api('api/control', { task_id: t.id, action, reason: '主人通过页面操作' })) }));
    $('#d-form').hidden = DONE.has(t.state);
  }
  async poll() {
    this.head();
    const tl = $('#timeline'), stick = nearBottom(tl);
    for (let more = true; more && drawer === this;) {
      const { events } = await api(`api/events?agent=${encodeURIComponent(this.agent)}&after=${this.cursor}&limit=500`);
      for (const e of events) { this.model.apply(e); this.cursor = e.seq; }
      more = events.length === 500;
    }
    if (drawer !== this) return;
    this.view.update();
    tl.querySelector(':scope > p.muted')?.remove();
    this.live.set(liveInfo(this.agent, this.model));
    if (!tl.children.length) tl.append(el('p', { class: 'muted pad', text: '还没有活动' }));
    if (stick) scrollEnd(tl);
  }
}

function openTask(id) {
  if (!snap?.tasks.some(t => t.id === id)) return;
  if (drawer?.task !== id) drawer = new Drawer(id);
  renderSide(); drawer.poll().catch(e => toast(e.message));
  document.body.classList.remove('nav-open');
}
function closeDrawer() {
  drawer = null; $('#drawer').hidden = true; document.body.classList.remove('drawer-open');
  history.replaceState(null, '', location.pathname + location.search); renderSide();
}
$('#d-close').onclick = closeDrawer;
$('#d-form').onsubmit = e => {
  e.preventDefault(); const input = $('#d-input'); const text = input.value.trim(); if (!text || !drawer) return;
  act(async () => { await api('api/chat', { to: drawer.agent, text, priority: $('#d-urgent').checked ? 10 : 9 }); input.value = ''; $('#d-urgent').checked = false; });
};

// 抽屉宽度可拖动，记住偏好。
{
  const handle = $('#d-resize'), saved = +localStorage.getItem('cthulhu-drawer');
  if (saved) document.documentElement.style.setProperty('--drawer', saved + 'px');
  handle.onpointerdown = e => {
    handle.setPointerCapture(e.pointerId);
    const move = ev => { const w = Math.min(Math.max(innerWidth - ev.clientX, 360), innerWidth * .75);
      document.documentElement.style.setProperty('--drawer', w + 'px'); localStorage.setItem('cthulhu-drawer', Math.round(w)); };
    handle.onpointermove = move; handle.onpointerup = () => (handle.onpointermove = null);
  };
}

// ---- 输入框 ----
const input = $('#input');
function setPriority(p) {
  priority = p; $('#composer').dataset.urgent = String(p === 10);
  for (const b of document.querySelectorAll('[data-priority]')) b.setAttribute('aria-pressed', String(+b.dataset.priority === p));
  input.placeholder = p === 10 ? '加急：立即打断 Master 当前回合' : '给 Master 发消息…（Enter 发送，Shift+Enter 换行）';
}
for (const b of document.querySelectorAll('[data-priority]')) b.onclick = () => setPriority(+b.dataset.priority);
const grow = n => { n.style.height = 'auto'; n.style.height = Math.min(n.scrollHeight, 240) + 'px'; };
input.oninput = () => grow(input);
input.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit(); } };
$('#composer').onsubmit = e => {
  e.preventDefault(); const text = input.value.trim(); if (!text) return;
  sentAt = Date.now(); chat.live.set({ phase: 'wait', text: '已发送' });
  act(async () => { await api('api/chat', { text, priority }); input.value = ''; grow(input); setPriority(9); scrollEnd($('#chat')); });
};

// ---- 模型 ----
const MODELS = { codex: ['gpt-6-astra', 'gpt-6.1-sol'], claude: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5'], demo: [] };
function modelLabel(a) {
  return [a.model || (a.backend === 'demo' ? '模拟' : '默认模型'), a.backend === 'codex' && a.reasoning_effort ? a.reasoning_effort : null,
          a.backend !== 'demo' ? a.backend : null].filter(Boolean).join('  ');
}
function openModel(aid) {
  const a = snap.agents.find(a => a.id === aid), f = $('#model-form');
  const node = snap.nodes.find(n => n.id === (a.node || 'local'));
  f.dataset.agent = aid;
  $('#model-who').textContent = aid === 'master' ? 'Master' : (snap.tasks.find(t => t.agent === aid)?.title || aid);
  f.elements.backend.replaceChildren(...['codex', 'claude', 'demo'].filter(b => !node?.backends?.length || node.backends.includes(b))
    .map(b => el('option', { value: b, text: b })));
  f.elements.backend.value = a.backend; f.elements.model.value = a.model || ''; f.elements.reasoning_effort.value = a.reasoning_effort || '';
  syncModelForm(a);
  dialog('#model-dialog');
}
function syncModelForm(a = snap.agents.find(x => x.id === $('#model-form').dataset.agent)) {
  const f = $('#model-form'), b = f.elements.backend.value;
  $('#model-list').replaceChildren(...MODELS[b].map(m => el('option', { value: m })));
  f.elements.reasoning_effort.closest('label').hidden = b !== 'codex';
  const fresh = b !== a.backend;
  $('#model-new').hidden = !fresh; f.elements.new_session.required = fresh && !!a.session;
  $('#model-note').textContent = fresh ? '换后端会开始新的原生会话，原会话的历史不会带过去。'
    : a.session ? '沿用当前原生会话，下一回合起生效。' : '下一回合起生效。';
}
$('#model-form').elements.backend.onchange = () => syncModelForm();
$('#model-form').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  act(async () => {
    await api('api/agent', { agent: f.dataset.agent, backend: f.elements.backend.value, model: f.elements.model.value,
      reasoning_effort: f.elements.reasoning_effort.value || null, new_session: f.elements.new_session.checked });
    $('#model-dialog').close(); f.elements.new_session.checked = false;
  });
};

// ---- 节点默认值 ----
function openNode(id) {
  const n = snap.nodes.find(n => n.id === id), f = $('#node-form'), d = n.defaults || {};
  f.dataset.node = id;
  $('#node-dialog-t').textContent = '节点 ' + id;
  $('#node-info').textContent = [n.online ? '在线' : '离线', n.host, n.home && '用户目录 ' + n.home,
    (n.backends || []).length && '可用后端 ' + n.backends.join(' ')].filter(Boolean).join('；');
  f.elements.backend.replaceChildren(el('option', { value: '', text: '同 Master' }),
    ...['codex', 'claude', 'demo'].filter(b => !n.backends?.length || n.backends.includes(b)).map(b => el('option', { value: b, text: b })));
  f.elements.backend.value = d.backend || ''; f.elements.model.value = d.model || '';
  f.elements.reasoning_effort.value = d.reasoning_effort || ''; f.elements.cwd.value = d.cwd || '';
  f.elements.cwd.placeholder = (n.home || '/home/user') + '/project';
  syncNodeForm(); dialog('#node-dialog');
}
function syncNodeForm() {
  const f = $('#node-form'), b = f.elements.backend.value;
  $('#node-models').replaceChildren(...(MODELS[b] || []).map(m => el('option', { value: m })));
  f.elements.model.disabled = !b; f.elements.reasoning_effort.closest('label').hidden = b !== 'codex';
}
$('#node-form').elements.backend.onchange = syncNodeForm;
$('#node-form').onsubmit = e => {
  e.preventDefault(); const f = e.target;
  act(async () => {
    await api('api/node', { node: f.dataset.node, backend: f.elements.backend.value, model: f.elements.model.value,
      reasoning_effort: f.elements.reasoning_effort.value, cwd: f.elements.cwd.value });
    $('#node-dialog').close();
  });
};

// ---- 对话框 ----
function dialog(id) { const d = $(id); d.showModal(); d.querySelector('input,textarea,select')?.focus(); return d; }
for (const d of document.querySelectorAll('dialog')) d.querySelector('[data-close]')?.addEventListener('click', () => d.close());

$('#new-task').onclick = () => {
  const f = $('#task-form'), select = f.elements.node;
  select.replaceChildren(...snap.nodes.map(n => el('option', { value: n.id, text: n.id + (n.online ? '' : '（离线）') })));
  select.value = snap.nodes.find(n => n.online && n.id !== 'local')?.id || 'local';
  const sync = () => {
    const d = snap.nodes.find(n => n.id === select.value)?.defaults || {};
    f.elements.cwd.placeholder = d.cwd ? '留空使用节点默认：' + d.cwd : '/home/user/project';
    f.elements.cwd.required = !d.cwd;
    f.elements.backend.options[0].textContent = d.backend ? `节点默认（${[d.backend, d.model].filter(Boolean).join(' ')}）` : '同 Master';
  };
  select.onchange = sync; sync();
  const d = new Date(Date.now() + 86400000); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
  f.elements.deadline.value ||= d.toISOString().slice(0, 16);
  f.elements.backend.value = '';
  dialog('#task-dialog');
};
$('#task-form').onsubmit = e => {
  e.preventDefault(); const f = e.target, v = Object.fromEntries(new FormData(f));
  const body = { title: v.title, description: v.description, acceptance: v.acceptance, cwd: v.cwd || undefined, node: v.node,
    deadline: new Date(v.deadline).toISOString() };
  if (v.backend) body.backend = v.backend;
  act(async () => { const t = await api('api/task', body); $('#task-dialog').close(); f.reset(); await poll(); openTask(t.id); });
};

$('#add-node').onclick = () => { $('#invite-out').hidden = true; $('#invite-form').reset(); dialog('#invite-dialog'); };
$('#invite-form').onsubmit = e => {
  e.preventDefault();
  act(async () => {
    const r = await api('api/invite', { name: e.target.elements.name.value.trim() });
    $('#invite-cmd').textContent = r.command; $('#invite-out').hidden = false;
    $('#invite-exp').textContent = '有效期至 ' + stamp(r.expires) + '，只能用于一台机器。命令含一次性邀请，请按密码对待。';
  });
};
$('#invite-copy').onclick = async () => {
  try { await navigator.clipboard.writeText($('#invite-cmd').textContent); $('#invite-copy').textContent = '已复制'; }
  catch { getSelection().selectAllChildren($('#invite-cmd')); }
  setTimeout(() => ($('#invite-copy').textContent = '复制'), 1500);
};

$('#nav-toggle').onclick = () => document.body.classList.toggle('nav-open');
addEventListener('keydown', e => { if (e.key === 'Escape' && drawer && !document.querySelector('dialog[open]')) closeDrawer(); });

// ---- 启动 ----
// #task=<id> 可直达某个任务的过程；在已打开的页面里改 hash 也生效。
const fromHash = () => { const id = location.hash.match(/task=([\w-]+)/)?.[1]; if (id) openTask(id); };
addEventListener('hashchange', fromHash);
setPriority(9);
if (!auth.token) showLogin(); else poll().then(fromHash);
setInterval(poll, 1000);
