// 只缓存当前凭证的浏览器内存；HTTP 304 必须先经服务器认证，不把“有缓存”当作登录成功。
export function createCanvasReader({ token, base, fetcher = fetch }) {
  let credential, epoch = 0;
  const cache = new Map(), latest = new Map();
  let serial = 0;
  function clear() { cache.clear(); latest.clear(); epoch++; }
  async function get(path, { signal } = {}) {
    const secret = token();
    if (secret !== credential) { credential = secret; clear(); }
    const generation = epoch, url = new URL(path, base).href, previous = cache.get(url), ticket = ++serial;
    latest.set(url, ticket);
    const headers = { Authorization: 'Bearer ' + secret, 'Content-Type': 'application/json' };
    if (previous?.etag) headers['If-None-Match'] = previous.etag;
    const response = await fetcher(url, { headers, signal, cache: 'no-store' });
    // 换凭证/本地配置写入后的迟到响应不能污染新作用域，也不能让旧登录结果显示成当前数据。
    if (generation !== epoch || secret !== token() || latest.get(url) !== ticket) { const error = Error('读取已被新状态替代'); error.stale = true; throw error; }
    let value;
    if (response.status === 304) {
      if (!previous) throw Error('中心返回未变化但本页没有对应数据，请重试');
      value = previous.value;
    } else {
      try { value = await response.json(); } catch { value = {}; }
      if (!response.ok || value.error) {
        if (response.status === 401) clear();
        const error = Error(value.error || response.statusText); error.status = response.status; throw error;
      }
      if (value.data_from_stdout) {
        if (typeof value.attempt?.stdout !== 'string' || !['json', 'text'].includes(value.data_from_stdout)) throw Error('中心紧凑数据格式无效');
        // stdout 原文完整保留，JSON 与文本都可逆恢复；失败回执不走此协议，不会覆盖最后成功数据。
        value.data = value.data_from_stdout === 'json' ? JSON.parse(value.attempt.stdout) : value.attempt.stdout;
        delete value.data_from_stdout;
      }
    }
    const now = Number(response.headers.get('X-Canvas-Now'));
    if (response.headers.has('X-Canvas-Now') && Number.isFinite(now)) {
      value = { ...value };
      if (Array.isArray(value.canvases)) value.canvases = value.canvases.map(b => ({ ...b, now }));
      else value.now = now;
    }
    if (generation !== epoch || secret !== token() || latest.get(url) !== ticket) { const error = Error('读取已被新状态替代'); error.stale = true; throw error; }
    const etag = response.headers.get('ETag');
    if (etag) {
      cache.delete(url); cache.set(url, { etag, value });
      while (cache.size > 24) cache.delete(cache.keys().next().value);
    } else cache.delete(url);
    return value;
  }
  return { get, clear };
}
