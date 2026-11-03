// 左栏任务分组：按状态分成固定顺序的几组，展开与否记在本浏览器。纯函数部分可在 node 下单测。

// 从上到下的顺序就是关注优先级。待审核等主人或 Master 拍板，和进行中放在一起，免得被收起的组藏住。
export const GROUPS = [
  { key: 'active', label: '进行中', states: ['running', 'waiting', 'review'], open: true },
  { key: 'paused', label: '暂停', states: ['paused'], open: true },
  { key: 'failed', label: '未达成', states: ['failed'], open: true },
  { key: 'cancelled', label: '已取消', states: ['cancelled'], open: false },
  { key: 'completed', label: '已完成', states: ['completed'], open: false },
];
export const ARCHIVE = { key: 'archived', label: '已归档', open: false };

const ended = t => t.completed_at || t.created_at || 0;

// 未归档任务按状态分组（空组省略），已归档的单独成组放最后。
// 进行中的组按创建时间新的在前；已结束的组按结束时间新的在前，刚结束的任务排在最上面。
// 未知状态归入进行中：宁可多显示，也不能让任务从列表里消失。
export function groupTasks(tasks) {
  const known = new Set(GROUPS.flatMap(g => g.states));
  const out = [];
  for (const g of GROUPS) {
    const list = tasks.filter(t => !t.archived && (g.states.includes(t.state) || (g.key === 'active' && !known.has(t.state))));
    list.sort(g.key === 'active' || g.key === 'paused' ? (a, b) => b.created_at - a.created_at : (a, b) => ended(b) - ended(a));
    if (list.length) out.push({ ...g, tasks: list });
  }
  const archived = tasks.filter(t => t.archived).sort((a, b) => ended(b) - ended(a));
  if (archived.length) out.push({ ...ARCHIVE, tasks: archived });
  return out;
}

// 展开状态：只存主人改过的组，没改过的用默认值。storage 可注入，便于测试。
export function groupOpen(key, storage = globalThis.localStorage) {
  const v = storage?.getItem('cthulhu-group-' + key);
  if (v === '1') return true;
  if (v === '0') return false;
  return (GROUPS.find(g => g.key === key) || ARCHIVE).open;
}
export function setGroupOpen(key, open, storage = globalThis.localStorage) {
  storage?.setItem('cthulhu-group-' + key, open ? '1' : '0');
}
