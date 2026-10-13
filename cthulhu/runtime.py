"""Codex app-server runtime (stdio JSON-RPC).

The rest of the harness only sees the Runtime interface; a Claude Code
backend would implement the same methods.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
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
    return {k: v for k, v in os.environ.items() if not k.startswith("CTHULHU_") and k != "COMPANY_CODEX_API_KEY"}


class CodexRuntime:
    def __init__(self, codex: str, cwd: str, *, model: str | None = None, effort: str | None = None,
                 sub_agents: bool = False, log_path: str | None = None,
                 api_transport: dict | None = None, service_tier: str | None = None):
        self.codex, self.cwd, self.model, self.effort = codex, cwd, model, effort
        self.sub_agents = sub_agents
        self.api_transport = api_transport
        self.service_tier = service_tier
        self.log_path = log_path
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.turns: dict[str, asyncio.Future] = {}   # turn id -> TurnResult future
        self.items: dict[str, list] = {}             # turn id -> completed items
        self.listeners: dict[str, EventHandler] = {}  # thread id -> event callback
        self.tool_handlers: dict[str, ToolHandler] = {}
        self.usage: dict[str, dict] = {}             # thread id -> last token usage
        self.serving: dict[asyncio.Task, str] = {}

    def launch_config(self) -> tuple[list[str], dict]:
        env = _child_env()
        overrides = []
        if self.api_transport is not None:
            cfg = self.api_transport
            for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT_ID"):
                env.pop(name, None)
            env.update(CODEX_HOME=cfg["home"], GALAXY_API_LOCAL_TOKEN=cfg["token"])
            bypass = env.get("no_proxy", env.get("NO_PROXY", ""))
            env["no_proxy"] = env["NO_PROXY"] = bypass + ",127.0.0.1,localhost,::1"
            values = {
                "model_provider": "galaxy_api",
                "model_providers.galaxy_api.name": "Galaxy API",
                "model_providers.galaxy_api.base_url": cfg["base_url"],
                "model_providers.galaxy_api.wire_api": "responses",
                "model_providers.galaxy_api.env_key": "GALAXY_API_LOCAL_TOKEN",
                "model_providers.galaxy_api.requires_openai_auth": False,
                "model_providers.galaxy_api.supports_websockets": False,
                "model_providers.galaxy_api.request_max_retries": 0,
                "model_providers.galaxy_api.stream_max_retries": 0,
            }
            for name, value in values.items():
                overrides.extend(["-c", f"{name}={json.dumps(value)}"])
        return overrides, env

    async def start(self) -> None:
        if self.alive:
            return
        overrides, env = self.launch_config()
        argv = [self.codex, *overrides,
                "-c", f"features.multi_agent={'true' if self.sub_agents else 'false'}",
                "-c", "features.apps=false", "-c", "features.plugins=false", "-c", "features.memories=false",
                "-c", "approval_policy=never", "-c", "sandbox_mode=danger-full-access",
                "app-server", "--listen", "stdio://"]
        stderr = open(self.log_path, "ab") if self.log_path else asyncio.subprocess.DEVNULL
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=self.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=stderr, limit=64 * 1024 * 1024, start_new_session=True)
        if self.log_path:
            stderr.close()
        self.reader = asyncio.create_task(self._read())
        await self.request("initialize", {"clientInfo": {"name": "cthulhu", "title": "Cthulhu", "version": "2.0"},
                                          "capabilities": {"experimentalApi": True}})
        await self._write({"method": "initialized"})

    async def close(self) -> None:
        # app-server runs in its own session; signal the group so running commands go down with it.
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                pass
        if self.proc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)

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
                    task = asyncio.create_task(self._serve(msg))
                    self.serving[task] = (msg.get('params') or {}).get('threadId', '')
                    task.add_done_callback(lambda t: self.serving.pop(t, None))
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
        if self.api_transport is not None:
            p["modelProvider"] = "galaxy_api"
        return p

    async def start_thread(self, instructions: str, tools: list[dict], handler: ToolHandler | None,
                           ephemeral: bool = False) -> str:
        params = self._thread_params(instructions) | {"dynamicTools": tools, "ephemeral": ephemeral}
        thread = (await self.request("thread/start", params))["thread"]["id"]
        if handler:
            self.tool_handlers[thread] = handler
        return thread

    async def resume_thread(self, thread_id: str, handler: ToolHandler | None) -> str:
        # Codex keeps the developer instructions the thread was started with; callers rotate threads when the skill changes.
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

    async def steer(self, thread_id: str, turn_id: str, text: str) -> bool:
        """Add input to the running turn; False if that turn is no longer the active one."""
        try:
            await self.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                              "input": [{"type": "text", "text": text, "text_elements": []}]}, timeout=10)
            return True
        except Exception as e:
            log.info("steer into %s/%s not accepted: %s", thread_id, turn_id, e)
            return False

    async def run_turn(self, thread_id: str, text: str, *, on_event: EventHandler | None = None,
                       output_schema: dict | None = None, timeout: float = 6 * 3600,
                       on_start: Callable[[str], None] | None = None, images: list[str] | None = None) -> TurnResult:
        inputs = [{"type": "text", "text": text, "text_elements": []}]
        inputs.extend({'type': 'localImage', 'path': path} for path in images or [])
        params = {"threadId": thread_id, "input": inputs,
                  "approvalPolicy": "never", "sandboxPolicy": {"type": "dangerFullAccess"}}
        if self.effort:
            params["effort"] = self.effort
        if self.service_tier is not None:
            params["serviceTier"] = self.service_tier
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
                result = await asyncio.wait_for(asyncio.shield(fut), 60)
                if result.status == 'completed':
                    return result
                return TurnResult('failed', result.text, 'turn timeout (execution settled)', result.items)
            finally:
                self.turns.pop(turn_id, None)
        finally:
            if on_event:
                self.listeners.pop(thread_id, None)

    async def recovery_checkpoint(self, thread_id: str, terminal: bool) -> str:
        # Do not start another model while a dispatched dynamic tool is still running.
        serving = [t for t, thread in self.serving.items() if thread == thread_id]
        if serving:
            _, pending = await asyncio.wait(serving, timeout=60)
            if pending:
                raise RuntimeError_('previous tool call is still running; inspect its existing execution')
        if not self.alive:
            await self.start()
            await self.resume_thread(thread_id, self.tool_handlers.get(thread_id))
        snapshot = (await self.request('thread/read', {'threadId': thread_id, 'includeTurns': True}, timeout=20)).get('thread')
        if not snapshot:
            raise RuntimeError_('persisted thread snapshot unavailable')
        turns = snapshot.get('turns', [])
        if turns and turns[-1].get('status') in ('inProgress', 'running'):
            turn_id = turns[-1]['id']
            await self.interrupt(thread_id, turn_id)
            for _ in range(60):
                await asyncio.sleep(0.5)
                snapshot = (await self.request('thread/read', {'threadId': thread_id, 'includeTurns': True}, timeout=10))['thread']
                turns = snapshot.get('turns', [])
                if turns and turns[-1].get('status') in ('completed', 'failed', 'interrupted'):
                    break
            else:
                raise RuntimeError_('previous turn has not settled')
        if not terminal and not (turns and turns[-1].get('status') in ('completed', 'failed', 'interrupted')):
            raise RuntimeError_('previous dispatch outcome is unknown')
        return json.dumps({'thread': thread_id, 'turns': turns[-3:]}, ensure_ascii=False)[-32000:]

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
