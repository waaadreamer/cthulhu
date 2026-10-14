"""Temple: the central server (v3, docs/design-v3.md).

Serves azathoth, the owner and the galaxies' hubs. Old_ones never connect here:
their hub forwards bindings, the task index, reports and cross-galaxy relays.
The temple keeps the global index (task_index, roster), turns checkpoint results
into one digest for azathoth every 10 minutes, and runs the owner-approved sudo queue.
Agents and galaxies are identified by the token in their hello frame; nothing
they claim about themselves is trusted.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .api_provider import ProviderBroker, API_VERSION
from . import BUILD
from . import roles
from .protocol import agent_token, dumps, sha256, maintenance_token
from .roles import OWNER, SYSTEM, Denied, Role
from .store import Store, new_id


def clip(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"

log = logging.getLogger(__name__)

SESSION_TTL = 30 * 86400
OWNER_PRIORITY = roles.OWNER_PRIORITY
DIGEST_EVERY = 600         # temple config "digest_every" overrides (tests)
SUDO_WAIT = 1800
TERMINAL = ("closed", "cancelled", "assign_failed")
FILE_PREVIEW_BYTES = 2 * 1024 * 1024
FILE_RAW_BYTES = 40 * 1024 * 1024    # base64 over the galaxy websocket stays under its 64 MB frame limit
MEDIA_EXT = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"},
    "video": {".mp4", ".webm", ".mov", ".m4v", ".ogv"},
    "audio": {".mp3", ".wav", ".ogg", ".m4a", ".flac"},
    "pdf": {".pdf"},
    "markdown": {".md", ".markdown", ".mdx"},
}


def file_kind(path: str) -> str:
    ext = Path(path).suffix.lower()
    return next((k for k, exts in MEDIA_EXT.items() if ext in exts), "text")


@dataclass(eq=False)
class Conn:
    ws: WebSocket
    kind: str                 # agent | galaxy | owner
    id: str
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_id: int = 0
    capabilities: list[str] = field(default_factory=list)
    version: str = ""
    deploy: dict = field(default_factory=dict)   # galaxies: {host, root, revision, writable} for deploy
    tasks: set = field(default_factory=set)

    async def send(self, frame: dict) -> bool:
        try:
            await asyncio.wait_for(self.ws.send_text(dumps(frame)), 10)
            return True
        except Exception:
            return False

    async def rpc(self, method: str, timeout: float = 30, **params) -> Any:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            if not await self.send({"type": "rpc", "id": rid, "method": method, "params": params}):
                raise ConnectionError(f"{self.id} disconnected")
            frame = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
        if not frame.get("ok"):
            raise RuntimeError(frame.get("error", "rpc failed"))
        return frame.get("result")


MODEL_CHOICES = {
    "claude": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    "codex": ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"],
}
MODEL_CHOICES["galaxy_api"] = []
# Effort used when a runtime names one of these models without an explicit effort.
DEFAULT_EFFORT = {"gpt-6-astra": "medium"}
EFFORTS = {None, "low", "medium", "high", "xhigh", "max"}
# Runtime recorded as the per-agent override of every newly spawned old_one (existing ones keep theirs).
OLD_ONE_RUNTIME = {"backend": "claude", "provider": "ali", "model": "claude-opus-5-5", "effort": "xhigh",
                   "fallback": {"backend": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}}


def validate_runtime(cfg: dict, nested: bool = False) -> dict:
    backend = cfg.get("backend")
    if backend not in MODEL_CHOICES:
        raise ValueError(f"unknown backend {backend}")
    if not cfg.get("model"):
        raise ValueError("model is required")
    if cfg.get("effort") not in EFFORTS:
        raise ValueError(f"bad effort {cfg.get('effort')}")
    tier = cfg.get("service_tier")
    if tier not in (None, "default", "fast", "priority"):
        raise ValueError("bad service_tier")
    if tier is not None and backend == "claude":
        raise ValueError("service_tier requires a Codex backend")
    out = {k: cfg[k] for k in ("backend", "model", "effort", "service_tier", "provider") if cfg.get(k)}
    if backend == "galaxy_api" and (not isinstance(cfg.get("provider"), str) or not cfg["provider"]):
        raise ValueError("galaxy_api requires a provider galaxy ID")
    if cfg.get("provider") and (not isinstance(cfg["provider"], str) or backend not in ("galaxy_api", "claude")):
        raise ValueError("provider requires a supported native Galaxy API runtime")
    if "effort" not in out and out["model"] in DEFAULT_EFFORT:
        out["effort"] = DEFAULT_EFFORT[out["model"]]
    if not nested:
        for k in ("memory_model", "memory_effort"):
            if cfg.get(k):
                out[k] = cfg[k]
        if cfg.get("fallback"):
            fb = validate_runtime(cfg["fallback"], nested=True)
            if fb["backend"] == out["backend"] and fb["model"] == out["model"] and fb.get("provider") == out.get("provider"):
                raise ValueError("fallback must differ from the primary")
            out["fallback"] = fb
    return out


def password_hash(password: str, salt: str) -> str:
    # Same parameters as the previous harness so migrated accounts keep working.
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1,
                          maxmem=64 * 1024 * 1024).hex()


class Temple:
    def __init__(self, config: dict):
        self.config = config
        self.secret: str = config["secret"]
        # Browsers share cookies across ports of one host: a second temple on the same host needs its own name.
        self.cookie: str = config.get("session_cookie", "cthulhu_session")
        self.store = Store(Path(config["db"]))
        self.agents: dict[str, Conn] = {}
        self.galaxies: dict[str, Conn] = {}
        self.maintainers: set[Conn] = set()
        self.watchers: set[Conn] = set()     # owner browsers
        self.status: dict[str, dict] = {}    # live agent status reported by agents
        self.login_failures: dict[str, list[float]] = {}
        self.api_broker = ProviderBroker(self)
        self.maintenance = set(self.store.setting("maintenance") or [])
        self.sudo_waiters: dict[str, asyncio.Future] = {}
        self.offline_since: dict[str, float] = {}
        self._seed()
        from .deploy import Deployer
        self.deployer = Deployer(self)

    def _seed(self) -> None:
        for g in self.config.get("galaxies", []):
            self.store.add_galaxy(g["id"], g["name"], sha256(g["token"]))
        self.closed_galaxies = {g["id"] for g in self.config.get("galaxies", []) if g.get("spawn") is False}
        for a in self.config.get("gods", []):
            self.store.upsert_agent(a["id"], a.get("name") or roles.DISPLAY[Role(a["role"])], a["role"],
                                    a["galaxy"], spec=a.get("spec") or {})
        channel = self.config.get('feishu')
        if channel:
            self.store.upsert_agent('nyarlathotep', roles.DISPLAY[Role.NYARLATHOTEP], Role.NYARLATHOTEP,
                                    channel['galaxy'], spec={'feishu_worker': channel.get('worker', {})})

    # lookups

    def role_of(self, agent_id: str) -> str | None:
        if agent_id in (OWNER, SYSTEM):
            return agent_id
        a = self.store.agent(agent_id)
        if a:
            return a["role"]
        return Role.OLD_ONE if self.store.old_one(agent_id) else None

    def god(self, role: str) -> dict | None:
        return next((a for a in self.store.agents() if a["role"] == role), None)

    def public_agent(self, a: dict) -> dict:
        live = self.status.get(a["id"], {})
        return {"id": a["id"], "name": a["name"], "role": a["role"], "galaxy_id": a["galaxy_id"],
                "parent_id": a["parent_id"], "desired": a["desired"], "task": a["spec"].get("task", ""),
                "online": a["id"] in self.agents, "state": live.get("state", "offline"),
                "current": live.get("current"), "context_fill": live.get("context_fill"),
                "runtime": live.get("runtime"), "admission": live.get("admission"),
                "stopped_by": (self.store.setting("force_stopped") or {}).get(a["id"]) if a["desired"] != "running" else None,
                "created_at": a["created_at"], "updated_at": a["updated_at"]}

    def public_old_one(self, r: dict) -> dict:
        d = r["data"]
        return {"id": r["id"], "name": r["name"], "role": Role.OLD_ONE.value, "galaxy_id": r["galaxy_id"], "parent_id": None,
                "desired": r["desired"], "task": "", "task_id": r["task_id"],
                "online": r["state"] != "offline" and r["galaxy_id"] in self.galaxies, "state": r["state"],
                "current": d.get("current"), "context_fill": d.get("context_fill"), "runtime": d.get("runtime"),
                "admission": None, "stopped_by": None, "queued": 0, "memory": {"entries": 0, "tokens": 0},
                "created_at": d.get("created_at") or r["updated_at"], "updated_at": r["updated_at"]}

    def directory(self) -> dict:
        pending = self.store.pending_counts()
        memory = self.store.memory_stats()
        agents = [self.public_agent(a) | {"queued": pending.get(a["id"], 0),
                                          "memory": memory.get(a["id"], {"entries": 0, "tokens": 0})}
                  for a in self.store.agents()] + [self.public_old_one(r) for r in self.store.roster()]
        galaxies = [g | {"online": g["id"] in self.galaxies, "version": getattr(self.galaxies.get(g["id"]), "version", ""),
                         "revision": (getattr(self.galaxies.get(g["id"]), "deploy", None) or {}).get("revision", ""),
                         "deployable": bool(getattr(self.galaxies.get(g["id"]), "deploy", None)),
                         "draining": g["id"] in self.maintenance, "spawn": g["id"] not in self.closed_galaxies,
                         "hub": "hub_v1" in getattr(self.galaxies.get(g["id"]), "capabilities", [])}
                    for g in self.store.galaxies()]
        return {"agents": agents, "galaxies": galaxies}

    async def broadcast(self, frame: dict) -> None:
        for w in list(self.watchers):
            if not await w.send(frame):
                self.watchers.discard(w)

    # messaging

    async def route(self, sender: str, receiver: str, text: str, *, kind="message",
                    priority=0, reply_to=None, message_id=None, task_id=None, status=None) -> dict:
        sender_role, receiver_role = self.role_of(sender), self.role_of(receiver)
        if receiver_role is None:
            raise Denied(f"unknown agent {receiver}")
        if sender != SYSTEM:
            roles.require_send(sender_role, receiver_role)
            roles.require_priority(sender_role, int(priority))
        if not text.strip():
            raise ValueError("empty message")
        if receiver_role == Role.OLD_ONE:
            return await self.to_old_one(sender, receiver, text, kind=kind, priority=int(priority),
                                         reply_to=reply_to, message_id=message_id, task_id=task_id)
        target = self.store.agent(receiver)
        if target and target["desired"] != "running":
            raise Denied(f"{target['name']} is stopped; messages are rejected until it is resumed")
        msg = self.store.add_message(sender, receiver, text, kind, int(priority), reply_to, message_id, task_id, status)
        await self.deliver(msg)
        await self.broadcast({"type": "message", "message": msg})
        return msg

    async def to_old_one(self, sender: str, receiver: str, text: str, *, kind="message", priority=0,
                         reply_to=None, message_id=None, task_id=None) -> dict:
        """Azathoth's or the owner's message for an old_one: recorded here, delivered by its hub."""
        r = self.store.old_one(receiver)
        if not r or r["desired"] != "running":
            raise Denied(f"{receiver} is not a running old_one")
        hub = self.galaxies.get(r["galaxy_id"])
        if hub is None:
            raise Denied(f"galaxy {r['galaxy_id']} is offline; the message cannot be delivered now")
        msg = self.store.add_message(sender, receiver, text, kind, priority, reply_to, message_id, task_id)
        await hub.rpc("hub_deliver", timeout=30, envelope=msg | self.sender_meta(sender))
        self.store.mark(msg["id"], "delivered")
        await self.broadcast({"type": "message", "message": msg})
        return msg

    async def deliver(self, msg: dict) -> None:
        current = self.store.message(msg['id'])
        if not current or current['state'] == 'cancelled':
            return
        recipient = self.store.agent(msg["receiver"])
        # The owner never waits for a deploy: their messages go through a drain (the deploy waits for them instead).
        if recipient and recipient["galaxy_id"] in self.maintenance and msg["sender"] != OWNER:
            return
        conn = self.agents.get(msg["receiver"])
        if conn is None:
            return
        envelope = msg | self.sender_meta(msg["sender"])
        if msg.get("task_id"):
            task = self.store.task(msg["task_id"])
            envelope["task_title"] = task and clip(task["description"], 40)
        if await conn.send({"type": "message", "message": envelope}):
            # Cancellation may retire this input while the socket send is in flight.
            if self.store.message(msg['id'])['state'] == 'queued':
                self.store.mark(msg["id"], "delivered")

    # v3 tasks: the temple indexes, the hubs run them

    def azathoth_id(self) -> str:
        return self.god(Role.AZATHOTH)["id"]

    def galaxy_by(self, key: str) -> dict:
        g = self.store.galaxy(key) or next((g for g in self.store.galaxies() if g["name"] == key), None)
        if not g:
            raise ValueError(f"unknown galaxy {key}; known: " + ", ".join(f"{x['id']}（{x['name']}）" for x in self.store.galaxies()))
        return g

    def hub(self, galaxy_id: str) -> Conn:
        conn = self.galaxies.get(galaxy_id)
        if conn is None:
            raise Denied(f"galaxy {galaxy_id} is offline")
        if "hub_v1" not in conn.capabilities:
            raise Denied(f"galaxy {galaxy_id} has no hub; upgrade it")
        return conn

    async def new_task(self, galaxy: str, description: str, acceptance: str = "", cwd: str | None = None) -> dict:
        g = self.galaxy_by(galaxy)
        if g["id"] in self.closed_galaxies:
            raise Denied(f"galaxy {g['name']} does not take tasks")
        if not description.strip():
            raise ValueError("description is required")
        conn = self.hub(g["id"])
        task_id = new_id("t")
        self.store.index_task(task_id, g["id"], description.strip(), acceptance.strip())
        await self.task_changed(task_id)
        try:
            r = await conn.rpc("hub_assign", timeout=180, task_id=task_id, description=description.strip(),
                               acceptance=acceptance.strip(), cwd=cwd)
        except Exception as e:
            self.store.set_task(task_id, state="assign_failed", closed_at=time.time(), line=f"派单失败：{clip(str(e), 120)}")
            await self.task_changed(task_id)
            raise RuntimeError(f"assign failed: {e}; task {task_id} marked assign_failed") from e
        return {"task_id": task_id, "galaxy": g["id"]} | {k: r.get(k) for k in ("agent_id", "agent_name", "reused", "reason")}

    async def task_command(self, method: str, task_id: str, **params) -> dict:
        t = self.store.task(task_id)
        if not t:
            raise ValueError(f"unknown task {task_id}")
        return await self.hub(t["galaxy_id"]).rpc(method, timeout=60, task_id=task_id, **params)

    async def task_view(self, task_id: str) -> dict:
        t = self.store.task(task_id)
        if not t:
            raise ValueError(f"unknown task {task_id}")
        try:
            return await self.hub(t["galaxy_id"]).rpc("hub_task", timeout=30, task_id=task_id) | {"galaxy_id": t["galaxy_id"]}
        except Exception as e:
            return t | {"details": f"明细不可用：{e}"}

    async def task_changed(self, task_id: str | None = None) -> None:
        if task_id:
            await self.broadcast({"type": "task", "task_id": task_id})
        frame = {"type": "bindings", "rows": self.bindings()}
        for conn in list(self.galaxies.values()):
            if "hub_v1" in conn.capabilities:
                await conn.send(frame)

    def bindings(self) -> list[dict]:
        return [{"task_id": t["id"], "galaxy_id": t["galaxy_id"], "executor": t["executor"], "executor_name": t["executor_name"],
                 "state": t["state"], "description": clip(t["description"], 80)}
                for t in self.store.tasks(open_only=True, limit=200)]

    def bindings_text(self) -> str:
        now = time.time()
        rows = self.store.tasks(open_only=True, limit=100) + [
            t for t in self.store.tasks(limit=100) if t["state"] in TERMINAL and (t["closed_at"] or 0) > now - 86400]
        if not rows:
            return "（暂无任务）"
        names = {g["id"]: g["name"] for g in self.store.galaxies()}
        out = []
        for t in rows:
            off = " · 星系离线" if t["galaxy_id"] not in self.galaxies and t["state"] not in TERMINAL else ""
            out.append(f"{t['id']} | {names.get(t['galaxy_id'], t['galaxy_id'])} | {t['executor_name'] or '—'}"
                       f"({t['executor'] or '—'}) | {t['state']}{off} | {t['line'] or '—'} | {clip(t['description'], 60)}")
        return "\n".join(out)

    def galaxies_text(self) -> str:
        """Every galaxy azathoth can reach, what it is for, and its old_ones (idle ones can take a quick send)."""
        cfg = {g["id"]: g for g in self.config.get("galaxies", [])}
        busy = {t["executor"]: t["id"] for t in self.store.tasks(open_only=True, limit=500) if t["executor"]}
        me = (self.god(Role.AZATHOTH) or {}).get("galaxy_id")
        out = []
        for g in self.store.galaxies():
            conn = self.galaxies.get(g["id"])
            if g["id"] == me:
                kind = "你所在的机器（中心神殿），不接任务"
            elif g["id"] in self.closed_galaxies:
                kind = "不接任务"
            elif conn is None:
                kind = "离线：new_task / send 会失败"
            elif "hub_v1" not in conn.capabilities:
                kind = "在线但未升级到 v3，不能派任务"
            else:
                kind = "在线，可派任务"
            olds = [f"{r['name']}({r['id']})·" + (f"在途 {busy[r['id']]}" if r["id"] in busy else
                                                   "空闲" if r["state"] != "offline" else "离线")
                    for r in self.store.roster(g["id"]) if r["desired"] == "running"]
            desc = cfg.get(g["id"], {}).get("description", "")
            out.append(f"- {g['id']}（{g['name']}）：{kind}" + (f"。用途：{desc}" if desc else "")
                       + (f"\n  旧日支配者：{'；'.join(olds)}" if olds else ""))
        return "\n".join(out) or "（没有星系）"

    async def azathoth_header(self) -> str:
        return ("<galaxies>\n星系（new_task 的 galaxy 参数用 ID）：\n" + self.galaxies_text() + "\n</galaxies>\n"
                "<bindings>\n任务绑定表（在途任务 + 24 小时内结束的任务）：\n"
                "task | 星系 | 执行者 | 状态 | 检查点 | 描述\n" + self.bindings_text() + "\n</bindings>\n\n")

    async def hub_report(self, galaxy_id: str, p: dict) -> bool:
        """A hub's report for azathoth: old_ones' reports go direct, checkpoint results into the digest."""
        if p.get("direct"):
            pr = roles.BLOCKER_PRIORITY if p.get("kind") == "blocker" else roles.REPORT_PRIORITY
            await self.route(SYSTEM, self.azathoth_id(), p["text"], kind="report", priority=pr, task_id=p.get("task_id"))
        else:
            self.store.add_digest(p.get("task_id"), p["text"], bool(p.get("attention")))
        return True

    async def digest_clock(self) -> None:
        while True:
            await asyncio.sleep(float(self.config.get("digest_every", DIGEST_EVERY)))
            try:
                self.expire_sudo()
                await self.watch_galaxies()
                await self.send_digest()
            except Exception:
                log.exception("digest failed")

    async def send_digest(self) -> None:
        rows = self.store.unsent_digest()
        if not rows:
            return
        az = self.azathoth_id()
        open_n = len(self.store.tasks(open_only=True, limit=500))
        start = min(r["created_at"] for r in rows)
        lines = [f"[进度汇总 {time.strftime('%H:%M', time.localtime(start))}–{time.strftime('%H:%M')}] "
                 f"在途 {open_n} · 需关注 {sum(r['attention'] for r in rows)}"]
        lines += [("⚠ " if r["attention"] else "✓ ") + r["text"] for r in rows]
        text = "\n".join(lines)
        # A digest azathoth has not picked up yet is folded into the new one, so digests never pile up.
        stale = self.store.one("SELECT * FROM messages WHERE receiver=? AND kind='digest' AND state='queued'"
                               " ORDER BY created_at DESC LIMIT 1", (az,))
        if stale:
            text += "\n（上一条未处理的汇总并入）\n" + stale["text"].split("\n", 1)[-1]
            self.store.mark(stale["id"], "cancelled")
        msg = await self.route(SYSTEM, az, text, kind="digest", priority=roles.DIGEST_PRIORITY)
        self.store.mark_digest([r["id"] for r in rows], msg["id"])

    async def watch_galaxies(self) -> None:
        now = time.time()
        for g in self.store.galaxies():
            if g["id"] in self.galaxies or not self.store.tasks(open_only=True, galaxy_id=g["id"], limit=1):
                self.offline_since.pop(g["id"], None)
                continue
            since = self.offline_since.setdefault(g["id"], now)
            if now - since >= float(self.config.get("digest_every", DIGEST_EVERY)) and not self.offline_since.get(g["id"] + ":told"):
                self.offline_since[g["id"] + ":told"] = now
                self.store.add_digest(None, f"星系 {g['name']}（{g['id']}）已离线超过 10 分钟，其上任务的检查点上报会延后到达。", True)
        for key in [k for k in self.offline_since if k.endswith(":told") and k[:-5] in self.galaxies]:
            self.offline_since.pop(key)

    # v3 sudo approvals (design §1.1.1b): the owner approves each command in the web UI

    async def sudo_request(self, caller_galaxy: str, p: dict) -> dict:
        command = str(p.get("command", "")).strip().removeprefix("sudo ").strip()
        if not command:
            raise ValueError("command is required")
        timeout = max(10.0, min(float(p.get("timeout") or 600), 3600.0))
        a = self.store.add_approval(command, str(p.get("reason", "")), p.get("cwd"), timeout)
        fut = asyncio.get_running_loop().create_future()
        self.sudo_waiters[a["id"]] = fut
        await self.broadcast({"type": "approval", "approval": a})
        try:
            decision = await asyncio.wait_for(fut, SUDO_WAIT)
        except asyncio.TimeoutError:
            self.store.set_approval(a["id"], state="expired", decided_at=time.time())
            await self.broadcast({"type": "approval", "approval": self.store.approval(a["id"])})
            return {"approved": False, "state": "expired", "note": "主人 30 分钟内没有批准，请求作废"}
        finally:
            self.sudo_waiters.pop(a["id"], None)
        if not decision["approve"]:
            return {"approved": False, "state": "rejected", "note": "主人拒绝" + (f"：{decision['note']}" if decision.get("note") else "")}
        target = self.galaxies.get(caller_galaxy)
        try:
            if target is None:
                raise RuntimeError("galaxy offline")
            # Exactly the text the owner saw, read back from the record.
            r = await target.rpc("sudo_exec", timeout=timeout + 30, command=a["command"], cwd=a["cwd"], limit=timeout)
            state = "done" if r.get("code") == 0 else "failed"
        except Exception as e:
            r, state = {"code": None, "stdout": "", "stderr": str(e)}, "failed"
        self.store.set_approval(a["id"], state=state, finished_at=time.time(), result=json.dumps(r, ensure_ascii=False)[:20000])
        await self.broadcast({"type": "approval", "approval": self.store.approval(a["id"])})
        return {"approved": True, "state": state, **r}

    async def sudo_decide(self, approval_id: str, approve: bool, note: str = "") -> dict:
        a = self.store.approval(approval_id)
        fut = self.sudo_waiters.get(approval_id)
        if not a or a["state"] != "pending" or not fut or fut.done():
            if a and a["state"] == "pending":
                self.store.set_approval(approval_id, state="expired", decided_at=time.time())
            raise ValueError("no pending approval with this id (it may have expired)")
        self.store.set_approval(approval_id, state="approved" if approve else "rejected", note=note, decided_at=time.time())
        fut.set_result({"approve": approve, "note": note})
        await self.broadcast({"type": "approval", "approval": self.store.approval(approval_id)})
        return self.store.approval(approval_id)

    def expire_sudo(self) -> None:
        # Requests orphaned by a temple restart can never be executed.
        for a in self.store.approvals("pending"):
            if a["id"] not in self.sudo_waiters:
                self.store.set_approval(a["id"], state="expired", decided_at=time.time())

    # agent calls: (conn, params) -> result

    async def handle_agent_call(self, conn: Conn, method: str, p: dict) -> Any:
        if self.store.agent(conn.id)["desired"] != "running":
            raise Denied("this agent has been stopped")
        if method.startswith('channel_'):
            from .feishu import channel_call
            return await channel_call(self, conn, method, p)
        if method.startswith("api_"):
            return await self.handle_api_call(conn, method, p)
        me = self.store.agent(conn.id)
        role = me["role"]
        if method == "send":
            msg = await self.route(conn.id, p["to"], p["text"], kind=p.get("kind", "message"),
                                   priority=int(p.get("priority") or 0), reply_to=p.get("reply_to"), task_id=p.get("task_id"))
            return {"id": msg["id"]}
        if method == "handled":
            msg = self.store.message(p["id"])
            if msg and msg["receiver"] == conn.id and msg['state'] != 'cancelled':
                self.store.mark(p["id"], "handled")
            return True
        if method == "status":
            self.status[conn.id] = p
            await self.broadcast({"type": "status", "agent_id": conn.id, "status": p})
            return True
        if method == "set_thread":
            self.store.set_agent(conn.id, thread_id=p["thread_id"])
            return True
        if method == "log_turn":
            from .feishu import check_scope
            scope = check_scope(self, conn.id, p.get('scope', ''))
            return self.store.log_turn(conn.id, p["backend"], p.get("model"), p["thread"], p["input"], p["reply"], p["status"], scope)
        if method == "turn_log":
            from .feishu import check_scope
            scope = check_scope(self, conn.id, p.get('scope', ''))
            return self.store.turn_log(conn.id, limit=min(int(p.get("limit", 20)), 100), scope=scope)
        if method in ("remember", "revise", "forget"):
            result = (self.store.remember(conn.id, p["text"], p.get("source")) if method == "remember" else
                      self.store.revise(conn.id, int(p["memory_id"]), p["text"], p.get("source")) if method == "revise" else
                      self.store.forget(conn.id, int(p["memory_id"])))
            # Open overview drawers refetch the entries when the directory's memory stats change.
            await self.broadcast({"type": "directory", **self.directory()})
            return result
        if method == "memory":
            return self.store.memory(conn.id)
        if method == "chat_update":
            turn = self.store.turn(p["turn_id"])
            if not turn or turn["agent_id"] != conn.id:
                raise Denied("not your chat turn")
            fields = {k: p[k] for k in ("reply", "status", "activity", "model") if k in p}
            self.store.update_turn(p["turn_id"], **fields)
            await self.broadcast({"type": "chat", "turn": self.store.turn(p["turn_id"])})
            return True
        if method == "chat_history":
            return self.store.turns(conn.id, limit=int(p.get("limit", 20)))
        if method == "task_input_guard":
            return {"allowed": True}
        if role != Role.AZATHOTH:
            raise Denied(f"unknown or forbidden call {method}")
        # azathoth only below
        if method == "notify_owner":
            await self.note_owner(p["text"], model=p.get("model"), origin=p.get("origin"), task_id=p.get("task_id"),
                                  kind=p.get("kind"), **({"activity": p["activity"]} if p.get("activity") else {}))
            return True
        if method in ("deploy", "deploy_status"):
            return await self.deploy_call(role, conn.id, method, p)
        if method == "azathoth_header":
            return await self.azathoth_header()
        if method == "new_task":
            return await self.new_task(p["galaxy"], p["description"], p.get("acceptance", ""), p.get("cwd"))
        if method == "close_task":
            return await self.task_command("hub_close", p["task_id"], note=p.get("note", ""))
        if method == "continue_task":
            return await self.task_command("hub_continue", p["task_id"], instruction=p["instruction"])
        if method == "cancel_task":
            return await self.task_command("hub_cancel", p["task_id"], reason=p.get("reason", ""))
        if method == "manage_old_one":
            return await self.control_old_one(p["action"], p["agent_id"])
        if method == "latest_result":
            return await self.latest_result(p["agent_id"])
        if method == "sudo_request":
            return await self.sudo_request(me["galaxy_id"], p)
        if method == "messages":
            return self.store.messages(p.get("agent_id"), limit=min(int(p.get("limit", 50)), 500),
                                       search=p.get("search"), task_id=p.get("task_id"))
        if method == "list_tasks":
            return [{k: t[k] for k in ("id", "galaxy_id", "state", "executor", "executor_name", "line", "summary", "description")}
                    for t in self.store.tasks(p.get("search"), limit=min(int(p.get("limit", 50)), 200), state=p.get("state"),
                                              galaxy_id=p.get("galaxy_id"), open_only=not p.get("all"))]
        if method == "task_view":
            return await self.task_view(p["task_id"])
        if method == "owner_chat":
            return self.store.turns(conn.id, limit=int(p.get("limit", 20)), search=p.get("search"))
        raise Denied(f"unknown or forbidden call {method}")

    async def control_old_one(self, action: str, agent_id: str) -> dict:
        r = self.store.old_one(agent_id)
        if not r:
            raise ValueError(f"unknown old_one {agent_id}")
        return await self.hub(r["galaxy_id"]).rpc("hub_control", timeout=30, action=action, agent_id=agent_id)

    async def latest_result(self, agent_id: str) -> dict:
        r = self.store.old_one(agent_id)
        if r:
            return await self.hub(r["galaxy_id"]).rpc("hub_latest", timeout=30, agent_id=agent_id)
        a = self.store.agent(agent_id)
        if not a:
            raise Denied(f"unknown agent {agent_id}")
        # The last finished turn comes from the temple's turn log, so it survives agent restarts
        # (an agent that finishes and is then restarted must still show its result to yog).
        last = self.store.turn_log(agent_id, limit=1)
        logged = {"last_reply": clip(last[0]["reply"], 3000), "last_status": last[0]["status"],
                  "finished_at": last[0]["created_at"]} if last else {}
        conn = self.agents.get(agent_id)
        if conn is None:
            return {"agent_id": agent_id, "name": a["name"], "online": False, "desired": a["desired"]} | logged
        try:
            r = await conn.rpc("latest_result", timeout=15)
        except Exception as e:
            r = {"error": str(e)}
        if logged and logged["finished_at"] > (r.get("finished_at") or 0):
            r = r | logged
        return {"agent_id": agent_id, "name": a["name"], "online": True} | r

    # file preview (owner only): files live on galaxies, read by the galaxy as its OS user

    async def locate_file(self, path: str, galaxy: str | None) -> list[str]:
        """Galaxies (hint first) on which `path` exists."""
        ids = [g for g in self.galaxies if g != galaxy]
        if galaxy in self.galaxies:
            ids.insert(0, galaxy)

        async def probe(gid: str):
            try:
                st = await self.galaxies[gid].rpc("read_file", timeout=10, path=path)
                return gid if st.get("exists") else None
            except Exception:
                return None
        return [g for g in await asyncio.gather(*(probe(g) for g in ids)) if g]

    async def read_file(self, path: str, galaxy: str | None, max_bytes: int) -> dict:
        found = await self.locate_file(path, galaxy)
        if not found:
            raise FileNotFoundError(f"no online galaxy has {path}")
        gid = found[0]
        r = await self.galaxies[gid].rpc("read_file", timeout=60, path=path, max_bytes=max_bytes)
        g = self.store.galaxy(gid)
        return r | {"galaxy": gid, "galaxy_name": g["name"] if g else gid, "found": found}

    async def note_owner(self, text: str, **meta) -> None:
        """A proactive turn in azathoth's chat: no owner text, just the words (and what prompted them)."""
        az = self.god(Role.AZATHOTH)
        tid = new_id("note")
        self.store.add_turn(tid, az["id"], "")
        self.store.update_turn(tid, reply=text, status="done", **{k: v for k, v in meta.items() if v is not None})
        await self.broadcast({"type": "chat", "turn": self.store.turn(tid)})

    # lifecycle

    async def delete(self, p: dict, by: str) -> bool:
        """A stopped agent leaves the directory; its messages and tasks stay in history."""
        if self.store.old_one(p["agent_id"]):
            await self.control_old_one("delete", p["agent_id"])
            return True
        a = self.store.agent(p["agent_id"])
        if not a or a["desired"] != "stopped":
            raise ValueError("only a stopped agent can be deleted")
        self.store.set_agent(a["id"], desired="deleted")
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    # The owner's / maintenance force-stop and resume of any agent.

    async def halt(self, agent_id: str, by: str, force: bool = False) -> bool:
        """Stop an agent now: revoke it (persisted), abort its turn, cancel its pending work, kill its processes.
        A force stop (owner / maintenance) can only be lifted by the owner or maintenance."""
        if self.store.old_one(agent_id):
            await self.control_old_one("stop", agent_id)
            return True
        a = self.store.agent(agent_id)
        if not a:
            raise Denied(f"unknown agent {agent_id}")
        log.warning("%s stopped by %s", a["name"], by)
        if force:
            self.store.set_setting("force_stopped", (self.store.setting("force_stopped") or {}) | {a["id"]: by})
        # Revoked first, so nothing routed from here on reaches it and it cannot reconnect.
        self.store.set_agent(a["id"], desired="stopped", epoch=a["epoch"] + 1)
        conn = self.agents.get(a["id"])
        if conn:
            with contextlib.suppress(Exception):
                await conn.rpc("halt", timeout=3)
        # Work handed to it before the stop does not run on resume.
        for m in self.store.pending_for(a["id"]):
            self.store.mark(m["id"], "cancelled")
        for turn in self.store.q("SELECT id FROM chat_turns WHERE agent_id=? AND status IN ('queued','running')", (a["id"],)):
            self.store.update_turn(turn["id"], status="interrupted", reply=f"已被强制停止（{by}）")
            await self.broadcast({"type": "chat", "turn": self.store.turn(turn["id"])})
        await self.drop_agent_conn(a["id"])
        self.status.pop(a["id"], None)
        await self.sync_galaxy(a["galaxy_id"])     # the galaxy kills its process tree
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    async def relaunch(self, agent_id: str, by: str, only_stopped: bool = False) -> bool:
        if self.store.old_one(agent_id):
            await self.control_old_one("resume", agent_id)
            return True
        a = self.store.agent(agent_id)
        if not a:
            raise Denied(f"unknown agent {agent_id}")
        if only_stopped and a["desired"] == "running":
            return False
        if a["desired"] == "deleted":
            raise Denied(f"{a['name']} was deleted")
        log.warning("%s (re)started by %s", a["name"], by)
        forced = self.store.setting("force_stopped") or {}
        if forced.pop(a["id"], None):
            self.store.set_setting("force_stopped", forced)
        self.store.set_agent(a["id"], desired="running", epoch=a["epoch"] + 1)
        await self.drop_agent_conn(a["id"])
        await self.sync_galaxy(a["galaxy_id"])
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    async def drop_agent_conn(self, agent_id: str) -> None:
        conn = self.agents.pop(agent_id, None)
        if conn:
            await conn.ws.close(4001, "epoch revoked")

    def launch_spec(self, a: dict) -> dict:
        return {"id": a["id"], "name": a["name"], "role": a["role"], "epoch": a["epoch"],
                "token": agent_token(self.secret, a["id"], a["epoch"]), "spec": a["spec"],
                "thread_id": a["thread_id"]}

    async def sync_galaxy(self, galaxy_id: str) -> None:
        conn = self.galaxies.get(galaxy_id)
        if conn:
            wanted = [self.launch_spec(a) for a in self.store.agents(galaxy_id, include_stopped=False)]
            await conn.send({"type": "desired", "agents": wanted})

    # runtime settings (owner-editable per role, or per agent for old_ones)

    def galaxy_ai_for(self, gid: str) -> tuple[dict | None, str]:
        """A hub's Galaxy AI settings and where they come from: owner setting > temple config for the galaxy > temple default."""
        owner = (self.store.setting("galaxy_ai") or {}).get(gid)
        if owner:
            return owner, "owner"
        gcfg = next((x for x in self.config.get("galaxies", []) if x["id"] == gid), {})
        if gcfg.get("galaxy_ai"):
            return gcfg["galaxy_ai"], "config"
        return self.config.get("galaxy_ai"), "default" if self.config.get("galaxy_ai") else "none"

    async def set_galaxy_ai(self, gid: str, cfg: dict | None) -> None:
        if not self.store.galaxy(gid):
            raise ValueError("unknown galaxy")
        if cfg is not None:
            provider, model = cfg.get("provider"), cfg.get("model")
            offered = next((p for p in self.api_broker.catalog(gid) if p["id"] == provider), None)
            if not offered or model not in offered["models"] or offered["model_backends"].get(model) != "claude":
                raise ValueError("Galaxy AI needs a Claude model offered to this galaxy by an online provider")
            cfg = {"provider": provider, "model": model}
        overrides = self.store.setting("galaxy_ai") or {}
        if cfg is None:
            overrides.pop(gid, None)
        else:
            overrides[gid] = cfg
        self.store.set_setting("galaxy_ai", overrides)
        conn = self.galaxies.get(gid)
        if conn and "hub_v1" in conn.capabilities:
            await conn.send({"type": "galaxy_ai", "config": self.galaxy_ai_for(gid)[0]})

    def runtime_for(self, a: dict) -> dict | None:
        overrides = self.store.setting("runtimes") or {}
        default = (self.config.get('feishu') or {}).get('runtime') if a['role'] == Role.NYARLATHOTEP else None
        if a["role"] == Role.OLD_ONE:
            # Per old_one > its galaxy's default ("galaxy:<id>") > all old_ones > temple config.
            gid = a.get("galaxy_id") or (self.store.old_one(a["id"]) or {}).get("galaxy_id")
            default = (overrides.get(f"galaxy:{gid}") or overrides.get(Role.OLD_ONE)
                       or self.config.get("old_one_runtime") or validate_runtime(OLD_ONE_RUNTIME))
            return overrides.get(a["id"]) or default
        return overrides.get(a["id"]) or overrides.get(a["role"]) or default

    def covers(self, key: str, r: dict) -> bool:
        """Whether a runtime setting key applies to this old_one (its own, its galaxy's, or all old_ones')."""
        return r["id"] == key or key == Role.OLD_ONE or key == f"galaxy:{r['galaxy_id']}"

    async def set_runtime(self, key: str, cfg: dict | None) -> None:
        if cfg is not None:
            cfg = dict(cfg)
            if cfg.get("fallback"):
                cfg["fallback"] = dict(cfg["fallback"])
            for choice in (cfg, cfg.get("fallback") or {}):
                if choice.get("provider") and choice.get("backend") in ("galaxy_api", "claude"):
                    offered = next((p for p in self.api_broker.catalog() if p["id"] == choice["provider"]), None)
                    if offered and choice.get("model") in offered["models"]:
                        native = offered["model_backends"][choice["model"]]
                        choice["backend"] = "claude" if native == "claude" else "galaxy_api"
                        if native == "claude":
                            choice.pop("service_tier", None)
            cfg = validate_runtime(cfg)
            for choice in (cfg, cfg.get("fallback") or {}):
                if choice.get("provider"):
                    targets = [a for a in self.store.agents(include_stopped=False)
                               if a["id"] == key or a["role"] == key] + [
                        r | {"role": Role.OLD_ONE} for r in self.store.roster() if self.covers(key, r)]
                    if key.startswith("galaxy:") and self.store.galaxy(key[7:]):
                        targets.append({"id": key, "role": Role.OLD_ONE, "galaxy_id": key[7:]})
                    for a in targets:
                        conn = self.agents.get(a["id"]) if a["role"] != Role.OLD_ONE else self.galaxies.get(a["galaxy_id"])
                        capability = "galaxy_api_claude_v1" if choice["backend"] == "claude" else "galaxy_api_v1"
                        if not conn or capability not in conn.capabilities:
                            raise ValueError("target agent requires the Galaxy API protocol upgrade")
                        offered = self.api_broker.catalog(a["galaxy_id"])
                        if not any(x["id"] == choice["provider"] and choice["model"] in x["models"] for x in offered):
                            raise ValueError("no eligible online provider for target")
        overrides = self.store.setting("runtimes") or {}
        if cfg is None:
            overrides.pop(key, None)
        else:
            overrides[key] = cfg
        self.store.set_setting("runtimes", overrides)
        for a in self.store.agents(include_stopped=False):
            if a["id"] == key or a["role"] == key:
                conn = self.agents.get(a["id"])
                if conn:
                    await conn.send({"type": "runtime", "config": self.runtime_for(a)})
        for r in self.store.roster():
            if self.covers(key, r):
                conn = self.galaxies.get(r["galaxy_id"])
                if conn:
                    await conn.send({"type": "runtime", "agent_id": r["id"],
                                     "config": self.runtime_for({"id": r["id"], "role": Role.OLD_ONE, "galaxy_id": r["galaxy_id"]})})

    # owner

    # deployment: the owner and azathoth deploy

    async def deploy_call(self, role: str, agent_id: str, method: str, p: dict):
        d = self.deployer
        if role not in roles.DEPLOY:
            raise Denied(f"{role} may not deploy")
        if method == "deploy_status":
            return d.status()
        if p.get("recover_job_id"):
            return await d.recover(p["recover_job_id"], by=agent_id)
        return await d.start(p.get("galaxies") or "all", by=agent_id, reason=str(p.get("reason", "")))

    async def notify_deploy(self, job: dict) -> None:
        """Tell azathoth how a deploy ended (it relays to the owner like any report)."""
        az = self.god(Role.AZATHOTH)
        lines = [f"- {','.join(g['galaxies']) or 'temple'}{' (+temple)' if g['temple'] else ''}: {g['state']}"
                 + (f" — {g['error']}" if g.get("error") else "") for g in job["groups"]]
        text = (f"[deploy {job['id']} {job['state']}] revision {job['revision'][:12]}, started by {job['by']}"
                + (f", reason: {job['reason']}" if job["reason"] else "") + "\n" + "\n".join(lines))
        try:
            await self.route(SYSTEM, az["id"], text, task_id=job.get("task_id"))
        except Exception:
            log.exception("could not report deploy result")

    async def owner_send(self, to: str, text: str, priority: int = OWNER_PRIORITY) -> dict:
        if self.store.old_one(to):
            return await self.route(OWNER, to, text, kind="message", priority=priority)
        target = self.store.agent(to)
        if target and target["desired"] != "running":
            raise Denied(f"{target['name']} is stopped; resume it first")
        mid = new_id("msg")
        self.store.add_turn(mid, to, text)
        msg = await self.route(OWNER, to, text, kind="chat", priority=priority, message_id=mid)
        await self.broadcast({"type": "chat", "turn": self.store.turn(mid)})
        return msg

    async def owner_stop(self, agent_id: str) -> bool:
        if self.store.old_one(agent_id):
            return bool((await self.control_old_one("interrupt", agent_id)).get("ok"))
        conn = self.agents.get(agent_id)
        if conn is None:
            return False
        await conn.rpc("interrupt", timeout=10)
        return True

    # websocket endpoint

    async def ws_endpoint(self, ws: WebSocket) -> None:
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
            conn = await self._authenticate(ws, hello)
        except Exception as e:
            log.info("handshake failed: %s", e)
            try:
                await ws.send_text(dumps({"type": "rejected", "reason": str(e)}))
                await ws.close(4003)
            except Exception:
                pass
            return
        try:
            async for raw in ws.iter_text():
                frame = json.loads(raw)
                if len(conn.tasks) >= 128:
                    await ws.close(4008, "too many concurrent calls")
                    break
                task = asyncio.create_task(self._on_frame(conn, frame))
                conn.tasks.add(task)
                task.add_done_callback(conn.tasks.discard)
        except (WebSocketDisconnect, RuntimeError):
            pass                                 # RuntimeError: we closed it ourselves (revoked agent)
        finally:
            await self._disconnect(conn)

    async def _authenticate(self, ws: WebSocket, hello: dict) -> Conn:
        kind, cid, token = hello.get("kind"), hello.get("id"), hello.get("token", "")
        if kind == "maintenance":
            if (not ws.client or ws.client.host not in ("127.0.0.1", "::1") or
                    not hmac.compare_digest(token, maintenance_token(self.secret))):
                raise Denied("local maintenance authentication required")
            conn = Conn(ws, "maintenance", secrets.token_hex(8), capabilities=["galaxy_api_v1", "galaxy_api_claude_v1"], version=BUILD)
            self.maintainers.add(conn)
            await conn.send({"type": "welcome", "api_version": API_VERSION, "version": BUILD})
        elif kind == "galaxy":
            g = self.store.galaxy(cid)
            if not g or not hmac.compare_digest(g["token_hash"], sha256(token)):
                raise Denied("bad galaxy credential")
            conn = Conn(ws, "galaxy", cid, capabilities=hello.get("capabilities", []), version=str(hello.get("version", ""))[:64],
                        deploy=hello.get("deploy") or {} if "deploy_v1" in hello.get("capabilities", []) else {})
            old = self.galaxies.get(cid)
            self.galaxies[cid] = conn
            if old:
                await old.ws.close(4002, "replaced")
            await conn.send({"type": "welcome", "id": cid, "name": g["name"], "api_version": API_VERSION, "version": BUILD,
                             "galaxy_ai": self.galaxy_ai_for(cid)[0]})
            await self.sync_galaxy(cid)
            if "hub_v1" in conn.capabilities:
                await conn.send({"type": "bindings", "rows": self.bindings()})
        elif kind == "agent":
            a = self.store.agent(cid)
            if not a or a["desired"] != "running" or not hmac.compare_digest(
                    agent_token(self.secret, cid, a["epoch"]), token):
                raise Denied("bad or revoked agent credential")
            conn = Conn(ws, "agent", cid, capabilities=hello.get("capabilities", []), version=str(hello.get("version", ""))[:64])
            old = self.agents.get(cid)
            self.agents[cid] = conn
            if old:
                await old.ws.close(4002, "replaced")
            await conn.send({"type": "welcome", "agent": self.public_agent(a) | {"spec": a["spec"]},
                             "memory": self.store.memory(cid), "thread_id": a["thread_id"],
                             "runtime": self.runtime_for(a), "draining": a["galaxy_id"] in self.maintenance,
                             "pending": self._envelopes(self.deliverable(a))})
            for m in self.deliverable(a):
                if m["state"] == "queued":
                    self.store.mark(m["id"], "delivered")
        else:
            raise Denied("unknown client kind")
        await self.broadcast({"type": "directory", **self.directory()})
        return conn

    def deliverable(self, a: dict) -> list[dict]:
        pending = self.store.pending_for(a["id"])
        return [m for m in pending if m["sender"] == OWNER] if a["galaxy_id"] in self.maintenance else pending

    def _envelopes(self, msgs: list[dict]) -> list[dict]:
        return [m | self.sender_meta(m["sender"]) for m in msgs]

    def sender_meta(self, sender_id: str) -> dict:
        a = self.store.agent(sender_id)
        if a:
            return {"sender_name": a["name"], "sender_role": a["role"]}
        if sender_id == SYSTEM:
            return {"sender_name": "中心神殿", "sender_role": SYSTEM}
        r = self.store.old_one(sender_id)
        if r:
            return {"sender_name": r["name"], "sender_role": Role.OLD_ONE.value}
        if sender_id == OWNER:
            return {"sender_name": "主人", "sender_role": OWNER}
        # Verified external Feishu delegates (feishu:<open_id>) never read as the owner.
        return {"sender_name": "飞书外部", "sender_role": "external"}

    async def _on_frame(self, conn: Conn, frame: dict) -> None:
        t = frame.get("type")
        if t == "rpc_result":
            fut = conn.pending.get(frame["id"])
            if fut and not fut.done():
                fut.set_result(frame)
        elif t == "call":
            try:
                if conn.kind == "agent":
                    if self.agents.get(conn.id) is not conn:
                        raise Denied("stale connection")
                    result = await self.handle_agent_call(conn, frame["method"], frame.get("params") or {})
                elif conn.kind == "maintenance":
                    result = await self.handle_maintenance_call(conn, frame["method"], frame.get("params") or {})
                elif conn.kind == "galaxy":
                    result = await self.handle_galaxy_call(conn, frame["method"], frame.get("params") or {})
                else:
                    raise Denied("owner uses the HTTP API")
                await conn.send({"type": "result", "id": frame["id"], "ok": True, "result": result})
            except Exception as e:
                if not isinstance(e, (Denied, ValueError, KeyError)):
                    log.exception("call %s failed", frame.get("method"))
                await conn.send({"type": "result", "id": frame["id"], "ok": False, "error": str(e)})
        elif t == "stream" and conn.kind == "agent":
            # Live reply deltas for owner chat; forwarded only to browsers.
            await self.broadcast({"type": "stream", "agent_id": conn.id, **{k: frame.get(k) for k in ("turn_id", "delta", "activity")}})

    async def handle_maintenance_call(self, conn, method, p):
        if conn not in self.maintainers:
            raise Denied("stale maintenance connection")
        if method == "maintenance_snapshot":
            return {"version": BUILD, **self.directory(), "providers": self.api_broker.catalog(),
                    "active_streams": len(self.api_broker.streams)}
        if method == "maintenance_runtime":
            if not self.store.old_one(p["agent_id"]):
                raise ValueError("maintenance runtime requires an existing old_one agent")
            await self.set_runtime(p["agent_id"], p["config"])
            return {"agent_id": p["agent_id"], "config": self.runtime_for({"id": p["agent_id"], "role": Role.OLD_ONE})}
        if method == "maintenance_canary":
            galaxy = self.galaxies.get(p["galaxy"])
            if not galaxy or "galaxy_api_v1" not in galaxy.capabilities:
                raise ValueError("galaxy protocol upgrade required")
            return await galaxy.rpc("api_canary", timeout=1200, provider=p.get("provider", "ali"))
        if method == "maintenance_delete":
            return await self.delete(p, OWNER)
        if method in ("maintenance_stop", "maintenance_resume"):
            if method == "maintenance_stop":
                await self.halt(p["agent_id"], "maintenance", force=True)
            else:
                await self.relaunch(p["agent_id"], "maintenance", only_stopped=True)
            return self.public_agent(self.store.agent(p["agent_id"]))
        if method == "maintenance_drain":
            return await self.maintenance_state(p["galaxy"], p.get("enabled"))
        if method in ("api_catalog", "api_open", "api_next", "api_cancel"):
            return await self.handle_api_call(conn, method, p)
        raise Denied("maintenance scope does not permit this operation")

    async def handle_api_call(self, conn: Conn, method: str, p: dict):
        broker = self.api_broker
        if method == "api_catalog":
            return broker.catalog(broker.consumer(conn))
        if method == "api_open":
            return await broker.open(conn, p)
        if method == "api_next":
            return await broker.next(conn, p)
        if method == "api_cancel":
            return await broker.cancel(conn, p)
        if method == "api_register":
            return broker.register(conn, p)
        if method == "api_cancel_ack":
            return broker.cancelled(conn, p)
        if method == "api_push":
            return await broker.push(conn, p)
        raise Denied("unknown API operation")

    async def handle_galaxy_call(self, conn: Conn, method: str, p: dict) -> Any:
        if self.galaxies.get(conn.id) is not conn:
            raise Denied("stale galaxy connection")
        if method.startswith("api_"):
            return await self.handle_api_call(conn, method, p)
        if method == "agent_exited":
            a = self.store.agent(p["agent_id"])
            if a and a["galaxy_id"] == conn.id:
                self.status[a["id"]] = {"state": "crashed", "current": p.get("reason")}
                await self.broadcast({"type": "directory", **self.directory()})
            return True
        if "hub_v1" not in conn.capabilities:
            raise Denied(f"unknown galaxy call {method}")
        return await self.handle_hub_call(conn, method, p)

    async def handle_hub_call(self, conn: Conn, method: str, p: dict) -> Any:
        """Calls from a galaxy's hub. A hub speaks only for its own galaxy's tasks and old_ones."""
        gid = conn.id
        if method == "roster_update":
            self.store.set_roster(gid, p.get("old_ones") or [])
            await self.broadcast({"type": "directory", **self.directory()})
            return True
        if method == "agent_status":
            r = self.store.old_one(p["agent_id"])
            if r and r["galaxy_id"] == gid:
                self.store.roster_status(p["agent_id"], p["status"])
                await self.broadcast({"type": "status", "agent_id": p["agent_id"], "status": p["status"]})
            return True
        if method == "old_one_runtime":
            r = self.store.old_one(p["agent_id"])
            if r and r["galaxy_id"] != gid:
                raise Denied("old_one belongs to another galaxy")
            return self.runtime_for({"id": p["agent_id"], "role": Role.OLD_ONE})
        if method in ("bound", "task_index_update", "report", "task_finished", "tell_owner"):
            t = self.store.task(p["task_id"]) if p.get("task_id") else None
            if p.get("task_id") and not t:
                raise ValueError(f"unknown task {p['task_id']}")
            if t and t["galaxy_id"] != gid:
                raise Denied("task belongs to another galaxy")
        if method == "bound":
            self.store.set_task(p["task_id"], executor=p["agent_id"], executor_name=p["agent_name"])
            self.store.add_digest(p["task_id"], f"新绑定：{p['task_id']} → {p['agent_name']}"
                                  f"（{'复用' if p['reused'] else '新建'}：{p['reason']}）", False)
            await self.task_changed(p["task_id"])
            return True
        if method == "task_index_update":
            self.store.set_task(p["task_id"], **(p.get("patch") or {}))
            await self.task_changed(p["task_id"])
            return True
        if method == "report":
            return await self.hub_report(gid, p)
        if method == "task_finished":
            await self.route(SYSTEM, self.azathoth_id(), p["text"], kind="task_finished", priority=roles.FINISH_PRIORITY,
                             task_id=p["task_id"])
            return True
        if method == "tell_owner":
            await self.note_owner(p["text"], origin="星系枢纽", task_id=p.get("task_id"), kind="blocked")
            return True
        if method == "relay":
            r = self.store.old_one(p["to"])
            sender = self.store.old_one(p["envelope"].get("sender", ""))
            if not r or not sender or sender["galaxy_id"] != gid:
                raise Denied("relay is only between old_ones")
            return await self.hub(r["galaxy_id"]).rpc("hub_deliver", timeout=30, envelope=p["envelope"] | {"receiver": p["to"]})
        if method == "task_view":
            return await self.task_view(p["task_id"])
        raise Denied(f"unknown hub call {method}")

    async def _disconnect(self, conn: Conn) -> None:
        await self.api_broker.disconnect(conn)
        self.maintainers.discard(conn)
        for task in list(conn.tasks):
            task.cancel()
        if conn.tasks:
            await asyncio.gather(*conn.tasks, return_exceptions=True)
        if conn.kind == "agent" and self.agents.get(conn.id) is conn:
            del self.agents[conn.id]
            self.status.pop(conn.id, None)
        elif conn.kind == "galaxy" and self.galaxies.get(conn.id) is conn:
            del self.galaxies[conn.id]
        elif conn.kind == "owner":
            self.watchers.discard(conn)
            return
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))
        await self.broadcast({"type": "directory", **self.directory()})

    async def maintenance_state(self, gid, enabled=None):
        if not self.store.galaxy(gid):
            raise ValueError("unknown galaxy")
        if enabled is not None:
            if enabled:
                self.maintenance.add(gid)
            else:
                self.maintenance.discard(gid)
            self.store.set_setting("maintenance", sorted(self.maintenance))
        results = []
        conn = self.galaxies.get(gid)
        if conn and "hub_v1" in conn.capabilities:
            try:
                results += await conn.rpc("hub_drain", timeout=30, enabled=enabled)
            except Exception:
                results.append({"id": f"hub:{gid}", "safe": False, "reason": "hub drain status unavailable"})
        for a in self.store.agents(gid, include_stopped=False):
            conn = self.agents.get(a["id"])
            result = {"id": a["id"], "safe": False, "reason": "offline or legacy agent; operator verification required"}
            if conn and "drain_v1" in conn.capabilities:
                try:
                    state = await conn.rpc("drain" if enabled is not None else "drain_status",
                                           timeout=10, **({"enabled": enabled} if enabled is not None else {}))
                    result = {"id": a["id"], **state}
                except Exception:
                    result["reason"] = "drain status unavailable"
            results.append(result)
            if enabled is False and conn:
                for message in self.store.pending_for(a["id"]):
                    await self.deliver(message)
        return {"galaxy": gid, "draining": gid in self.maintenance, "agents": results,
                "safe_to_restart": gid in self.maintenance and all(a["safe"] for a in results)}

    async def health(self, request):
        return JSONResponse({"ok": True, "version": BUILD, "api_version": API_VERSION})

    # HTTP API for the owner

    def _session(self, request: Request) -> str | None:
        token = request.cookies.get(self.cookie)
        sess = token and self.store.session(sha256(token))
        return sess["username"] if sess else None

    def _guard(self, request: Request) -> str:
        user = self._session(request)
        if not user:
            raise PermissionError("login required")
        if request.method != "GET" and request.headers.get("x-cthulhu") != "1":
            raise PermissionError("missing csrf header")
        return user

    async def api(self, request: Request) -> Response:
        path = request.path_params["path"]
        try:
            if path == "login" and request.method == "POST":
                return await self._login(request)
            if path == "session":
                return JSONResponse({"user": self._session(request)})
            self._guard(request)
            body = await request.json() if request.method == "POST" else {}
            q = request.query_params
            if path == "logout":
                token = request.cookies.get(self.cookie, "")
                self.store.drop_session(sha256(token))
                resp = JSONResponse({"ok": True})
                resp.delete_cookie(self.cookie)
                return resp
            if path == "maintenance":
                return JSONResponse(await self.maintenance_state(
                    body["galaxy"] if request.method == "POST" else q["galaxy"],
                    bool(body["enabled"]) if request.method == "POST" else None))
            if path == "providers":
                return JSONResponse({"providers": self.api_broker.catalog(), "active_streams": len(self.api_broker.streams)})
            if path == "directory":
                return JSONResponse(self.directory() | {"gods": {r: (self.god(r) or {}).get("id") for r in roles.GODS}})
            if path == "approvals":
                if request.method == "POST":
                    return JSONResponse(await self.sudo_decide(body["id"], bool(body["approve"]), body.get("note", "")))
                return JSONResponse(self.store.approvals(q.get("state") or None, limit=min(int(q.get("limit", 50)), 200)))
            if path == "chat":
                agent = q.get("agent_id") or self.god(Role.AZATHOTH)["id"]
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.turns(agent, limit=int(q.get("limit", 40)), before=before,
                                                     search=q.get("search") or None))
            if path == "send":
                if self.role_of(body["to"]) not in roles.SEND[OWNER]:
                    raise Denied("this role does not accept owner messages")
                return JSONResponse(await self.owner_send(body["to"], body["text"],
                                                          roles.URGENT if body.get("urgent") else OWNER_PRIORITY))
            if path == "stop":
                return JSONResponse({"ok": await self.owner_stop(body["agent_id"])})
            if path == "delete":
                return JSONResponse({"ok": await self.delete(body, OWNER)})
            if path == "task":
                if request.method == "POST":
                    # The owner acts with azathoth's task powers.
                    action = body["action"]
                    if action == "close":
                        return JSONResponse(await self.task_command("hub_close", body["task_id"], note=body.get("note", "主人验收")))
                    if action == "continue":
                        return JSONResponse(await self.task_command("hub_continue", body["task_id"], instruction=body["instruction"]))
                    if action == "cancel":
                        return JSONResponse(await self.task_command("hub_cancel", body["task_id"], reason=body.get("reason", "主人取消")))
                    raise ValueError(f"unknown action {action}")
                return JSONResponse(await self.task_view(q["task_id"]))
            if path in ("force_stop", "resume"):
                if path == "force_stop":
                    await self.halt(body["agent_id"], "主人", force=True)
                else:
                    await self.relaunch(body["agent_id"], "主人", only_stopped=True)
                a = self.store.agent(body["agent_id"])
                return JSONResponse(self.public_agent(a) if a else {"id": body["agent_id"], "ok": True})
            if path == "hub_messages":
                r = self.store.old_one(q["agent_id"])
                if not r:
                    raise ValueError("unknown old_one")
                return JSONResponse(await self.hub(r["galaxy_id"]).rpc("hub_messages", timeout=30, agent_id=q["agent_id"],
                                                                       limit=int(q.get("limit", 100))))
            if path == "messages":
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.messages(q.get("agent_id") or None,
                                                        limit=min(int(q.get("limit", 100)), 500), before=before,
                                                        task_id=q.get("task_id") or None, before_id=q.get("before_id") or None))
            if path == "tasks":
                return JSONResponse(self.store.tasks(q.get("search") or None,
                    limit=max(1, min(int(q.get("limit", 50)), 100)), before_id=q.get("before_id") or None,
                    state=q.get("state") or None, galaxy_id=q.get("galaxy_id") or None))
            if path == "memory":
                if request.method == "POST":
                    # The owner corrects or removes an entry by hand.
                    if body.get("delete"):
                        self.store.forget(body["agent_id"], int(body["id"]))
                    else:
                        self.store.revise(body["agent_id"], int(body["id"]), body["text"].strip(), "owner")
                    return JSONResponse(self.store.memory(body["agent_id"]))
                return JSONResponse(self.store.memory(q["agent_id"]))
            if path == "runtimes":
                if request.method == "POST":
                    await self.set_runtime(body["key"], body.get("config"))
                providers = self.api_broker.catalog()
                choices = {k: v for k, v in MODEL_CHOICES.items() if k != "galaxy_api"}
                if providers:
                    choices["galaxy_api"] = sorted({m for p in providers for m in p["models"]})
                defaults = {f"galaxy:{g['id']}": self.runtime_for({"id": "", "role": Role.OLD_ONE, "galaxy_id": g["id"]})
                            for g in self.store.galaxies() if g["id"] not in self.closed_galaxies}
                return JSONResponse({"overrides": self.store.setting("runtimes") or {},
                                     "models": choices, "providers": providers, "old_one_defaults": defaults})
            if path == "galaxy_ai":
                if request.method == "POST":
                    await self.set_galaxy_ai(body["galaxy"], body.get("config"))
                rows = []
                for g in self.store.galaxies():
                    conn = self.galaxies.get(g["id"])
                    if conn is None or "hub_v1" not in conn.capabilities:
                        continue
                    cfg, source = self.galaxy_ai_for(g["id"])
                    models = sorted({(p["id"], m) for p in self.api_broker.catalog(g["id"])
                                     for m, b in p["model_backends"].items() if b == "claude"})
                    rows.append({"id": g["id"], "name": g["name"], "config": cfg, "source": source,
                                 "choices": [{"provider": p, "model": m} for p, m in models]})
                return JSONResponse(rows)
            if path == "file":
                # Text preview: up to 2 MB inline. Media and downloads go through /api/raw.
                r = await self.read_file(q["path"], q.get("galaxy") or None, FILE_PREVIEW_BYTES)
                kind = file_kind(r["path"]) if not r.get("is_dir") else "dir"
                r["kind"] = kind
                if kind in ("image", "video", "audio", "pdf"):
                    r.pop("data", None)
                elif "data" in r:
                    raw = base64.b64decode(r.pop("data"))
                    r["text"] = raw.decode("utf-8", errors="replace")
                    if b"\0" in raw[:4096]:
                        r["kind"], r["text"] = "binary", None
                return JSONResponse(r)
            if path == "raw":
                r = await self.read_file(q["path"], q.get("galaxy") or None, FILE_RAW_BYTES)
                if r.get("is_dir") or "data" not in r:
                    return JSONResponse({"error": r.get("error") or "not a file"}, status_code=400)
                mime = mimetypes.guess_type(r["path"])[0] or "application/octet-stream"
                headers = {"cache-control": "private, max-age=60", "x-content-type-options": "nosniff",
                           "content-security-policy": "sandbox"}
                if q.get("download"):
                    headers["content-disposition"] = f"attachment; filename*=UTF-8''{quote(Path(r['path']).name)}"
                return Response(base64.b64decode(r["data"]), media_type=mime, headers=headers)
            if path == "deploy":
                if request.method == "POST":
                    if body.get("recover_job_id"):
                        return JSONResponse(await self.deployer.recover(body["recover_job_id"], by="主人"))
                    return JSONResponse(await self.deployer.start(body.get("galaxies") or "all", by="主人",
                                                                  reason=body.get("reason", "")))
                return JSONResponse(self.deployer.status())
            if path == "deploy_latest":
                return JSONResponse({"revision": await self.deployer.latest()})
            if path == "latest":
                return JSONResponse(await self.latest_result(q["agent_id"]))
            return JSONResponse({"error": "not found"}, status_code=404)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        except Denied as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except FileNotFoundError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad request: {e}"}, status_code=400)

    async def _login(self, request: Request) -> Response:
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
        now = time.time()
        recent = [t for t in self.login_failures.get(ip, []) if now - t < 600]
        if len(recent) >= 8:
            return JSONResponse({"error": "too many attempts, try later"}, status_code=429)
        body = await request.json()
        acct = self.store.account(body.get("username", ""))
        ok = acct and hmac.compare_digest(
            acct["password_hash"], await asyncio.to_thread(password_hash, body.get("password", ""), acct["salt"]))
        if not ok:
            self.login_failures[ip] = recent + [now]
            return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
        token = secrets.token_urlsafe(32)
        self.store.add_session(sha256(token), acct["username"], SESSION_TTL)
        resp = JSONResponse({"user": acct["username"]})
        resp.set_cookie(self.cookie, token, max_age=SESSION_TTL, httponly=True, secure=True, samesite="strict")
        return resp

    async def owner_ws(self, ws: WebSocket) -> None:
        # Browser websocket: authenticated by cookie, read-only event stream.
        token = ws.cookies.get(self.cookie, "")
        if not self.store.session(sha256(token)):
            await ws.close(4003)
            return
        await ws.accept()
        conn = Conn(ws, "owner", "owner")
        self.watchers.add(conn)
        await conn.send({"type": "welcome", **self.directory()})
        try:
            async for _ in ws.iter_text():
                pass
        except WebSocketDisconnect:
            pass
        finally:
            self.watchers.discard(conn)

    def app(self) -> Starlette:
        static = Path(self.config.get("static", Path(__file__).parent / "static"))
        index = static / "index.html"

        async def spa(_request: Request) -> Response:
            if index.exists():
                return FileResponse(index, headers={"cache-control": "no-cache"})
            return Response("web UI not built", status_code=503)

        routes = [
            WebSocketRoute("/ws", self.ws_endpoint),
            Route("/health", self.health),
            WebSocketRoute("/api/events", self.owner_ws),
            Route("/api/{path:path}", self.api, methods=["GET", "POST"]),
        ]
        if (static / "assets").exists():
            routes.append(Mount("/assets", StaticFiles(directory=static / "assets")))
        routes.append(Route("/{path:path}", spa))

        @contextlib.asynccontextmanager
        async def lifespan(_app):
            asyncio.create_task(self.deployer.resume())
            self.expire_sudo()
            clock = asyncio.create_task(self.digest_clock())
            yield
            clock.cancel()
        return Starlette(routes=routes, lifespan=lifespan)
