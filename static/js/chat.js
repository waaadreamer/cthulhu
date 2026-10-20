// 主会话：主人消息、Master 回复、公开汇报与任务动态。Master 每个回合的工作过程折叠成一组。
import { el, markdown, clock, rich } from './dom.js';
import { Model } from './model.js';
import { View, Live, render, who, elapsed } from './render.js';

const LINES = {
  task_created: '新任务', instruction: '已下达', completed: '审核通过', review_requested: '申请结束', task_failed: '未达成', review_returned: '退回', task_reopened: '重新打开', question: '提问', ask_owner: '需要你决定',
  answer: '回答', question_timeout: '提问超时', control: '控制', overdue: '超出期限', node_attention: '节点异常',
  interrupted: '已打断', urgent: '加急', runtime_error: '运行失败', master_attention: 'Master 已暂停',
  master_config: '配置', master_control: 'Master', node_joined: '节点', memory: '偏好', deadline: 'DDL 调整',
  recovery: '恢复', recovery_exhausted: '续办已暂停', orphan_process: '旧进程',
  question_delegated: '交给 Master', deploy: '更新', deploy_failed: '更新失败', feishu_in: '飞书', feishu_error: '飞书异常', node_update: '节点更新', node_updated: '节点更新', node_update_failed: '更新失败', settings: '设置', agent_config: '模型', node_config: '节点',
};
const LEVEL = { deploy_failed: 'error', task_failed: 'error', review_returned: 'warn', review_requested: 'ask', completed: 'ok', node_joined: 'ok', node_attention: 'error', runtime_error: 'error', master_attention: 'error',
  orphan_process: 'error', feishu_error: 'error', node_update_failed: 'error', node_updated: 'ok', recovery_exhausted: 'error', overdue: 'warn', interrupted: 'warn', urgent: 'warn', question_timeout: 'warn',
  question: 'ask', ask_owner: 'ask' };

class Process {
  constructor(chat, e) {
    this.model = new Model('master', 'process', { harnessTools: chat.demo });
    this.start = e.at; this.done = false; this.touched = false;
    this.body = el('div', { class: 'proc-body' });
    this.view = new View(this.body, this.model);
    this.title = el('span', { class: 'proc-t' });
    this.root = el('details', { class: 'process', open: true, ontoggle: () => (this.touched ||= this.settled !== undefined) },
      el('summary', {}, el('span', { class: 'spin', 'aria-hidden': 'true' }), this.title), this.body);
    chat.root.append(this.root);
  }
  finish(e) {
    this.done = true; this.end = e.at;
    // 结束后自动收起（除非用户自己展开/收起过），主会话只保留结论。
    if (!this.touched) this.root.open = false;
    this.settled = true;
  }
  update() {
    this.view.update();
    const n = this.model.steps();
    this.root.dataset.state = this.done ? 'done' : 'running';
    if (this.done && n === 0) { this.root.remove(); return; }
    const took = this.done && this.end ? `，用时 ${elapsed(Math.max(1, Math.round(this.end - this.start)))}` : '';
    this.title.textContent = this.done ? `工作过程：${n} 步${took}` : `工作过程${n ? `：${n} 步` : ''}`;
  }
}

export class Chat {
  constructor(root, opts) {
    const { openTask, taskTitle, taskNode } = opts;
    Object.assign(this, { root, opts, openTask, taskTitle, taskNode: taskNode || (() => null) });
    this.proc = null; this.dirty = new Set(); this.demo = false;
    this.live = new Live(root);
  }

  current(e) {
    if (!this.proc || this.proc.done) { this.proc = new Process(this, e); }
    this.dirty.add(this.proc);
    return this.proc;
  }

  apply(e) {
    this.root.querySelector('.empty')?.remove();
    const d = e.data || {};
    if (e.kind === 'dispatch' && e.target === 'master') { this.proc && !this.proc.done && this.proc.finish(e); this.proc = null; this.current(e); return; }
    if (e.actor === 'master' && e.kind.startsWith('native:')) { this.current(e).model.apply(e); return; }
    if (e.actor === 'master' && e.kind === 'harness_tool') { if (this.demo) this.current(e).model.apply(e); return; }
    if (e.actor === 'master' && e.kind === 'remote_result') { if (this.proc) { this.proc.finish(e); this.dirty.add(this.proc); } return; }
    if (e.kind === 'message') {
      if (e.actor === 'owner' && e.target === 'master' && !d.copy) this.root.append(el('div', { class: 'me' }, el('div', { class: 'me-b' }, markdown(e.text)),
        el('div', { class: 'me-m' }, d.priority >= 10 ? el('span', { class: 'tag warn', text: '加急' }) : null, el('time', { text: clock(e.at) }))));
      return;
    }
    if (e.kind === 'feishu_in') {
      // 飞书原文按来源展示：主人的消息和网页输入同侧，其他人的消息只作为一行动态。
      if (d.owner) this.root.append(el('div', { class: 'me' }, el('div', { class: 'me-b' }, markdown(e.text)),
        el('div', { class: 'me-m' }, el('span', { class: 'tag', text: '飞书' }), el('time', { text: clock(e.at) }))));
      else this.line(e);
      return;
    }
    if ((e.kind === 'chat' || e.kind === 'review') && e.actor === 'master') {
      // 最终回复单独展示；过程中同一段文字（Claude 的最后一个文本块）不重复出现。
      if (this.proc) { this.proc.model.dropText(e.text); this.dirty.add(this.proc); }
      this.root.append(el('div', { class: 'reply' + (e.kind === 'review' ? ' review' : '') },
        el('div', { class: 'reply-h' }, el('span', { class: 'av', 'aria-hidden': 'true', text: '✳' }),
          el('span', { text: e.kind === 'review' ? 'Master 定期审核' : 'Master' }), el('time', { text: clock(e.at) })),
        markdown(e.text)));
      return;
    }
    if (LINES[e.kind] && (e.visible || e.kind === 'task_created')) this.line(e);
  }

  line(e) {
    const title = e.task && this.taskTitle(e.task);
    let text = e.text;
    if (e.kind === 'instruction') text = `→ ${title || who(e.target)}：${e.text}（已入队）`;
    else if (e.kind === 'question') text = `${title || who(e.actor)}：${e.text}`;
    else if (e.kind === 'answer') text = `${who(e.actor)}：${e.text}`;
    else if (e.kind === 'feishu_in') text = `${e.data?.sender_id || '未知发送者'}：${e.text}`;
    else if (['completed', 'review_requested', 'task_failed', 'task_reopened', 'review_returned'].includes(e.kind)) text = `${title || ''}${title ? ' — ' : ''}${e.text}`;
    const row = el('div', { class: 'event ' + (LEVEL[e.kind] || 'info') },
      el('span', { class: 'event-k', text: LINES[e.kind] }), el('span', { class: 'event-t' }, rich(text, { node: e.task ? this.taskNode(e.task) : null })), el('time', { text: clock(e.at) }));
    if (e.task) row.append(el('button', { class: 'link', type: 'button', text: '过程 ›', onclick: () => this.openTask(e.task) }));
    this.root.append(row);
  }

  flush() { for (const p of this.dirty) p.update(); this.dirty.clear(); }
  // 进行中的回合模型（给工作中指示器读取当前动作）。
  running() { return this.proc && !this.proc.done ? this.proc.model : null; }
}
export { render };
