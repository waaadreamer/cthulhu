// 还原 /api/state 的增量（格式见 cthulhu_v4/web.py 的 snapshot_delta）。返回新对象，不修改 old。
export function applyDelta(old, delta) {
  const out = { ...old };
  for (const [k, d] of Object.entries(delta)) {
    if (d.drop) delete out[k];
    else if ('value' in d) out[k] = d.value;
    else if (d.list) {
      const prev = old[k] || [], by = new Map(prev.map(x => [x.id, x]));
      for (const x of d.list.set) by.set(x.id, x);
      out[k] = (d.list.ids || prev.map(x => x.id)).map(id => by.get(id));
    } else if (d.dict) {
      const m = { ...(old[k] || {}) };
      for (const key of d.dict.del) delete m[key];
      out[k] = Object.assign(m, d.dict.set);
    }
  }
  return out;
}
