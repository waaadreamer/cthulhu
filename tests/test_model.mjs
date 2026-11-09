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
  assert.equal(mcp.name, 'ask_master'); assert.equal(mcp.server, 'cthulhu'); assert.equal(mcp.status, 'failed');
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

// 原生重连只保留最新进度，并在工作中指示里显示。
{
  const m = new Model('master', 'process');
  m.apply(ev('master', 'native:error', { error: { message: 'Reconnecting... 2/5' } }));
  m.apply(ev('master', 'native:error', { error: { message: 'Reconnecting... 3/5' } }));
  assert.equal(m.live().length, 1);
  assert.deepEqual(m.activity(), { kind: 'retry', text: '模型连接中断，正在重连（3/5）' });
  m.apply(item('master', { id: 'c2', type: 'commandExecution', command: 'nvidia-smi', status: 'inProgress' }, false));
  assert.equal(m.activity().kind, 'command');
}

assert.equal(unwrapShell("/bin/bash -lc 'echo '\\''hi'\\'''"), "echo 'hi'");
console.log('test_model: ok');

// 文件链接识别：固定写法、裸绝对路径、代码与 Markdown 链接里的路径；不误伤 URL 与普通文字。
{
  const { scan, fileCtx } = await import('../static/js/dom.js');
  fileCtx.nodes = ['S', 'R6003', 'ali'];
  const files = (t, ctx) => scan(t, ctx).filter(g => g.type === 'file').map(g => (g.node || '?') + '::' + g.path);
  assert.deepEqual(files('S 节点：report.md (/home/wangzeyuan/code-reviews/t_80825262-20261019/report.md)'),
    ['S::/home/wangzeyuan/code-reviews/t_80825262-20261019/report.md']);
  assert.deepEqual(files('见 S::/data/run/config.yaml。'), ['S::/data/run/config.yaml']);
  assert.deepEqual(files('输出在 `/tmp/out/log.txt`'), ['?::/tmp/out/log.txt']);
  assert.deepEqual(files('输出在 `/tmp/out/log.txt`', { node: 'R6003' }), ['R6003::/tmp/out/log.txt']);
  assert.deepEqual(files('[报告](ali::/srv/a.md) 和 https://example.com/a/b'), ['ali::/srv/a.md']);
  assert.deepEqual(files('比例 3/4，日期 2026/10/19，单位 MB/s'), []);
  assert.deepEqual(files('/root/a.py:12 处报错'), ['?::/root/a.py']);
  assert.equal(scan('访问 https://h/x/y').find(g => g.type === 'url').href, 'https://h/x/y');
  // 节点名大小写严格匹配，不把 “sali” 当成 ali。
  assert.deepEqual(files('sali 上的 /a/b'), ['?::/a/b']);
}
console.log('test_model: files ok');

// 模型选择：以节点上报的目录为准；当前值不在目录里时保留并标出；推理强度跟随所选模型。
{
  const { modelChoices, effortChoices, catalogNote, FALLBACK, EFFORTS } = await import('../static/js/models.js');
  const node = { models: {
    codex: { list: [{ id: 'gpt-a', name: 'GPT-A', efforts: ['low', 'ultra'] }, { id: 'gpt-b', name: 'gpt-b' }], at: 1000, error: null },
    claude: { list: [{ id: 'claude-opus-9-9', name: 'opus → claude-opus-9-9', alias: 'opus' }], at: 1000, error: '超时' } } };
  assert.deepEqual(modelChoices(node, 'codex').map(m => m.id), ['gpt-a', 'gpt-b']);
  assert.equal(modelChoices(node, 'codex')[0].label, 'GPT-A（gpt-a）');
  assert.equal(modelChoices(node, 'claude')[0].label, 'opus（claude-opus-9-9）');
  assert.deepEqual(modelChoices(node, 'claude')[0], { id: 'claude-opus-9-9', title: 'opus', sub: 'claude-opus-9-9', label: 'opus（claude-opus-9-9）', tag: '' });
  assert.equal(modelChoices(node, 'codex')[1].sub, '');                                // 显示名就是 id：不重复显示
  assert.deepEqual(modelChoices(node, 'codex', 'old-model').map(m => m.id), ['old-model', 'gpt-a', 'gpt-b']);
  assert.deepEqual(modelChoices({}, 'codex').map(m => m.id), FALLBACK.codex);          // 节点未上报：内置列表
  assert.deepEqual(effortChoices(node, 'codex', 'gpt-a'), ['low', 'ultra']);
  assert.deepEqual(effortChoices(node, 'codex', 'unknown'), EFFORTS);
  assert.deepEqual(effortChoices(node, 'claude', 'claude-opus-9-9'), []);
  assert.equal(catalogNote(node, 'codex', 1000 + 30 * 60), '2 个模型，30 分钟前更新');
  assert.match(catalogNote(node, 'claude', 1000), /上次发现失败：超时/);
  assert.match(catalogNote({}, 'codex'), /尚未上报/);
}
console.log('test_model: models ok');

