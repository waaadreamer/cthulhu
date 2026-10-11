"""In-process stand-in for ClaudeSDKClient, installed by tests via CTHULHU_FAKE_CLAUDE=1.

Mirrors fake_codex.py: echoes input, runs `CALL <tool> <json>` through the
temple MCP server, and keeps per-session history so forks can be checked.
"""
import json
import uuid

from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock

SESSIONS: dict[str, list[str]] = {}


class FakeClient:
    def __init__(self, options, tools):
        self.o = options
        sid = options.session_id or options.resume
        if options.fork_session:
            SESSIONS[options.session_id] = list(SESSIONS.get(options.resume, []))
            sid = options.session_id
        self.sid = sid
        SESSIONS.setdefault(sid, [])
        self.tools = {f"mcp__temple__{t.name}": t for t in tools}

    async def connect(self, prompt=None):
        pass

    async def disconnect(self):
        pass

    async def interrupt(self):
        pass

    async def query(self, text):
        self.pending = text

    async def receive_response(self):
        text = self.pending
        hist = SESSIONS[self.sid]
        if self.o.model == "claude-broken":
            # Simulates the relay rejecting the request (rate limit / content filter).
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1,
                                session_id=self.sid, result="API Error: 403 temporarily restricted", uuid="x")
            return
        yield StreamEvent(uuid="u", session_id=self.sid, event={"type": "message_start"})
        if "只输出一个 JSON" in text:
            reply = json.dumps({"remember": ["记住: " + (hist[-1] if hist else "")[-20:]], "forget": []}, ensure_ascii=False)
        elif "CALL " in text.split("</handoff>")[-1]:
            name, args = text.split("</handoff>")[-1].split("CALL ", 1)[1].split(" ", 1)
            full = f"mcp__temple__{name}"
            yield AssistantMessage(content=[ToolUseBlock(id="t1", name=full, input=json.loads(args.splitlines()[0]))],
                                   model=self.o.model)
            tool = self.tools.get(full)
            if tool is None:
                reply = f"tool: error: {name} not available"
            else:
                out = await tool.handler(json.loads(args.splitlines()[0]))
                reply = "tool: " + out["content"][0]["text"]
        else:
            reply = f"echo[{self.o.model}]: " + text
        hist.append(text)
        yield StreamEvent(uuid="u", session_id=self.sid,
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": reply}})
        yield AssistantMessage(content=[TextBlock(text=reply)], model=self.o.model,
                               usage={"input_tokens": 10, "cache_read_input_tokens": 0})
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id=self.sid, result=reply, uuid=str(uuid.uuid4()))
