"""Bounded, harmless Galaxy API checks. No company credential is read here."""
import argparse
import asyncio
import base64
import json
import secrets
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .ops import connect


async def response(link, provider, body):
    start = time.monotonic()
    opened = await link.call("api_open", provider=provider, body=body, timeout=20)
    pending = b""
    terminal = None
    first_event = first_text = None
    text = ""
    try:
        while True:
            part = await link.call("api_next", id=opened["id"], timeout=100)
            if part.get("error"):
                return {"ok": False, **opened, "error": part["error"]}, None
            pending += base64.b64decode(part.get("data", ""))
            while b"\n\n" in pending:
                block, pending = pending.split(b"\n\n", 1)
                data = b"\n".join(x[5:].lstrip() for x in block.split(b"\n") if x.startswith(b"data:"))
                if not data:
                    continue
                event = json.loads(data)
                at = time.monotonic() - start
                if first_event is None:
                    first_event = at
                if event.get("type") == "response.output_text.delta" and event.get("delta"):
                    first_text = first_text if first_text is not None else at
                    text += event["delta"]
                if event.get("type") == "response.completed":
                    terminal = event.get("response", {})
            if part.get("done"):
                break
        result = {"ok": bool(terminal and terminal.get("status") == "completed"), **opened,
                  "returned_model": (terminal or {}).get("model"),
                  "returned_tier": (terminal or {}).get("service_tier"),
                  "usage": (terminal or {}).get("usage"), "first_event_s": first_event,
                  "first_visible_s": first_text, "completed_s": time.monotonic() - start,
                  "visible_nonempty": bool(text)}
        return result, terminal
    finally:
        if link.connected.is_set():
            try:
                await link.call("api_cancel", id=opened["id"], timeout=6)
            except Exception:
                pass


