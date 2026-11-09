"""Canvas 私有条件读取；验证来源/凭证之后才可调用，绝不作为鉴权缓存。"""
import gzip
import hashlib
import json


def canvas_etag(value):
    # 只排除协议层的中心当前时间；业务 data 内的 now 必须参与，否则会把真实更新误判为未变化。
    stable = {k: v for k, v in value.items() if k != 'now'}
    if 'canvases' in stable:
        stable['canvases'] = [{k: v for k, v in b.items() if k != 'now'} for b in stable['canvases']]
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return 'W/"' + hashlib.sha256(raw).hexdigest() + '"'


def respond_canvas(handler, value, csp):
    """认证成功的 GET 可复用浏览器当前内存；公共代理/磁盘缓存不得存业务数据。"""
    etag = canvas_etag(value)
    unchanged = handler.headers.get('If-None-Match') == etag
    raw = b'' if unchanged else json.dumps(value, ensure_ascii=False).encode()
    compressed = len(raw) > 1024 and 'gzip' in handler.headers.get('Accept-Encoding', '')
    if compressed:
        raw = gzip.compress(raw, 6)
    now = value.get('now')
    if now is None and value.get('canvases'):
        now = value['canvases'][0].get('now')
    handler.send_response(304 if unchanged else 200)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(raw)))
    if compressed:
        handler.send_header('Content-Encoding', 'gzip')
    handler.send_header('Cache-Control', 'private, no-store')
    handler.send_header('Vary', 'Authorization, Accept-Encoding')
    handler.send_header('ETag', etag)
    if now is not None:
        handler.send_header('X-Canvas-Now', str(now))
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Content-Security-Policy', csp)
    handler.end_headers()
    try:
        handler.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
        pass
