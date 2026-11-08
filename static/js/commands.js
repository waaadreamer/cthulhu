// 节点详情命令行。输出只作文本；轮询元数据有变化时才拉全文，避免反复传输 1MiB 已截断内容。
import { $, el, api as request, clean, stamp } from './dom.js';
const api = (path, body) => request(path, body, { resultError: true });

const ACTIVE = new Set(['queued', 'dispatched', 'running', 'cancelling']);
const LABEL = { queued: '等待派发', dispatched: '已派发', running: '执行中', cancelling: '正在停止',
  completed: '已结束', failed: '启动失败', cancelled: '已停止', timed_out: '已超时', unknown: '结果未知，请核验已有副作用' };
const contexts = new Map();
let current = null, generation = 0, polling = false;
const form = () => $('#command-form');
const signature = x => JSON.stringify([x.state, x.output_bytes, x.truncated, x.started_at, x.finished_at]);

function error(message = '') {
  $('#command-error').textContent = message;
  $('#command-error').hidden = !message;
}

function buttons() {
  if (!current) return;
  const pending = Boolean(current.pending), active = ACTIVE.has(current.result?.state);
  $('#command-run').disabled = current.busy || (!pending && !current.available);
  $('#command-run').textContent = pending ? '确认本次提交' : active ? '另起命令' : '执行';
  $('#command-stop').disabled = current.busy || !active || current.result?.state === 'cancelling';
  // 提交回执丢失时保留同一请求号与原参数，不能让“重试”悄悄变成第二个有副作用的命令。
  for (const field of ['cwd', 'script', 'timeout']) form().elements[field].disabled = pending || current.busy;
}

function render(result) {
  current.id = result.id; current.result = result; current.signature = signature(result);
  $('#command-state').textContent = [result.id, LABEL[result.state],
    result.exit_code != null && `退出码 ${result.exit_code}`,
    result.started_at && `开始 ${stamp(result.started_at)}`,
    result.finished_at && `结束 ${stamp(result.finished_at)}`,
    result.truncated && '输出已截断（stdout + stderr 上限 1MiB，后续输出已丢弃）', result.error].filter(Boolean).join('；');
  for (const name of ['stdout', 'stderr']) {
    const box = $('#command-' + name), bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
    const value = clean(result[name] || '');
    if (box.textContent !== value) box.textContent = value;
    if (bottom) box.scrollTop = box.scrollHeight;
  }
  buttons();
}

async function refresh(force = false) {
  if (!current || !$('#node-dialog').open || polling) return;
  const context = current, gen = generation;
  polling = true;
  try {
    const list = await api('api/executions?node_id=' + encodeURIComponent(context.node) + '&limit=10');
    if (gen !== generation) return;
    if (!context.pending) error();
    const select = $('#command-history');
    select.replaceChildren(el('option', { value: '', text: '选择一条命令查看输出' }),
      ...list.map(x => el('option', { value: x.id, text: `${stamp(x.created_at)} · ${x.id} · ${LABEL[x.state]}` })));
    context.id ||= list[0]?.id || '';
    if (context.id && !list.some(x => x.id === context.id)) select.append(el('option', { value: context.id, text: context.id }));
    select.value = context.id;
    const meta = list.find(x => x.id === context.id);
    if (context.id && (force || !meta || context.signature !== signature(meta))) {
      const result = await api('api/executions?id=' + encodeURIComponent(context.id));
      if (gen === generation) render(result);
    }
  } catch (e) {
    if (gen === generation) error(e.message);
  } finally { polling = false; }
}

export function openCommands(node) {
  if (current) current.draft = { cwd: form().elements.cwd.value, script: form().elements.script.value, timeout: form().elements.timeout.value };
  generation++;
  current = contexts.get(node.id) || { node: node.id, id: '', result: null, busy: false, pending: null };
  contexts.set(node.id, current);
  current.available = node.online && node.features?.includes('commands-v1');
  const draft = current.draft || { cwd: node.defaults?.cwd || node.home || '/', script: '', timeout: '60' };
  for (const name of ['cwd', 'script', 'timeout']) form().elements[name].value = draft[name];
  $('#command-note').textContent = current.available ? '以节点用户运行 /bin/sh；关闭窗口后继续执行。停止仅作用于选中的命令。' : '节点离线或尚未升级命令执行能力。';
  $('#command-history').replaceChildren();
  $('#command-state').textContent = '';
  $('#command-stdout').textContent = ''; $('#command-stderr').textContent = '';
  error(); buttons();
  if (current.result) render(current.result);
  // openNode 随后才 showModal；下一次事件循环开始读取，避免把隐藏窗口误判成关闭。
  setTimeout(() => refresh(true), 0);
}

export function initCommands() {
  form().onsubmit = async event => {
    event.preventDefault();
    const context = current, gen = generation;
    if (!context || context.busy) return;
    context.pending ||= { node_id: context.node, cwd: form().elements.cwd.value,
      script: form().elements.script.value, timeout: Number(form().elements.timeout.value), request_id: 'ui:' + crypto.randomUUID() };
    context.busy = true; error(); buttons();
    try {
      const result = await api('api/executions', context.pending);
      context.pending = null; context.id = result.id; context.result = result;
      if (gen === generation) { render(result); await refresh(true); }
    } catch (e) {
      if (e.status) context.pending = null;
      if (gen === generation) error(e.message + (!e.status ? '；提交回执待确认，再次点击会沿用同一请求号。' : ''));
    } finally {
      context.busy = false;
      if (gen === generation) buttons();
    }
  };
  $('#command-stop').onclick = async () => {
    const context = current, gen = generation;
    if (!context?.id || context.busy) return;
    context.busy = true; error(); buttons();
    try {
      const result = await api('api/executions/cancel', { id: context.id });
      context.result = result;
      if (gen === generation) render(result);
    } catch (e) { if (gen === generation) error(e.message); }
    finally { context.busy = false; if (gen === generation) buttons(); }
  };
  $('#command-history').onchange = () => { current.id = $('#command-history').value; current.signature = null; refresh(true); };
  setInterval(() => refresh(), 1000);
}
