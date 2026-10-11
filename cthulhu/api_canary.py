"""Bounded, harmless Galaxy API checks. No company credential is read here."""
import argparse
import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

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


async def run(link, provider="ali", mode="text"):
    catalog = await link.call("api_catalog", timeout=15)
    offered = next((p for p in catalog if p["id"] == provider), None)
    if not offered:
        return {"status": "blocked", "reason": "eligible provider is not registered", "generated_requests": 0}
    results = []
    for model in ("gpt-6-astra", "gpt-6.1-sol"):
        if model not in offered["models"]:
            return {"status": "blocked", "reason": "required model not advertised", "generated_requests": len(results)}
        body = {"model": model, "input": "Reply with the single word ready.",
                "reasoning": {"effort": "low"}, "service_tier": "default", "max_output_tokens": 256}
        result, _ = await response(link, provider, body)
        results.append({"check": "text", **result})
        if not result["ok"] or not result["visible_nonempty"]:
            return {"status": "failed", "results": results, "generated_requests": len(results)}
    if mode == "text":
        return {"status": "passed", "results": results, "generated_requests": 2}
    if mode != "full":
        raise ValueError("invalid canary mode")

    nonce = "echo_" + secrets.token_hex(4)
    history = [{"role": "user", "content": f"Call probe_echo with value {nonce}. After its result, reply with that value."}]
    tool = {"type": "function", "name": "probe_echo", "description": "Return the supplied value; no side effects.",
            "strict": True, "parameters": {"type": "object", "properties": {"value": {"type": "string"}},
                                           "required": ["value"], "additionalProperties": False}}
    body = {"model": "gpt-6-astra", "input": history, "tools": [tool],
            "tool_choice": {"type": "function", "name": "probe_echo"}, "parallel_tool_calls": False,
            "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "low"}, "max_output_tokens": 256}
    result, terminal = await response(link, provider, body)
    results.append({"check": "tool_call", **result})
    calls = [i for i in (terminal or {}).get("output", []) if i.get("type") == "function_call"]
    if not result["ok"] or len(calls) != 1 or calls[0].get("name") != "probe_echo":
        return {"status": "failed", "results": results, "generated_requests": 3, "echo_executions": 0}
    call = calls[0]
    if json.loads(call["arguments"]) != {"value": nonce}:
        return {"status": "failed", "results": results, "generated_requests": 3, "echo_executions": 0}
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
        return {"status": "failed", "results": results, "generated_requests": 4, "echo_executions": 1, "tool_ledger": ledger}

    opened = await link.call("api_open", provider=provider, body={
        "model": "gpt-6.1-sol", "input": "Count slowly from one to twenty.", "max_output_tokens": 128})
    first = await link.call("api_next", id=opened["id"], timeout=100)
    started = bool(first.get("data")) and not first.get("error") and not first.get("done")
    cancelled = await link.call("api_cancel", id=opened["id"], timeout=10)
    if not isinstance(cancelled, dict):
        cancelled = {"cancelled": True, "upstream_closed": False}
    cancelled["started_event_observed"] = bool(started)
    results.append({"check": "targeted_cancel", "id": opened["id"], **cancelled})
    return {"status": "passed" if started and cancelled.get("upstream_closed") else "incomplete",
            "results": results, "generated_requests": 5, "echo_executions": 1, "tool_ledger": ledger,
            "provider_disconnect": "local integration test only; production provider WS not disrupted",
            "fast": "not verified; no additional tier benchmark"}


async def main(args):
    link, task = await connect(args.config)
    try:
        result = await run(link, args.provider, args.mode)
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
    p.add_argument("--output", required=True)
    asyncio.run(main(p.parse_args()))
