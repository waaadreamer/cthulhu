// 模板固定数据面：明确列白名单，不能把配置/凭证随中心响应整包传入 iframe。
export function frameSnapshot(data, summary = {}) {
  const merged = { ...data, ...summary };
  // 列表和数据来自不同 HTTP 请求，编辑/回收可以发生在两次读取之间。
  // 保留数据对应的时间，并在版本不一致时撤销 fresh，不能把旧配置数据标成新模板的实时结果。
  merged.sampled_at = data?.sampled_at ?? null;
  if (merged.fresh && (data?.revision !== merged.revision || data?.data_revision !== merged.revision)) {
    merged.fresh = false; merged.stale = true;
    if (['fresh', 'collecting'].includes(merged.status)) merged.status = 'stale';
  }
  const fields = ['id', 'name', 'state', 'status', 'stale', 'fresh', 'node_online', 'collector_online',
    'sampled_at', 'now', 'revision', 'blocked_unknown', 'data_revision', 'data', 'format', 'attempt'];
  return Object.fromEntries(fields.map(k => [k, merged[k] ?? null]));
}

export const STATUS = { fresh: '采集正常', collecting: '采集中', waiting: '等待首次采集', stale: '数据已过期',
  failed: '采集失败 · 保留上次成功数据', node_offline: '节点离线', collector_offline: 'Canvas 服务离线',
  paused: '已暂停', closed: '已关闭', unknown: '命令结果未知 · 已暂停' };
