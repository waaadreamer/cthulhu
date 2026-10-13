"""Galaxy: runs on one machine as one OS user and keeps its agents alive.

It connects to the temple, receives the desired set of agents for this galaxy
and reconciles: start missing agent processes, stop removed ones, restart
crashed ones with backoff. Agents inherit this process's OS user.
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
    def __init__(self, spec: dict):
        self.spec = spec
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

        self.provider = ProviderWorker(self.link, config["api_provider"]) if config.get("api_provider") else None
        if self.provider:
            self.link.on_disconnect = self.provider.disconnect

    async def _welcome(self, w: dict) -> None:
        log.info("galaxy %s connected to temple", w.get("name"))
        # Connected on the deployed revision: the start-up guard can stand down.
        (self.state / "deploy_pending.json").unlink(missing_ok=True)
        if self.provider and w.get("api_version") == 1:
            asyncio.create_task(self._register_provider())

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
        elif frame.get("type") == "rpc" and frame["method"] == "probe":
            # A task's progress probe, run as this galaxy's OS user in the executor's working directory.
            p = frame["params"]
            proc = self.procs.get(p["agent_id"])
            return await run_probe(p["command"], self.agent_cwd(proc.spec) if proc else None, float(p.get("timeout_s", 30)))
        elif frame.get("type") == "rpc" and frame["method"] == "read_file":
            # The owner previews a file an agent produced; read as this galaxy's OS user.
            return await asyncio.to_thread(read_file, **frame.get("params", {}))

    async def reconcile(self, wanted: list[dict]) -> None:
        wanted_ids = {a["id"] for a in wanted}
        for aid in list(self.procs):
            if aid not in wanted_ids:
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
        if not p:
            return
        if p.task:
            p.task.cancel()
        await self._kill(p)

    async def _kill(self, p: Proc) -> None:
        if p.process and p.process.returncode is None:
            try:
                os.killpg(p.process.pid, signal.SIGTERM)
                await asyncio.wait_for(p.process.wait(), 10)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(p.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def agent_cwd(self, spec: dict) -> str:
        if spec["spec"].get("worktree"):
            return str(self.state / spec["id"] / "worktree")
        return spec["spec"].get("cwd") or self.config.get("cwd") or str(Path.home())

    async def worktree(self, spec: dict) -> None:
        """Ghroth's old_ones each work on their own branch of the harness repo; only ghroth merges."""
        path, branch = self.agent_cwd(spec), spec["spec"]["worktree"]
        if Path(path).exists():
            return
        repo = self.config.get("harness_repo") or ROOT
        git = ["git", "-C", repo]
        for cmd in (git + ["worktree", "add", "-B", branch, path, "main"],
                    git + ["config", "extensions.worktreeConfig", "true"],
                    ["git", "-C", path, "config", "--worktree", "remote.origin.pushurl", "no-push://old_ones-do-not-push"]):
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(f"{' '.join(cmd)}: {err.decode()[-300:]}")

    async def supervise(self, p: Proc) -> None:
        try:
            while True:
                launch = {k: p.spec[k] for k in ("id", "name", "role", "epoch", "spec", "thread_id")}
                if launch["spec"].get("worktree"):
                    await self.worktree(p.spec)
                    launch["spec"] = launch["spec"] | {"cwd": self.agent_cwd(p.spec)}
                adir = self.state / p.spec["id"]
                adir.mkdir(parents=True, exist_ok=True)
                env = os.environ | {"CTHULHU_AGENT_TOKEN": p.spec["token"], "PYTHONPATH": ROOT}
                p.started_at = time.time()
                with open(adir / "agent.log", "ab") as out:
                    p.process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "cthulhu", "agent", "--galaxy-config", self.config["_path"],
                        "--launch", json.dumps(launch, ensure_ascii=False), env=env,
                        stdout=out, stderr=out, start_new_session=True)
                code = await p.process.wait()
                if time.time() - p.started_at > 120:
                    p.failures = 0
                p.failures += 1
                delay = min(2 ** p.failures, 300)
                log.warning("agent %s exited with %s; restarting in %ss", p.spec["name"], code, delay)
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
        await asyncio.wait([link, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
        for aid in list(self.procs):
            await self.stop(aid)
        if self.link.fatal:
            raise SystemExit(f"temple rejected galaxy: {self.link.fatal}")
