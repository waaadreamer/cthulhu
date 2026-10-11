"""Temple: the central server.

Holds the agent registry, routes every message, enforces the authority matrix
and serves the owner web UI. Agents and galaxies connect over websockets and
are identified by the token in their hello frame; nothing they claim about
themselves is trusted.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import roles
from .protocol import agent_token, dumps, sha256
from .roles import OWNER, Denied, Role
from .store import Store, new_id

log = logging.getLogger(__name__)

SESSION_TTL = 30 * 86400
OWNER_PRIORITY = 10
TASK_STATUSES = ("progress", "blocked", "done")


@dataclass(eq=False)
class Conn:
    ws: WebSocket
    kind: str                 # agent | galaxy | owner
    id: str
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    next_id: int = 0

    async def send(self, frame: dict) -> bool:
        try:
            await self.ws.send_text(dumps(frame))
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
    "codex": ["gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-sol"],
}
EFFORTS = {None, "low", "medium", "high", "xhigh", "max"}


def validate_runtime(cfg: dict, nested: bool = False) -> dict:
    backend = cfg.get("backend")
    if backend not in MODEL_CHOICES:
        raise ValueError(f"unknown backend {backend}")
    if not cfg.get("model"):
        raise ValueError("model is required")
    if cfg.get("effort") not in EFFORTS:
        raise ValueError(f"bad effort {cfg.get('effort')}")
    out = {k: cfg[k] for k in ("backend", "model", "effort") if cfg.get(k)}
    if not nested:
        for k in ("memory_model", "memory_effort"):
            if cfg.get(k):
                out[k] = cfg[k]
        if cfg.get("fallback"):
            fb = validate_runtime(cfg["fallback"], nested=True)
            if fb["backend"] == out["backend"] and fb["model"] == out["model"]:
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
        self.agents: dict[str, Conn] = {}
        self.galaxies: dict[str, Conn] = {}
        self.watchers: set[Conn] = set()     # owner browsers
        self.status: dict[str, dict] = {}    # live agent status reported by agents
        self.login_failures: dict[str, list[float]] = {}
        self._seed()

    def _seed(self) -> None:
        for g in self.config.get("galaxies", []):
            self.store.add_galaxy(g["id"], g["name"], sha256(g["token"]))
        self.closed_galaxies = {g["id"] for g in self.config.get("galaxies", []) if g.get("spawn") is False}
        for a in self.config.get("gods", []):
            self.store.upsert_agent(a["id"], a.get("name") or roles.DISPLAY[Role(a["role"])], a["role"],
                                    a["galaxy"], spec=a.get("spec") or {})

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
                "runtime": live.get("runtime"),
                "created_at": a["created_at"], "updated_at": a["updated_at"]}

    def directory(self) -> dict:
        pending = self.store.pending_counts()
        memory = self.store.memory_stats()
        agents = [self.public_agent(a) | {"queued": pending.get(a["id"], 0),
                                          "memory": memory.get(a["id"], {"entries": 0, "tokens": 0})}
                  for a in self.store.agents()]
        galaxies = [g | {"online": g["id"] in self.galaxies, "spawn": g["id"] not in self.closed_galaxies}
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
        target = self.store.agent(receiver)
        if target["desired"] != "running":
            raise Denied(f"{target['name']} is stopped")
        roles.require_send(sender_role, receiver_role)
        if not text.strip():
            raise ValueError("empty message")
        msg = self.store.add_message(sender, receiver, text, kind, priority, reply_to, message_id, task_id, status)
        await self.deliver(msg)
        await self.broadcast({"type": "message", "message": msg})
        return msg

    def task_fields(self, sender_role: str, receiver_role: str | None, p: dict) -> tuple[str | None, str | None]:
        """Work azathoth hands to shub is a task; shub's reports on it must say whether it is finished."""
        task_id = p.get("task_id") or None
        if task_id and not self.store.task(task_id):
            raise ValueError(f"unknown task {task_id}")
        if sender_role == Role.AZATHOTH and receiver_role == Role.SHUB:
            if not task_id:
                title = (p.get("title") or p["text"]).strip().splitlines()[0][:60]
                task_id = self.store.add_task(title)["id"]
            elif self.store.task(task_id)["state"] == "done":
                self.store.set_task(task_id, "open")   # azathoth reopened it with a follow-up
            return task_id, None
        if sender_role == Role.SHUB and receiver_role == Role.AZATHOTH:
            status = p.get("status")
            if not task_id or status not in TASK_STATUSES:
                raise ValueError("reports to azathoth need task_id and status (progress | blocked | done)")
            return task_id, status
        return None, None

    async def deliver(self, msg: dict) -> None:
        conn = self.agents.get(msg["receiver"])
        if conn is None:
            return
        sender = self.store.agent(msg["sender"])
        envelope = msg | {"sender_name": sender["name"] if sender else "主人",
                          "sender_role": sender["role"] if sender else OWNER}
        if msg.get("task_id"):
            task = self.store.task(msg["task_id"])
            envelope["task_title"] = task and task["title"]
        if await conn.send({"type": "message", "message": envelope}):
            if msg["state"] == "queued":
                self.store.mark(msg["id"], "delivered")

    # agent calls: (conn, params) -> result

    async def handle_agent_call(self, conn: Conn, method: str, p: dict) -> Any:
        me = self.store.agent(conn.id)
        role = me["role"]
        if method == "send":
            task_id, status = self.task_fields(role, self.role_of(p["to"]), p)
            msg = await self.route(conn.id, p["to"], p["text"], kind=p.get("kind", "message"),
                                   priority=int(p.get("priority", 0)), reply_to=p.get("reply_to"),
                                   task_id=task_id, status=status)
            if status == "done":
                self.store.set_task(task_id, "done")
            return {"id": msg["id"], "task_id": task_id}
        if method == "handled":
            msg = self.store.message(p["id"])
            if msg and msg["receiver"] == conn.id:
                self.store.mark(p["id"], "handled")
            return True
        if method == "directory":
            d = self.directory()
            if role not in roles.ADMIN_READ:
                # Others only see whom they can talk to.
                d["agents"] = [a for a in d["agents"] if roles.can_send(role, a["role"]) or a["id"] == conn.id]
            return d
        if method == "status":
            self.status[conn.id] = p
            await self.broadcast({"type": "status", "agent_id": conn.id, "status": p})
            return True
        if method == "set_thread":
            self.store.set_agent(conn.id, thread_id=p["thread_id"])
            return True
        if method == "log_turn":
            return self.store.log_turn(conn.id, p["backend"], p.get("model"), p["thread"], p["input"], p["reply"], p["status"])
        if method == "turn_log":
            return self.store.turn_log(conn.id, limit=min(int(p.get("limit", 20)), 100))
        if method == "remember":
            return self.store.remember(conn.id, p["text"], p.get("source"))
        if method == "revise":
            return self.store.revise(conn.id, int(p["memory_id"]), p["text"], p.get("source"))
        if method == "forget":
            return self.store.forget(conn.id, int(p["memory_id"]))
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
        if method == "patrol":
            if role != Role.YOG:
                raise Denied("only yog patrols")
            return await self.patrol()
        if method == "patrol_log":
            if role != Role.YOG:
                raise Denied("only yog patrols")
            self.store.add_patrol(p["summary"])
            await self.broadcast({"type": "patrol"})
            return True
        if method == "patrol_log_recent":
            if role != Role.YOG:
                raise Denied("only yog patrols")
            return self.store.patrols(3)
        if method == "notify_owner":
            if role != Role.AZATHOTH:
                raise Denied("only azathoth speaks to the owner unprompted")
            # A proactive turn: no owner text, just azathoth's words (and what prompted them).
            tid = new_id("note")
            self.store.add_turn(tid, conn.id, "")
            self.store.update_turn(tid, reply=p["text"], status="done", model=p.get("model"), origin=p.get("origin"),
                                   task_id=p.get("task_id"), kind=p.get("kind"),
                                   **({"activity": p["activity"]} if p.get("activity") else {}))
            await self.broadcast({"type": "chat", "turn": self.store.turn(tid)})
            return True
        if method in ("spawn", "stop", "restart"):
            if role not in roles.SPAWN:
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
        conn = self.agents.get(agent_id)
        if conn is None:
            return {"agent_id": agent_id, "name": a["name"], "online": False, "desired": a["desired"]}
        try:
            r = await conn.rpc("latest_result", timeout=15)
        except Exception as e:
            r = {"error": str(e)}
        return {"agent_id": agent_id, "name": a["name"], "online": True} | r

    async def patrol(self) -> list[dict]:
        targets = [a for a in self.store.agents(include_stopped=False) if a["role"] in roles.SUPERVISED]
        return list(await asyncio.gather(*(self.latest_result(a["id"]) for a in targets)))

    # lifecycle

    async def spawn(self, p: dict, creator: str) -> dict:
        galaxy = self.store.galaxy(p["galaxy_id"])
        if not galaxy:
            raise Denied(f"unknown galaxy {p['galaxy_id']}")
        if galaxy["id"] in self.closed_galaxies:
            raise Denied(f"galaxy {galaxy['name']} does not host old_ones")
        if not p.get("task", "").strip():
            raise ValueError("an old_one needs a task")
        spec = {"task": p["task"], "skill": p.get("skill", ""), "cwd": p.get("cwd"), "model": p.get("model")}
        a = self.store.upsert_agent(new_id("old"), p["name"], Role.OLD_ONE, galaxy["id"], creator, spec)
        await self.sync_galaxy(galaxy["id"])
        await self.broadcast({"type": "directory", **self.directory()})
        return {"id": a["id"], "name": a["name"], "galaxy_id": galaxy["id"]}

    async def stop(self, p: dict, _by: str) -> bool:
        a = self._managed(p["agent_id"])
        self.store.set_agent(a["id"], desired="stopped", epoch=a["epoch"] + 1)
        await self.drop_agent_conn(a["id"])
        await self.sync_galaxy(a["galaxy_id"])
        await self.broadcast({"type": "directory", **self.directory()})
        return True

    async def restart(self, p: dict, _by: str) -> bool:
        a = self._managed(p["agent_id"])
        self.store.set_agent(a["id"], desired="running", epoch=a["epoch"] + 1)
        await self.drop_agent_conn(a["id"])
        await self.sync_galaxy(a["galaxy_id"])
        return True

    def _managed(self, agent_id: str) -> dict:
        a = self.store.agent(agent_id)
        if not a or a["role"] != Role.OLD_ONE:
            raise Denied("only old_ones can be managed")
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
        return overrides.get(a["id"]) or overrides.get(a["role"])

    async def set_runtime(self, key: str, cfg: dict | None) -> None:
        if cfg is not None:
            cfg = validate_runtime(cfg)
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

    async def owner_send(self, to: str, text: str) -> dict:
        mid = new_id("msg")
        self.store.add_turn(mid, to, text)
        msg = await self.route(OWNER, to, text, kind="chat", priority=OWNER_PRIORITY, message_id=mid)
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
                asyncio.create_task(self._on_frame(conn, frame))
        except WebSocketDisconnect:
            pass
        finally:
            await self._disconnect(conn)

    async def _authenticate(self, ws: WebSocket, hello: dict) -> Conn:
        kind, cid, token = hello.get("kind"), hello.get("id"), hello.get("token", "")
        if kind == "galaxy":
            g = self.store.galaxy(cid)
            if not g or not hmac.compare_digest(g["token_hash"], sha256(token)):
                raise Denied("bad galaxy credential")
            conn = Conn(ws, "galaxy", cid)
            old = self.galaxies.get(cid)
            self.galaxies[cid] = conn
            if old:
                await old.ws.close(4002, "replaced")
            await conn.send({"type": "welcome", "id": cid, "name": g["name"]})
            await self.sync_galaxy(cid)
        elif kind == "agent":
            a = self.store.agent(cid)
            if not a or a["desired"] != "running" or not hmac.compare_digest(
                    agent_token(self.secret, cid, a["epoch"]), token):
                raise Denied("bad or revoked agent credential")
            conn = Conn(ws, "agent", cid)
            old = self.agents.get(cid)
            self.agents[cid] = conn
            if old:
                await old.ws.close(4002, "replaced")
            await conn.send({"type": "welcome", "agent": self.public_agent(a) | {"spec": a["spec"]},
                             "memory": self.store.memory(cid), "thread_id": a["thread_id"],
                             "runtime": self.runtime_for(a),
                             "pending": self._envelopes(self.store.pending_for(cid))})
            for m in self.store.pending_for(cid):
                if m["state"] == "queued":
                    self.store.mark(m["id"], "delivered")
        else:
            raise Denied("unknown client kind")
        await self.broadcast({"type": "directory", **self.directory()})
        return conn

    def _envelopes(self, msgs: list[dict]) -> list[dict]:
        out = []
        for m in msgs:
            s = self.store.agent(m["sender"])
            out.append(m | {"sender_name": s["name"] if s else "主人", "sender_role": s["role"] if s else OWNER})
        return out

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

    async def handle_galaxy_call(self, conn: Conn, method: str, p: dict) -> Any:
        if method == "agent_exited":
            a = self.store.agent(p["agent_id"])
            if a and a["galaxy_id"] == conn.id:
                self.status[a["id"]] = {"state": "crashed", "current": p.get("reason")}
                await self.broadcast({"type": "directory", **self.directory()})
            return True
        raise Denied(f"unknown galaxy call {method}")

    async def _disconnect(self, conn: Conn) -> None:
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
            if path == "directory":
                return JSONResponse(self.directory() | {"gods": {r: (self.god(r) or {}).get("id") for r in roles.GODS}})
            if path == "chat":
                agent = q.get("agent_id") or self.god(Role.AZATHOTH)["id"]
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.turns(agent, limit=int(q.get("limit", 40)), before=before,
                                                     search=q.get("search") or None))
            if path == "send":
                if self.role_of(body["to"]) not in roles.SEND[OWNER]:
                    raise Denied("you may only talk to azathoth and ghroth")
                return JSONResponse(await self.owner_send(body["to"], body["text"]))
            if path == "stop":
                return JSONResponse({"ok": await self.owner_stop(body["agent_id"])})
            if path == "messages":
                before = float(q["before"]) if q.get("before") else None
                return JSONResponse(self.store.messages(q.get("agent_id") or None,
                                                        limit=min(int(q.get("limit", 100)), 500), before=before,
                                                        task_id=q.get("task_id") or None))
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
                return JSONResponse({"overrides": self.store.setting("runtimes") or {}, "models": MODEL_CHOICES})
            if path == "patrols":
                return JSONResponse(self.store.patrols(int(q.get("limit", 10))))
            if path == "latest":
                return JSONResponse(await self.latest_result(q["agent_id"]))
            return JSONResponse({"error": "not found"}, status_code=404)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=401)
        except Denied as e:
            return JSONResponse({"error": str(e)}, status_code=403)
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
            WebSocketRoute("/api/events", self.owner_ws),
            Route("/api/{path:path}", self.api, methods=["GET", "POST"]),
        ]
        if (static / "assets").exists():
            routes.append(Mount("/assets", StaticFiles(directory=static / "assets")))
        routes.append(Route("/{path:path}", spa))
        return Starlette(routes=routes)
