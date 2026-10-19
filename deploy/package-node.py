"""为已登记的 galaxy-01 更新连接包；保留凭证，包内永不包含节点状态或原生历史。"""
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import sqlite3

ROOT = Path(__file__).resolve().parent.parent
state = ROOT / '.state-demo'
meta = json.loads((state / 'galaxy-01-enrollment.json').read_text())
archive = Path(meta['archive'])
owner = archive.stat()
fd, temporary = tempfile.mkstemp(dir=archive.parent, suffix='.tmp')
os.close(fd)
try:
    with tarfile.open(temporary, 'w:gz') as tar:
        def add(name, content, mode=0o644):
            data = content if isinstance(content, bytes) else content.encode()
            item = tarfile.TarInfo('cthulhu-galaxy-01/' + name)
            item.mode, item.size = mode, len(data)
            tar.addfile(item, io.BytesIO(data))
        for folder, pattern in [('cthulhu_v4', '*.py'), ('skills', '*.md'), ('static', '*')]:
            for path in sorted((ROOT / folder).glob(pattern)):
                if path.is_file(): add(str(path.relative_to(ROOT)), path.read_bytes())
        add('requirements.txt', (ROOT / 'requirements.txt').read_bytes())
        add('node-token', (state / 'node-galaxy-01-token').read_bytes(), 0o600)
        add('center-ca.pem', Path('/etc/cthulhu/tls/ip.crt').read_bytes())
        add('connect.sh', (ROOT / 'deploy/connect-node.sh').read_bytes(), 0o700)
        add('recover-state.py', (ROOT / 'deploy/recover-node.py').read_bytes())
        # 只打包身份校验信息，不打包状态库、事件或原生历史；原节点仍须保有这些文件。
        db = sqlite3.connect((state / 'state.db').resolve().as_uri() + '?mode=ro', uri=True)
        try:
            node = json.loads(db.execute("SELECT data FROM objects WHERE kind='node' AND id='galaxy-01'").fetchone()[0])
            agents = [json.loads(r[0]) for r in db.execute("SELECT data FROM objects WHERE kind='agent'")]
            add('node-identity.json', json.dumps({'instance': node['instance'], 'minimum_event_seq': node['ack'],
                'sessions': {a['id']: a['session'] for a in agents if a.get('node') == node['id'] and a.get('session')}}, ensure_ascii=False))
        finally: db.close()
        add('README.txt', 'Linux / Python 3.11+。运行 bash connect.sh。\n更新时先停止节点，在原目录覆盖代码，保留 .state-node 与原生 CLI 会话目录。\n本连接包含专用凭证，只在原执行机使用，不要分享。\nCodex 使用 --yolo；Claude 使用 --dangerously-skip-permissions。\n')
    os.chmod(temporary, 0o600)
    os.chown(temporary, owner.st_uid, owner.st_gid)
    os.replace(temporary, archive)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
print('已更新连接包；节点凭证未变，未打包任何运行状态。')
