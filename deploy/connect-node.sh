#!/usr/bin/env bash
# 从连接包所在目录启动；所有状态留在此目录，重启不创建新身份或新 session。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $(uname -s) != Linux ]]; then
  echo '当前执行节点使用 Linux /proc 核验原生进程，请在 Linux 执行机运行。' >&2
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11 或更新版本"'
umask 077
chmod 600 node-token
# 代码可在新目录，状态必须沿用原目录。恢复工具只写路径，不搬运或重置原会话。
node_state_dir=${CTHULHU_NODE_STATE:-}
if [[ -z "$node_state_dir" && -f node-state-path ]]; then IFS= read -r node_state_dir < node-state-path; fi
node_state_dir=${node_state_dir:-.state-node}
if [[ -f node-state-path && ! -f "$node_state_dir/node.db" ]]; then
  echo '固定的原状态目录不可用，请恢复该目录；拒绝创建新节点身份。' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then python3 -m venv .venv; fi
# 已安装固定版本就直接连接，避免每次重启访问执行机失效的私有包源。
if ! .venv/bin/python -c 'from importlib.metadata import version; assert version("websockets") == "15.0.1"' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
fi
# 已接入过的节点不能因目录选错而创建另一份空数据库。连接中心前先做本地只读校验。
.venv/bin/python - "$node_state_dir" <<'CHECK'
import json
from pathlib import Path
import sqlite3
import sys
manifest = Path('node-identity.json')
expected = json.loads(manifest.read_text()).get('instance') if manifest.exists() else None
if expected:
    path = Path(sys.argv[1]).resolve() / 'node.db'
    if not path.is_file():
        raise SystemExit('原节点数据库不存在。请先运行 .venv/bin/python recover-state.py 查找旧状态目录。')
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute("SELECT data FROM objects WHERE kind='settings' AND id='node'").fetchone()
        actual = json.loads(row[0]).get('instance') if row else None
    finally: db.close()
    if actual != expected:
        raise SystemExit(f'当前状态目录身份不匹配：{path.parent}\n原身份：{expected}\n当前身份：{actual}\n请先运行 .venv/bin/python recover-state.py；不要重置 instance。')
CHECK
exec .venv/bin/python -m cthulhu_v4.node \
  --id galaxy-01 --center wss://47.115.202.248:8001/v4/galaxy \
  --token-file node-token --ca center-ca.pem --state "$node_state_dir" --capacity 2
