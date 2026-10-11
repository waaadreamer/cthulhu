"""One-click deployment of a pushed revision to the temple and its galaxies.

Code is shared per (host, code root): on the temple's machine the temple and
the local galaxies all run from one checkout. A deploy job, for each such
group:

  1. drains every galaxy of the group (running turns finish, nothing new starts),
  2. updates the checkout to the target revision (fast-forward only, must be on
     the repository's main branch, clean tree), reinstalls dependencies when
     pyproject.toml changed and import-checks the new code; any failure rolls
     the checkout back,
  3. restarts the galaxies (they exit and their supervisor - systemd or run.sh -
     starts them again on the new code) and waits for them to report it,
  4. undrains. If the temple shares the checkout it restarts itself last.

A galaxy that fails to come back is rolled back: by the temple when it owns the
checkout, otherwise by the galaxy's own start-up guard (see __main__.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
PENDING = "deploy_pending.json"          # in a galaxy's state dir while a restart is unconfirmed
DRAIN_TIMEOUT = 30 * 60
RESTART_TIMEOUT = 5 * 60
MAX_JOBS = 20


async def git(root: Path, *args: str, check: bool = True) -> str:
    p = await asyncio.create_subprocess_exec(
        "git", "-c", f"safe.directory={root}", *args, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(p.communicate(), 300)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(err or out).decode(errors='replace').strip()[:500]}")
    return out.decode().strip()


def local_info(root: Path = ROOT) -> dict:
    """What a process tells the temple about its checkout."""
    import subprocess
    try:
        rev = subprocess.run(["git", "-c", f"safe.directory={root}", "rev-parse", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        rev = ""
    return {"host": socket.gethostname(), "root": str(root), "revision": rev,
            "writable": os.access(root / ".git", os.W_OK) if (root / ".git").exists() else False}


async def update_code(root: Path, revision: str, python: str = sys.executable) -> dict:
    """Move the checkout at `root` to `revision`. Returns {"prev", "revision", "deps"}; raises with the reason."""
    root = Path(root)
    if await git(root, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("工作区有未提交的修改，拒绝部署")
    prev = await git(root, "rev-parse", "HEAD")
    await git(root, "fetch", "-q", "origin", "main")
    revision = await git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if not await _is_ancestor(root, revision, "FETCH_HEAD"):
        raise RuntimeError(f"{revision[:12]} 不在仓库 main 分支上")
    if prev == revision:
        return {"prev": prev, "revision": revision, "deps": False, "unchanged": True}
    if not await _is_ancestor(root, prev, revision):
        raise RuntimeError(f"当前版本 {prev[:12]} 不是 {revision[:12]} 的祖先（本地有未推送的提交或目标更旧）")
    deps = bool(await git(root, "diff", "--name-only", prev, revision, "--", "pyproject.toml"))
    await git(root, "merge", "-q", "--ff-only", revision)
    try:
        if deps:
            await _run(python, "-m", "pip", "install", "-q", "-e", str(root), cwd=root, timeout=900)
        await _run(python, "-c", "import cthulhu.cli, cthulhu.galaxy, cthulhu.agent, cthulhu.gods, cthulhu.temple",
                   cwd=root, env={"PYTHONPATH": str(root)}, timeout=120)
    except Exception:
        await rollback(root, prev)
        raise
    return {"prev": prev, "revision": revision, "deps": deps}


async def rollback(root: Path, prev: str) -> None:
    await git(Path(root), "reset", "-q", "--keep", prev)


async def _is_ancestor(root: Path, a: str, b: str) -> bool:
    p = await asyncio.create_subprocess_exec("git", "-c", f"safe.directory={root}", "merge-base", "--is-ancestor",
                                             a, b, cwd=root)
    return await p.wait() == 0


async def _run(*cmd: str, cwd: Path, env: dict | None = None, timeout: float = 300) -> None:
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=os.environ | (env or {}),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(p.communicate(), timeout)
    if p.returncode:
        raise RuntimeError(f"{Path(cmd[0]).name} {' '.join(cmd[1:3])} 失败：{out.decode(errors='replace')[-800:]}")


class Deployer:
    """Runs deploy jobs inside the temple. Jobs are persisted in the `deploy_jobs` setting."""

    def __init__(self, temple):
        self.t = temple
        cfg = temple.config.get("deploy", {})
        self.root = Path(cfg.get("code_root") or ROOT)
        self.repo = cfg.get("repo")
        self.me = local_info(self.root)
        self.running: dict[str, asyncio.Task] = {}
        self.requests: dict[str, dict] = {}      # ghroth's deploy requests awaiting azathoth

    # bookkeeping

    def jobs(self) -> list[dict]:
        return self.t.store.setting("deploy_jobs") or []

    def job(self, job_id: str) -> dict | None:
        return next((j for j in self.jobs() if j["id"] == job_id), None)

    async def save(self, job: dict) -> None:
        jobs = [j for j in self.jobs() if j["id"] != job["id"]] + [job]
        self.t.store.set_setting("deploy_jobs", jobs[-MAX_JOBS:])
        await self.t.broadcast({"type": "deploy", "job": job})

    async def step(self, job: dict, text: str, galaxy: str | None = None) -> None:
        log.info("deploy %s: %s %s", job["id"], galaxy or "", text)
        job["steps"].append({"at": time.time(), "galaxy": galaxy, "text": text})
        await self.save(job)

    def status(self) -> dict:
        return {"temple": self.me, "galaxies": {gid: c.deploy for gid, c in self.t.galaxies.items()},
                "jobs": self.jobs()[::-1], "requests": list(self.requests.values())}

    async def latest(self) -> str:
        """The head of main in the repository."""
        repo = self.repo or await git(self.root, "remote", "get-url", "origin")
        out = await git(self.root, "ls-remote", repo, "refs/heads/main")
        if not out:
            raise RuntimeError("无法读取仓库 main 分支")
        return out.split()[0]

    # groups

    def groups(self, galaxy_ids: list[str]) -> list[dict]:
        """Requested galaxies grouped by shared checkout; each group pulls in its other members."""
        return self._groups(galaxy_ids)

    def _groups(self, galaxy_ids: list[str]) -> list[dict]:
        conns = self.t.galaxies
        key = lambda d: (d.get("host"), d.get("root"))
        out: dict[tuple, dict] = {}
        for gid in galaxy_ids:
            c = conns.get(gid)
            if gid == "__temple__":
                continue
            if c is None:
                out[("offline", gid)] = {"key": None, "galaxies": [gid], "error": "星系离线"}
                continue
            if not c.deploy:
                out[("legacy", gid)] = {"key": None, "galaxies": [gid], "error": "星系版本太旧，不支持远程部署，需要现场升级一次"}
                continue
            k = key(c.deploy)
            g = out.setdefault(k, {"key": k, "galaxies": [], "temple": k == key(self.me)})
            for other, oc in conns.items():
                if oc.deploy and key(oc.deploy) == k and other not in g["galaxies"]:
                    g["galaxies"].append(other)
        # The temple's own checkout is updated even if none of its galaxies were named explicitly.
        if "__temple__" in galaxy_ids or not galaxy_ids:
            k = key(self.me)
            out.setdefault(k, {"key": k, "galaxies": [g for g, c in conns.items() if c.deploy and key(c.deploy) == k],
                               "temple": True})
        return list(out.values())

    # jobs

    async def start(self, galaxies: list[str] | str, by: str, reason: str = "", task_id: str | None = None,
                    revision: str | None = None) -> dict:
        if any(not t.done() for t in self.running.values()) or \
                any(j["state"] in ("running", "restarting_temple") for j in self.jobs()):
            raise ValueError("已有部署在进行中")
        everything = galaxies in ("all", ["all"])
        if everything:
            ids = [g["id"] for g in self.t.store.galaxies()] + ["__temple__"]
        else:
            ids = [galaxies] if isinstance(galaxies, str) else list(galaxies)
        target = revision or await self.latest()
        job = {"id": f"deploy_{int(time.time())}", "by": by, "reason": reason, "task_id": task_id,
               "revision": target, "state": "running", "created_at": time.time(), "finished_at": None,
               "groups": [{"galaxies": g["galaxies"], "temple": g.get("temple", False),
                           # "all" skips galaxies that cannot take part (offline / too old) instead of failing.
                           "state": "skipped" if everything and g.get("error") else "pending",
                           "error": g.get("error")} for g in self.groups(ids)], "steps": []}
        # The temple's group goes first (new temple stays compatible with old galaxies); the job resumes
        # in the restarted temple process.
        job["groups"].sort(key=lambda g: not g["temple"])
        await self.save(job)
        await self.step(job, f"目标版本 {target[:12]}")
        for g in job["groups"]:
            if g["state"] == "skipped":
                await self.step(job, f"跳过：{g['error']}", ",".join(g["galaxies"]))
        self.running[job["id"]] = asyncio.create_task(self.run(job))
        return job

    async def run(self, job: dict) -> None:
        try:
            for rec in job["groups"]:
                if rec["state"] != "pending":
                    continue
                label = ",".join(rec["galaxies"]) or "temple"
                if rec.get("error"):
                    rec["state"] = "failed"
                    await self.step(job, rec["error"], label)
                    continue
                try:
                    rec["state"] = await self.deploy_group(job, rec)
                except Exception as e:
                    rec["state"], rec["error"] = "failed", str(e)
                    await self.step(job, f"失败：{e}", label)
                await self.save(job)
                if rec["state"] == "restarting_temple":
                    return          # continued by resume() in the new temple process
            await self.finish(job)
        except Exception as e:
            log.exception("deploy job crashed")
            job["state"] = "failed"
            await self.step(job, f"部署中断：{e}")
            await self.finish(job)

    async def deploy_group(self, job: dict, g: dict) -> str:
        label = ",".join(g["galaxies"]) or "temple"
        gids = g["galaxies"]
        await self.wait_online(gids)
        for gid in gids:
            await self.t.maintenance_state(gid, True)
        try:
            if gids:
                await self.step(job, "排空中：等待正在进行的回合、记忆整理和巡检结束", label)
                await self.wait_drained(gids)
                await self.step(job, "已排空", label)
            updater = None if g["temple"] else next(
                (gid for gid in gids if self.t.galaxies[gid].deploy.get("writable")), None)
            if not g["temple"] and updater is None:
                raise RuntimeError("这组星系都没有写代码目录的权限")
            if g["temple"]:
                r = await update_code(self.root, job["revision"])
            else:
                r = await self.t.galaxies[updater].rpc("deploy_update", timeout=1200, revision=job["revision"])
            if r.get("unchanged"):
                await self.step(job, "已经是目标版本，无需更新", label)
                return "done"
            await self.step(job, f"代码已更新 {r['prev'][:12]} → {r['revision'][:12]}" +
                            ("，依赖已重装" if r.get("deps") else ""), label)
            if gids and not await self.restart_galaxies(job, gids, r, owner_rolls_back=g["temple"], updater=updater):
                if g["temple"]:
                    await rollback(self.root, r["prev"])
                    await self.step(job, f"已回滚到 {r['prev'][:12]}，星系会以旧版本自动重启", label)
                return "failed"
        finally:
            for gid in gids:
                await self.t.maintenance_state(gid, False)
        if g["temple"]:
            g["state"] = job["state"] = "restarting_temple"
            await self.step(job, "中心神殿即将重启以加载新版本（只断线重连，不中断回合）", "temple")
            asyncio.get_running_loop().call_later(1, os.kill, os.getpid(), signal.SIGTERM)
            return "restarting_temple"
        return "done"

    async def wait_online(self, gids: list[str], timeout: float = 90) -> None:
        end = time.time() + timeout
        while True:
            missing = [gid for gid in gids if not (self.t.galaxies.get(gid) and self.t.galaxies[gid].deploy)]
            if not missing:
                return
            if time.time() > end:
                raise RuntimeError(f"星系离线或不支持远程部署：{', '.join(missing)}")
            await asyncio.sleep(3)

    async def wait_drained(self, gids: list[str]) -> None:
        end = time.time() + DRAIN_TIMEOUT
        while True:
            states = [await self.t.maintenance_state(gid) for gid in gids]
            if all(s["safe_to_restart"] for s in states):
                return
            if time.time() > end:
                busy = [a["id"] for s in states for a in s["agents"] if not a.get("safe")]
                raise RuntimeError(f"排空超时（{DRAIN_TIMEOUT // 60} 分钟），仍未空闲：{', '.join(busy)}")
            await asyncio.sleep(5)

    async def restart_galaxies(self, job: dict, gids: list[str], r: dict, owner_rolls_back: bool,
                               updater: str | None) -> bool:
        for gid in gids:
            conn = self.t.galaxies.get(gid)
            if conn:
                await conn.rpc("deploy_restart", timeout=30, prev=r["prev"], target=r["revision"],
                               guard=not owner_rolls_back and gid == updater)
        await self.step(job, "已通知星系重启", ",".join(gids))
        end = time.time() + RESTART_TIMEOUT
        new = lambda gid: (c := self.t.galaxies.get(gid)) is not None and c.deploy.get("revision") == r["revision"]
        while time.time() < end:
            if all(new(gid) for gid in gids):
                await self.step(job, "星系已用新版本重新连上神殿", ",".join(gids))
                return True
            await asyncio.sleep(3)
        missing = [gid for gid in gids if not new(gid)]
        await self.step(job, f"{RESTART_TIMEOUT // 60} 分钟内没有以新版本连上：{', '.join(missing)}"
                        + ("" if owner_rolls_back else "。星系的启动保护会在连续启动失败后自动回滚到旧版本"),
                        ",".join(gids))
        return False

    async def finish(self, job: dict) -> None:
        job["state"] = "done" if all(g["state"] in ("done", "skipped") for g in job["groups"]) else "failed"
        job["finished_at"] = time.time()
        await self.save(job)
        await self.t.notify_deploy(job)

    async def resume(self) -> None:
        """In a freshly started temple: continue a job that restarted the previous temple process."""
        for job in self.jobs():
            if job["state"] != "restarting_temple":
                continue
            ok = self.me["revision"] == job["revision"]
            for g in job["groups"]:
                if g["state"] == "restarting_temple":
                    g["state"] = "done" if ok else "failed"
            job["state"] = "running"
            await self.step(job, "中心神殿已以新版本启动" if ok else
                            f"中心神殿重启后版本是 {self.me['revision'][:12]}，与目标不符", "temple")
            self.running[job["id"]] = asyncio.create_task(self.run(job))

    # ghroth asks, azathoth decides

    def request(self, galaxies, reason: str) -> dict:
        rid = f"deployreq_{int(time.time() * 1000) % 10**9}"
        req = {"id": rid, "galaxies": galaxies, "reason": reason, "created_at": time.time(), "state": "pending"}
        self.requests[rid] = req
        return req


def guard_startup(config_path: str) -> None:
    """Run before anything else imports the (possibly new) code: if a deployed galaxy keeps failing to
    start, put its checkout back to the previous revision so its supervisor brings the old code up."""
    try:
        cfg = json.loads(Path(config_path).read_text())
        pending = Path(cfg["state"]) / PENDING
        data = json.loads(pending.read_text())
    except (OSError, ValueError, KeyError):
        return
    data["starts"] = data.get("starts", 0) + 1
    if data["starts"] <= 3:
        pending.write_text(json.dumps(data))
        return
    import subprocess
    subprocess.run(["git", "-c", f"safe.directory={ROOT}", "reset", "-q", "--keep", data["prev"]], cwd=ROOT, check=False)
    pending.unlink(missing_ok=True)
    print(f"deploy guard: {data['target'][:12]} failed to start 3 times; rolled back to {data['prev'][:12]}",
          file=sys.stderr)
