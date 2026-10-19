"""无状态的小工具：常量、ID、时间与进程身份。"""
import secrets
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = {'completed', 'cancelled'}
# 中心与节点的协议版本。不一致直接拒绝连接并提示重跑安装命令；
# 不在旧适配器上“尽量兼容”，避免旧节点用错误的权限策略启动原生会话。
PROTOCOL = 2
MAX_FRAME = 20 * 1024 * 1024
LEASE = 30
LOCAL = 'local'


def uid(prefix):
    return prefix + '_' + secrets.token_hex(4)


def deadline(value):
    d = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('deadline 必须包含时区，如 2026-10-18T18:00:00+08:00')
    return d.timestamp()


def process_identity(pid):
    """PID 可被复用，必须同时核对本次开机 ID 和进程启动时刻。"""
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + fields[19]
    except (OSError, ValueError, IndexError, TypeError):
        return None


def process_alive(record):
    return bool(record and record.get('pid_identity')
                and process_identity(record.get('pid')) == record['pid_identity'])
