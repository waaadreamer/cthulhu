"""Codex app-server runtime (stdio JSON-RPC).

The rest of the harness only sees the Runtime interface; a Claude Code
backend would implement the same methods.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[Any]]
EventHandler = Callable[[str, dict], None]


class RuntimeError_(Exception):
    pass


@dataclass
class TurnResult:
    status: str                      # completed | interrupted | failed
    text: str = ""                   # final agent message
    error: str | None = None
    items: list[dict] = field(default_factory=list)


def tool(name: str, description: str, properties: dict | None = None, required: list | None = None) -> dict:
    return {"type": "function", "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": required or [], "additionalProperties": False}}


def _child_env() -> dict:
    # The agent's temple credential must never reach the model's shell.
    return {k: v for k, v in os.environ.items() if not k.startswith("CTHULHU_")}


class CodexRuntime:
    def __init__(self, codex: str, cwd: str, *, model: str | None = None, effort: str | None = None,
                 sub_agents: bool = False, log_path: str | None = None):
        self.codex, self.cwd, self.model, self.effort = codex, cwd, model, effort
        self.sub_agents = sub_agents
        self.log_path = log_path
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.turns: dict[str, asyncio.Future] = {}   # turn id -> TurnResult future
        self.items: dict[str, list] = {}             # turn id -> completed items
        self.listeners: dict[str, EventHandler] = {}  # thread id -> event callback
        self.tool_handlers: dict[str, ToolHandler] = {}
        self.usage: dict[str, dict] = {}             # thread id -> last token usage

    async def start(self) -> None:
        argv = [self.codex,
                "-c", f"features.multi_agent={'true' if self.sub_agents else 'false'}",
                "-c", "features.apps=false", "-c", "features.plugins=false", "-c", "features.memories=false",
                "-c", "approval_policy=never", "-c", "sandbox_mode=danger-full-access",
                "app-server", "--listen", "stdio://"]
        stderr = open(self.log_path, "ab") if self.log_path else asyncio.subprocess.DEVNULL
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, env=_child_env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=64 * 1024 * 1024, start_new_session=True)
        self.reader = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "cthulhu", "title": "Cthulhu", "version": "2.0"},
                                          "capabilities": {"experimentalApi": True}})
        await self._write({"method": "initialized"})

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _write(self, msg: dict) -> None:
        if not self.alive:
            raise RuntimeError_("codex app-server is not running")
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self._write({"id": rid, "method": method, "params": params})
            resp = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
        if "error" in resp:
            raise RuntimeError_(f"{method}: {resp['error'].get('message', resp['error'])}")
        return resp.get("result", {})

    async def _read(self) -> None:
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "id" in msg and "method" not in msg:
                    fut = self.pending.get(msg["id"])
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif "id" in msg:
                    asyncio.create_task(self._serve(msg))
                else:
                    self._notify(msg.get("method", ""), msg.get("params") or {})
        finally:
            for fut in list(self.pending.values()) + list(self.turns.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError_("codex app-server exited"))

    def _notify(self, method: str, p: dict) -> None:
        thread = p.get("threadId")
        if method == "item/completed":
            self.items.setdefault(p.get("turnId", ""), []).append(p["item"])
        elif method == "thread/tokenUsage/updated":
            self.usage[thread] = p["tokenUsage"]
        elif method == "turn/completed":
            turn = p["turn"]
            items = self.items.pop(turn["id"], [])
            text = next((i["text"] for i in reversed(items)
                         if i["type"] == "agentMessage" and i.get("phase") != "commentary"), "")
            err = turn.get("error")
            result = TurnResult(turn["status"], text, err.get("message") if err else None, items)
            fut = self.turns.setdefault(turn["id"], asyncio.get_running_loop().create_future())
            if not fut.done():
                fut.set_result(result)
        if thread in self.listeners:
            try:
                self.listeners[thread](method, p)
            except Exception:
                log.exception("runtime listener failed")

    async def _serve(self, msg: dict) -> None:
        method, p = msg["method"], msg.get("params") or {}
        try:
            if method == "item/tool/call":
                handler = self.tool_handlers.get(p["threadId"])
                if handler is None:
                    raise RuntimeError_("no tools bound to this thread")
                try:
                    value = await handler(p["tool"], p.get("arguments") or {})
                    text, ok = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), True
                except Exception as e:
                    text, ok = f"error: {e}", False
                result = {"success": ok, "contentItems": [{"type": "inputText", "text": text}]}
            elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                            "execCommandApproval", "applyPatchApproval"):
                result = {"decision": "accept"}
            elif method == "item/permissions/requestApproval":
                result = {"permissions": p.get("permissions"), "scope": "session"}
            else:
                await self._write({"id": msg["id"], "error": {"code": -32601, "message": "unsupported"}})
                return
            await self._write({"id": msg["id"], "result": result})
        except Exception:
            log.exception("failed to serve %s", method)

    # threads and turns

    def _thread_params(self, instructions: str | None) -> dict:
        p = {"cwd": self.cwd, "approvalPolicy": "never", "sandbox": "danger-full-access"}
        if instructions is not None:
            p["developerInstructions"] = instructions
        if self.model:
            p["model"] = self.model
        return p

    async def start_thread(self, instructions: str, tools: list[dict], handler: ToolHandler | None,
                           ephemeral: bool = False) -> str:
        params = self._thread_params(instructions) | {"dynamicTools": tools, "ephemeral": ephemeral}
        thread = (await self.request("thread/start", params))["thread"]["id"]
        if handler:
            self.tool_handlers[thread] = handler
        return thread

    async def resume_thread(self, thread_id: str, handler: ToolHandler | None) -> str:
        await self.request("thread/resume", self._thread_params(None) | {"threadId": thread_id, "excludeTurns": True})
        if handler:
            self.tool_handlers[thread_id] = handler
        return thread_id

    async def fork_thread(self, thread_id: str, handler: ToolHandler | None = None) -> str:
        r = await self.request("thread/fork", self._thread_params(None) |
                               {"threadId": thread_id, "ephemeral": True, "excludeTurns": True})
        fork = r["thread"]["id"]
        if handler:
            self.tool_handlers[fork] = handler
        return fork

    async def drop_thread(self, thread_id: str) -> None:
        self.tool_handlers.pop(thread_id, None)
        self.listeners.pop(thread_id, None)
        try:
            await self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=10)
        except Exception:
            pass

    async def run_turn(self, thread_id: str, text: str, *, on_event: EventHandler | None = None,
                       output_schema: dict | None = None, timeout: float = 6 * 3600,
                       on_start: Callable[[str], None] | None = None) -> TurnResult:
        params = {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}],
                  "approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"}}
        if self.effort:
            params["effort"] = self.effort
        if output_schema:
            params["outputSchema"] = output_schema
        if on_event:
            self.listeners[thread_id] = on_event
        try:
            turn_id = (await self.request("turn/start", params))["turn"]["id"]
            fut = self.turns.setdefault(turn_id, asyncio.get_running_loop().create_future())
            if on_start:
                on_start(turn_id)
            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                await self.interrupt(thread_id, turn_id)
                return await asyncio.wait_for(fut, 60)
            finally:
                self.turns.pop(turn_id, None)
        finally:
            if on_event:
                self.listeners.pop(thread_id, None)

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        try:
            await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
        except Exception as e:
            log.warning("interrupt failed: %s", e)

    def context_fill(self, thread_id: str) -> float:
        """Fraction of the model window used by the last request on this thread."""
        u = self.usage.get(thread_id)
        if not u or not u.get("modelContextWindow"):
            return 0.0
        return u["last"]["inputTokens"] / u["modelContextWindow"]
