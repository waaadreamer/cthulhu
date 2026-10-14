"""Galaxy: runs on one machine as one OS user and keeps its agents alive.

It connects to the temple and hosts two kinds of agents:
  - gods the temple wants here (desired set, reconciled from the temple), and
  - old_ones owned by this galaxy's hub (hub.py), which connect to the hub only.
Crashed agents restart with backoff. Agents inherit this process's OS user, unless
`agent_user` names another one (azathoth's galaxy: the galaxy may sudo, the agent may not).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import BUILD
from .protocol import Link
from .api_provider import ProviderWorker

log = logging.getLogger(__name__)

ROOT = str(Path(__file__).resolve().parent.parent)


class Proc:
    def __init__(self, spec: dict, hub: bool = False):
        self.spec = spec
        self.hub = hub
        self.process: asyncio.subprocess.Process | None = None
        self.failures = 0
        self.started_at = 0.0
        self.task: asyncio.Task | None = None


DIR_LIMIT = 500


def read_file(path: str, max_bytes: int = 0, offset: int = 0) -> dict:
    """Stat (max_bytes=0) or read a file; list a directory. Paths must be absolute."""
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("path must be absolute")
    try:
        st = p.stat()
    except OSError as e:
        return {"exists": False, "error": e.strerror or str(e)}
    out = {"exists": True, "path": str(p), "size": st.st_size, "mtime": st.st_mtime, "is_dir": p.is_dir()}
    if out["is_dir"]:
        entries = []
        try:
            for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))[:DIR_LIMIT]:
                try:
                    es = e.stat()
                    entries.append({"name": e.name, "is_dir": e.is_dir(), "size": es.st_size, "mtime": es.st_mtime})
                except OSError:
                    entries.append({"name": e.name, "is_dir": False, "size": None, "mtime": None})
        except OSError as e:
            out["error"] = e.strerror or str(e)
        out["entries"] = entries
    elif max_bytes:
        try:
            with open(p, "rb") as f:
                f.seek(offset)
                data = f.read(max_bytes)
        except OSError as e:
            out["error"] = e.strerror or str(e)
            return out
        out["data"] = base64.b64encode(data).decode()
        out["truncated"] = offset + len(data) < st.st_size
    return out


async def run_probe(command: str, cwd: str | None, limit: float) -> dict:
    proc = await asyncio.create_subprocess_shell(command, cwd=cwd if cwd and Path(cwd).is_dir() else None,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                 start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return {"code": None, "stdout": "", "stderr": f"timed out after {limit:g}s"}
    return {"code": proc.returncode, "stdout": out.decode(errors="replace")[-200:], "stderr": err.decode(errors="replace")[-200:]}


async def run_root(command: str, cwd: str | None, limit: float) -> dict:
    proc = await asyncio.create_subprocess_exec("sudo", "-n", "--", "bash", "-c", command,
                                                cwd=cwd if cwd and Path(cwd).is_dir() else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), limit)
    except asyncio.TimeoutError:
        os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        return {"code": None, "stdout": "", "stderr": f"timed out after {limit:g}s"}
    return {"code": proc.returncode, "stdout": out.decode(errors="replace")[-12000:], "stderr": err.decode(errors="replace")[-4000:]}


class Galaxy:
    def __init__(self, config: dict):
        self.config = config
        self.id = config["id"]
        self.state = Path(config["state"])
        self.state.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, Proc] = {}
        from .deploy import local_info
        self.link = Link(config["temple_ws"], {"kind": "galaxy", "id": self.id, "token": config["token"],
                                                "capabilities": ["galaxy_api_v1", "galaxy_api_claude_v1", "deploy_v1", "deploy_recovery_v1"], "version": BUILD,
                                                "deploy": local_info()},
                         self._welcome, self._frame, tls_pin=config.get("tls_pin"))

        # Agents read this copy: no galaxy token, so an agent cannot impersonate its galaxy.
        self.agent_config = self.state / "agent-config.json"
        self.agent_config.write_text(json.dumps({k: v for k, v in config.items() if k not in ("token", "api_provider")},
                                                ensure_ascii=False))
        os.chmod(self.agent_config, 0o644)
        self.provider = ProviderWorker(self.link, config["api_provider"]) if config.get("api_provider") else None
        if self.provider:
            self.link.on_disconnect = self.provider.disconnect
        self.hub = None
        if config.get("hub", True):
            from .hub import Hub
            self.hub = Hub(self)
            self.link.hello["capabilities"].append("hub_v1")

    async def _welcome(self, w: dict) -> None:
        log.info("galaxy %s connected to temple", w.get("name"))
        # Connected on the deployed revision: the start-up guard can stand down.
        (self.state / "deploy_pending.json").unlink(missing_ok=True)
        if self.provider and w.get("api_version") == 1:
            asyncio.create_task(self._register_provider())
        if self.hub:
            asyncio.create_task(self.hub.on_temple())

    async def _register_provider(self):
        try:
            from . import BUILD
            await self.provider.register(BUILD)
        except Exception:
            log.warning("API provider registration unavailable")

    async def _frame(self, frame: dict):
        if frame.get("type") in ("api_request", "api_cancel"):
            if self.provider:
                await self.provider.frame(frame)
            return
        if frame.get("type") == "desired":
            await self.reconcile(frame["agents"])
        elif frame.get("type") in ("bindings", "runtime") and self.hub:
            await self.hub.frame(frame)
        elif frame.get("type") == "rpc" and frame["method"].startswith("hub_"):
            if not self.hub:
                raise ValueError("this galaxy has no hub")
            return await self.hub.rpc(frame["method"], frame.get("params") or {})
        elif frame.get("type") == "rpc" and frame["method"] == "sudo_exec":
            # Only on a galaxy configured for it, and only for a command the owner approved in the temple.
            if not self.config.get("sudo_exec"):
                raise ValueError("this galaxy does not execute sudo commands")
            p = frame["params"]
            return await run_root(p["command"], p.get("cwd"), float(p.get("limit") or 600))
        elif frame.get("type") == "rpc" and frame["method"] == "api_canary":
            from .api_canary import run
            return await run(self.link, frame.get("params", {}).get("provider", "ali"), mode="text")
        elif frame.get("type") == "rpc" and frame["method"] == "deploy_update":
            # Only a revision on the repository's main branch, fast-forward, clean tree (see deploy.update_code).
            from .deploy import ROOT, update_code
            return await update_code(ROOT, str(frame["params"]["revision"]))
        elif frame.get("type") == "rpc" and frame["method"] == "deploy_rollback":
            from .deploy import ROOT, git, rollback
            p = frame["params"]
            current = await git(ROOT, "rev-parse", "HEAD")
            if current not in (p["prev"], p["target"]):
                raise RuntimeError("checkout changed since deployment; refusing rollback")
            await rollback(ROOT, p["prev"])
            return {"revision": p["prev"]}
        elif frame.get("type") == "rpc" and frame["method"] == "deploy_restart":
            p = frame.get("params", {})
            if p.get("guard"):
                (self.state / "deploy_pending.json").write_text(json.dumps({"prev": p["prev"], "target": p["target"]}))
            # Exit; systemd (Restart=always) or run.sh brings the galaxy back on the new code.
            asyncio.get_running_loop().call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)
            return {"restarting": True}
        elif frame.get("type") == "rpc" and frame["method"] == "read_file":
            # The owner previews a file an agent produced; read as this galaxy's OS user.
            return await asyncio.to_thread(read_file, **frame.get("params", {}))
        elif frame.get('type') == 'rpc' and frame['method'] == 'agent_stopped':
            aid = frame['params']['agent_id']
            return {'agent_id': aid, 'stopped': aid not in self.procs}

    def launch_hub_agent(self, spec: dict) -> None:
        p = self.procs.get(spec["id"])
        if p and p.spec["epoch"] == spec["epoch"]:
            return
        self.stop_hub_agent(spec["id"])
        p = self.procs[spec["id"]] = Proc(spec, hub=True)
        p.task = asyncio.create_task(self.supervise(p))

    def stop_hub_agent(self, aid: str) -> None:
        p = self.procs.get(aid)
        if p and p.hub:
            self.procs.pop(aid)
            asyncio.create_task(self.retire(p))

    async def reconcile(self, wanted: list[dict]) -> None:
        wanted_ids = {a["id"] for a in wanted}
        for aid in list(self.procs):
            if aid not in wanted_ids and not self.procs[aid].hub:
                await self.stop(aid)
        for spec in wanted:
            p = self.procs.get(spec["id"])
            if p and p.spec["epoch"] != spec["epoch"]:
                await self.stop(spec["id"])
                p = None
            if p is None:
                p = self.procs[spec["id"]] = Proc(spec)
                p.task = asyncio.create_task(self.supervise(p))
            else:
                p.spec = spec

    async def stop(self, aid: str) -> None:
        p = self.procs.pop(aid, None)
        if p:
            await self.retire(p)

    async def retire(self, p: Proc) -> None:
        if p.task:
            p.task.cancel()
            await asyncio.gather(p.task, return_exceptions=True)
        await self._kill(p)

    async def _kill(self, p: Proc) -> None:
        if p.process and p.process.returncode is None:
            try:
                await self.signal_group(p, signal.SIGTERM)
                await asyncio.wait_for(p.process.wait(), 10)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    await self.signal_group(p, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def signal_group(self, p: Proc, sig: int) -> None:
        if self.config.get("agent_user") and not p.hub:
            # The group belongs to another user (via sudo): signal it with sudo too.
            proc = await asyncio.create_subprocess_exec("sudo", "-n", "kill", f"-{int(sig)}", "--", f"-{p.process.pid}",
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            return
        os.killpg(p.process.pid, sig)

    def agent_cwd(self, spec: dict) -> str:
        return spec["spec"].get("cwd") or self.config.get("cwd") or str(Path.home())

    async def supervise(self, p: Proc) -> None:
        try:
            while True:
                launch = {k: p.spec[k] for k in ("id", "name", "role", "epoch", "spec", "thread_id") if k in p.spec}
                if p.spec.get("hub_url"):
                    launch["hub_url"] = p.spec["hub_url"]
                adir = self.state / p.spec["id"]
                adir.mkdir(parents=True, exist_ok=True)
                env = os.environ | {"CTHULHU_AGENT_TOKEN": p.spec["token"], "PYTHONPATH": ROOT}
                argv = [sys.executable, "-m", "cthulhu", "agent", "--galaxy-config", str(self.agent_config),
                        "--launch", json.dumps(launch, ensure_ascii=False)]
                user = self.config.get("agent_user")
                if user and not p.hub:
                    # The agent runs as an unprivileged user; root goes only through approved sudo_exec.
                    env |= {"HOME": self.config.get("agent_home") or str(adir)}
                    argv = ["sudo", "-n", "-E", "-u", user, "--"] + argv
                    os.chmod(adir, 0o777)
                p.started_at = time.time()
                with open(adir / "agent.log", "ab") as out:
                    p.process = await asyncio.create_subprocess_exec(*argv, env=env, stdout=out, stderr=out, cwd=ROOT,
                                                                    start_new_session=True)
                code = await p.process.wait()
                if time.time() - p.started_at > 120:
                    p.failures = 0
                p.failures += 1
                delay = min(2 ** p.failures, 300)
                log.warning("agent %s exited with %s; restarting in %ss", p.spec["name"], code, delay)
                if self.hub and p.hub:
                    self.hub.status[p.spec["id"]] = {"state": "crashed", "current": f"退出码 {code}"}
                    asyncio.create_task(self.hub.push_roster())
                else:
                    try:
                        await self.link.call("agent_exited", timeout=5, agent_id=p.spec["id"], reason=f"退出码 {code}")
                    except Exception:
                        pass
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            await self._kill(p)
            raise

    async def main(self) -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        link = asyncio.create_task(self.link.run())
        waits = [link, asyncio.create_task(stop.wait())]
        if self.hub:
            waits.append(asyncio.create_task(self.hub.serve()))
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        for aid in list(self.procs):
            await self.stop(aid)
        if self.link.fatal:
            raise SystemExit(f"temple rejected galaxy: {self.link.fatal}")
