// 模型选择：以节点上报的本机模型目录为准；节点还没上报（旧版本或首次发现未完成）时退回内置列表。
// 纯函数，便于 node tests/test_model.mjs 直接测试。
export const FALLBACK = { codex: ['gpt-6-astra', 'gpt-6.1-sol'], claude: ['fable', 'opus', 'sonnet', 'haiku'], demo: [] };
export const backendLabel = b => b === 'antigravity' ? 'Antigravity' : b;
export const EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];

// 返回 [{id, title, sub, label, tag}]：title 是好认的名字（显示名或别名），sub 是实际传给 CLI 的完整名。
// 当前保存的模型即使不在目录里也保留，避免打开对话框时被静默改掉。
export function modelChoices(node, backend, current) {
  const found = node?.models?.[backend]?.list;
  const out = found?.length
    ? found.map(m => {
        const title = m.alias || (m.name && m.name !== m.id ? m.name : m.id);
        return { id: m.id, title, sub: title === m.id ? '' : m.id, label: title === m.id ? m.id : `${title}（${m.id}）`,
                 tag: m.availability === 'unavailable' ? '渠道未列出' : m.availability === 'listed' ? '渠道已列出 · 推理未验证' : backend === 'antigravity' ? '可用性未验证' : m.default ? 'CLI 默认' : '' };
      })
    : (FALLBACK[backend] || []).map(id => ({ id, title: id, sub: '', label: id, tag: '' }));
  if (current && !out.some(m => m.id === current))
    out.unshift({ id: current, title: current, sub: '当前设置，不在该节点目录中', label: current + '（当前，不在该节点目录中）', tag: '' });
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
  if (!info) return backend === 'demo' ? '' : backend === 'antigravity' ? '待配置：尚未上报 Antigravity 模型目录；默认继承此节点 Claude 的认证与地址' : '该节点尚未上报模型目录，显示内置列表';
  const age = info.at ? Math.round((now - info.at) / 60) : null;
  const when = age == null ? '' : age < 1 ? '刚刚更新' : age < 120 ? `${age} 分钟前更新` : `${Math.round(age / 60)} 小时前更新`;
  const proof = { protocol_unsupported: '原生验证失败：该渠道不支持 Gemini 协议', failed: '最近原生请求失败', completed: '最近原生请求完成' }[info.inference_status];
  return [proof && `${proof}（${info.inference_model || '默认模型'}）`, info.inference_status === 'failed' && info.inference_error, info.base_source === 'node' && '认证继承 Claude，地址使用节点显式配置', info.source, `${info.list.length} 个模型`, when, info.status === 'unconfigured' && '待配置', info.status === 'unavailable' && '渠道目录未列出候选模型', info.error && (backend === 'antigravity' ? info.error : '上次发现失败：' + info.error)].filter(Boolean).join('，');
}
