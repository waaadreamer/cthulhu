"""只读寻找原节点数据库，再固定状态目录；不改 instance、不复制数据库或会话。"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time


SKIP = {'.git', '.venv', 'node_modules', '.cache', '__pycache__', '.codex', '.claude', '.npm'}


def inspect(path, expected):
    """UUID 相同还不够：旧会话和已确认事件也必须存在，避免选到过期备份。"""
    db = None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
        row = db.execute("SELECT data FROM objects WHERE kind='settings' AND id='node'").fetchone()
        if not row or json.loads(row[0]).get('instance') != expected['instance']:
            return False
        for aid, session in expected.get('sessions', {}).items():
            row = db.execute("SELECT data FROM objects WHERE kind='agent' AND id=?", (aid,)).fetchone()
            if not row or json.loads(row[0]).get('session') != session:
                return False
        highwater = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
        return highwater >= expected.get('minimum_event_seq', 0)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        return False
    finally:
        if db: db.close()


def find_states(roots, expected, *, seconds=30):
    found, visited = set(), set()
    until = time.monotonic() + seconds
    for root in roots:
        root = root.expanduser().resolve()
        for current, dirs, files in os.walk(root, followlinks=False):
            if time.monotonic() > until:
                raise RuntimeError('搜索时间超过 30 秒；请用 --root 指定原安装目录后重试')
            p = Path(current)
            if p in visited:
                dirs[:] = []
                continue
            visited.add(p)
            dirs[:] = [d for d in dirs if d not in SKIP]
            if 'node.db' in files and inspect(p / 'node.db', expected):
                found.add(p.resolve())
    return sorted(found)


def pin_state(directory, target):
    # 必须先停止旧节点。只固定路径，不修改原数据库；最终启动仍由 node.lock 排他保护。
    with (directory / 'node.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError('原节点仍在运行，请先 Ctrl+C 停止它')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(str(directory.resolve()) + '\n')
        temporary.chmod(0o600)
        temporary.replace(target)


def main():
    p = argparse.ArgumentParser(description='寻找原节点状态，保留原 session 和事件发件箱')
    p.add_argument('--root', action='append', type=Path, help='原安装目录或其上层，可重复')
    args = p.parse_args()
    package = Path(__file__).resolve().parent
    expected = json.loads((package / 'node-identity.json').read_text())
    if not expected.get('instance'):
        raise SystemExit('中心尚未保存节点身份，无需执行恢复；首次接入请运行 connect.sh')
    roots = args.root or [package.parent, Path.home()]
    print('查找原节点身份：' + expected['instance'], flush=True)
    matches = find_states(roots, expected)
    if len(matches) != 1:
        for m in matches: print('匹配目录：' + str(m))
        raise SystemExit('未找到唯一且完整的旧状态目录。请用 --root /原安装目录 缩小搜索；不要修改 instance 或删除数据库。')
    pin_state(matches[0], package / 'node-state-path')
    print('已固定原状态目录：' + str(matches[0]))
    print('原会话指针与已确认事件记录校验通过。现在运行 bash connect.sh；连接成功后再恢复原任务。')


if __name__ == '__main__':
    main()
