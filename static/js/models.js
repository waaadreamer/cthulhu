// 模型选择：以节点上报的本机模型目录为准；节点还没上报（旧版本或首次发现未完成）时退回内置列表。
// 纯函数，便于 node tests/test_model.mjs 直接测试。
export const FALLBACK = { codex: ['gpt-6-astra', 'gpt-6.1-sol'], claude: ['fable', 'opus', 'sonnet', 'haiku'], demo: [] };
export const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

// 返回 [{id, label}]：当前保存的模型即使不在目录里也保留，避免打开对话框时被静默改掉。
export function modelChoices(node, backend, current) {
  const found = node?.models?.[backend]?.list;
  const out = found?.length
    ? found.map(m => ({ id: m.id, label: m.alias ? `${m.alias}（${m.id}）` : (m.name && m.name !== m.id ? `${m.name}（${m.id}）` : m.id) }))
    : (FALLBACK[backend] || []).map(id => ({ id, label: id }));
  if (current && !out.some(m => m.id === current)) out.unshift({ id: current, label: current + '（当前，不在该节点目录中）' });
  return out;
}

// 推理强度只对 Codex 有意义；选了目录里的模型就只给它支持的档位。
export function effortChoices(node, backend, model) {
  if (backend !== 'codex') return [];
  const m = node?.models?.codex?.list?.find(x => x.id === model);
  return m?.efforts?.length ? m.efforts : EFFORTS;
}

// 节点对话框里的状态行：说明模型列表从哪来、多久前更新、上次是否失败。
export function catalogNote(node, backend, now = Date.now() / 1000) {
  const info = node?.models?.[backend];
  if (!info) return backend === 'demo' ? '' : '该节点尚未上报模型目录，显示内置列表';
  const age = info.at ? Math.round((now - info.at) / 60) : null;
  const when = age == null ? '' : age < 1 ? '刚刚更新' : age < 120 ? `${age} 分钟前更新` : `${Math.round(age / 60)} 小时前更新`;
  return [`${info.list.length} 个模型`, when, info.error && '上次发现失败：' + info.error].filter(Boolean).join('，');
}
