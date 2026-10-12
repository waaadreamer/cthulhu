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


def continuation_fixture(body, instructions):
    """Deterministic model decisions only for the isolated continuation scenario."""
    if 'DIRECT_NUDGE_TEST' in body and '定时巡检' in body:
        match = re.search(r'未完成任务[^\n]*\n', body)
        tasks = json.JSONDecoder().raw_decode(body[match.end():])[0] if match else []
        for row in tasks:
            if row['task']['title'] == 'DIRECT_NUDGE_TEST' and not row.get('worker_nudges'):
                return 'temple_worker_review', {'task_id': row['task']['id'], 'revision': row['revision'],
                     'agent_id': row['executors'][0]['agent_id'], 'state': 'stalled',
                     'evidence': 'Isolated fixture confirms existing assignment made no progress; no external workload is running.'}
        return None
    if 'Task-scoped worker nudge' in body and 'DIRECT_NUDGE_WORKER' in json.dumps(instructions) and '定时巡检' not in body:
        return 'temple_agents', {}
    if 'Automatic waiting-state inspection' in body and 'LEGACY_WAIT_TEST' in body and '定时巡检' not in body:
        row = json.loads(body.split('【续办前最新状态，以此为准；已完成、仍在执行或真正待决策时不要重试】\n')[-1])
        worker = re.search(r'EXISTING_EXECUTOR=(old_\w+)', row['instruction']['text']).group(1)
        return 'temple_send', {'to': worker, 'task_id': row['task']['id'], 'text': 'READONLY_WAIT_COLLECTION: inspect existing completed fixture; do not restart.'}
    if 'READONLY_WAIT_COLLECTION' in body and '定时巡检' not in body and 'LEGACY_WAIT_WORKER' in json.dumps(instructions):
        task = re.search(r'任务 (task_\w+)', body).group(1)
        return 'temple_send', {'to': 'sh', 'task_id': task, 'text': 'WAIT_COLLECTION_COMPLETE: existing result collected; no workload launched.'}
    if 'WAIT_COLLECTION_COMPLETE' in body and '定时巡检' not in body:
        task = re.search(r'任务 (task_\w+)', body).group(1)
        return 'temple_send', {'to': 'az', 'task_id': task, 'status': 'done', 'text': 'Existing fixture completion collected and verified.'}
    if 'AUTO_CONTINUATION_TEST' in body and '定时巡检' in body:
        match = re.search(r'未完成任务[^\n]*\n', body)
        tasks = json.JSONDecoder().raw_decode(body[match.end():])[0] if match else []
        for row in tasks:
            if row['task']['title'] == 'AUTO_CONTINUATION_TEST' and not any(row[k] for k in ('active', 'pending', 'unavailable', 'owner_blocked')) and not row['continuation']:
                return 'temple_task_continuation', {'task_id': row['task']['id'], 'revision': row['revision'],
                    'state': 'ready', 'evidence': 'Isolated fixture: no external processes; latest instruction is ready.'}
        return None
    if 'Automatic continuation review' in body and 'AUTO_CONTINUATION_TEST' in body:
        row = json.loads(body.split('【续办前最新状态，以此为准；已完成、仍在执行或真正待决策时不要重试】\n')[-1])
        return 'temple_spawn', {'name': 'continuation fixture worker', 'galaxy_id': 'g1',
            'task_id': row['task']['id'], 'task': row['instruction']['text'], 'skill': 'CONTINUATION_WORKER_FIXTURE'}
    if 'CONTINUATION_WORKER_FIXTURE' in json.dumps(instructions) and '你刚被创建' in body:
        return 'temple_agents', {}
    return None


for line in sys.stdin:
    msg = json.loads(line)
    if "method" not in msg:
        # response to our tool call
        entry = pending.pop(msg["id"])
        thread, turn = entry[:2]
        if len(entry) > 2:
            out({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': turn, 'item': entry[2]}})
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
        fixture = continuation_fixture(body, threads.get(p['threadId'], {})) if not p.get('outputSchema') else None
        if p.get("outputSchema"):
            finish(p["threadId"], turn, json.dumps({"add": ["记住: " + text[-20:]], "update": [], "forget": []}))
        elif fixture:
            name, args = fixture
            cid = next(ids)
            pending[cid] = (p['threadId'], turn, {'type': 'dynamicToolCall', 'id': str(cid), 'tool': name, 'arguments': args})
            out({'id': cid, 'method': 'item/tool/call', 'params': {'threadId': p['threadId'], 'turnId': turn,
                 'callId': 'c', 'tool': name, 'arguments': args}})
        elif "CALL " in body and "定时巡检" not in body:
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
