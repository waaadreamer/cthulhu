// 前端会话模型的不变量：用合成原生帧驱动，不依赖浏览器或模型。node tests/test_model.mjs
import assert from 'node:assert/strict';
import { Model, unwrapShell, diffStat } from '../static/js/model.js';

let seq = 0;
const ev = (actor, kind, data = {}, text = '', extra = {}) => ({ seq: ++seq, at: 1000 + seq, actor, kind, data, text, ...extra });
const item = (actor, it, done = true) => ev(actor, 'native:item/' + (done ? 'completed' : 'started'), { item: it });

// Codex：start/delta/complete 合并为一块；命令、工具、补丁成为可折叠步骤；最终结果单独成卡片。
{
  const m = new Model('old_a');
  m.apply(ev('system', 'dispatch', { job: 'turn_1' }, '', { target: 'old_a' }));
  m.apply(ev('master', 'message', { priority: 9 }, '你的唯一任务：\n' + JSON.stringify({ title: 'T', description: 'D', acceptance: 'A', node: 'n1' }), { target: 'old_a' }));
  m.apply(item('old_a', { id: 'x', type: 'agentMessage', phase: 'commentary', text: '' }, false));
  m.apply(ev('old_a', 'native:item/agentMessage/delta', { itemId: 'x', delta: '先看' }));
  m.apply(ev('old_a', 'native:item/agentMessage/delta', { itemId: 'x', delta: '文件' }));
  m.apply(item('old_a', { id: 'x', type: 'agentMessage', phase: 'commentary', text: '先看文件' }));
  m.apply(item('old_a', { id: 'c', type: 'commandExecution', command: "/bin/bash -lc 'ls -la'", status: 'inProgress' }, false));
  m.apply(ev('old_a', 'native:item/commandExecution/outputDelta', { itemId: 'c', delta: 'a.py\n' }));
  m.apply(item('old_a', { id: 'c', type: 'commandExecution', command: "/bin/bash -lc 'ls -la'", status: 'completed', exitCode: 1, aggregatedOutput: 'a.py\n' }));
  m.apply(item('old_a', { id: 'p', type: 'fileChange', status: 'completed', changes: [{ path: '/r/a.py', diff: '@@\n-x\n+y\n+z' }] }));
  m.apply(item('old_a', { id: 'r', type: 'reasoning', summary: [], content: [] }));
  m.apply(ev('old_a', 'native:thread/tokenUsage/updated', { usage: 1 }));
  m.apply(ev('old_a', 'completed', { evidence: '测试通过' }, '完成了'));
  const types = m.live().map(b => b.type);
  assert.deepEqual(types, ['turn', 'task', 'text', 'command', 'patch', 'result']);
  const [, , text, cmd, patch] = m.live();
  assert.equal(text.text, '先看文件');
  assert.equal(cmd.command, 'ls -la');
  assert.equal(cmd.status, 'failed');          // 非零退出码标为失败
  assert.equal(patch.files[0].path, '/r/a.py');
  assert.equal(m.steps(), 3);
  m.apply(ev('old_a', 'completed', {}, '重复事件', { seq: 13 }));
  assert.equal(m.live().length, 6);             // 同一 seq 不重复处理
}

// Claude：stream 流式文本 + tool_use/tool_result 配对；Edit 转为 diff；思考单独成块。
{
  const m = new Model('old_c');
  m.apply(ev('old_c', 'native:stream_event', { event: { type: 'message_start', message: { id: 'msg1' } } }));
  m.apply(ev('old_c', 'native:stream_event', { event: { type: 'content_block_start', index: 0, content_block: { type: 'thinking', thinking: '' } } }));
  m.apply(ev('old_c', 'native:stream_event', { event: { type: 'content_block_delta', index: 0, delta: { type: 'thinking_delta', thinking: '想一想' } } }));
  m.apply(ev('old_c', 'native:assistant', { message: { id: 'msg1', content: [{ type: 'thinking', thinking: '想一想' },
    { type: 'tool_use', id: 'tu1', name: 'Bash', input: { command: 'pytest -q' } },
    { type: 'tool_use', id: 'tu2', name: 'Edit', input: { file_path: '/r/b.py', old_string: 'a', new_string: 'b' } },
    { type: 'tool_use', id: 'tu3', name: 'mcp__cthulhu__ask_master', input: { text: '?' } }] } }));
  m.apply(ev('old_c', 'native:user', { message: { content: [{ type: 'tool_result', tool_use_id: 'tu1', content: '3 passed', is_error: false },
    { type: 'tool_result', tool_use_id: 'tu3', content: [{ type: 'text', text: 'ok' }], is_error: true }] } }));
  const [think, bash, edit, mcp] = m.live();
  assert.equal(think.type, 'reasoning'); assert.equal(think.text, '想一想');
  assert.equal(bash.type, 'command'); assert.equal(bash.output, '3 passed'); assert.equal(bash.status, 'completed');
  assert.equal(edit.type, 'patch'); assert.deepEqual(diffStat(edit.files[0].diff), { add: 1, del: 1 });
  assert.equal(edit.status, 'running');
  assert.equal(mcp.name, 'cthulhu · ask_master'); assert.equal(mcp.status, 'failed');
}

// 主会话 process 模式：Master 的最终回复不进过程区（由 chat 事件单独显示），其他 agent 的消息不出现。
{
  const m = new Model('master', 'process');
  m.apply(item('master', { id: 'f', type: 'agentMessage', phase: 'final_answer', text: '结论' }));
  m.apply(ev('owner', 'message', {}, '你好', { target: 'master' }));
  m.apply(item('master', { id: 't', type: 'mcpToolCall', server: 'cthulhu', tool: 'list_nodes', arguments: {}, status: 'completed' }));
  assert.deepEqual(m.live().map(b => b.type), ['tool']);
  assert.equal(m.dropText('不存在'), false);
}

assert.equal(unwrapShell("/bin/bash -lc 'echo '\\''hi'\\'''"), "echo 'hi'");
console.log('test_model: ok');
