"""远程更新：中心把自己正在运行的代码推给星系节点，节点空闲时自动换新并重启。

为什么这样设计：
- 版本以代码内容的摘要标识，而不是版本号：中心与节点代码逐字节一致才算同一版本，
  不会出现“版本号相同、代码不同”的混跑。
- 只更新由安装脚本管理的节点（代码在 ~/.cthulhu/app）。手动从仓库目录启动的节点不改动，
  否则可能覆盖开发中的工作区。
- 只在节点没有任何运行中的回合时更新：中心先停止向该节点派发新回合（消息照常排队不丢），
  等已派发的回合结束再下发更新。原生会话在回合之间本来就是关闭的，重启不会中断会话。
- 换新是目录级原子替换，旧代码保留为 app.old；新代码先在独立进程里试导入，导入失败不替换。
  新进程连上中心后才删除 app.old；连续启动失败时由启动流程回滚。
"""
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path

FEATURE = 'update'
# 进入更新包的文件。测试与状态目录不进包；安装脚本也要随包更新，重跑命令时用的是新脚本。
PATTERNS = ('pyproject.toml', 'requirements.txt', 'README.md', 'AGENTS.md', 'deploy/install.sh',
            'cthulhu_v4/**/*.py', 'skills/*.md', 'static/**/*')
MARKER = 'update.json'
MAX_ATTEMPTS = 3


def files(root):
    root = Path(root)
    out = set()
    for pattern in PATTERNS:
        for p in root.glob(pattern):
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
                out.add(p.relative_to(root).as_posix())
    return sorted(out)


def digest(root):
    """代码摘要：文件名与内容一起参与计算，增删改任何一个文件都会改变它。"""
    h = hashlib.sha256()
    for rel in files(root):
        h.update(rel.encode() + b'\0' + (Path(root) / rel).read_bytes() + b'\0')
    return h.hexdigest()[:16]


def shape(root):
    """影响安装方式的部分：依赖、包列表、命令入口。它们变化时只换代码不够，必须重跑安装命令。"""
    try:
        data = tomllib.loads((Path(root) / 'pyproject.toml').read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get('project', {})
    return {'dependencies': sorted(project.get('dependencies', [])), 'scripts': project.get('scripts', {}),
            'packages': sorted(data.get('tool', {}).get('setuptools', {}).get('packages', []))}


def commit(root):
    try:
        out = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--', *PATTERNS], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() + ('+' if dirty.stdout.strip() else '') if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


class Release:
    """中心当前运行代码的更新包。启动时打包一次：之后磁盘上的代码再变，也不会推给节点未运行过的版本。"""

    def __init__(self, root):
        self.root = Path(root)
        self.code = digest(self.root)
        self.commit = commit(self.root)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            for rel in files(self.root):
                tar.add(self.root / rel, arcname=rel, recursive=False)
            manifest = json.dumps({'code': self.code, 'commit': self.commit, 'shape': shape(self.root)}).encode()
            info = tarfile.TarInfo('.cthulhu-release.json'); info.size = len(manifest); info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(manifest))
        self.blob = buf.getvalue()
        self.sha256 = hashlib.sha256(self.blob).hexdigest()
        # 中心目录有未提交的改动（例如另一个会话正在开发）时不向节点推送：
        # 自动更新只分发明确提交过的版本，避免把半成品带到所有集群。不是 git 仓库时不做此判断。
        self.clean = not (self.commit or '').endswith('+')

    def offer(self):
        return {'code': self.code, 'commit': self.commit, 'sha256': self.sha256, 'size': len(self.blob)}

    def download(self):
        return {'code': self.code, 'sha256': self.sha256, 'content': base64.b64encode(self.blob).decode()}

    def public(self):
        return {'code': self.code, 'commit': self.commit, 'clean': self.clean}


