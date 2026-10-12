"""Claude Code runtime via the Claude Agent SDK.

Implements the same surface as CodexRuntime. A "thread" is a Claude Code
session; each live thread keeps one ClaudeSDKClient (one CLI process) open.
Temple tools are served by an in-process MCP server named "temple".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Callable

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent,
                              TextBlock, ToolUseBlock, create_sdk_mcp_server)
from claude_agent_sdk import tool as sdk_tool

from .runtime import EventHandler, RuntimeError_, ToolHandler, TurnResult

log = logging.getLogger(__name__)

MCP = "temple"


def _env() -> dict:
    keep = {k: v for k, v in os.environ.items() if not k.startswith("CTHULHU_")}
    return keep


def _items(msg: AssistantMessage) -> list[dict]:
    """Translate Claude content blocks into the Codex item shapes the agent layer summarizes."""
    out = []
    for b in msg.content:
        if isinstance(b, ToolUseBlock):
            if b.name == "Bash":
                out.append({"type": "commandExecution", "command": b.input.get("command", "")})
            elif b.name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                out.append({"type": "fileChange", "changes": [{"path": b.input.get("file_path", "?")}]})
            elif b.name.startswith(f"mcp__{MCP}__"):
                out.append({"type": "dynamicToolCall", "tool": b.name.split("__", 2)[2], "arguments": b.input})
            elif b.name in ("WebSearch", "WebFetch"):
                out.append({"type": "webSearch"})
            else:
                out.append({"type": "dynamicToolCall", "tool": b.name, "arguments": b.input})
    return out


class _Thread:
    def __init__(self, client: ClaudeSDKClient, session_id: str):
        self.client = client
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.fill = 0.0


class ClaudeRuntime:
    def __init__(self, cwd: str, *, model: str, effort: str | None = None, log_path: str | None = None,
                 cli_path: str | None = None):
        self.cwd, self.model, self.effort = cwd, model, effort
        self.log_path, self.cli_path = log_path, cli_path
        self.threads: dict[str, _Thread] = {}
        self.instructions: dict[str, str] = {}   # session -> system prompt, kept for forks/resumes
        self.tool_specs: dict[str, list[dict]] = {}
        self.handlers: dict[str, ToolHandler] = {}
        self.turn_ids: dict[str, str] = {}
        self.sdk_tools: dict[str, list] = {}       # session -> SdkMcpTool list (tests read it)
        self.checkpoints: dict[str, list] = {}
        self._log = open(log_path, "a") if log_path else None

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        for t in list(self.threads.values()):
            try:
                await t.client.disconnect()
            except Exception:
                pass
        self.threads.clear()

    @property
    def alive(self) -> bool:
        return True

    @property
    def proc(self):
        # Agent.main waits on runtime.proc.wait(); Claude clients are per-thread, so never "exit".
        class _Forever:
            async def wait(self_inner):
                await asyncio.Event().wait()
        return _Forever()

    def _stderr(self, line: str) -> None:
        if self._log:
            self._log.write(line.rstrip() + "\n")
            self._log.flush()

    def _mcp(self, tools: list[dict], handler: ToolHandler | None):
        self._last_sdk_tools = []
        if not tools or handler is None:
            return {}, []
        sdk_tools = []
        for spec in tools:
            async def run(args, _name=spec["name"]):
                try:
                    value = await handler(_name, args)
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    return {"content": [{"type": "text", "text": text}]}
                except Exception as e:
                    return {"content": [{"type": "text", "text": f"error: {e}"}], "is_error": True}
            sdk_tools.append(sdk_tool(spec["name"], spec["description"], spec["inputSchema"])(run))
        self._last_sdk_tools = sdk_tools
        server = create_sdk_mcp_server(MCP, tools=sdk_tools)
        return {MCP: server}, [f"mcp__{MCP}__{s['name']}" for s in tools]

    async def _open(self, session_id: str, *, resume: bool, fork_from: str | None = None,
                    model: str | None = None, effort: str | None = None) -> _Thread:
        tools = self.tool_specs.get(session_id, [])
        mcp, allowed = self._mcp(tools, self.handlers.get(session_id))
        self.sdk_tools[session_id] = self._last_sdk_tools
        opts = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": self.instructions[session_id]},
            mcp_servers=mcp, allowed_tools=allowed, permission_mode="bypassPermissions",
            model=model or self.model, effort=effort or self.effort, cwd=self.cwd, env=_env(),
            setting_sources=[], include_partial_messages=True, stderr=self._stderr,
            cli_path=self.cli_path,
        )
        if fork_from:
            opts.resume, opts.fork_session, opts.session_id = fork_from, True, session_id
        elif resume:
            opts.resume = session_id
        else:
            opts.session_id = session_id
        cls = _client_class()
        client = cls(opts, self.sdk_tools[session_id]) if cls is not ClaudeSDKClient else cls(opts)
        await client.connect()
        t = self.threads[session_id] = _Thread(client, session_id)
        return t

    async def start_thread(self, instructions: str, tools: list[dict], handler: ToolHandler | None,
                           ephemeral: bool = False) -> str:
        sid = str(uuid.uuid4())
        self.instructions[sid], self.tool_specs[sid] = instructions, tools
        if handler:
            self.handlers[sid] = handler
        await self._open(sid, resume=False)
        return sid

    async def resume_thread(self, thread_id: str, handler: ToolHandler | None, *,
                            instructions: str | None = None, tools: list[dict] | None = None) -> str:
        if instructions is None:
            raise RuntimeError_("claude resume needs the thread's instructions")
        self.instructions[thread_id], self.tool_specs[thread_id] = instructions, tools or []
        if handler:
            self.handlers[thread_id] = handler
        await self._open(thread_id, resume=True)
        return thread_id

    async def fork_thread(self, thread_id: str, handler: ToolHandler | None = None, *,
                          model: str | None = None, effort: str | None = None) -> str:
        sid = str(uuid.uuid4())
        # Same prompt and tools as the parent: the history references those tools, and an
        # identical prefix keeps the prompt cache warm.
        self.instructions[sid] = self.instructions[thread_id]
        self.tool_specs[sid] = self.tool_specs.get(thread_id, [])
        if handler or thread_id in self.handlers:
            self.handlers[sid] = handler or self.handlers[thread_id]
        await self._open(sid, resume=False, fork_from=thread_id, model=model, effort=effort)
        return sid

    async def drop_thread(self, thread_id: str) -> None:
        t = self.threads.pop(thread_id, None)
        for d in (self.instructions, self.tool_specs, self.handlers):
            d.pop(thread_id, None)
        if t:
            try:
                await t.client.disconnect()
            except Exception:
                pass

    async def run_turn(self, thread_id: str, text: str, *, on_event: EventHandler | None = None,
                       output_schema: dict | None = None, timeout: float = 6 * 3600,
                       on_start: Callable[[str], None] | None = None) -> TurnResult:
        t = self.threads.get(thread_id)
        if t is None and thread_id in self.instructions:
            t = await self._open(thread_id, resume=True)
        if t is None:
            raise RuntimeError_(f"unknown thread {thread_id}")
        if output_schema:
            text += ("\n\n只输出一个 JSON 对象，不要任何其他文字或代码块标记。JSON Schema：\n"
                     + json.dumps(output_schema, ensure_ascii=False))
        async with t.lock:
            turn_id = str(uuid.uuid4())
            self.turn_ids[thread_id] = turn_id
            if on_start:
                on_start(turn_id)
            try:
                return await asyncio.wait_for(self._turn(t, text, on_event, turn_id, output_schema is not None), timeout)
            except asyncio.TimeoutError:
                await t.client.interrupt()
                # Drain to a terminal SDK result before permitting continuation.
                async def settle():
                    async for msg in t.client.receive_response():
                        if isinstance(msg, ResultMessage):
                            return
                    raise RuntimeError_('timeout: previous SDK execution did not settle')
                await asyncio.wait_for(settle(), 60)
                return TurnResult("failed", error="turn timeout (execution settled)")
            except asyncio.CancelledError:
                await t.client.interrupt()
                raise
            except Exception as e:
                # SDK transport loss: disconnect owns/settles its process. Reopen
                # this same session on the next attempt, retaining observed effects.
                await asyncio.wait_for(t.client.disconnect(), 60)
                self.threads.pop(thread_id, None)
                return TurnResult('failed', error=f'{type(e).__name__}: {e}')
            finally:
                self.turn_ids.pop(thread_id, None)

    async def _turn(self, t: _Thread, text: str, on_event: EventHandler | None, turn_id: str, json_only: bool) -> TurnResult:
        checkpoint = self.checkpoints.setdefault(t.session_id, [])
        checkpoint.append({'input': text[-16000:]})
        del checkpoint[:-60]
        await t.client.query(text)
        items: list[dict] = []
        texts: list[str] = []
        msg_n = 0

        def emit(method: str, p: dict) -> None:
            if on_event:
                try:
                    on_event(method, {"threadId": t.session_id, "turnId": turn_id, **p})
                except Exception:
                    log.exception("listener failed")

        async for msg in t.client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if msg.parent_tool_use_id is None and ev.get("type") == "content_block_delta" \
                        and ev.get("delta", {}).get("type") == "text_delta":
                    emit("item/agentMessage/delta", {"itemId": f"m{msg_n}", "delta": ev["delta"]["text"]})
                elif ev.get("type") == "message_start" and msg.parent_tool_use_id is None:
                    msg_n += 1
                    emit("item/started", {"item": {"type": "agentMessage", "id": f"m{msg_n}", "phase": "final_answer"}})
            elif isinstance(msg, AssistantMessage):
                if msg.parent_tool_use_id is not None:
                    continue
                body = "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                new = _items(msg)
                checkpoint.append({'text': body, 'items': new})
                del checkpoint[:-60]
                if new and body:
                    # Text before a tool call is commentary, not the answer.
                    emit("item/completed", {"item": {"type": "agentMessage", "id": f"c{msg_n}", "text": body, "phase": "commentary"}})
                elif body:
                    texts.append(body)
                for it in new:
                    items.append(it)
                    emit("item/completed", {"item": it})
                usage = msg.usage or {}
                ctx = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0))
                if ctx:
                    t.fill = ctx / 1_000_000
            elif isinstance(msg, ResultMessage):
                status = "interrupted" if (msg.terminal_reason or "").startswith("aborted") else \
                    ("failed" if msg.is_error else "completed")
                final = texts[-1] if texts else (msg.result or "")
                if json_only:
                    final = _extract_json(msg.result or final)
                err = "; ".join(msg.errors or []) or (msg.result if msg.is_error else None)
                return TurnResult(status, final, err, items)
        return TurnResult("failed", "", "claude session ended without a result", items)

    async def recovery_checkpoint(self, thread_id: str, terminal: bool) -> str:
        t = self.threads.get(thread_id)
        if (t and t.lock.locked()) or not terminal:
            raise RuntimeError_('previous Claude execution outcome is unknown')
        return json.dumps({'thread': thread_id, 'events': self.checkpoints.get(thread_id, [])}, ensure_ascii=False)[-32000:]

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        t = self.threads.get(thread_id)
        if t and self.turn_ids.get(thread_id) == turn_id:
            try:
                await t.client.interrupt()
            except Exception as e:
                log.warning("interrupt failed: %s", e)

    def context_fill(self, thread_id: str) -> float:
        t = self.threads.get(thread_id)
        return t.fill if t else 0.0


def _client_class():
    if os.environ.get("CTHULHU_FAKE_CLAUDE"):
        from tests.fake_claude import FakeClient
        return FakeClient
    return ClaudeSDKClient


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text
