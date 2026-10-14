"""Stand-in for the Galaxy AI judgments, installed by CTHULHU_FAKE_GALAXY_AI=1.

Deterministic: probes containing "BADPROBE" are rejected, a probe update whose stage_note
contains "DODGE" is judged not genuine, and "AI_DOWN" anywhere makes the call fail
(so the hub's program fallbacks are exercised). Reuse picks the first candidate whose
recent tasks share a word starting with "REPO" with the new task.
"""
import json
import re


def reply(system: str, content: str) -> str:
    p = json.loads(content)
    if "AI_DOWN" in content:
        raise RuntimeError("fake galaxy AI is down")
    if "旧日支配者（每个附最近做过的任务）" in system:
        words = set(re.findall(r"REPO\w+", p["task"]))
        for c in p["candidates"]:
            if words & set(re.findall(r"REPO\w+", json.dumps(c, ensure_ascii=False))):
                return json.dumps({"reuse": c["id"], "reason": "同一仓库"})
        return json.dumps({"reuse": None, "reason": "无关"})
    if "进度探针" in system:
        ok = "BADPROBE" not in p["command"]
        out = {"ok": ok, "reason": "合理" if ok else "与任务无关"}
        if "stage_change_genuine" in system:
            out["stage_change_genuine"] = "DODGE" not in p.get("stage_note", "")
        return json.dumps(out, ensure_ascii=False)
    if "pace" in system:
        s = p["samples"]
        grew = len(s) >= 2 and s[-1] > s[-2]
        return json.dumps({"pace": "ok" if grew else "none", "summary": f"本期样本 {s[-3:]}",
                           "note_to_azathoth": "", "probe_stale": "STALE" in p["recent_work"]}, ensure_ascii=False)
    return json.dumps({"done": "前任做了一些事", "artifacts": "无", "stuck_on": "未登记探针", "next_steps": "登记探针"},
                      ensure_ascii=False)
