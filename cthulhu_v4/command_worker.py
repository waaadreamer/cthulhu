"""单次命令的进程看守；stdin 是节点的生命线，绝不传给用户命令。

为什么单独看守：节点被 SIGKILL 时 asyncio 的 finally 不会运行。生命线 EOF 仍让此进程
清理自己创建的命令进程组；它从不扫描/终止节点的 agent 或其他命令进程。
此文件直接由 Python 执行，不导入应用、凭证或模型运行时。
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def main():
    raw = sys.stdin.buffer.readline()
    if not raw:
        return 125  # 节点尚未交付命令便退出，没有启动任何用户进程。
    spec = json.loads(raw)
    stop = False

    def stopped(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    proc, outcome, error, code = None, 'failed', '', None
    started = time.time()
    try:
        # 新 session 的组只属于这次 shell；即使命令派生后台子进程，也能一起收尾。
        proc = subprocess.Popen(['/bin/sh', '-c', spec['script']], cwd=spec['cwd'],
                                stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + spec['timeout']
        while True:
            exited = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if exited:
                code = exited.si_status if exited.si_code == os.CLD_EXITED else -exited.si_status
                outcome = 'completed'; break
            if stop:
                outcome = 'cancelled'; break
            if time.monotonic() >= end:
                outcome = 'timed_out'; break
            ready, _, _ = select.select([sys.stdin.buffer], [], [], .05)
            if ready:
                command = os.read(sys.stdin.fileno(), 1)
                if command == b'C':
                    outcome = 'cancelled'; break
                if not command:
                    outcome, error = 'unknown', '节点生命线关闭；已停止命令组，已有副作用须核验'
                    break
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    finally:
        if proc:
            # 即便 shell 正常返回，也清掉它留下的后台进程，不能把一次性命令变成无人管理的服务。
            # waitid(WNOWAIT) 暂不回收 shell：即使它已退出且组内没有孩子，PID/PGID 也不会
            # 在两次 killpg 之间被无关进程重用；清组完成后才 wait() 回收。
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            time.sleep(.2)
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
            if code is None:
                code = proc.returncode
        result = dict(state=outcome, exit_code=code, started_at=started,
                      finished_at=time.time(), error=error)
        path = Path(spec['result'])
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(result)); os.replace(tmp, path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