# ---- 节点侧 ----
def _extract(blob, dest):
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tar:
        if hasattr(tarfile, 'data_filter'):
            tar.extractall(dest, filter='data')
            return
        # 旧版 Python 没有 data 过滤器：手动拒绝绝对路径、上跳路径、链接与设备文件。
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not (m.isfile() or m.isdir()) or not target.is_relative_to(dest.resolve()):
                raise ValueError('更新包包含不安全的路径：' + m.name)
        tar.extractall(dest)


def install(app, payload, expect):
    """校验并换新代码。成功返回旧代码目录；任何一步失败都不动现有代码。"""
    app = Path(app)
    blob = base64.b64decode(payload['content'])
    if hashlib.sha256(blob).hexdigest() != expect['sha256'] or payload.get('code') != expect['code']:
        raise ValueError('更新包校验失败（传输损坏或中心代码已变化），本次不更新')
    new, old = app.with_name(app.name + '.new'), app.with_name(app.name + '.old')
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(parents=True)
    try:
        _extract(blob, new)
        manifest = json.loads((new / '.cthulhu-release.json').read_text())
        if digest(new) != manifest['code'] or manifest['code'] != expect['code']:
            raise ValueError('解包后的代码与中心版本不一致，本次不更新')
        if manifest.get('shape') != shape(app):
            raise ValueError('依赖或命令入口有变化，自动更新只换代码；请在该机器重跑安装命令')
        # 独立进程试导入新代码：语法错误或缺模块时直接放弃，节点继续用旧代码运行。
        probe = subprocess.run(
            [sys.executable, '-c', 'import cthulhu_v4, cthulhu_v4.cli, cthulhu_v4.node, cthulhu_v4.runtime, cthulhu_v4.bridge; print(cthulhu_v4.__file__)'],
            cwd=new, env=dict(os.environ, PYTHONPATH=str(new)), capture_output=True, text=True, timeout=60)
        if probe.returncode != 0 or not Path(probe.stdout.strip()).resolve().is_relative_to(new.resolve()):
            raise ValueError('新代码试导入失败：' + (probe.stderr.strip().splitlines() or ['未知错误'])[-1])
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    # 保留 git 元数据：之后重跑安装命令仍可按 git 更新。
    if (app / '.git').exists():
        os.rename(app / '.git', new / '.git')
    shutil.rmtree(old, ignore_errors=True)
    os.rename(app, old)
    os.rename(new, app)
    return old


def mark(base, previous, current, old):
    (Path(base) / MARKER).write_text(json.dumps({'from': previous, 'to': current, 'old': str(old), 'attempts': 0, 'at': time.time()}))


def confirm(base):
    """新代码已连上中心：更新生效，清理旧代码。"""
    marker = Path(base) / MARKER
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    shutil.rmtree(info.get('old', ''), ignore_errors=True) if info.get('old') else None
    marker.unlink(missing_ok=True)
    return info


def check_rollback(base, app):
    """每次节点启动时调用。更新后连续多次没能连上中心，就换回旧代码。返回 True 表示已回滚、需要重新执行。"""
    marker = Path(base) / MARKER
    try:
        info = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    info['attempts'] = info.get('attempts', 0) + 1
    old = Path(info.get('old', ''))
    if info['attempts'] <= MAX_ATTEMPTS or not old.is_dir():
        marker.write_text(json.dumps(info))
        return False
    app = Path(app)
    failed = app.with_name(app.name + '.failed')
    shutil.rmtree(failed, ignore_errors=True)
    os.rename(app, failed)
    os.rename(old, app)
    marker.unlink(missing_ok=True)
    (Path(base) / 'update-rollback.json').write_text(json.dumps(info | {'rolled_back_at': time.time()}))
    return True


def reexec(app):
    """用同一组参数重新启动节点进程。PID 不变，systemd 与 nohup 都能继续管理它。

    先切到代码目录（按路径重新打开）：如果当前目录在旧代码里，换新后它已经变成 app.old，
    `-m` 会从当前目录导入，重启后跑的仍是旧代码。
    """
    os.chdir(app)
    os.execv(sys.executable, [sys.executable, '-m', 'cthulhu_v4', *sys.argv[1:]])