// 飞书白名单页：按人名或规则搜索（多词都要命中、名字命中排前）；拆出授权来源前缀；规则相同的人归组。
{
  const { filterAcl, splitRule, groupOf } = await import('../static/js/acl.js');
  const list = [
    { id: 'ou_1', name: '张三', rules: ['允许查看 S 集群训练状态'] },
    { id: 'ou_2', name: '李四', rules: ['允许查看 S 集群训练状态'] },
    { id: 'ou_3', name: '王五（张三的同事）', rules: ['允许重启 R6003 policy'] },
  ];
  assert.deepEqual(filterAcl(list, '').map(a => a.id), ['ou_1', 'ou_2', 'ou_3']);
  assert.deepEqual(filterAcl(list, '张三').map(a => a.id), ['ou_1', 'ou_3']);
  assert.deepEqual(filterAcl(list, 'r6003').map(a => a.id), ['ou_3']);             // 不区分大小写，搜规则内容
  assert.deepEqual(filterAcl(list, '李四 训练').map(a => a.id), ['ou_2']);          // 每个词都要命中
  assert.deepEqual(filterAcl(list, '训练 张三').map(a => a.id), ['ou_1']);
  assert.deepEqual(filterAcl(list, '不存在'), []);
  const r = splitRule('主人授权来源：m_2a170233/飞书om_x100b62b3，2026-10-29；允许重启 R6002 policy');
  assert.equal(r.text, '允许重启 R6002 policy');
  assert.match(r.source, /^主人授权来源：m_2a170233/);
  assert.deepEqual(splitRule('允许查看文档'), { source: '', text: '允许查看文档' });
  const same = groupOf(list);
  assert.deepEqual(list.map(same), [2, 2, 1]);
}
console.log('test_model: acl ok');

{
  const { speaker } = await import('../static/js/history.js');
  assert.equal(speaker({ kind: 'message', actor: 'owner' }), '你');
  assert.equal(speaker({ kind: 'reply', actor: 'master' }), 'Master · 侧栏');
  assert.equal(speaker({ kind: 'feishu_in', data: { name: '张三' } }), '张三');
  assert.equal(speaker({ kind: 'feishu_in', data: { owner: true } }), '你 · 飞书');
}

// 任务分组：固定顺序、空组省略、待审核算进行中、未知状态不丢；展开状态只记主人改过的组。
{
  const { groupTasks, groupOpen, setGroupOpen } = await import('../static/js/tasks.js');
  const t = (id, state, extra = {}) => ({ id, state, created_at: +id.slice(1), ...extra });
  const groups = groupTasks([t('t1', 'completed', { completed_at: 50 }), t('t2', 'running'), t('t3', 'review'), t('t4', 'paused'),
    t('t5', 'completed', { completed_at: 90 }), t('t6', 'weird'), t('t7', 'failed', { archived: true }), t('t8', 'waiting')]);
  assert.deepEqual(groups.map(g => g.key), ['active', 'paused', 'completed', 'archived']);
  assert.deepEqual(groups[0].tasks.map(x => x.id), ['t8', 't6', 't3', 't2']);   // 新建的在前
  assert.deepEqual(groups[2].tasks.map(x => x.id), ['t5', 't1']);               // 刚结束的在前
  const mem = new Map(), store = { getItem: k => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) };
  assert.equal(groupOpen('active', store), true); assert.equal(groupOpen('completed', store), false);
  setGroupOpen('completed', true, store); setGroupOpen('active', false, store);
  assert.equal(groupOpen('completed', store), true); assert.equal(groupOpen('active', store), false);
  assert.equal(groupOpen('archived', store), false);
}
console.log('test_model: task groups ok');