async def run(link, provider="ali", mode="text", models=None):
    if mode not in ("text", "full"):
        raise ValueError("invalid canary mode")
    models = list(dict.fromkeys(models if models is not None else ("gpt-6-astra", "gpt-6.1-sol")))
    if not models or not all(isinstance(m, str) and m for m in models):
        raise ValueError("canary requires at least one model")
    catalog = await link.call("api_catalog", timeout=15)
    offered = next((p for p in catalog if p["id"] == provider), None)
    if not offered:
        return {"status": "blocked", "reason": "eligible provider is not registered", "generated_requests": 0}
    results = []
    if any(model not in offered["models"] for model in models):
        return {"status": "blocked", "reason": "required model not advertised", "generated_requests": 0}
    if any(offered.get("model_backends", {}).get(m, "codex") != "codex" for m in models):
        return {"status": "blocked", "reason": "use --native-claude for Claude models", "generated_requests": 0}
    for model in models:
        body = {"model": model, "input": "Reply with the single word ready.",
                "reasoning": {"effort": "low"}, "service_tier": "default", "max_output_tokens": 256}
        result, _ = await response(link, provider, body)
        results.append({"check": "text", **result})
        if not result["ok"] or not result["visible_nonempty"]:
            return {"status": "failed", "results": results, "generated_requests": len(results)}
    if mode == "text":
        return {"status": "passed", "results": results, "generated_requests": len(results)}

    nonce = "echo_" + secrets.token_hex(4)
    history = [{"role": "user", "content": f"Call probe_echo with value {nonce}. After its result, reply with that value."}]
    tool = {"type": "function", "name": "probe_echo", "description": "Return the supplied value; no side effects.",
            "strict": True, "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                           "required": ["value"], "additionalProperties": False}}
    body = {"model": models[0], "input": history, "tools": [tool],
            "tool_choice": {"type": "function", "name": "probe_echo"}, "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "low"}, "max_output_tokens": 256}
    result, terminal = await response(link, provider, body)
    results.append({"check": "tool_call", **result})
    calls = [i for i in (terminal or {}).get("output", []) if i.get("type") == "function_call"]
    if not result["ok"] or len(calls) != 1 or calls[0].get("name") != "probe_echo":
        return {"status": "failed", "results": results, "generated_requests": len(results), "echo_executions": 0}
    call = calls[0]
    if json.loads(call["arguments"]) != {"value": nonce}:
        return {"status": "failed", "results": results, "generated_requests": len(results), "echo_executions": 0}
    ledger = [{"call_id": call["call_id"], "request_id": result["id"],
               "tool": "probe_echo", "executions": 1, "arguments_validated": True}]
    # The sole handler is an in-memory echo. Full reasoning/tool history is retained.
    history += terminal["output"] + [{"type": "function_call_output", "call_id": call["call_id"], "output": nonce}]
    result, final = await response(link, provider, body | {"input": history, "tool_choice": "none"})
    items = (final or {}).get("output", [])
    output = "".join(c.get("text", "") for i in items if i.get("type") == "message"
                     for c in i.get("content", []) if c.get("type") == "output_text")
    result["echo_matches"] = nonce in output and not any(i.get("type") == "function_call" for i in items)
    results.append({"check": "tool_result_and_history", **result})
    if not result["ok"] or not result["echo_matches"] or not result["visible_nonempty"]:
        return {"status": "failed", "results": results, "generated_requests": len(results), "echo_executions": 1, "tool_ledger": ledger}

    opened = await link.call("api_open", provider=provider, body={
        "model": models[-1], "input": "Count slowly from one to twenty.", "max_output_tokens": 128})
    first = await link.call("api_next", id=opened["id"], timeout=100)
    started = bool(first.get("data")) and not first.get("error") and not first.get("done")
    cancelled = await link.call("api_cancel", id=opened["id"], timeout=10)
    if not isinstance(cancelled, dict):
        cancelled = {"cancelled": True, "upstream_closed": False}
    cancelled["started_event_observed"] = bool(started)
    results.append({"check": "targeted_cancel", "id": opened["id"], **cancelled})
    return {"status": "passed" if started and cancelled.get("upstream_closed") else "incomplete",
            "results": results, "generated_requests": len(results), "echo_executions": 1, "tool_ledger": ledger,
            "provider_disconnect": "local integration test only; production provider WS not disrupted",
            "fast": "not verified; no additional tier benchmark"}


async def run_native_claude(link, provider, model, mode="full"):
    """Existing native Claude runtime, real SDK tools; no company config here."""
    from .backends import build
    from .runtime import tool
    catalog = await link.call("api_catalog", timeout=15)
    if not any(p["id"] == provider and p.get("model_backends", {}).get(model) == "claude" for p in catalog):
        return {"status": "blocked", "reason": "native Claude model not advertised", "generated_requests": 0}
    requests, effects, deltas = [], [], []
    request_limit = 2  # One tool request and one continuation; no hidden model replay.
    async def call(method, **kw):
        if method == "api_open" and len(requests) >= request_limit:
            raise RuntimeError("native canary request budget exhausted")
        result = await link.call(method, **kw)
        if method == "api_open":
            requests.append(dict(result))
        elif method == "api_next":
            for r in requests:
                if r["id"] == kw["id"]:
                    r.update(result.get("meta") or {})
                    if result.get("error"):
                        r["error"] = result["error"]
        return result
    scoped = SimpleNamespace(call=call, connected=link.connected)
    nonce = "echo_" + secrets.token_hex(4)
    # The answer is generated by the tool and never included in the prompt.
    answer = "tool_result_" + secrets.token_hex(8)
    async def echo(name, args):
        if name != "echo" or args != {"value": nonce} or effects:
            raise ValueError("unexpected canary tool invocation")
        effects.append({"tool": name, "arguments_validated": True, "executions": 1})
        return answer
    def event(method, params):
        if method == "item/agentMessage/delta" and params.get("delta"):
            deltas.append(time.monotonic())
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="cthulhu-native-canary-") as directory:
        rt = build({"backend": "claude", "provider": provider, "model": model},
                   codex="must-not-run-codex", cwd=directory, log_dir=Path(directory),
                   sub_agents=False, api_link=scoped)
        try:
            await rt.start()
            thread = await rt.start_thread("Run only the requested harmless echo check.",
                [tool("echo", "Return a fresh verification value; no side effects.",
                      {"value": {"type": "string"}}, ["value"])], echo)
            result = await rt.run_turn(thread,
                f"Call echo exactly once with value {nonce}. Reply with its returned value exactly.",
                on_event=event, timeout=120)
            native_tool = any(i.get("tool") == "echo" for i in result.items)
            ok = (result.status == "completed" and len(effects) == 1 and native_tool and
                  answer in result.text and bool(deltas) and rt.api_result.get("returned_model") == model)
            receipt = {"status": "passed" if ok else "failed", "backend": "claude",
                       "runtime": type(rt).__name__, "results": requests, "native_tool_observed": native_tool,
                       "tool_ledger": effects, "echo_matches": answer in result.text,
                       "streaming_observed": bool(deltas), "completed_s": time.monotonic() - started,
                       "error": result.error, "generated_requests": len(requests)}
        finally:
            await rt.close()
    if not ok or mode != "full":
        return receipt
    request_limit = 3
    opened = await call("api_open", provider=provider, protocol="messages", body={
        "model": model, "messages": [{"role": "user", "content": "Count slowly from one to one hundred."}],
        "max_tokens": 512})
    try:
        first = await call("api_next", id=opened["id"], timeout=100)
    finally:
        cancelled = await link.call("api_cancel", id=opened["id"], timeout=10)
    started = bool(first.get("data")) and not first.get("error") and not first.get("done")
    receipt["targeted_transport_cancel"] = {**cancelled, "started_event_observed": started}
    receipt["generated_requests"] = len(requests)
    if not started or not cancelled.get("upstream_closed"):
        receipt["status"] = "incomplete"
    return receipt


async def main(args):
    link, task = await connect(args.config)
    try:
        result = (await run_native_claude(link, args.provider, args.model[0], args.mode) if args.native_claude
                  else await run(link, args.provider, args.mode, args.model))
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"status": result["status"], "generated_requests": result["generated_requests"]}))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="local temple config, never a company credential")
    p.add_argument("--provider", default="ali")
    p.add_argument("--mode", choices=["text", "full"], default="text")
    p.add_argument("--model", action="append", help="advertised model to check; repeat for multiple models")
    p.add_argument("--native-claude", action="store_true", help="use existing Claude SDK runtime and its actual echo tool")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    if args.native_claude and (not args.model or len(args.model) != 1):
        p.error("--native-claude requires exactly one --model")
    asyncio.run(main(args))
