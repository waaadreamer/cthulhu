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
import re
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


async def _communicate(p, timeout: float, operation: str):
    """Settle our entire command group before rollback or cancellation continues."""
    communication = asyncio.create_task(p.communicate())
    try:
        return await asyncio.wait_for(asyncio.shield(communication), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await communication
        if isinstance(error, asyncio.CancelledError):
            raise
        raise RuntimeError(f"{operation} timed out after {timeout:g}s; subprocess group stopped") from None


async def git(root: Path, *args: str, check: bool = True, timeout: float = 300) -> str:
    p = await asyncio.create_subprocess_exec(
        "git", "-c", f"safe.directory={root}", *args, cwd=root,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    out, err = await _communicate(p, timeout, f"git {args[0] if args else ''}".strip())
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(err or out).decode(errors='replace').strip()[:500]}")
    return out.decode().strip()


def local_info(root: Path = ROOT) -> dict:
    """What a process tells the temple about its checkout."""
    import subprocess
    root = Path(root).resolve()
    rev, writable, error = "", False, "运行目录不是有效的 Git 检出；版本未知，需现场部署"
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "rev-parse", "--show-toplevel",
             "--absolute-git-dir", "--git-common-dir", "HEAD"], cwd=root,
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            top, git_dir, common_dir, head = result.stdout.strip().splitlines()
            if Path(top).resolve() == root and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
                rev = head
                writable = all(os.access(p, os.W_OK) for p in (root, Path(git_dir), root / common_dir))
                error = "" if writable else "代码目录或 Git 元数据不可写；需现场检查权限"
    except (OSError, ValueError, subprocess.SubprocessError):
        error = "无法读取 Git 版本；需现场检查 Git 和代码目录"
    return {"host": socket.gethostname(), "root": str(root), "revision": rev,
            "writable": writable, "error": error or None}


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
    return await _ancestry(root, a, b) == 0