// Markdown：表格、公式、嵌套列表、任务列表、引用里的块；流式输出时半截的围栏与公式不吞掉前文。
{
  const { parseBlocks, cells } = await import('../static/js/markdown.js');
  const { scan } = await import('../static/js/dom.js');
  const types = t => parseBlocks(t).map(b => b.type);
  const [tbl] = parseBlocks('| 名称 | 数量 |\n|:---|---:|\n| a | 1 |\n| `x|y` | 2 | 多余 |\n| b |');
  assert.deepEqual(tbl.head, ['名称', '数量']); assert.deepEqual(tbl.align, ['left', 'right']);
  assert.deepEqual(tbl.rows, [['a', '1'], ['`x|y`', '2'], ['b', '']]);   // 反引号里的 | 不切；列数以表头为准
  assert.deepEqual(cells('a \\| b | c'), ['a | b', 'c']);
  assert.deepEqual(types('标题\n---'), ['para', 'hr']);                    // 一列的 “---” 不是表格
  assert.deepEqual(parseBlocks('$$\n\\int_0^1 x\\,dx\n$$')[0], { type: 'math', text: '\\int_0^1 x\\,dx' });
  assert.deepEqual(parseBlocks('$$a^2$$')[0], { type: 'math', text: 'a^2' });
  assert.deepEqual(parseBlocks('\\[\nx\n\\]')[0], { type: 'math', text: 'x' });
  const [ul] = parseBlocks('- 一\n  - 子\n    1. 孙\n- [x] 完成\n- [ ] 未完');
  assert.equal(ul.items.length, 3);
  assert.equal(ul.items[0].blocks[1].items[0].blocks[1].ordered, true);
  assert.deepEqual(ul.items.map(i => i.check), [null, true, false]);
  assert.equal(parseBlocks('3. 三\n4. 四')[0].start, 3);
  assert.deepEqual(parseBlocks('> 引\n> - 列')[0].blocks.map(b => b.type), ['para', 'list']);
  assert.deepEqual(types('前文\n```py\nprint(1)'), ['para', 'code']);    // 未闭合的围栏到末尾为止
  assert.deepEqual(types('前文\n$$\nx'), ['para', 'math']);
  assert.deepEqual(parseBlocks('段一\n续行\n## 标')[0].lines, ['段一', '续行']);
  // 行内：公式、斜体、删除线、转义；金额与 snake_case 不误判。
  const kinds = t => scan(t).filter(g => g.type !== 'text').map(g => g.type + ':' + g.text);
  assert.deepEqual(kinds('质能 $E=mc^2$ 与 \\(a+b\\)'), ['math:E=mc^2', 'math:a+b']);
  assert.deepEqual(kinds('价格 $5 到 $10，变量 a_b_c'), []);
  assert.deepEqual(kinds('*斜* 与 ~~删~~ 与 **粗**'), ['em:斜', 'del:删', 'bold:粗']);
  assert.deepEqual(kinds('2 * 3 * 4'), []);
  assert.equal(scan('\\*不是斜体\\*').map(g => g.text).join(''), '*不是斜体*');
}
console.log('test_model: markdown ok');

// Antigravity：真实公开 ACP chunk 合并，但工具前后的正文保持顺序；更新不擦掉输入。
{
  const m = new Model('a'); let seq = 0;
  const send = data => m.apply({ seq: ++seq, kind: 'native:agy_update', data });
  send({ sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'before ' } });
  send({ sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'tool' } });
  send({ sessionUpdate: 'tool_call', toolCallId: 'x', title: 'list_tasks', rawInput: { q: 1 }, status: 'in_progress' });
  send({ sessionUpdate: 'tool_call_update', toolCallId: 'x', status: 'completed', content: [{ type: 'content', content: { type: 'text', text: 'ok' } }] });
  send({ sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'after tool' } });
  assert.deepEqual(m.live().map(x => x.type), ['text', 'tool', 'text']);
  assert.equal(m.live()[0].text, 'before tool');
  assert.deepEqual(m.live()[1].input, { q: 1 });
  assert.equal(m.live()[1].status, 'completed');
  assert.equal(m.live()[1].output, 'ok');
  assert.equal(m.live()[2].text, 'after tool');
  const { modelChoices, catalogNote } = await import('../static/js/models.js');
  assert.deepEqual(modelChoices({}, 'antigravity', ''), []);
  assert.match(catalogNote({}, 'antigravity'), /待配置/);
  const n = { models: { antigravity: { list: [{ id: 'gemini-4-argon-high', availability: 'unavailable' }], status: 'unavailable', source: '本机 CLI 目录', error: '渠道未报告模型' } } };
  assert.match(modelChoices(n, 'antigravity', '')[0].tag, /渠道未列出/);
  assert.match(catalogNote(n, 'antigravity'), /渠道目录未列出候选模型/);
}
console.log('test_model: antigravity ok');
