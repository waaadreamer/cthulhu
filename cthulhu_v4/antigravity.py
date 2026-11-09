"""Antigravity 的节点本机配置与目录，不把认证材料传回中心。

agy 1.5.3 支持 Gemini API，不能把 Claude 的 Messages 协议当成兼容证据。
只继承本项目 ClaudeRuntime 会读取的设置，不执行交互 shell 函数或 apiKeyHelper。
"""
import asyncio
import json
import hashlib
import re
import signal
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


SOURCE = '本机 agy models（CLI 目录，不代表渠道已可调用）'


def claude_credentials(env, cwd=None):
    merged = dict(env)
    root = Path(env.get('CLAUDE_CONFIG_DIR') or Path(env.get('HOME') or Path.home()) / '.claude')
    paths = [root / 'settings.json']
    if cwd:
        paths += [Path(cwd) / '.claude' / 'settings.json', Path(cwd) / '.claude' / 'settings.local.json']
    paths += [Path('/etc/claude-code/managed-settings.json')]
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            # 不把配置内容或 JSON 异常中的原文放入公开错误，里面可能含 key。
            raise ValueError('Claude 设置无法解析；请在所属节点检查配置') from None
        if not isinstance(data, dict) or not isinstance(data.get('env') or {}, dict):
            raise ValueError('Claude 设置格式不正确；请在所属节点检查配置')
        values = data.get('env') or {}
        merged.update({k: v for k, v in values.items() if isinstance(v, str)})
    key = merged.get('ANTHROPIC_AUTH_TOKEN') or merged.get('ANTHROPIC_API_KEY')
    base = (merged.get('ANTHROPIC_BASE_URL') or '').rstrip('/')
    if not key or not base:
        raise ValueError('待配置：未找到 Claude 的 auth/base_url；不会回退到 Google 地址')
    u = urllib.parse.urlsplit(base)
    if (u.scheme not in ('https', 'http') or not u.hostname or u.username or u.password or u.query or u.fragment):
        raise ValueError('Claude base_url 不是可继承的 HTTP(S) 地址（不能含内嵌凭据、查询或片段）')
    return key, base


def effective_base(base, override=None):
    if not override:
        return base
    target = str(override).rstrip('/')
    old, new = urllib.parse.urlsplit(base), urllib.parse.urlsplit(target)
    origin = lambda u: (u.scheme, u.hostname, u.port or (443 if u.scheme == 'https' else 80))
    if (new.scheme not in ('http', 'https') or not new.hostname or new.username or new.password
            or new.query or new.fragment or origin(old) != origin(new)):
        raise ValueError('Antigravity 显式地址必须与 Claude base_url 同源；不会把继承凭据发到其他域名/端口')
    # 只接受节点显式配置的有据入口，不自动裁剪 /anthropic 或猜测 /gemini。
    return target


def credential_profile(key, base):
    # 只在节点私有状态中关联证据；不能把一套凭据/地址的失败推广到另一套配置。
    return hashlib.sha256((base + '\0' + key).encode()).hexdigest()


def native_env(env, key, base):
    # 不继承另一套 Google 凭据/地址。key 来自本机 Claude，base 使用原地址或显式同源入口，
    # 不能仅设置 key 后让原生 CLI 默认把它发给 Google。凭据只进入本机子进程环境。
    out = {k: v for k, v in env.items() if not k.startswith(('GEMINI_', 'GOOGLE_', 'ANTHROPIC_'))}
    out.update(GEMINI_API_KEY=key, GOOGLE_GEMINI_BASE_URL=base)
    return out


def redact(text, key):
    return str(text).replace(key, '[redacted]') if key else str(text)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # models GET 不跟跳转，防止网关把认证 header 带到其他域名。
        return None


def gateway_models(key, base, env, timeout=15):
    u = urllib.parse.urlsplit(base)
    if u.path not in ('', '/') and not env.get('CTHULHU_ANTIGRAVITY_BASE_URL'):
        # 现有 agy 在 base 后附加 /v1beta，不能猜测 /v1、反向代理前缀如何改写。
        raise ValueError('渠道地址含路径，Gemini 模型查询路径尚未验证；请核验同地址的 Gemini 协议')
    proxy_url = env.get('HTTPS_PROXY') or env.get('https_proxy')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({'https': proxy_url} if proxy_url else {}))
    out, cursor = [], None
    for _ in range(10):
        url = base + '/v1beta/models' + ('?' + urllib.parse.urlencode({'pageToken': cursor}) if cursor else '')
        req = urllib.request.Request(url, headers={'x-goog-api-key': key})
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('渠道模型目录过大')
            page = json.loads(raw)
        except urllib.error.HTTPError as e:
            raise ValueError(f'渠道模型查询 HTTP {e.code}；未验证 Gemini 协议或额度') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError('渠道模型查询连接失败；未验证 Gemini 协议或额度') from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError('渠道未返回 Gemini 模型目录 JSON') from None
        if not isinstance(page, dict) or not isinstance(page.get('models'), list):
            raise ValueError('渠道未返回 Gemini models 列表')
        out.extend(m for m in page['models'] if isinstance(m, dict))
        cursor = page.get('nextPageToken')
        if not cursor:
            return out
    raise ValueError('渠道模型目录超过分页上限')


async def catalog(env, cwd=None, timeout=30):
    """目录查询不发推理请求。CLI slug 与渠道目录分开，不能拿官方目录证明账户可用。"""
    info = {'list': [], 'source': SOURCE, 'status': 'unconfigured', 'error': None}
    try:
        key, base = claude_credentials(env, cwd)
        base = effective_base(base, env.get('CTHULHU_ANTIGRAVITY_BASE_URL'))
        info['base_source'] = 'node' if env.get('CTHULHU_ANTIGRAVITY_BASE_URL') else 'claude'
    except ValueError as e:
        info['error'] = str(e)
        return info
    info['_profile'] = credential_profile(key, base)
    child_env = native_env(env, key, base)
    with tempfile.TemporaryDirectory(prefix='cthulhu-agy-models-') as tmp:
        proc = await asyncio.create_subprocess_exec('agy', '--settings', '{"modelProvider":"gemini"}', 'models',
            cwd=tmp, env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        finally:
            if proc.returncode is None:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await proc.wait()
    if proc.returncode:
        raise RuntimeError('agy models 失败：' + redact(stderr.decode(errors='replace'), key)[-250:])
    for line in stdout.decode(errors='replace').splitlines():
        slug, sep, name = line.partition('\t')
        if sep and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', slug):
            info['list'].append({'id': slug, 'name': name[:120], 'availability': 'unverified'})
    if not info['list']:
        raise RuntimeError('agy models 未返回可解析的 slug/名称目录')
    info['status'] = 'unverified'
    try:
        reported = await asyncio.to_thread(gateway_models, key, base, child_env)
        names = {str(m.get('name', '')).removeprefix('models/') for m in reported}
        for model in info['list']:
            # high/medium/low 是原生模型选择档位，网关一般报告不带档位的底层模型名。
            name = re.sub(r'-(high|medium|low)$', '', model['id'])
            model['availability'] = 'listed' if model['id'] in names or name in names else 'unavailable'
        if not any(m['availability'] == 'listed' for m in info['list']):
            info['status'] = 'unavailable'
            info['error'] = '继承 Claude 的渠道目录未报告这些 Gemini 模型；尚不能确认调用能力，需核验 Gemini 协议与额度'
        else:
            info['error'] = '已继承 Claude auth/base_url；渠道列出了模型，推理协议与额度仍未验证'
    except ValueError as e:
        info['error'] = str(e)
    return info