async def _ancestry(root: Path, a: str, b: str) -> int:
    """0: ancestor, 1: incompatible, other: history unavailable in this checkout."""
    p = await asyncio.create_subprocess_exec("git", "-c", f"safe.directory={root}", "merge-base", "--is-ancestor",
                                             a, b, cwd=root, stdout=asyncio.subprocess.DEVNULL,
                                             stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    await _communicate(p, 5, "git merge-base")
    return p.returncode


async def _run(*cmd: str, cwd: Path, env: dict | None = None, timeout: float = 300) -> None:
    p = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=os.environ | (env or {}),
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                             start_new_session=True)
    out, _ = await _communicate(p, timeout, Path(cmd[0]).name)
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
        groups = self.groups([g["id"] for g in self.t.store.galaxies()] + ["__temple__"])
        blocked = {gid: g["error"] for g in groups if g.get("error")
                   for gid in g["galaxies"] + (["__temple__"] if g.get("temple") else [])}
        return {"temple": self.me, "galaxies": {gid: c.deploy for gid, c in self.t.galaxies.items()},
                "jobs": self.jobs()[::-1], "requests": list(self.requests.values()),
                "blocked": blocked, "blocked_reason": self.blocked_reason()}

    def blocked_reason(self) -> str | None:
        if any(not t.done() for t in self.running.values()) or \
                any(j["state"] in ("running", "restarting_temple") for j in self.jobs()):
            return "已有部署在进行中"
        if any(g.get("recovery_required") for j in self.jobs() for g in j["groups"]):
            return "存在未确认的回滚，目标保持排空；需先恢复部署任务"
        return None

    async def latest(self) -> str:
        """The head of main in the repository."""
        repo = self.repo
        if not repo:
            try:
                repo = await git(self.root, "remote", "get-url", "origin", timeout=5)
            except (RuntimeError, OSError):
                raise RuntimeError("未配置 deploy.repo，且代码目录没有可用的 origin；无法查询仓库 main") from None
        try:
            out = await git(self.root, "ls-remote", repo, "refs/heads/main", timeout=30)
        except (RuntimeError, OSError):
            # Git errors can contain credentials from the repository URL. Never send them to the UI.
            raise RuntimeError("仓库 main 查询失败或超时；请检查仓库配置、网络和读取权限后重试") from None
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1] == "refs/heads/main" and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", fields[0]):
                return fields[0]
        raise RuntimeError("仓库未返回有效的 refs/heads/main；无法确定部署目标")

    async def target_blocks(self, revision: str) -> dict[str, str]:
        """Reject known incompatible history without fetching or changing the checkout.

        New commits absent locally still pass to update_code's authoritative post-fetch checks.
        In particular, a main that is still on v2 must not invite a rollback from v3.
        """
        infos = {"__temple__": self.me, **{gid: c.deploy for gid, c in self.t.galaxies.items()}}
        if not self.me.get("revision"):
            return {gid: "中心没有可用的 Git 历史，无法核验 main 兼容性；请先恢复中心 Git 目录"
                    for gid, info in infos.items() if info.get("revision")}
        blocked = {}
        for current in {d.get("revision") for d in infos.values()} - {None, "", revision}:
            try:
                incompatible = await _ancestry(self.root, current, revision) == 1
            except (OSError, RuntimeError):
                incompatible = False  # This is not evidence of incompatible history.
            if incompatible:
                reason = (f"当前版本 {current[:7]} 无法快进到 main {revision[:7]}；禁止回退或覆盖，"
                          "需先协调 main 发布历史")
                blocked.update({gid: reason for gid, info in infos.items() if info.get("revision") == current})
        for group in self.groups(list(infos)):
            members = group["galaxies"] + (["__temple__"] if group.get("temple") else [])
            if reason := next((blocked[gid] for gid in members if gid in blocked), None):
                blocked.update({gid: reason for gid in members})
        return blocked

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
        for g in out.values():
            if g.get("error"):
                continue
            infos = [self.me] if g.get("temple") else [conns[gid].deploy for gid in g["galaxies"]]
            if not any(d.get("revision") and d.get("writable") for d in infos):
                g["error"] = next((d["error"] for d in infos if d.get("error")), None) or (
                    "无法确认 Git 版本；需现场部署" if not any(d.get("revision") for d in infos)
                    else "这组星系都没有写代码目录的权限")
        return list(out.values())

    # jobs

    async def start(self, galaxies: list[str] | str, by: str, reason: str = "", task_id: str | None = None,
                    revision: str | None = None) -> dict:
        if reason_blocked := self.blocked_reason():
            raise ValueError(reason_blocked)
        everything = galaxies in ("all", ["all"])
        if everything:
            ids = [g["id"] for g in self.t.store.galaxies()] + ["__temple__"]
        else:
            ids = [galaxies] if isinstance(galaxies, str) else list(galaxies)
        groups = self.groups(ids)
        for group in groups:
            if group.get("error") and (not everything or group.get("temple")):
                raise ValueError(group["error"])
        target = revision or await self.latest()
        target_blocks = await self.target_blocks(target)
        for group in groups:
            members = group["galaxies"] + (["__temple__"] if group.get("temple") else [])
            if error := next((target_blocks[gid] for gid in members if gid in target_blocks), None):
                if not everything or group.get("temple"):
                    raise ValueError(error)
                group["error"] = error
        job = {"id": f"deploy_{int(time.time())}", "by": by, "reason": reason, "task_id": task_id,
               "revision": target, "state": "running", "created_at": time.time(), "finished_at": None,
               "groups": [{"galaxies": g["galaxies"], "temple": g.get("temple", False),
                           # "all" skips galaxies that cannot take part (offline / too old) instead of failing.
                           "state": "skipped" if everything and g.get("error") else "pending",
                           "error": g.get("error")} for g in groups], "steps": []}
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
        release = False
        try:
            await self.wait_online(gids)
            for gid in gids:
                await self.t.maintenance_state(gid, True)
            await self.step(job, "排空中：等待正在进行的回合、记忆整理和巡检结束", label)
            await self.wait_drained(gids)
            updater = None if g["temple"] else next(
                (gid for gid in gids if self.t.galaxies[gid].deploy.get("writable")
                 and self.t.galaxies[gid].deploy.get("revision")), None)
            if not g["temple"] and updater is None:
                raise RuntimeError("这组星系都没有写代码目录的权限")
            # Journal before mutation: a lost update RPC response must not lose the rollback target.
            if "previous_revision" not in g:
                g["previous_revision"] = (await git(self.root, "rev-parse", "HEAD") if g["temple"]
                                          else self.t.galaxies[updater].deploy["revision"])
            g["updater"] = updater
            g["phase"] = "updating"
            await self.save(job)
            if g["temple"]:
                result = await update_code(self.root, job["revision"])
            else:
                result = await self.t.galaxies[updater].rpc("deploy_update", timeout=1200, revision=job["revision"])
            r = {**result, "prev": g["previous_revision"]}
            g["update"] = r
            g["phase"] = "restarting"
            await self.save(job)
            # A checkout already at target does not prove its running processes loaded that target.
            needs_restart = any(self.t.galaxies[gid].deploy.get("revision") != job["revision"] for gid in gids)
            if not result.get("unchanged") or needs_restart:
                await self.step(job, f"确认代码版本 {r['revision'][:12]}，等待运行版本一致", label)
                if not await self.restart_galaxies(job, gids, r, owner_rolls_back=g["temple"], updater=updater):
                    raise RuntimeError("星系未在期限内以目标版本重连")
            g["phase"] = "confirmed"
            g.pop("recovery_required", None)
            await self.save(job)
            release = True
        except Exception:
            if g.get("phase") in ("updating", "restarting", "confirmed"):
                release = await self.recover_group(job, g)
            else:
                # No mutation was attempted: restoring delivery cannot expose partly updated code.
                release = True
            raise
        finally:
            if release:
                for gid in gids:
                    await self.t.maintenance_state(gid, False)
        if g["temple"] and self.me["revision"] != job["revision"]:
            g["state"] = job["state"] = "restarting_temple"
            await self.step(job, "中心神殿即将重启以加载新版本（只断线重连，不中断回合）", "temple")
            self.restart_temple()
            return "restarting_temple"
        return "done"

    def restart_temple(self) -> None:
        asyncio.get_running_loop().call_later(1, os.kill, os.getpid(), signal.SIGTERM)

    async def recover_group(self, job: dict, g: dict) -> bool:
        """Rollback only while drained; never resume delivery on an unconfirmed rollback."""
        gids = g["galaxies"]
        label = ",".join(gids) or "temple"
        g["recovery_required"] = True
        await self.save(job)
        try:
            prev = g["previous_revision"]
            await self.wait_online(gids)
            for gid in gids:
                await self.t.maintenance_state(gid, True)
            await self.wait_drained(gids)
            if g["temple"]:
                current = await git(self.root, "rev-parse", "HEAD")
                if current not in (prev, job["revision"]):
                    raise RuntimeError("代码已被其他操作修改，拒绝覆盖回滚")
                await rollback(self.root, prev)
            else:
                updater = self.t.galaxies[g["updater"]]
                if "deploy_recovery_v1" not in updater.capabilities:
                    raise RuntimeError("旧部署协议不支持远程确认回滚；等待现场恢复，保持排空")
                await updater.rpc("deploy_rollback", timeout=1200, prev=prev, target=job["revision"])
            rollback_result = {"prev": prev, "revision": prev}
            if not await self.restart_galaxies(job, gids, rollback_result,
                                               owner_rolls_back=True, updater=None):
                raise RuntimeError("回滚后运行版本未确认")
            g["phase"] = "rolled_back"
            if g["temple"] and self.me["revision"] != prev:
                job["recovery_only"] = True
                await self.step(job, "星系已回滚；神殿将重启加载旧版本，再确认恢复投递", label)
                self.restart_temple()
                return False
            g.pop("recovery_required", None)
            await self.step(job, f"已回滚并确认运行版本 {prev[:12]}", label)
            return True
        except Exception as error:
            await self.step(job, f"回滚待恢复，保持排空，禁止新部署：{error}", label)
            return False

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
            if conn and conn.deploy.get("revision") != r["revision"]:
                try:
                    await conn.rpc("deploy_restart", timeout=30, prev=r["prev"], target=r["revision"],
                                   guard=not owner_rolls_back and gid == updater)
                except Exception as error:
                    # Lost ACK is ambiguous: observe authenticated reconnect before rollback.
                    await self.step(job, f"重启应答未确认，等待实际版本：{error}", gid)
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
            if job["id"] in self.running and not self.running[job["id"]].done():
                continue
            if job["state"] == "running":
                await self.step(job, "神殿异常中断后接续部署：重新排空并核对代码与运行版本")
                recovering = job.get("recovery_only") or any(g.get("recovery_required") for g in job["groups"])
                worker = self.retry_recovery(job) if recovering else self.run(job)
                self.running[job["id"]] = asyncio.create_task(worker)
                continue
            if job["state"] == "failed" and any(g.get("recovery_required") for g in job["groups"]):
                self.running[job["id"]] = asyncio.create_task(self.retry_recovery(job))
                continue
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

    async def recover(self, job_id: str, by: str) -> dict:
        if any(not t.done() for t in self.running.values()):
            raise ValueError("已有部署在进行中")
        job = self.job(job_id)
        if not job or job["state"] != "failed" or not any(g.get("recovery_required") for g in job["groups"]):
            raise ValueError("该任务没有待确认的回滚")
        job.update(state="running", recovery_only=True, finished_at=None)
        await self.step(job, f"{by} 请求重新确认回滚；不会重新部署失败版本")
        self.running[job_id] = asyncio.create_task(self.retry_recovery(job))
        return job

    async def retry_recovery(self, job: dict) -> None:
        try:
            for g in job["groups"]:
                if g.get("recovery_required"):
                    g["state"] = "failed"
                    if await self.recover_group(job, g):
                        for gid in g["galaxies"]:
                            await self.t.maintenance_state(gid, False)
        except Exception as error:
            await self.step(job, f"恢复收尾失败：{error}")
        finally:
            await self.finish(job)

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
