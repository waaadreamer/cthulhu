"""Galaxy: runs on one machine as one OS user and keeps its agents alive.

It connects to the temple, receives the desired set of agents for this galaxy
and reconciles: start missing agent processes, stop removed ones, restart
crashed ones with backoff. Agents inherit this process's OS user.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .protocol import Link

log = logging.getLogger(__name__)

ROOT = str(Path(__file__).resolve().parent.parent)


class Proc:
    def __init__(self, spec: dict):
        self.spec = spec
        self.process: asyncio.subprocess.Process | None = None
        self.failures = 0
        self.started_at = 0.0
        self.task: asyncio.Task | None = None


class Galaxy:
    def __init__(self, config: dict):
        self.config = config
        self.id = config["id"]
        self.state = Path(config["state"])
        self.state.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, Proc] = {}
        self.link = Link(config["temple_ws"], {"kind": "galaxy", "id": self.id, "token": config["token"]},
                         self._welcome, self._frame, tls_pin=config.get("tls_pin"))

    async def _welcome(self, w: dict) -> None:
        log.info("galaxy %s connected to temple", w.get("name"))

    async def _frame(self, frame: dict) -> None:
        if frame.get("type") == "desired":
            await self.reconcile(frame["agents"])

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

    async def supervise(self, p: Proc) -> None:
        try:
            while True:
                launch = {k: p.spec[k] for k in ("id", "name", "role", "epoch", "spec", "thread_id")}
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
