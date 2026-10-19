"""文件链接：`节点::绝对路径` 指向某台执行机上的文件或目录，网页点开即可查看。

读取发生在文件所在的节点上（节点主动连中心，中心无法直接访问执行机），中心只做转发与缓存：
- 中心把读取请求附在该节点下一次同步的响应里，节点读完后经同一条连接回传；
- 读取请求只放在内存：它是网页上的一次查看，不是业务状态。中心重启时网页那边超时，重试即可；
- 缓存放在中心状态目录，按 mtime/size 判断是否变化；节点离线时返回旧副本，并明确标注是旧的。
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path

REF = re.compile(r'^([A-Za-z0-9_-]{1,64})::(/.*)$')
FEATURE = 'files'      # 节点在同步时报告此能力；旧节点不认识读取请求，必须先升级
MAX_BYTES = 4 * 1024 * 1024   # base64 后约 5.4 MB，低于 WebSocket 单帧上限
MAX_ENTRIES = 500
FRESH = 15             # 秒：短时间内重复打开直接用缓存，不打扰节点
TIMEOUT = 20           # 网页请求总时限 30 秒，留出余量
CACHE_LIMIT = 256 * 1024 * 1024


def parse(ref):
    m = REF.match(str(ref or '').strip())
    if not m:
        raise ValueError('文件链接应写成 节点::绝对路径，例如 S::/home/user/report.md')
    return m.group(1), m.group(2)


# ---- 节点侧：真正读文件 ----
def read_path(path, known=None, max_bytes=MAX_BYTES, protected=()):
    """在节点上读取一个路径。返回可直接回传给中心的字典；出错时返回 {'error': ...}。"""
    p = Path(path)
    if not p.is_absolute():
        return {'error': '只能打开绝对路径'}
    try:
        real = p.resolve(strict=True)
    except FileNotFoundError:
        return {'error': f'文件不存在：{path}'}
    except (OSError, RuntimeError) as e:
        return {'error': f'无法解析路径：{e}'}
    for guard in protected:
        # 节点凭证与状态目录（中心机上还包括 web-token 与节点 token）不能经网页读出；
        # 先 resolve 再比较，符号链接绕不过去。
        if real == guard or guard in real.parents:
            return {'error': '该路径存放 Cthulhu 凭证与状态，不能经网页打开'}
    try:
        st = real.stat()
        if stat.S_ISDIR(st.st_mode):
            names = sorted(os.listdir(real))
            entries = []
            for name in names[:MAX_ENTRIES]:
                try:
                    cs = (real / name).stat()
                    entries.append({'name': name, 'dir': stat.S_ISDIR(cs.st_mode), 'size': cs.st_size, 'mtime': cs.st_mtime})
                except OSError:
                    entries.append({'name': name, 'dir': False, 'size': None, 'mtime': None})
            entries.sort(key=lambda e: (not e['dir'], e['name'].lower()))
            return {'kind': 'dir', 'path': str(real), 'mtime': st.st_mtime, 'entries': entries,
                    'truncated': len(names) > MAX_ENTRIES, 'total': len(names)}
        if not stat.S_ISREG(st.st_mode):
            return {'error': '不是普通文件或目录'}
        if known and known.get('mtime') == st.st_mtime and known.get('size') == st.st_size:
            return {'unchanged': True}
        with open(real, 'rb') as f:
            data = f.read(max_bytes)
        return {'kind': 'file', 'path': str(real), 'size': st.st_size, 'mtime': st.st_mtime,
                'truncated': st.st_size > len(data), 'content': base64.b64encode(data).decode()}
    except PermissionError:
        return {'error': f'节点用户没有读取权限：{path}'}
    except OSError as e:
        return {'error': f'读取失败：{e}'}


# ---- 中心侧：转发与缓存 ----
class FileService:
    def __init__(self, center):
        self.c = center
        self.dir = center.state_dir / 'file-cache'
        self.pending = {}   # 读取请求 id → {node, path, known, future, sent}

    # 由 hub.sync 调用：把尚未下发的读取请求交给该节点。只在内存里标记，节点断线重连后网页可重试。
    def requests_for(self, node):
        out = []
        for fid, p in self.pending.items():
            if p['node'] == node and not p['sent']:
                p['sent'] = True
                out.append({'id': fid, 'path': p['path'], 'known': p['known'], 'max': MAX_BYTES})
        return out

    # 由 hub.request(op='file') 调用：节点回传读取结果。只接受发给该节点的请求。
    def deliver(self, node, body):
        p = self.pending.get(body.get('id'))
        if not p or p['node'] != node:
            return {'ok': False}
        if not p['future'].done():
            p['future'].set_result(body)
        return {'ok': True}

    def _key(self, ref):
        return hashlib.sha256(ref.encode()).hexdigest()

    def cached(self, ref):
        key = self._key(ref)
        try:
            meta = json.loads((self.dir / (key + '.json')).read_text())
            data = (self.dir / (key + '.bin')).read_bytes() if meta.get('kind') == 'file' else b''
            return meta, data
        except (OSError, ValueError):
            return None, b''

    def store(self, ref, body):
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = self._key(ref)
        meta = {k: body.get(k) for k in ('kind', 'path', 'size', 'mtime', 'truncated', 'entries', 'total')}
        meta.update(ref=ref, fetched_at=time.time())
        data = base64.b64decode(body['content']) if body.get('kind') == 'file' else b''
        if meta['kind'] == 'file':
            (self.dir / (key + '.bin')).write_bytes(data)
        (self.dir / (key + '.json')).write_text(json.dumps(meta, ensure_ascii=False))
        self.evict()
        return meta, data

    def touch(self, ref, meta):
        meta['fetched_at'] = time.time()
        (self.dir / (self._key(ref) + '.json')).write_text(json.dumps(meta, ensure_ascii=False))

    def evict(self):
        """缓存总量超限时按最近读取时间淘汰最旧的；缓存只是加速与离线兜底，删了可以再取。"""
        items = []
        for meta_file in self.dir.glob('*.json'):
            binary = meta_file.with_suffix('.bin')
            size = (binary.stat().st_size if binary.exists() else 0) + meta_file.stat().st_size
            try:
                at = json.loads(meta_file.read_text()).get('fetched_at', 0)
            except ValueError:
                at = 0
            items.append((at, size, meta_file, binary))
        total = sum(i[1] for i in items)
        for at, size, meta_file, binary in sorted(items):
            if total <= CACHE_LIMIT:
                break
            meta_file.unlink(missing_ok=True); binary.unlink(missing_ok=True)
            total -= size

    @staticmethod
    def reply(ref, meta, data, *, cached, stale=None):
        node, path = parse(ref)
        out = {k: meta.get(k) for k in ('kind', 'size', 'mtime', 'truncated', 'entries', 'total', 'fetched_at')}
        out.update(ref=ref, node=node, path=path, real_path=meta.get('path'), cached=cached, stale=stale)
        if meta.get('kind') == 'file':
            out['content'] = base64.b64encode(data).decode()
        return out

    async def read(self, ref, refresh=False):
        node, path = parse(ref)
        n = self.c.store.find('node', node)
        if not n:
            raise ValueError(f'节点 {node} 不存在')
        meta, data = self.cached(ref)
        now = time.time()
        if meta and not refresh and now - meta['fetched_at'] < FRESH:
            return self.reply(ref, meta, data, cached=True)

        def stale(reason):
            age = max(1, int(now - meta['fetched_at']))
            return self.reply(ref, meta, data, cached=True,
                              stale=f'{reason}，显示 {age // 60} 分钟前的缓存' if age >= 60 else f'{reason}，显示 {age} 秒前的缓存')

        if not self.c.hub.online(node):
            if meta:
                return stale(f'节点 {node} 离线')
            raise ValueError(f'节点 {node} 离线，中心也没有这个文件的缓存')
        if FEATURE not in (n.get('features') or []):
            raise ValueError(f'节点 {node} 的代码版本还不支持打开文件；在该机器重跑安装命令即可升级')
        fid = secrets.token_hex(8)
        future = asyncio.get_running_loop().create_future()
        known = {'mtime': meta['mtime'], 'size': meta['size']} if meta and meta.get('kind') == 'file' and not meta.get('truncated') else None
        self.pending[fid] = {'node': node, 'path': path, 'known': known, 'future': future, 'sent': False}
        self.c.hub.wake_node(node)
        try:
            body = await asyncio.wait_for(future, TIMEOUT)
        except asyncio.TimeoutError:
            if meta:
                return stale(f'节点 {node} {TIMEOUT} 秒内没有响应')
            raise ValueError(f'节点 {node} {TIMEOUT} 秒内没有返回文件内容，请稍后重试')
        finally:
            self.pending.pop(fid, None)
        if body.get('error'):
            raise ValueError(body['error'])
        if body.get('unchanged') and meta:
            self.touch(ref, meta)
            return self.reply(ref, meta, data, cached=True)
        meta, data = self.store(ref, body)
        return self.reply(ref, meta, data, cached=False)
