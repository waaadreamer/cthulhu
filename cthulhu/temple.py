"""Temple: the central server.

Holds the agent registry, routes every message, enforces the authority matrix
and serves the owner web UI. Agents and galaxies connect over websockets and
are identified by the token in their hello frame; nothing they claim about
themselves is trusted.
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
from .roles import OWNER, Denied, Role
from .store import Store, new_id


def clip(text: str | None, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"

log = logging.getLogger(__name__)

SESSION_TTL = 30 * 86400
OWNER_PRIORITY = 9
TASK_STATUSES = ("progress", "blocked")   # labels on a manager's report; tasks close only by verdict
SYSTEM = "temple"   # sender id of the temple's own notices
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
        self.store = Store(Path(config["db"]))
        from .tasks import Discipline
        self.discipline = Discipline(self)
        self.agents: dict[str, Conn] = {}
        self.galaxies: dict[str, Conn] = {}
        self.maintainers: set[Conn] = set()
        self.watchers: set[Conn] = set()     # owner browsers
        self.status: dict[str, dict] = {}    # live agent status reported by agents
        self.login_failures: dict[str, list[float]] = {}
        self.api_broker = ProviderBroker(self)
        self.maintenance = set(self.store.setting("maintenance") or [])
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
        if agent_id == OWNER:
            return OWNER
        a = self.store.agent(agent_id)
        return a["role"] if a else None

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

    def directory(self) -> dict:
        pending = self.store.pending_counts()
        memory = self.store.memory_stats()
        agents = [self.public_agent(a) | {"queued": pending.get(a["id"], 0),
                                          "memory": memory.get(a["id"], {"entries": 0, "tokens": 0})}
                  for a in self.store.agents()]
        galaxies = [g | {"online": g["id"] in self.galaxies, "version": getattr(self.galaxies.get(g["id"]), "version", ""),
                         "revision": (getattr(self.galaxies.get(g["id"]), "deploy", None) or {}).get("revision", ""),
                         "deployable": bool(getattr(self.galaxies.get(g["id"]), "deploy", None)),
                         "draining": g["id"] in self.maintenance, "spawn": g["id"] not in self.closed_galaxies}
                    for g in self.store.galaxies()]
        return {"agents": agents, "galaxies": galaxies}

    async def broadcast(self, frame: dict) -> None:
        for w in list(self.watchers):
            if not await w.send(frame):
                self.watchers.discard(w)

    # messaging

    async def route(self, sender: str, receiver: str, text: str, *, kind="message",
                    priority=0, reply_to=None, message_id=None, task_id=None, status=None) -> dict:
        task = self.store.task(task_id) if task_id else None
        if task and task['data'].get('cancellation') and receiver == task['executor']:
            raise Denied('cancelled task cannot receive new executor input')
        sender_role, receiver_role = self.role_of(sender), self.role_of(receiver)
        if receiver_role is None:
            raise Denied(f"unknown agent {receiver}")
        target = self.store.agent(receiver)
        if target and target["desired"] != "running":
            raise Denied(f"{target['name']} is stopped; messages are rejected until it is resumed")
        if sender != SYSTEM:                     # the temple's own notices (deploys, task discipline)
            roles.require_send(sender_role, receiver_role)
            # A manager and an old_one talk only within the manager's own team.
            for manager, worker in ((sender, receiver), (receiver, sender)):
                if self.role_of(manager) in roles.MANAGERS and self.role_of(worker) == Role.OLD_ONE \
                        and self.store.agent(worker)["parent_id"] != manager:
                    raise Denied(f"{worker} belongs to another manager")
        priority = int(priority)
        roles.require_priority(sender_role, priority)
        if Role.NYARLATHOTEP in (sender_role, receiver_role):
            other = receiver if sender_role == Role.NYARLATHOTEP else sender
            other_role = self.role_of(other)
            if other_role not in (OWNER, Role.AZATHOTH, Role.SHUB, SYSTEM):
                if not task_id or not self.store.task(task_id):
                    raise Denied('Nyarl peer communication requires a task')
                links = self.store.q("SELECT sender,receiver FROM messages WHERE task_id=? AND kind!='deploy_request'", (task_id,))
                linked = {m[k] for m in links for k in ('sender', 'receiver')}
                associated = self.store.agent(other)
                if other_role != Role.YOG and other not in linked and (associated or {}).get('spec', {}).get('task_id') != task_id:
                    raise Denied('peer is not associated with this task')
                nyarl = sender if sender_role == Role.NYARLATHOTEP else receiver
                if nyarl not in linked:
                    raise Denied('Nyarl has not been delegated this task')
        if not text.strip():
            raise ValueError("empty message")
        msg = self.store.add_message(sender, receiver, text, kind, priority, reply_to, message_id, task_id, status)
        await self.deliver(msg)
        await self.broadcast({"type": "message", "message": msg})
        return msg

    def task_fields(self, sender_role: str, receiver_role: str | None, p: dict) -> tuple[str | None, str | None]:
        """A message may name an existing task; tasks themselves are made only by task_create / spawn.
        A manager's report to azathoth may carry a display label; it never changes the task."""
        task_id = p.get("task_id") or None
        if task_id and not self.store.task(task_id):
            raise ValueError(f"unknown task {task_id}")
        status = p.get("status")
        if sender_role in roles.MANAGERS and receiver_role == Role.AZATHOTH and task_id and status in TASK_STATUSES:
            return task_id, status
        return task_id, None

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
            envelope["task_title"] = task and task["title"]
        if await conn.send({"type": "message", "message": envelope}):
            # Cancellation may retire this input while the socket send is in flight.
            if self.store.message(msg['id'])['state'] == 'queued':
                self.store.mark(msg["id"], "delivered")

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
        if method.startswith("task_"):
            return await self.discipline.call(conn.id, method, p)
        if method == "send":
            task_id, status = self.task_fields(role, self.role_of(p["to"]), p)
            msg = await self.route(conn.id, p["to"], p["text"], kind=p.get("kind", "message"),
                                   priority=int(p.get("priority") or 0), reply_to=p.get("reply_to"),
                                   task_id=task_id, status=status)
            return {"id": msg["id"], "task_id": task_id}
        if method == "handled":
            msg = self.store.message(p["id"])
            if msg and msg["receiver"] == conn.id and msg['state'] != 'cancelled':
                self.store.mark(p["id"], "handled")
            return True
        if method == "directory":
            d = self.directory()
            if role not in roles.ADMIN_READ:
                # Others only see whom they can talk to, without Nyarl's admission policy.
                d["agents"] = [a | {"admission": None} for a in d["agents"] if roles.can_send(role, a["role"]) or a["id"] == conn.id]
            return d
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
        if method == "latest_result":
            if role not in roles.LATEST_RESULT:
                raise Denied("latest_result is reserved for yog")
            return await self.latest_result(p["agent_id"])
        if method == "patrol_log":
            if role != Role.YOG:
                raise Denied("only yog writes the patrol log")
            self.store.add_patrol(p["summary"])
            await self.broadcast({"type": "patrol"})
            return True
        if method in ("deploy", "deploy_request", "deploy_requests", "deploy_status"):
            return await self.deploy_call(role, conn.id, method, p)
        if method == "notify_owner":
            if role != Role.AZATHOTH:
                raise Denied("only azathoth speaks to the owner unprompted")
            await self.note_owner(p["text"], model=p.get("model"), origin=p.get("origin"), task_id=p.get("task_id"),
                                  kind=p.get("kind"), **({"activity": p["activity"]} if p.get("activity") else {}))
            return True
        if method in ("spawn", "stop", "restart", "delete"):
            if role not in roles.MANAGERS:
                raise Denied(f"{role} may not manage old_ones")
            return await getattr(self, method)(p, conn.id)
        if role in roles.ADMIN_READ:
            if method == "messages":
                return self.store.messages(p.get("agent_id"), limit=min(int(p.get("limit", 50)), 500),
                                           search=p.get("search"), task_id=p.get("task_id"))
            if method == "tasks":
                return self.store.tasks(p.get("search"), limit=min(int(p.get("limit", 20)), 100))
            if method == "agent":
                a = self.store.agent(p["agent_id"])
                return a and (self.public_agent(a) | {"spec": a["spec"]})
            if method == "owner_chat":
                az = self.god(Role.AZATHOTH)
                return self.store.turns(az["id"], limit=int(p.get("limit", 20)), search=p.get("search"))
        raise Denied(f"unknown or forbidden call {method}")

    async def latest_result(self, agent_id: str) -> dict:
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

    async def spawn(self, p: dict, creator: str) -> dict:
        """An old_one belongs to the manager that created it and is born bound to a task. Ghroth's work in
        their own git worktree of the harness on ghroth's galaxy (the galaxy creates it)."""
        harness = self.role_of(creator) == Role.GHROTH
        galaxy = self.store.galaxy(self.store.agent(creator)["galaxy_id"] if harness else p["galaxy_id"])
        if not galaxy:
            raise Denied(f"unknown galaxy {p.get('galaxy_id')}")
        if galaxy["id"] in self.closed_galaxies and not harness:
            raise Denied(f"galaxy {galaxy['name']} does not host old_ones")
        if not p.get("task", "").strip():
            raise ValueError("an old_one needs a task")
        aid = new_id("old")
        if p.get('task_id'):
            await self.discipline.prepare_handover(creator, p['task_id'])
        task = self.discipline.bind_spawn(creator, p, aid)
        spec = {"task": p["task"], "skill": p.get("skill", ""), "model": p.get("model"),
                "task_id": task.id, "acceptance": task.acceptance, "deadline": task.deadline,
                **({"worktree": f"old/{aid}"} if harness else {"cwd": p.get("cwd")})}
        a = self.store.upsert_agent(aid, p["name"], Role.OLD_ONE, galaxy["id"], creator, spec)
        # Recorded before the galaxy launches it, so its first welcome already carries this runtime.
        overrides = self.store.setting("runtimes") or {}
        overrides[a["id"]] = validate_runtime(OLD_ONE_RUNTIME)
        self.store.set_setting("runtimes", overrides)
        await self.sync_galaxy(galaxy["id"])
        await self.broadcast({"type": "directory", **self.directory()})
        await self.discipline.changed(task)
        return {"id": a["id"], "name": a["name"], "galaxy_id": galaxy["id"], "task_id": task.id}

    async def stop(self, p: dict, by: str) -> bool:
        return await self.halt(self._managed(p["agent_id"], by)["id"], by)

    async def delete(self, p: dict, by: str) -> bool:
        """A stopped agent leaves the directory; its messages and tasks stay in history."""
        a = self._managed(p["agent_id"], by) if by != OWNER else self.store.agent(p["agent_id"])
        if not a or a["desired"] != "stopped":
            raise ValueError("only a stopped agent can be deleted")
        self.store.set_agent(a["id"], desired="deleted")
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    async def restart(self, p: dict, by: str) -> bool:
        a = self._managed(p["agent_id"], by)
        if a["id"] in (self.store.setting("force_stopped") or {}):
            raise Denied(f"{a['name']} was force-stopped by the owner; only the owner can resume it")
        return await self.relaunch(a["id"], by)

    # Shub's old_one stop/restart, and the owner's / maintenance force-stop and resume of any agent.

    async def halt(self, agent_id: str, by: str, force: bool = False) -> bool:
        """Stop an agent now: revoke it (persisted), abort its turn, cancel its pending work, kill its processes.
        A force stop (owner / maintenance) can only be lifted by the owner or maintenance."""
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
        for row in self.store.q("SELECT id FROM tasks WHERE executor=? AND state IN ('active','submitted','blocked_on_owner')", (agent_id,)):
            task = self.discipline.get(row['id'])
            task.log('executor_stopped', '执行者已撤权，未完成合同待实名改派或结束；原截止保留', by=by)
            task.save(self.store)
            await self.discipline.tell(self.discipline.superior(task), f'[executor stopped] {task.brief()}\n'
                                       'Preserve artifacts and reassign or end this contract; other work is not canceled.', task)
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    async def relaunch(self, agent_id: str, by: str, only_stopped: bool = False) -> bool:
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

    def _managed(self, agent_id: str, manager: str) -> dict:
        a = self.store.agent(agent_id)
        if not a or a["role"] != Role.OLD_ONE or a["parent_id"] != manager:
            raise Denied("a manager manages only its own old_ones")
        return a

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

    def runtime_for(self, a: dict) -> dict | None:
        overrides = self.store.setting("runtimes") or {}
        default = (self.config.get('feishu') or {}).get('runtime') if a['role'] == Role.NYARLATHOTEP else None
        return overrides.get(a["id"]) or overrides.get(a["role"]) or default

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
                               if a["id"] == key or a["role"] == key]
                    for a in targets:
                        conn = self.agents.get(a["id"])
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

    # owner

    # deployment: the owner and azathoth deploy; ghroth asks and azathoth decides

    async def deploy_call(self, role: str, agent_id: str, method: str, p: dict):
        d = self.deployer
        if method == "deploy_status":
            if role not in roles.DEPLOY | roles.DEPLOY_REQUEST:
                raise Denied(f"{role} may not deploy")
            return d.status()
        if method == "deploy_request":
            if role not in roles.DEPLOY_REQUEST:
                raise Denied(f"{role} may not request deploys")
            req = d.request(p.get("galaxies") or "all", str(p.get("reason", "")))
            az = self.god(Role.AZATHOTH)
            await self.route(agent_id, az["id"], kind="deploy_request", text=f"[deploy request {req['id']}] Ghroth asks to deploy the latest main "
                             f"to {req['galaxies']}. Reason: {req['reason']}\nApprove with temple_deploy(request_id="
                             f"\"{req['id']}\"), or reject with temple_deploy(request_id=..., reject=true).",
                             task_id=p.get("task_id"))
            return req
        if role not in roles.DEPLOY:
            raise Denied(f"{role} may not deploy")
        if p.get("recover_job_id"):
            return await d.recover(p["recover_job_id"], by=agent_id)
        if method == "deploy_requests":
            return [r for r in d.requests.values() if r["state"] == "pending"]
        if p.get("request_id"):
            req = d.requests.get(p["request_id"])
            if not req or req["state"] != "pending":
                raise ValueError(f"no pending deploy request {p['request_id']}")
            if p.get("reject"):
                req["state"] = "rejected"
                return req
            req["state"] = "approved"
            return await d.start(req["galaxies"], by=f"{agent_id}（批准格赫罗斯的请求）", reason=req["reason"],
                                 task_id=p.get("task_id"))
        return await d.start(p.get("galaxies") or "all", by=agent_id, reason=str(p.get("reason", "")),
                             task_id=p.get("task_id"))

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
        target = self.store.agent(to)
        if target and target["desired"] != "running":
            raise Denied(f"{target['name']} is stopped; resume it first")
        mid = new_id("msg")
        self.store.add_turn(mid, to, text)
        msg = await self.route(OWNER, to, text, kind="chat", priority=priority, message_id=mid)
        await self.broadcast({"type": "chat", "turn": self.store.turn(mid)})
        return msg

    async def owner_stop(self, agent_id: str) -> bool:
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
            await conn.send({"type": "welcome", "id": cid, "name": g["name"], "api_version": API_VERSION, "version": BUILD})
            await self.sync_galaxy(cid)
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
            agent = self.store.agent(p["agent_id"])
            if not agent or agent["role"] != Role.OLD_ONE:
                raise ValueError("maintenance runtime requires an existing old_one agent")
            await self.set_runtime(agent["id"], p["config"])
            return {"agent_id": agent["id"], "config": self.runtime_for(agent)}
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
        raise Denied(f"unknown galaxy call {method}")

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
        token = request.cookies.get("cthulhu_session")
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
                token = request.cookies.get("cthulhu_session", "")
                self.store.drop_session(sha256(token))
                resp = JSONResponse({"ok": True})
                resp.delete_cookie("cthulhu_session")
                return resp
            if path == "maintenance":
                return JSONResponse(await self.maintenance_state(
                    body["galaxy"] if request.method == "POST" else q["galaxy"],
                    bool(body["enabled"]) if request.method == "POST" else None))
            if path == "providers":
                return JSONResponse({"providers": self.api_broker.catalog(), "active_streams": len(self.api_broker.streams)})
            if path == "directory":
                return JSONResponse(self.directory() | {"gods": {r: (self.god(r) or {}).get("id") for r in roles.GODS}})
            if path == "chat":
                agent = q.get("agent_id") or self.god(Role.AZATHOTH)["id"]
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.turns(agent, limit=int(q.get("limit", 40)), before=before,
                                                     search=q.get("search") or None))
            if path == "send":
                if self.role_of(body["to"]) not in roles.SEND[OWNER]:
                    raise Denied("this role does not accept owner chat")
                return JSONResponse(await self.owner_send(body["to"], body["text"],
                                                          roles.URGENT if body.get("urgent") else OWNER_PRIORITY))
            if path == "stop":
                return JSONResponse({"ok": await self.owner_stop(body["agent_id"])})
            if path == "delete":
                return JSONResponse({"ok": await self.delete(body, OWNER)})
            if path == "task":
                if request.method == "POST":
                    return JSONResponse(await self.discipline.owner(body))
                return JSONResponse(self.discipline.view(q["task_id"]))
            if path in ("force_stop", "resume"):
                if path == "force_stop":
                    await self.halt(body["agent_id"], "主人", force=True)
                else:
                    await self.relaunch(body["agent_id"], "主人", only_stopped=True)
                return JSONResponse(self.public_agent(self.store.agent(body["agent_id"])))
            if path == "messages":
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.messages(q.get("agent_id") or None,
                                                        limit=min(int(q.get("limit", 100)), 500), before=before,
                                                        task_id=q.get("task_id") or None, before_id=q.get("before_id") or None))
            if path == "tasks":
                return JSONResponse(self.store.tasks(q.get("search") or None,
                    limit=max(1, min(int(q.get("limit", 50)), 100)), before_id=q.get("before_id") or None,
                    state=q.get("state") or None, history=True))
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
                return JSONResponse({"overrides": self.store.setting("runtimes") or {},
                                     "models": choices, "providers": providers})
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
            if path == "patrols":
                return JSONResponse(self.store.patrols(int(q.get("limit", 10))))
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
        resp.set_cookie("cthulhu_session", token, max_age=SESSION_TTL, httponly=True, secure=True, samesite="strict")
        return resp

    async def owner_ws(self, ws: WebSocket) -> None:
        # Browser websocket: authenticated by cookie, read-only event stream.
        token = ws.cookies.get("cthulhu_session", "")
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
            clock = asyncio.create_task(self.discipline.run())
            yield
            clock.cancel()
        return Starlette(routes=routes, lifespan=lifespan)
