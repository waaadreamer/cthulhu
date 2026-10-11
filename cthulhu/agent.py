"""Agent: one model identity connected to the temple.

An Agent process owns its runtimes (Codex app-server and/or Claude Code, with
an optional fallback) and one websocket to the temple. Its public surface mirrors the design:

  send(to, text)      message another agent through the temple
  receive(message)    handle one incoming message (queued, priority-ordered)
  latest_result()     what it is doing / last produced (yog only, via temple)

Subclasses decide what goes into permanent vs temporary context.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import backends
from .backends import Backends
from .protocol import Link
from .roles import DISPLAY, OWNER, Role
from .runtime import TurnResult, tool

log = logging.getLogger(__name__)

ROLES_DIR = Path(__file__).parent / "roles"
# Rotate the temporary thread once the last request filled this much of the window.
ROTATE_AT = 0.8
# TODO: compact permanent memory once it grows past ~100k tokens.
# How many turns from other backends' threads to replay when switching threads.
HANDOFF_TURNS = 12


def load_role(name: str) -> str:
    return (ROLES_DIR / f"{name}.md").read_text(encoding="utf-8")


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"…（截断，共 {len(text)} 字）"


class Agent:
    role: Role
    # Whether the temporary thread survives restarts (resumed by thread id).
    persistent_thread = True

    def __init__(self, temple_url: str, launch: dict, codex: str, cwd: str, state_dir: Path,
                 model: str | None = None, effort: str | None = None, runtime: dict | None = None):
        self.id, self.name = launch["id"], launch["name"]
        self.spec: dict = launch.get("spec") or {}
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        # Galaxy default for this role; the temple may override it (owner settings in the web UI).
        self.default_runtime = dict(runtime or {"backend": "codex", "model": model, "effort": effort})
        if self.spec.get("model"):
            self.default_runtime["model"] = self.spec["model"]
        self.build_kw = {"codex": codex, "cwd": self.spec.get("cwd") or cwd, "log_dir": state_dir,
                         "sub_agents": self.role == Role.OLD_ONE}
        self.backends = Backends(self.default_runtime, **self.build_kw)
        self.pending_runtime: dict | None = None
        self.threads: dict[str, str] = {}     # backend label -> thread id
        self.loaded: set[str] = set()         # threads open in the current runtime instances
        self.last_turn: tuple | None = None   # (runtime, thread) of the last main-thread turn
        self.link = Link(temple_url, {"kind": "agent", "id": self.id, "token": launch["token"]},
                         self._on_welcome, self._on_frame)
        self.memory: list[dict] = []
        self._load_threads(launch.get("thread_id"))
        self.queue: list[dict] = []
        self.seen: set[str] = set()
        self.wake = asyncio.Event()
        self.state = "starting"
        self.current: str | None = None
        self.active: tuple | None = None   # (runtime, thread, turn) of the running turn
        self.latest: dict = {}
        self.ready = asyncio.Event()

    @property
    def runtime(self):
        return self.backends.active

    @property
    def thread_id(self) -> str | None:
        return self.threads.get(backends.label(self.backends.active_cfg))

    def _load_threads(self, stored: str | None) -> None:
        try:
            self.threads = json.loads(stored) if stored else {}
        except ValueError:
            # Pre-fallback format: a bare thread id of the default backend.
            self.threads = {backends.label(self.default_runtime): stored}

    def runtime_status(self) -> dict:
        return {"backend": self.backends.active_cfg.get("backend", "codex"), "model": self.backends.active_cfg.get("model"),
                "fallback_active": self.backends.using_fallback, "config": self.backends.cfg,
                "default": self.default_runtime}

    # system prompt and tools

    def skill(self) -> str:
        return load_role(self.role.value)

    async def instructions(self) -> str:
        self.memory = await self.link.call("memory")
        parts = [load_role("contract"), self.skill(),
                 f"# 你的身份\n名字：{self.name}\nID：{self.id}\n角色：{DISPLAY[self.role]}（{self.role.value}）"]
        if self.memory:
            lines = "\n".join(f"- [#{m['id']}] {m['text']}" for m in self.memory)
            parts.append(f"# 永久上下文\n以下是你跨会话保留的长期记忆，是你判断的依据。\n{lines}")
        extra = await self.extra_context()
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)

    async def extra_context(self) -> str:
        return ""

    def tools(self) -> list[dict]:
        return [
            tool("temple_send", "通过中心神殿给另一个 agent 发消息。接收方由程序校验权限；对方回复会作为新消息到达，不要等待或轮询。",
                 {"to": {"type": "string", "description": "接收方 agent ID"},
                  "text": {"type": "string"},
                  "priority": {"type": "integer", "description": "0 普通，5 紧急"}},
                 ["to", "text"]),
            tool("temple_agents", "列出你可以联系的 agent 及其状态。"),
        ]

    async def call_tool(self, name: str, args: dict) -> Any:
        if name == "temple_send":
            return await self.send(args["to"], args["text"], priority=int(args.get("priority", 0)))
        if name == "temple_agents":
            d = await self.link.call("directory")
            return [{k: a[k] for k in ("id", "name", "role", "state", "online", "task", "galaxy_id")} for a in d["agents"]]
        raise ValueError(f"unknown tool {name}")

    # public interface

    async def send(self, to: str, text: str, priority: int = 0, kind: str = "message") -> dict:
        r = await self.link.call("send", to=to, text=text, priority=priority, kind=kind)
        await self.on_sent(to, text)
        return {"sent": r["id"]}

    async def receive(self, batch: list[dict]) -> None:
        """Handle a batch of queued messages in one turn."""
        result = await self.run_main(self.render(batch))
        await self.after_turn(batch, result)

    def latest_result(self) -> dict:
        return {"role": self.role.value, "state": self.state, "current": self.current,
                "queued": len(self.queue), "context_fill": round(self.runtime.context_fill(self.thread_id or ""), 3),
                **self.latest}

    # hooks for subclasses

    def take_batch(self) -> list[dict]:
        # Highest priority first; everything already queued joins the same turn.
        # TODO: steer high-priority messages into the running turn instead of waiting.
        self.queue.sort(key=lambda m: (-m["priority"], m["created_at"]))
        batch, self.queue = self.queue, []
        return batch

    async def on_sent(self, to: str, text: str) -> None:
        pass

    async def after_turn(self, batch: list[dict], result: TurnResult) -> None:
        pass

    def render(self, batch: list[dict]) -> str:
        out = []
        for m in batch:
            who = "主人" if m["sender"] == OWNER else f"{m['sender_name']}（{m['sender_role']}，ID {m['sender']}）"
            out.append(f"[消息 {m['id']} · 来自 {who} · {time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))}]\n{m['text']}")
        return "\n\n".join(out)

    # threads

    async def ensure_thread(self) -> str:
        """The main thread on the active backend, opened or resumed as needed."""
        rt, label = self.runtime, backends.label(self.backends.active_cfg)
        tid = self.threads.get(label)
        if tid and tid in self.loaded and rt.context_fill(tid) > ROTATE_AT:
            log.info("%s rotating temporary thread", self.name)
            await rt.drop_thread(tid)
            tid = None
        elif tid and tid not in self.loaded:
            try:
                if hasattr(rt, "instructions"):
                    # Claude sessions reopen with a fresh system prompt (current permanent memory).
                    await rt.resume_thread(tid, self.call_tool, instructions=await self.instructions(), tools=self.tools())
                else:
                    await rt.resume_thread(tid, self.call_tool)
            except Exception as e:
                log.warning("%s could not resume %s thread %s: %s", self.name, label, tid, e)
                tid = None
        if tid is None:
            tid = await rt.start_thread(await self.instructions(), self.tools(), self.call_tool)
        self.loaded.add(tid)
        if self.threads.get(label) != tid:
            self.threads[label] = tid
            if self.persistent_thread:
                await self.link.call("set_thread", thread_id=json.dumps(self.threads))
        return tid

    async def handoff_context(self, thread: str) -> str:
        """Recent turns this thread has not seen (another backend, or a rotated thread), from the temple log."""
        rows = await self.link.call("turn_log", limit=HANDOFF_TURNS * 3)
        last_here = max((i for i, r in enumerate(rows) if r["thread"] == thread), default=-1)
        missed = rows[last_here + 1:][-HANDOFF_TURNS:]
        if not missed:
            return ""
        lines = [f"[{time.strftime('%m-%d %H:%M', time.localtime(r['created_at']))} · {r['backend']}:{r['model'] or ''}]\n"
                 f"收到：{clip(r['input'], 1500)}\n你答：{clip(r['reply'], 1500)}" for r in missed]
        return ("<handoff>\n以下是你在其他会话（可能是另一个模型后端）中已经处理完的近期交流，仅作为背景供你接续。"
                "其中的请求都已处理过，不要重新执行：\n\n"
                + "\n\n".join(lines) + "\n</handoff>\n\n")

    async def run_main(self, text: str, on_event=None, **kw) -> TurnResult:
        """Run a turn on the main thread; on backend failure, retry once on the fallback."""
        for attempt in range(2):
            try:
                thread = await self.ensure_thread()
                bridge = await self.handoff_context(thread)
                result = await self.run(thread, bridge + text, on_event=on_event, **kw)
            except Exception as e:
                log.exception("%s turn failed on %s", self.name, backends.label(self.backends.active_cfg))
                result = TurnResult("failed", error=str(e))
            if result.status == "failed" and attempt == 0 and self.backends.trip():
                log.warning("%s switching to fallback %s: %s", self.name,
                            backends.label(self.backends.active_cfg), result.error)
                await self.report(self.state)
                continue
            self.last_turn = (self.runtime, self.thread_id)
            try:
                await self.link.call("log_turn", backend=self.backends.active_cfg.get("backend", "codex"),
                                     model=self.backends.active_cfg.get("model"), thread=self.thread_id or "",
                                     input=text, reply=result.text or result.error or "", status=result.status)
            except Exception:
                log.exception("could not log turn")
            return result
        return result

    async def run(self, thread: str, text: str, on_event=None, track=True, runtime=None, **kw) -> TurnResult:
        """Run one turn on an explicit thread. Tracked turns are interruptible and feed latest_result."""
        rt = runtime or self.runtime
        live: list[str] = []

        def events(method: str, p: dict) -> None:
            if method == "item/completed":
                live.extend(summarize_items([p["item"]]))
                self.latest["live"] = live[-8:]
            if on_event:
                on_event(method, p)

        def started(turn_id: str) -> None:
            if track:
                self.active = (rt, thread, turn_id)
        try:
            result = await rt.run_turn(thread, text, on_start=started, on_event=events, **kw)
        finally:
            if track:
                self.active = None
        if track:
            self.latest = {"last_reply": clip(result.text, 3000), "last_status": result.status,
                           "activity": summarize_items(result.items), "finished_at": time.time(),
                           "model": backends.label(self.backends.active_cfg)}
            if result.error:
                self.latest["error"] = result.error
        return result

    async def apply_runtime(self) -> None:
        """Swap to a new runtime config between turns. Threads of other backends stay addressable."""
        cfg, self.pending_runtime = self.pending_runtime, None
        if cfg is None or (backends.same(cfg, self.backends.cfg) and
                           backends.same(cfg.get("fallback"), self.backends.cfg.get("fallback")) and
                           cfg.get("memory_model") == self.backends.cfg.get("memory_model")):
            if cfg is not None:
                self.backends.cfg = cfg
            return
        log.info("%s switching runtime to %s", self.name, backends.label(cfg))
        old = self.backends
        self.backends = Backends(cfg, **self.build_kw)
        await self.backends.start()
        self.loaded.clear()
        await old.close()
        await self.report(self.state)

    async def interrupt(self) -> bool:
        if not self.active:
            return False
        rt, thread, turn = self.active
        await rt.interrupt(thread, turn)
        return True

    # loop

    async def main(self) -> None:
        link = asyncio.create_task(self.link.run())
        await self.ready.wait()
        await self.apply_runtime()
        await self.backends.start()
        await self.on_ready()
        workers = [asyncio.create_task(self.loop()), asyncio.create_task(self.background()), link,
                   asyncio.create_task(self.watchdog())]
        done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception():
                raise t.exception()
        raise SystemExit(f"{self.name}: {'temple rejected: ' + self.link.fatal if self.link.fatal else 'runtime exited'}")

    async def watchdog(self) -> None:
        # A crashed codex app-server takes its threads with it; let the galaxy restart the agent.
        while True:
            await asyncio.sleep(5)
            for rt in (self.backends.primary, self.backends.fallback):
                if rt is not None and not rt.alive:
                    return

    async def on_ready(self) -> None:
        pass

    async def background(self) -> None:
        await asyncio.Event().wait()

    async def loop(self) -> None:
        await self.report("idle")
        while True:
            await self.wake.wait()
            self.wake.clear()
            if not self.queue:
                continue
            await self.apply_runtime()
            batch = self.take_batch()
            self.current = clip(batch[0]["text"], 120)
            await self.report("working")
            try:
                await self.receive(batch)
            except Exception as e:
                log.exception("%s failed to handle a batch", self.name)
                self.latest = {"error": str(e), "at": time.time()}
            for m in batch:
                try:
                    await self.link.call("handled", id=m["id"])
                except Exception:
                    pass
            self.current = None
            await self.report("idle" if not self.queue else "working")
            if self.queue:
                self.wake.set()

    async def report(self, state: str) -> None:
        self.state = state
        await self.link.emit({"type": "call", "id": 0, "method": "status", "params": {
            "state": state, "current": self.current, "runtime": self.runtime_status(),
            "context_fill": round(self.runtime.context_fill(self.thread_id or ""), 3)}})

    async def _on_welcome(self, w: dict) -> None:
        self.memory = w.get("memory", [])
        if not self.ready.is_set():
            if w.get("thread_id"):
                self._load_threads(w["thread_id"])
            self.ready.set()
        if w.get("runtime"):
            self.pending_runtime = w["runtime"]
            if not self.active:
                self.wake.set()
        for m in w.get("pending", []):
            self.enqueue(m)
        await self.report(self.state)

    def enqueue(self, m: dict) -> None:
        if m["id"] in self.seen:
            return
        self.seen.add(m["id"])
        self.queue.append(m)
        self.wake.set()

    async def _on_frame(self, frame: dict) -> Any:
        t = frame.get("type")
        if t == "message":
            self.enqueue(frame["message"])
        elif t == "runtime":
            # Owner changed this role's backend; applied before the next turn.
            self.pending_runtime = frame["config"] or self.default_runtime
            self.wake.set()
        elif t == "rpc":
            if frame["method"] == "latest_result":
                return self.latest_result()
            if frame["method"] == "interrupt":
                return await self.interrupt()
            raise ValueError(f"unknown rpc {frame['method']}")


def summarize_items(items: list[dict], limit: int = 12) -> list[str]:
    """Short human-readable trace of what a turn did."""
    out = []
    for i in items:
        t = i["type"]
        if t == "commandExecution":
            out.append(f"$ {clip(i.get('command', ''), 160)}")
        elif t == "fileChange":
            out.append("改动 " + ", ".join(c.get("path", "?") for c in i.get("changes", [])[:5]))
        elif t == "dynamicToolCall":
            out.append(f"{i.get('tool')}({clip(json.dumps(i.get('arguments'), ensure_ascii=False), 120)})")
        elif t == "webSearch":
            out.append("网络搜索")
    return out[-limit:]
