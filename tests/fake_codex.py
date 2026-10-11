#!/usr/bin/env python3
"""Minimal stand-in for `codex app-server` used by the tests.

Replies to each turn with "echo: <input>". If the input contains
`CALL <tool> <json>`, it first invokes that dynamic tool and echoes the result.
`REPLY <text>` answers exactly <text>.
Memory distillation turns (with outputSchema) return {"add": [...], "update": [], "forget": []}.
"""
import itertools
import json
import time
import re
import sys

ids = itertools.count(1)
threads = {}
pending = {}


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def finish(thread, turn, text):
    item = {"type": "agentMessage", "id": f"i{next(ids)}", "text": text, "phase": "final_answer"}
    out({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": item}})
    out({"method": "item/agentMessage/delta", "params": {"threadId": thread, "turnId": turn, "itemId": item["id"], "delta": text}})
    out({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": item}})
    out({"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": turn, "tokenUsage": {
        "total": {"inputTokens": 10}, "last": {"inputTokens": 10}, "modelContextWindow": 1000}}})
    out({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed", "error": None, "items": []}}})


def with_task(args, text):
    # "$TASK" in a CALL is replaced by the task id the incoming message was tagged with.
    m = re.search(r"任务 (task_\w+)", text)
    return args.replace("$TASK", m.group(1)) if m else args


for line in sys.stdin:
    msg = json.loads(line)
    if "method" not in msg:
        # response to our tool call
        thread, turn = pending.pop(msg["id"])
        text = msg["result"]["contentItems"][0]["text"]
        finish(thread, turn, "tool: " + text)
        continue
    m, p, rid = msg["method"], msg.get("params", {}), msg.get("id")
    if rid is None:
        continue
    if m == "initialize":
        out({"id": rid, "result": {}})
    elif m in ("thread/start", "thread/fork"):
        t = f"th{next(ids)}"
        threads[t] = p
        out({"id": rid, "result": {"thread": {"id": t}}})
    elif m == "thread/resume":
        out({"id": rid, "result": {"thread": {"id": p["threadId"]}}})
    elif m == "turn/start":
        turn = f"tu{next(ids)}"
        out({"id": rid, "result": {"turn": {"id": turn}}})
        text = p["input"][0]["text"]
        body = text.split("</handoff>")[-1]
        if p.get("outputSchema"):
            finish(p["threadId"], turn, json.dumps({"add": ["记住: " + text[-20:]], "update": [], "forget": []}))
        elif "CALL " in body:
            _, rest = body.split("CALL ", 1)
            name, args = rest.split(" ", 1)
            args = with_task(args, text)
            cid = next(ids)
            pending[cid] = (p["threadId"], turn)
            out({"id": cid, "method": "item/tool/call", "params": {"threadId": p["threadId"], "turnId": turn,
                 "callId": "c", "tool": name, "arguments": json.loads(args.strip().splitlines()[0])}})
        elif "SLEEP " in body and "定时巡检" not in body:   # yog's patrol prompt quotes it; only the target sleeps
            time.sleep(float(body.split("SLEEP ", 1)[1].split()[0]))
            finish(p["threadId"], turn, "slept")
        elif "REPLY " in body:
            finish(p["threadId"], turn, body.split("REPLY ", 1)[1].strip())
        else:
            finish(p["threadId"], turn, "echo[codex]: " + text)
    else:
        out({"id": rid, "result": {}})
