"""Hub: a galaxy's task desk (design §1.3, §2, §3).

Runs inside the galaxy process. Old_ones connect only here (a localhost websocket);
the hub is authoritative for their tasks, messages and checkpoints (hub_store), and
forwards to the temple only what the temple must know: bindings, the task index,
reports for azathoth and cross-galaxy relays. A dropped temple link only delays
those (outbox); checkpoints, nudges and local messages keep running.

Models decide only four narrow questions (galaxy_ai); every one has a program fallback.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import secrets
import time
from pathlib import Path

import websockets

from . import roles
from .galaxy_ai import GalaxyAI
from .hub_store import HubStore, new_id
from .protocol import agent_token, dumps
from .roles import OWNER, SYSTEM, URGENT, NUDGE, Denied, Role

log = logging.getLogger(__name__)

CHECK_EVERY = 600            # checkpoint interval
START_GRACE = 300            # a bound old_one must start its first turn within this
PROBE_TIMEOUT = 30
SWEEP = 10
RESPAWN_LIMIT = 1            # design D7
REUSE_MAX_FILL = 0.7         # design D7
REVIEW_REMIND = 1800         # design D3
REVIEW_OWNER = 3600
STAGE_CHANGE_WINDOW = 3600   # design D11
IN_FLIGHT = ("assigning", "awaiting_probe", "running", "review", "suspended")
TERMINAL = ("closed", "cancelled", "assign_failed")
TICKING = ("awaiting_probe", "running")


def clip(text: str | None, n: int = 300) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "…"


def stamp(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "—"


def parse_number(out: str) -> float | None:
    try:
        v = float((out or "").strip())
        return v if math.isfinite(v) else None
    except ValueError:
        return None


class Hub:
    def __init__(self, galaxy):
        self.g = galaxy
        self.link = galaxy.link
        self.store = HubStore(Path(galaxy.state) / "hub.db")
        self.ai = GalaxyAI(self.link, galaxy.config)
        self.secret = self.store.setting("secret") or secrets.token_hex(32)
        self.store.set_setting("secret", self.secret)
        self.conns: dict[str, object] = {}        # old_one id -> websocket
        self.pending: dict[str, dict] = {}        # (agent, rpc id) futures
        self.status: dict[str, dict] = {}         # live status reported by old_ones
        self.streams: dict[str, str] = {}         # Galaxy API stream id -> old_one id
        self.assign_lock = asyncio.Lock()
        self.task_locks: dict[str, asyncio.Lock] = {}
        self.bindings: list[dict] = []            # global bindings table pushed by the temple
        self.port = 0
        self.draining = False
        self.rpc_id = 0
        cfg = galaxy.config
        # Overridable for tests; production uses the design's 10 minutes.
        self.check_every = float(cfg.get("check_every", CHECK_EVERY))
        self.start_grace = float(cfg.get("start_grace", START_GRACE))
        self.sweep_every = min(SWEEP, self.check_every / 4)

    # lifecycle

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/hub"

    async def serve(self) -> None:
        server = await websockets.serve(self.ws_handler, "127.0.0.1", int(self.g.config.get("hub_port", 0)),
                                        max_size=64 * 1024 * 1024, ping_interval=20, ping_timeout=60)
        self.port = server.sockets[0].getsockname()[1]
        log.info("hub listening on %s", self.url)
        self.launch_all()
        await asyncio.gather(self.clock(), server.wait_closed())

    def launch_all(self) -> None:
        for a in self.store.old_ones(include_stopped=False):
            self.g.launch_hub_agent(self.launch_spec(a))

    def launch_spec(self, a: dict) -> dict:
        return {"id": a["id"], "name": a["name"], "role": Role.OLD_ONE.value, "epoch": a["epoch"],
                "token": agent_token(self.secret, a["id"], a["epoch"]), "spec": a["spec"],
                "thread_id": a["thread_id"], "hub_url": self.url}

    async def on_temple(self) -> None:
        """(Re)connected to the temple: replay the outbox, then push the full roster and task index."""
        for row in self.store.outbox():
            try:
                await self.link.call(row["method"], timeout=30, **row["params"])
                self.store.drop_outbox(row["id"])
            except Exception as e:
                log.warning("outbox replay stopped at %s: %s", row["method"], e)
                return
        await self.push_roster()
        for t in self.store.tasks(IN_FLIGHT):
            await self.push_index(t)

    async def to_temple(self, method: str, **params) -> None:
        """Deliver an event to the temple now, or queue it for the next connection."""
        params.setdefault("event_id", new_id("event"))
        if self.link.connected.is_set():
            try:
                await self.link.call(method, timeout=30, **params)
                return
            except Exception as e:
                log.warning("temple %s failed, queued: %s", method, e)
        self.store.push_outbox(method, params)

    async def push_roster(self) -> None:
        if not self.link.connected.is_set():
            return
        rows = []
        for a in self.store.old_ones():
            t = self.current_task(a["id"])
            live = self.status.get(a["id"], {})
            rows.append({"id": a["id"], "name": a["name"], "desired": a["desired"],
                         "state": live.get("state", "offline") if a["id"] in self.conns else "offline",
                         "task_id": t and t["id"], "data": {"created_at": a["created_at"], "cwd": a["spec"].get("cwd"),
                                                           "context_fill": live.get("context_fill"), "runtime": live.get("runtime"),
                                                           "current": live.get("current")}})
        try:
            await self.link.call("roster_update", timeout=15, old_ones=rows)
        except Exception:
            log.warning("roster update failed")

    async def push_index(self, t: dict) -> None:
        a = t["executor"] and self.store.old_one(t["executor"])
        await self.to_temple("task_index_update", task_id=t["id"], patch={
            "state": t["state"], "executor": t["executor"], "executor_name": a and a["name"],
            "line": self.line(t), "summary": t["data"].get("last_summary", ""),
            **({"closed_at": t["data"].get("closed_at")} if t["state"] in TERMINAL else {})})

    def line(self, t: dict) -> str:
        d = t["data"]
        cp = d.get("checkpoint", {})
        probe = d.get("probe") or {}
        vals = [s["value"] for s in d.get("samples", []) if s.get("stage") == probe.get("stage") and s.get("value") is not None]
        parts = [f"CP{cp.get('n', 0)}"]
        if probe:
            parts.append(f"S{probe.get('stage', 1)}")
            parts.append("probe " + ("→".join(f"{v:g}" for v in vals[-3:]) if vals else "—"))
        if d.get("last_pace"):
            parts.append(d["last_pace"])
        if d.get("blocker"):
            parts.append("阻塞")
        return " · ".join(parts)

    # tasks: helpers

    def task(self, task_id: str) -> dict:
        t = self.store.task(task_id)
        if not t:
            raise ValueError(f"unknown task {task_id}")
        return t

    def lock(self, task_id: str) -> asyncio.Lock:
        return self.task_locks.setdefault(task_id, asyncio.Lock())

    def current_task(self, agent_id: str) -> dict | None:
        return next(iter(self.store.tasks(IN_FLIGHT, executor=agent_id)), None)

    def event(self, t: dict, kind: str, text: str, **extra) -> None:
        events = t["data"].setdefault("events", [])
        events.append({"at": time.time(), "kind": kind, "text": text, **extra})
        del events[:-300]

    async def save(self, t: dict) -> None:
        self.store.save_task(t)
        await self.push_index(t)

    async def report(self, t: dict | None, kind: str, text: str, attention: bool = False) -> None:
        """To azathoth via the temple: finish/report go direct, everything else into the 10-minute digest."""
        await self.to_temple("report", task_id=t and t["id"], kind=kind, text=text, attention=attention)

    # tasks: assignment (design §2.2)

    async def assign_task(self, task_id: str, description: str, acceptance: str, cwd: str | None) -> dict:
        if self.store.task(task_id):
            return {"task_id": task_id, "duplicate": True}
        async with self.assign_lock:
            busy = {t["executor"] for t in self.store.tasks(IN_FLIGHT)}
            candidates = [a for a in self.store.old_ones(include_stopped=False)
                          if a["id"] not in busy and self.status.get(a["id"], {}).get("state") != "crashed"
                          and (self.status.get(a["id"], {}).get("context_fill") or 0) < REUSE_MAX_FILL]
            choice, reason = None, "没有空闲的旧日支配者，新建"
            if candidates:
                info = [{"id": a["id"], "name": a["name"], "cwd": a["spec"].get("cwd"),
                         "context_fill": self.status.get(a["id"], {}).get("context_fill"),
                         "recent_tasks": [{"description": clip(h.get("description"), 300), "summary": clip(h.get("summary"), 300)}
                                          for h in a["spec"].get("history", [])[-3:]]} for a in candidates]
                try:
                    r = await self.ai.choose_executor(description, info)
                    if r["reuse"] in {a["id"] for a in candidates}:
                        choice, reason = r["reuse"], r["reason"] or "AI 判定可复用"
                    else:
                        reason = r["reason"] or "AI 判定新建"
                except Exception as e:
                    reason = f"复用判定不可用（{clip(str(e), 80)}），新建"
            data = {"reused": bool(choice), "reason": reason, "bound_at": time.time(), "cwd": cwd,
                    "checkpoint": {"n": 0, "next_at": None, "paused": False, "no_probe_hits": 0, "stall_streak": 0,
                                   "probe_fail_streak": 0},
                    "respawns": 0, "samples": [], "probe_history": [], "submissions": [], "events": [], "executors": []}
            if choice:
                agent = self.store.old_one(choice)
            else:
                agent = self.new_old_one(description, cwd)
            data["executors"].append({"agent_id": agent["id"], "from": time.time(), "to": None, "reason": "初始绑定"})
            self.store.add_task(task_id, description, acceptance, "awaiting_probe", agent["id"], data)
        t = self.task(task_id)
        self.event(t, "bound", f"绑定 {agent['name']}（{'复用' if choice else '新建'}：{reason}）")
        await self.save(t)
        await self.brief(t, agent, handover=None)
        if not choice:
            self.g.launch_hub_agent(self.launch_spec(agent))
        await self.push_roster()
        await self.to_temple("bound", task_id=task_id, agent_id=agent["id"], agent_name=agent["name"],
                             reused=bool(choice), reason=reason)
        return {"task_id": task_id, "agent_id": agent["id"], "agent_name": agent["name"], "reused": bool(choice), "reason": reason}

    def new_old_one(self, description: str, cwd: str | None) -> dict:
        n = len(self.store.q("SELECT id FROM old_ones")) + 1
        return self.store.add_old_one(f"{self.g.config.get('name') or self.g.id}·旧日支配者{n}",
                                      {"cwd": cwd or self.g.config.get("cwd"), "history": []})

    async def brief(self, t: dict, agent: dict, handover: str | None) -> None:
        lines = [f"[新任务 {t['id']}] 阿撒托斯派给你的任务，原文如下：", t["description"]]
        if t["acceptance"]:
            lines += ["", "验收标准：" + t["acceptance"]]
        if t["data"].get("cwd"):
            lines += ["", f"工作目录：{t['data']['cwd']}"]
        if handover:
            lines += ["", handover]
        lines += ["", "第一件事：调用 register_task_probe 登记进度探针。10 分钟检查点时仍未登记会被强制打断，"
                  "20 分钟仍未登记会被替换。完成后调用 finish_task 提交可核验的证据。"]
        await self.deliver_system(agent["id"], "\n".join(lines), t, priority=NUDGE, kind="task_brief", footer=False)

    # tasks: commands from azathoth / the owner (via the temple)

    async def close_task(self, task_id: str, note: str = "") -> dict:
        async with self.lock(task_id):
            t = self.task(task_id)
            if t["state"] in TERMINAL:
                return {"task_id": task_id, "state": t["state"]}
            t["state"] = "closed"
            t["data"]["closed_at"] = time.time()
            t["data"]["checkpoint"]["paused"] = True
            subs = t["data"]["submissions"]
            if subs and subs[-1].get("verdict") is None:
                subs[-1].update(verdict="passed", note=note)
            self.event(t, "closed", "阿撒托斯验收通过" + (f"：{note}" if note else ""))
            self.remember_history(t)
            await self.save(t)
        await self.push_roster()
        return {"task_id": task_id, "state": "closed"}

    async def continue_task(self, task_id: str, instruction: str) -> dict:
        async with self.lock(task_id):
            t = self.task(task_id)
            if t["state"] not in ("review", "running", "suspended", "awaiting_probe"):
                raise ValueError(f"task is {t['state']}")
            cp = t["data"]["checkpoint"]
            subs = t["data"]["submissions"]
            if t["state"] == "review" and subs and subs[-1].get("verdict") is None:
                subs[-1].update(verdict="rejected", note=instruction)
            t["state"] = "running" if t["data"].get("probe") else "awaiting_probe"
            cp.update(paused=False, next_at=time.time() + self.check_every)
            t["data"]["blocker"] = None
            t["data"].pop("review_since", None)
            t["data"].pop("review_reminded", None)
            self.event(t, "continued", "阿撒托斯要求继续：" + clip(instruction, 500))
            await self.save(t)
        await self.deliver_system(t["executor"], f"[任务 {task_id} 未通过验收 / 继续] 阿撒托斯的指令：\n{instruction}",
                                  t, priority=NUDGE, kind="task_control", footer="continue")
        return {"task_id": task_id, "state": t["state"]}

    async def cancel_task(self, task_id: str, reason: str = "") -> dict:
        async with self.lock(task_id):
            t = self.task(task_id)
            if t["state"] in TERMINAL:
                return {"task_id": task_id, "state": t["state"]}
            t["state"] = "cancelled"
            t["data"]["closed_at"] = time.time()
            t["data"]["checkpoint"]["paused"] = True
            self.event(t, "cancelled", "任务取消" + (f"：{reason}" if reason else ""))
            self.remember_history(t)
            await self.save(t)
        for m in self.store.q("SELECT id FROM messages WHERE task_id=? AND state IN ('queued','delivered')", (task_id,)):
            self.store.mark(m["id"], "cancelled")
        if t["executor"] in self.conns:
            await self.send_agent(t["executor"], {"type": "task_input_retired", "task_id": task_id})
            try:
                await self.agent_rpc(t["executor"], "task_cancel", task_id=task_id)
            except Exception:
                pass
        await self.push_roster()
        return {"task_id": task_id, "state": "cancelled"}

    def remember_history(self, t: dict) -> None:
        a = self.store.old_one(t["executor"]) if t["executor"] else None
        if not a:
            return
        spec = a["spec"]
        spec.setdefault("history", []).append({"task_id": t["id"], "description": clip(t["description"], 500),
                                               "summary": t["data"].get("last_summary", ""), "state": t["state"]})
        spec["history"] = spec["history"][-10:]
        self.store.set_old_one(a["id"], spec=spec)

    def view(self, task_id: str) -> dict:
        t = self.task(task_id)
        a = t["executor"] and self.store.old_one(t["executor"])
        return t | {"executor_name": a and a["name"],
                    "messages": [{k: m[k] for k in ("id", "sender", "sender_name", "receiver", "kind", "priority", "text", "created_at")}
                                 for m in self.store.messages(task_id=task_id, limit=60)]}

    # old_one calls

    async def register_task_probe(self, agent_id: str, command: str, rationale: str, stage_note: str = "") -> dict:
        t = self.current_task(agent_id)
        if not t or t["state"] not in TICKING:
            raise Denied("you have no task waiting for a probe or running")
        update = t["state"] == "running" and bool(t["data"].get("probe"))
        if update and not stage_note.strip():
            raise ValueError("更新探针必须填写 stage_note：上一阶段做到哪了、为什么要换")
        trial = await self.run_probe(agent_id, command)
        if trial["value"] is None:
            return {"accepted": False, "reason": "试跑未通过：" + trial["error"] + "。命令须退出码为 0，stdout 只有一个数字。"}
        payload = {"task": t["description"], "acceptance": t["acceptance"], "command": command,
                   "rationale": rationale, "trial_value": trial["value"]}
        if update:
            old = t["data"]["probe"]
            payload |= {"old_probe": old["command"], "old_samples": self.stage_values(t)[-8:], "stage_note": stage_note,
                        "recent_work": self.turn_digest(agent_id, t)}
        try:
            verdict = await self.ai.judge_probe(payload, update)
            state, reason = ("approved" if verdict["ok"] else "rejected"), verdict["reason"]
        except Exception as e:
            state, reason = "unreviewed", f"AI 判定不可用，先接受，下个检查点复审（{clip(str(e), 80)}）"
        async with self.lock(t["id"]):
            t = self.task(t["id"])
            if state == "rejected":
                self.event(t, "probe_rejected", f"探针被驳回：{command} — {reason}")
                t["data"]["probe_rejections"] = t["data"].get("probe_rejections", 0) + 1
                await self.save(t)
                await self.report(t, "probe_rejected", f"探针被驳回：`{command}` — {reason}", attention=False)
                if update:
                    return {"accepted": False, "reason": reason, "note": "旧探针继续生效"}
                return {"accepted": False, "reason": reason, "note": "立刻按原因重新登记；检查点计时没有暂停"}
            now = time.time()
            stage = (t["data"]["probe"]["stage"] + 1) if update else 1
            if update:
                old = t["data"]["probe"]
                t["data"]["probe_history"].append(old | {"to": now, "stage_note": stage_note,
                                                         "last_value": (self.stage_values(t) or [None])[-1]})
                changes = [c for c in t["data"].get("stage_changes", []) if now - c < STAGE_CHANGE_WINDOW] + [now]
                t["data"]["stage_changes"] = changes
            t["data"]["probe"] = {"stage": stage, "command": command, "rationale": rationale, "verdict": state,
                                  "reason": reason, "registered_at": now, "baseline": trial["value"], "stage_note": stage_note}
            t["data"]["samples"].append({"ts": now, "stage": stage, "value": trial["value"], "baseline": True})
            t["data"]["checkpoint"]["probe_fail_streak"] = 0
            if not update:
                t["state"] = "running"
                self.event(t, "probe", f"探针已登记（{state}）：{command}")
            else:
                self.event(t, "stage", f"换阶段到 S{stage}：{command}（{stage_note}）")
                many = len(t["data"]["stage_changes"]) > 2
                await self.report(t, "stage", f"换阶段到 S{stage}：`{command}`。理由：{stage_note}"
                                  + ("（1 小时内已多次更换探针）" if many else ""), attention=many)
            await self.save(t)
        return {"accepted": True, "stage": stage, "verdict": state, "reason": reason, "baseline": trial["value"]}

    async def finish_task(self, agent_id: str, evidence: str) -> dict:
        t = self.current_task(agent_id)
        if not t or t["state"] not in TICKING:
            raise Denied("you have no running task to finish")
        if not evidence.strip():
            raise ValueError("evidence is required")
        final = None
        if t["data"].get("probe"):
            final = (await self.run_probe(agent_id, t["data"]["probe"]["command"]))["value"]
        async with self.lock(t["id"]):
            t = self.task(t["id"])
            now = time.time()
            t["state"] = "review"
            t["data"]["checkpoint"]["paused"] = True
            t["data"]["review_since"] = now
            if final is not None:
                t["data"]["samples"].append({"ts": now, "stage": t["data"]["probe"]["stage"], "value": final, "final": True})
            t["data"]["submissions"].append({"at": now, "evidence": evidence, "verdict": None, "note": None})
            self.event(t, "finished", "提交审核：" + clip(evidence, 500))
            await self.save(t)
        await self.to_temple("task_finished", task_id=t["id"], text=(
            f"[待审核 {t['id']}] {self.store.old_one(agent_id)['name']} 提交了完成。\n任务：{clip(t['description'], 1500)}\n"
            f"验收标准：{t['acceptance'] or '（未设）'}\n证据：\n{evidence}\n"
            f"最终 probe：{final if final is not None else '—'}\n最近总结：{t['data'].get('last_summary') or '—'}\n"
            "通过：close_task；不通过：continue_task（附新指令）。"))
        return {"submitted": True, "state": "review", "note": "已提交阿撒托斯审核，等待结果；被驳回会收到新指令。"}

    async def report_up(self, agent_id: str, text: str, blocker: bool, reply_to: str | None = None) -> dict:
        # Answering azathoth's (or the owner's) message is exempt once per message, like peer replies.
        orig = reply_to and self.store.message(reply_to)
        exempt = bool(orig and orig["receiver"] == agent_id and orig["sender_role"] in (Role.AZATHOTH.value, OWNER)
                      and not orig["replied"])
        last = (self.store.setting("report_at") or {}).get(agent_id, 0)
        if not exempt and last + roles.REPORT_INTERVAL > time.time():
            return {"error": "rate_limited", "next_allowed_at": stamp(last + roles.REPORT_INTERVAL),
                    "note": "向阿撒托斯汇报每 10 分钟一次；把要说的合并成一次写完整。回复阿撒托斯发来的消息时填 reply_to，每条免限一次。"}
        if not text.strip():
            raise ValueError("empty report")
        t = self.current_task(agent_id)
        if exempt:
            self.store.x("UPDATE messages SET replied=1 WHERE id=?", (reply_to,))
        else:
            self.store.set_setting("report_at", (self.store.setting("report_at") or {}) | {agent_id: time.time()})
        name = self.store.old_one(agent_id)["name"]
        if t:
            async with self.lock(t["id"]):
                t = self.task(t["id"])
                if blocker:
                    t["data"]["blocker"] = {"text": text, "at": time.time()}
                self.event(t, "blocker" if blocker else "report", text, agent_id=agent_id)
                await self.save(t)
        label = "阻塞" if blocker else "回复" if exempt else "汇报"
        await self.to_temple("report", task_id=t and t["id"], kind="blocker" if blocker else "report",
                             text=f"[{label} · {name}（{agent_id}）" + (f" · 任务 {t['id']}" if t else "")
                                  + (f" · 回复 {reply_to}" if exempt else "") + f"]\n{text}",
                             attention=True, direct=True)
        return {"sent": True}

    async def peer_send(self, agent_id: str, to: str, text: str, priority: int = 0, reply_to: str | None = None) -> dict:
        roles.require_priority(Role.OLD_ONE, int(priority))
        if to == agent_id:
            raise ValueError("cannot message yourself")
        if not text.strip():
            raise ValueError("empty message")
        local = self.store.old_one(to)
        exempt = False
        if reply_to:
            orig = self.store.message(reply_to)
            exempt = bool(orig and orig["receiver"] == agent_id and orig["sender"] == to and not orig["replied"])
        if not exempt:
            last = self.store.last_sent(agent_id, to)
            if last and time.time() - last < roles.PEER_INTERVAL:
                return {"error": "rate_limited", "next_allowed_at": stamp(last + roles.PEER_INTERVAL),
                        "note": "给同一个旧日支配者每 10 分钟只能发一条；回复别人发来的消息（带 reply_to）不受限，每条来信可免限回复一次。"}
        me = self.store.old_one(agent_id)
        # Tagged with the receiver's task (if any), so it can be steered into the receiver's running turn.
        their = self.current_task(to) if local else None
        m = {"id": new_id("msg"), "sender": agent_id, "sender_name": me["name"], "sender_role": Role.OLD_ONE.value,
             "receiver": to, "priority": int(priority), "text": text, "reply_to": reply_to if exempt else None,
             "task_id": their and their["id"], "created_at": time.time()}
        if exempt:
            self.store.x("UPDATE messages SET replied=1 WHERE id=?", (reply_to,))
        if local:
            await self.deliver(self.store.add_message(m))
        else:
            # Another galaxy's old_one: the temple checks it exists and relays to its hub.
            try:
                await self.link.call("relay", timeout=30, to=to, envelope=m)
            except Exception as e:
                raise Denied(f"cannot reach {to}: {e}. Only old_ones can be messaged; to reach azathoth use report") from e
            self.store.add_message(m | {"kind": "relayed"})
        return {"sent": m["id"]}

    # message delivery to old_ones

    async def deliver_system(self, agent_id: str, text: str, t: dict | None, *, priority: int, kind: str,
                             footer: bool | str = True) -> None:
        if footer and t:
            text += self.footer(t, strong=footer == "continue")
        msg = self.store.add_message({"sender": SYSTEM, "sender_name": "星系枢纽", "sender_role": SYSTEM,
                                      "receiver": agent_id, "kind": kind, "priority": priority, "text": text,
                                      "task_id": t and t["id"]})
        await self.deliver(msg)

    def footer(self, t: dict, strong: bool = False) -> str:
        p = t["data"].get("probe")
        if not p:
            return "\n\n[probe] 你还没有登记探针：立刻调用 register_task_probe。"
        vals = self.stage_values(t)[-3:]
        hint = ("新指令可能改变了工作内容，请确认 probe 是否仍然适用；不适用就用 register_task_probe 更新（填 stage_note）。"
                if strong else "如果你的工作已进入新阶段、这个 probe 不再反映进展，请调用 register_task_probe 更新（填 stage_note）。")
        return (f"\n\n[probe] 当前 stage {p['stage']}：`{p['command']}`，最近值 "
                f"{' → '.join(f'{v:g}' for v in vals) or '—'}（CP{t['data']['checkpoint']['n']}）\n{hint}")

    async def deliver_from_temple(self, envelope: dict) -> dict:
        """Azathoth's, the owner's, or another galaxy's old_one's message for one of ours."""
        to = envelope["receiver"]
        a = self.store.old_one(to)
        if not a or a["desired"] != "running":
            raise Denied(f"{to} is not a running old_one here")
        text = envelope["text"]
        t = self.current_task(to)
        if envelope.get("sender_role") in (Role.AZATHOTH.value, OWNER) and t:
            text += self.footer(t)
            if t["data"].get("blocker") and envelope.get("sender_role") == Role.AZATHOTH.value:
                async with self.lock(t["id"]):
                    t = self.task(t["id"])
                    t["data"]["blocker"] = None
                    self.event(t, "unblocked", "阿撒托斯已回复，阻塞解除")
                    await self.save(t)
        fields = {k: envelope[k] for k in ("id", "sender", "sender_name", "sender_role", "receiver", "priority", "reply_to",
                                           "created_at") if envelope.get(k) is not None}
        # A peer's task id names the sender's task; the receiver's own task tags what it receives.
        own = envelope.get("task_id") if envelope.get("sender_role") != Role.OLD_ONE.value else None
        msg = self.store.add_message(fields | {"kind": "message", "text": text, "task_id": own or (t and t["id"])})
        await self.deliver(msg)
        return {"id": msg["id"]}

    def envelope(self, m: dict) -> dict:
        t = m.get("task_id") and self.store.task(m["task_id"])
        return m | {"task_title": t and clip(t["description"], 40)}

    async def deliver(self, msg: dict) -> None:
        if msg["state"] == "cancelled" or (self.draining and msg["sender"] != OWNER):
            return
        if await self.send_agent(msg["receiver"], {"type": "message", "message": self.envelope(msg)}):
            if self.store.message(msg["id"])["state"] == "queued":
                self.store.mark(msg["id"], "delivered")

    async def send_agent(self, agent_id: str, frame: dict) -> bool:
        ws = self.conns.get(agent_id)
        if ws is None:
            return False
        try:
            await asyncio.wait_for(ws.send(dumps(frame)), 10)
            return True
        except Exception:
            return False

    async def agent_rpc(self, agent_id: str, method: str, timeout: float = 15, **params):
        ws = self.conns.get(agent_id)
        if ws is None:
            raise ConnectionError(f"{agent_id} is not connected")
        self.rpc_id += 1
        rid = self.rpc_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[(agent_id, rid)] = fut
        try:
            await ws.send(dumps({"type": "rpc", "id": rid, "method": method, "params": params}))
            frame = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop((agent_id, rid), None)
        if not frame.get("ok"):
            raise RuntimeError(frame.get("error", "rpc failed"))
        return frame.get("result")

    # agent websocket

    async def ws_handler(self, ws) -> None:
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
            a = self.store.old_one(hello.get("id", ""))
            if (hello.get("kind") != "agent" or not a or a["desired"] != "running" or
                    not hmac.compare_digest(agent_token(self.secret, a["id"], a["epoch"]), hello.get("token", ""))):
                raise Denied("bad or revoked agent credential")
        except Exception as e:
            try:
                await ws.send(dumps({"type": "rejected", "reason": str(e)}))
            except Exception:
                pass
            return
        aid = a["id"]
        old = self.conns.get(aid)
        self.conns[aid] = ws
        if old:
            await old.close(4002, "replaced")
        try:
            runtime = await self.link.call("old_one_runtime", timeout=10, agent_id=aid)
        except Exception:
            runtime = (self.store.setting("runtimes") or {}).get(aid)
        if runtime:
            self.store.set_setting("runtimes", (self.store.setting("runtimes") or {}) | {aid: runtime})
        pending = self.store.pending_for(aid) if not self.draining else [m for m in self.store.pending_for(aid) if m["sender"] == OWNER]
        await ws.send(dumps({"type": "welcome", "agent": {"id": aid, "name": a["name"], "role": Role.OLD_ONE.value, "spec": a["spec"]},
                             "memory": [], "thread_id": a["thread_id"], "runtime": runtime, "draining": self.draining,
                             "pending": [self.envelope(m) for m in pending]}))
        for m in pending:
            if m["state"] == "queued":
                self.store.mark(m["id"], "delivered")
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in ws:
                frame = json.loads(raw)
                task = asyncio.create_task(self.on_frame(aid, ws, frame))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except websockets.WebSocketException:
            pass
        finally:
            for task in tasks:
                task.cancel()
            if self.conns.get(aid) is ws:
                del self.conns[aid]
                self.status.pop(aid, None)
            for sid in [s for s, owner in self.streams.items() if owner == aid]:
                self.streams.pop(sid, None)
                try:
                    await self.link.call("api_cancel", id=sid, timeout=5)
                except Exception:
                    pass
            for (who, rid), fut in list(self.pending.items()):
                if who == aid and not fut.done():
                    fut.set_exception(ConnectionError("disconnected"))
            asyncio.create_task(self.push_roster())

    async def on_frame(self, aid: str, ws, frame: dict) -> None:
        t = frame.get("type")
        if t == "rpc_result":
            fut = self.pending.get((aid, frame["id"]))
            if fut and not fut.done():
                fut.set_result(frame)
        elif t == "call":
            try:
                if self.conns.get(aid) is not ws:
                    raise Denied("stale connection")
                result = await self.agent_call(aid, frame["method"], frame.get("params") or {})
                ok = {"ok": True, "result": result}
            except Exception as e:
                if not isinstance(e, (Denied, ValueError, KeyError)):
                    log.exception("hub call %s failed", frame.get("method"))
                ok = {"ok": False, "error": str(e)}
            if frame.get("id"):
                try:
                    await ws.send(dumps({"type": "result", "id": frame["id"], **ok}))
                except Exception:
                    pass

    async def agent_call(self, aid: str, method: str, p: dict):
        if method.startswith("api_"):
            return await self.api_call(aid, method, p)
        if method == "status":
            prev = self.status.get(aid, {}).get("state")
            self.status[aid] = p
            if self.link.connected.is_set():
                asyncio.create_task(self.link.emit({"type": "call", "id": 0, "method": "agent_status",
                                                    "params": {"agent_id": aid, "status": p}}))
            if prev != p.get("state"):
                asyncio.create_task(self.push_roster())
            return True
        if method == "handled":
            m = self.store.message(p["id"])
            if m and m["receiver"] == aid and m["state"] != "cancelled":
                self.store.mark(p["id"], "handled")
            return True
        if method == "set_thread":
            self.store.set_old_one(aid, thread_id=p["thread_id"])
            return True
        if method == "log_turn":
            return self.store.log_turn(aid, p["backend"], p.get("model"), p["thread"], p["input"], p["reply"], p["status"], p.get("task_id"))
        if method == "turn_log":
            return self.store.turn_log(aid, limit=min(int(p.get("limit", 20)), 100))
        if method == "memory":
            return []
        if method == "task_started":
            t = self.current_task(aid)
            if t and not t["data"].get("started_at"):
                async with self.lock(t["id"]):
                    t = self.task(t["id"])
                    t["data"]["started_at"] = time.time()
                    t["data"]["checkpoint"]["next_at"] = time.time() + self.check_every
                    self.event(t, "started", "执行者开始工作，检查点计时开始")
                    await self.save(t)
            return True
        if method == "task_input_guard":
            task = p.get("task_id") and self.store.task(p["task_id"])
            return {"allowed": not task or task["state"] not in ("cancelled", "closed")}
        if method == "header":
            return self.header(aid)
        if method == "send":
            return await self.peer_send(aid, p["to"], p["text"], int(p.get("priority") or 0), p.get("reply_to"))
        if method == "report":
            return await self.report_up(aid, p["text"], bool(p.get("blocker")), p.get("reply_to"))
        if method == "register_task_probe":
            return await self.register_task_probe(aid, p["command"], p.get("rationale", ""), p.get("stage_note", ""))
        if method == "finish_task":
            return await self.finish_task(aid, p.get("evidence", ""))
        if method == "list_tasks":
            return self.bindings_table(full=True)
        if method == "task_view":
            t = self.store.task(p["task_id"])
            if t:
                return self.view(p["task_id"])
            return await self.link.call("task_view", timeout=30, task_id=p["task_id"])
        raise Denied(f"unknown or forbidden call {method}")

    async def api_call(self, aid: str, method: str, p: dict):
        """Galaxy API for old_ones: forwarded under the galaxy's identity, streams scoped to their opener."""
        if method == "api_catalog":
            return await self.link.call("api_catalog", timeout=15)
        if method == "api_open":
            r = await self.link.call("api_open", timeout=60, **p)
            self.streams[r["id"]] = aid
            return r
        if method in ("api_next", "api_cancel"):
            if self.streams.get(p.get("id")) != aid:
                raise Denied("unknown caller stream")
            try:
                r = await self.link.call(method, timeout=float(p.get("timeout") or 120) + 10, id=p["id"])
            finally:
                if method == "api_cancel":
                    self.streams.pop(p["id"], None)
            if method == "api_next" and isinstance(r, dict) and r.get("done"):
                self.streams.pop(p["id"], None)
            return r
        raise Denied("unknown API operation")

    # context for old_ones (design §4.2.2)

    def bindings_table(self, full=False) -> list[dict]:
        rows = self.bindings if self.bindings else [
            {"task_id": t["id"], "galaxy_id": self.g.id, "executor": t["executor"], "state": t["state"],
             "description": clip(t["description"], 80)} for t in self.store.tasks(IN_FLIGHT)]
        return rows if full else [r for r in rows if r.get("state") in IN_FLIGHT]

    def header(self, aid: str) -> str:
        t = self.current_task(aid)
        lines = ["<context>"]
        if t:
            d = t["data"]
            p = d.get("probe")
            lines.append(f"你的当前任务 {t['id']}（{t['state']}，CP{d['checkpoint']['n']}）：{clip(t['description'], 600)}")
            if t["acceptance"]:
                lines.append("验收标准：" + clip(t["acceptance"], 300))
            if p:
                vals = self.stage_values(t)[-5:]
                lines.append(f"探针 S{p['stage']}：`{p['command']}`，本阶段最近值：{' → '.join(f'{v:g}' for v in vals) or '—'}")
            else:
                lines.append("探针：未登记 —— 立刻调用 register_task_probe。")
            if d.get("last_nudge"):
                lines.append("上次催办：" + clip(d["last_nudge"], 200))
        else:
            lines.append("你当前没有任务。")
        others = [b for b in self.bindings_table() if b.get("executor") != aid]
        if others:
            lines.append("其他在途任务（找人问问题时用；同一个对象每 10 分钟只能发一条）：")
            lines += [f"- {b.get('executor_name') or b.get('executor')}（{b.get('executor')}，星系 {b.get('galaxy_id')}）"
                      f"：{clip(b.get('description'), 80)}" for b in others[:30]]
        rl = self.store.q("SELECT receiver, MAX(created_at) t FROM messages WHERE sender=? AND kind IN ('message','relayed')"
                          " AND reply_to IS NULL GROUP BY receiver", (aid,))
        waits = [f"{r['receiver']} 于 {stamp(r['t'] + roles.PEER_INTERVAL)} 后" for r in rl if time.time() - r["t"] < roles.PEER_INTERVAL]
        last_report = (self.store.setting("report_at") or {}).get(aid, 0)
        if time.time() - last_report < roles.REPORT_INTERVAL:
            waits.append(f"report 于 {stamp(last_report + roles.REPORT_INTERVAL)} 后")
        if waits:
            lines.append("限流中：" + "；".join(waits))
        lines.append("</context>\n")
        return "\n".join(lines)

    # probes

    def agent_cwd(self, agent_id: str, t: dict | None = None) -> str | None:
        if t and t["data"].get("cwd"):
            return t["data"]["cwd"]
        a = self.store.old_one(agent_id)
        return a and a["spec"].get("cwd")

    async def run_probe(self, agent_id: str, command: str) -> dict:
        from .galaxy import run_probe
        r = await run_probe(command, self.agent_cwd(agent_id, self.current_task(agent_id)), PROBE_TIMEOUT)
        if r.get("code") != 0:
            return {"value": None, "error": f"退出码 {r.get('code')}：{clip(r.get('stderr') or r.get('stdout'), 160)}"}
        v = parse_number(r.get("stdout", ""))
        if v is None:
            return {"value": None, "error": f"输出 {clip((r.get('stdout') or '').strip(), 80)!r} 不是单个数字"}
        return {"value": v, "error": ""}

    def stage_values(self, t: dict) -> list[float]:
        stage = (t["data"].get("probe") or {}).get("stage")
        return [s["value"] for s in t["data"]["samples"] if s.get("stage") == stage and s.get("value") is not None]

    def turn_digest(self, agent_id: str, t: dict, live: dict | None = None, until: float | None = None) -> str:
        """Current-task evidence only, including reports and work in an unfinished/recovered turn."""
        since = max(t["created_at"], t["data"].get("bound_at", 0), t["data"].get("evidence_at", 0))
        until = time.time() if until is None else until
        rows = self.store.q("SELECT * FROM turn_log WHERE agent_id=? AND task_id=? AND created_at>=? AND created_at<=?"
                            " ORDER BY id DESC LIMIT 8", (agent_id, t["id"], since, until))[::-1]
        reports = [e for e in t["data"].get("events", []) if since <= e["at"] <= until
                   and e["kind"] in ("report", "blocker", "submission")]
        parts = [f"[本期任务汇报 {stamp(e['at'])}] {clip(e['text'], 2500)}" for e in reports[-8:]]
        active = live or {}
        if (active.get("activity_task") == t["id"] and active.get("current_task") == t["id"]
                and since <= (active.get("last_activity_at") or 0) <= until):
            parts.append("[本期正在进行的活动，非完成结论] " + "\n".join(active.get("live") or active.get("activity") or [])[-5000:])
        parts.extend(f"[本期完成回合 {stamp(r['created_at'])}] {clip(r['reply'], 1200)}"
                     for r in rows if r["status"] == "completed")
        failed = sum(r["status"] != "completed" for r in rows)
        if failed:
            parts.append(f"本期曾有 {failed} 个失败/中断回合；不能据此说没有工作，优先核对新探针、任务汇报和实时活动。")
        return "\n\n".join(parts) or "本期没有可归属本任务的文字证据；以本次探针为准，不沿用旧回合结论。"

    # checkpoints (design §2.5)

    async def clock(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_every)
            try:
                await self.sweep()
            except Exception:
                log.exception("hub sweep failed")

    async def sweep(self) -> None:
        now = time.time()
        for t in self.store.tasks(IN_FLIGHT):
            d, cp = t["data"], t["data"]["checkpoint"]
            if t["state"] in TICKING and not cp.get("paused"):
                if not d.get("started_at"):
                    if now - d.get("bound_at", now) > self.start_grace and not d.get("start_alarm"):
                        await self.start_alarm(t)
                elif cp.get("next_at") and now >= cp["next_at"]:
                    async with self.lock(t["id"]):
                        await self.checkpoint(self.task(t["id"]))
            elif t["state"] == "review":
                since = d.get("review_since", now)
                level = d.get("review_reminded", 0)
                if level < 1 and now - since > REVIEW_REMIND:
                    await self.mark(t, review_reminded=1)
                    await self.report(t, "review_overdue", f"任务 {t['id']} 已提交审核 30 分钟，仍待你 close_task 或 continue_task。",
                                      attention=True)
                elif level < 2 and now - since > REVIEW_OWNER:
                    await self.mark(t, review_reminded=2)
                    await self.to_temple("tell_owner", task_id=t["id"],
                                         text=f"任务 {t['id']} 提交审核已超过 1 小时，阿撒托斯尚未处理：{clip(t['description'], 200)}")

    async def mark(self, t: dict, **fields) -> None:
        async with self.lock(t["id"]):
            t = self.task(t["id"])
            t["data"].update(fields)
            self.store.save_task(t)

    async def start_alarm(self, t: dict) -> None:
        """Bound but no turn started within the grace period: restart the process once, then tell azathoth."""
        await self.mark(t, start_alarm=time.time())
        a = self.store.old_one(t["executor"])
        if a:
            self.store.set_old_one(a["id"], epoch=a["epoch"] + 1)
            self.g.stop_hub_agent(a["id"])
            self.g.launch_hub_agent(self.launch_spec(self.store.old_one(a["id"])))
        await self.report(t, "not_started", f"任务 {t['id']} 绑定 5 分钟后执行者仍未开始工作，已重启其进程。", attention=True)

    async def checkpoint(self, t: dict) -> None:
        cp = t["data"]["checkpoint"]
        cp["n"] += 1
        cp["next_at"] = time.time() + self.check_every
        if t["state"] == "awaiting_probe":
            await self.checkpoint_no_probe(t)
        else:
            await self.checkpoint_running(t)

    async def checkpoint_no_probe(self, t: dict) -> None:
        d, cp = t["data"], t["data"]["checkpoint"]
        cp["no_probe_hits"] += 1
        if cp["no_probe_hits"] == 1:
            reason = d["events"][-1]["text"] if d.get("events") and d["events"][-1]["kind"] == "probe_rejected" else ""
            self.event(t, "probe_demand", "第 1 个检查点仍无探针：P10 强令登记")
            await self.save(t)
            await self.deliver_system(t["executor"], f"[检查点 CP{cp['n']} · 加急] 你还没有登记进度探针。立刻停下手上的事，"
                                      "调用 register_task_probe 登记一条输出单个数字、随真实进展增长的命令。下个检查点仍没有，你会被替换。"
                                      + (f"\n上次被驳回的原因：{reason}" if reason else ""), t, priority=URGENT, kind="checkpoint",
                                      footer=False)
            await self.report(t, "no_probe", f"任务 {t['id']} 第 1 个检查点仍未登记探针，已加急强令。", attention=True)
            return
        if d.get("respawns", 0) >= RESPAWN_LIMIT:
            t["state"] = "suspended"
            cp["paused"] = True
            self.event(t, "suspended", "重建后仍未登记探针，任务挂起，等阿撒托斯处理")
            await self.save(t)
            await self.report(t, "suspended", f"任务 {t['id']} 的执行者被替换后仍 20 分钟未登记探针，已挂起。"
                              "请决定：send 给它新指令后 continue_task、cancel_task，或重新 new_task。", attention=True)
            return
        await self.respawn(t, "连续两个检查点未登记探针")

    async def respawn(self, t: dict, reason: str) -> None:
        old = self.store.old_one(t["executor"])
        rows = self.store.turn_log(old["id"], limit=12)
        raw = "\n\n".join(f"收到：{clip(r['input'], 600)}\n答：{clip(r['reply'], 1200)}" for r in rows)
        try:
            h = await self.ai.summarize_for_handover({"task": t["description"], "turns": raw[-20000:]})
            summary = (f"已完成：{h['done']}\n产物：{h['artifacts']}\n卡在：{h['stuck_on']}\n建议下一步：{h['next_steps']}")
        except Exception:
            summary = "（交接摘要不可用，以下是前任最近的回合原文）\n" + clip(raw, 6000)
        self.store.set_old_one(old["id"], desired="stopped", epoch=old["epoch"] + 1)
        self.g.stop_hub_agent(old["id"])
        new = self.new_old_one(t["description"], t["data"].get("cwd"))
        now = time.time()
        d = t["data"]
        d["executors"][-1]["to"] = now
        d["executors"].append({"agent_id": new["id"], "from": now, "to": None, "reason": reason})
        d["respawns"] = d.get("respawns", 0) + 1
        d["started_at"] = None
        d["bound_at"] = now
        d.pop("start_alarm", None)
        d["checkpoint"].update(no_probe_hits=0, next_at=None)
        t["executor"] = new["id"]
        self.event(t, "respawn", f"{reason}：{old['name']} 被替换为 {new['name']}")
        await self.save(t)
        handover = (f"<handover>\n你接替前任 {old['name']}（{old['id']}）。前任因为{reason}被替换。\n{summary}\n"
                    f"前任的完整回合记录可用 task_view 查看。已完成的操作不要重复执行。\n</handover>")
        await self.brief(t, new, handover=handover)
        self.g.launch_hub_agent(self.launch_spec(new))
        await self.push_roster()
        await self.report(t, "respawn", f"任务 {t['id']}：{reason}，已杀掉 {old['name']} 并拉起 {new['name']} 接手。", attention=True)

    async def checkpoint_running(self, t: dict) -> None:
        d, cp = t["data"], t["data"]["checkpoint"]
        evidence_at = time.time()
        live = {}
        if t["executor"] in self.conns:
            try:
                live = await self.agent_rpc(t["executor"], "latest_result", timeout=5)
            except Exception:
                pass  # Probe and persisted task reports still provide evidence.
        probe = d["probe"]
        values = self.stage_values(t)
        sample = await self.run_probe(t["executor"], probe["command"])
        d["samples"].append({"ts": time.time(), "stage": probe["stage"], "value": sample["value"], "error": sample["error"] or None})
        del d["samples"][:-500]
        notes = []
        if sample["value"] is None:
            cp["probe_fail_streak"] = cp.get("probe_fail_streak", 0) + 1
            notes.append("probe 失败：" + sample["error"])
            grew = False
        else:
            cp["probe_fail_streak"] = 0
            grew = bool(values) and sample["value"] > values[-1]
        if probe.get("verdict") == "unreviewed":
            try:
                v = await self.ai.judge_probe({"task": t["description"], "acceptance": t["acceptance"], "command": probe["command"],
                                               "rationale": probe["rationale"], "trial_value": probe["baseline"]}, False)
                probe["verdict"], probe["reason"] = ("approved" if v["ok"] else "rejected"), v["reason"]
            except Exception:
                pass
        payload = {"task": t["description"], "acceptance": t["acceptance"], "probe": probe["command"],
                   "probe_meaning": probe["rationale"], "samples": (values + ([sample["value"]] if sample["value"] is not None else []))[-10:],
                   "grew_since_last": grew, "previous_summary_unverified": d.get("last_summary", ""),
                   "recent_work": self.turn_digest(t["executor"], t, live, evidence_at)}
        try:
            j = await self.ai.judge_progress(payload)
        except Exception as e:
            j = {"pace": "ok" if grew else "none", "note_to_azathoth": "",
                 "summary": f"（AI 总结不可用）probe {('→'.join(f'{v:g}' for v in payload['samples'][-3:])) or '—'}；"
                            "无可用的本期文字总结",
                 "probe_stale": False}
            notes.append(f"AI 不可用：{clip(str(e), 60)}")
        if not grew:
            j["pace"] = "none"
        d["last_pace"], d["last_summary"] = j["pace"], j["summary"]
        d["evidence_at"] = evidence_at
        nudged = False
        if probe.get("verdict") == "rejected":
            self.event(t, "probe_rejected", "复审驳回探针：" + probe["reason"])
            t["state"] = "awaiting_probe"
            d["probe_history"].append(probe | {"to": time.time(), "rejected": True})
            d["probe"] = None
            cp["no_probe_hits"] = 0
            await self.deliver_system(t["executor"], f"[检查点 CP{cp['n']} · 加急] 你的探针复审未通过：{probe['reason']}\n"
                                      "立刻用 register_task_probe 重新登记。", t, priority=URGENT, kind="checkpoint", footer=False)
            notes.append("探针复审被驳回")
            nudged = True
        elif cp["probe_fail_streak"] >= 2:
            await self.deliver_system(t["executor"], f"[检查点 CP{cp['n']} · 加急] 你的探针连续两次执行失败（{sample['error']}）。"
                                      "立刻修复，或用 register_task_probe 重新登记。", t, priority=URGENT, kind="checkpoint", footer=False)
            nudged = True
        if j["pace"] in ("none", "slow"):
            cp["stall_streak"] = cp.get("stall_streak", 0) + 1
            if not nudged and not d.get("blocker"):
                text = (f"[检查点 CP{cp['n']} · 催办] 进度{'没有增长' if j['pace'] == 'none' else '过慢'}"
                        f"（连续 {cp['stall_streak']} 次）。程序对这一期的总结：{j['summary']}\n"
                        "要么加快推进，要么用 report(blocker=true) 说明卡在哪里。")
                if j["probe_stale"]:
                    text += "\n看起来你已进入新阶段：请用 register_task_probe 更新 probe（填 stage_note）。"
                d["last_nudge"] = text.split("\n")[0]
                await self.deliver_system(t["executor"], text, t, priority=NUDGE, kind="checkpoint")
        else:
            cp["stall_streak"] = 0
        self.event(t, "checkpoint", f"CP{cp['n']} · {j['pace']} · {j['summary']}", value=sample["value"])
        await self.save(t)
        attention = j["pace"] != "ok" or bool(notes) or j["probe_stale"]
        tags = []
        if j["pace"] != "ok":
            tags.append(f"{'无增长' if j['pace'] == 'none' else '过慢'}（连续 {cp['stall_streak']} 次）")
        if j["probe_stale"]:
            tags.append("探针可能已过时")
        if d.get("blocker"):
            tags.append("仍在等你处理阻塞，未催办")
        vals = [v for v in payload["samples"]][-3:]
        line = (f"{t['id']} {self.store.old_one(t['executor'])['name']} · S{probe['stage']} · "
                f"probe {'→'.join(f'{v:g}' for v in vals) or '—'} · {'、'.join(tags + notes) or 'ok'} · {j['summary']}"
                + (f" ｜ 提醒：{j['note_to_azathoth']}" if j.get("note_to_azathoth") else ""))
        await self.to_temple("report", task_id=t["id"], kind="checkpoint", text=line, attention=attention,
                             progress={"summary": j["summary"], "pace": j["pace"], "at": d["samples"][-1]["ts"],
                                       "stage": probe["stage"], "value": sample["value"], "notes": tags})

    # owner / azathoth controls of old_ones, forwarded by the temple

    async def control(self, action: str, agent_id: str) -> dict:
        a = self.store.old_one(agent_id)
        if not a:
            raise ValueError(f"unknown old_one {agent_id}")
        if action == "interrupt":
            return {"ok": await self.agent_rpc(agent_id, "interrupt") if agent_id in self.conns else False}
        if action == "stop":
            self.store.set_old_one(agent_id, desired="stopped", epoch=a["epoch"] + 1)
            if agent_id in self.conns:
                try:
                    await self.agent_rpc(agent_id, "halt", timeout=3)
                except Exception:
                    pass
            self.g.stop_hub_agent(agent_id)
            for m in self.store.pending_for(agent_id):
                self.store.mark(m["id"], "cancelled")
            t = self.current_task(agent_id)
            if t and t["state"] in TICKING + ("review",):
                async with self.lock(t["id"]):
                    t = self.task(t["id"])
                    t["state"] = "suspended"
                    t["data"]["checkpoint"]["paused"] = True
                    self.event(t, "suspended", "执行者被停止，任务挂起")
                    await self.save(t)
                await self.report(t, "suspended", f"任务 {t['id']} 的执行者 {a['name']} 被停止，任务已挂起。", attention=True)
        elif action == "resume":
            if a["desired"] == "deleted":
                raise ValueError("deleted")
            self.store.set_old_one(agent_id, desired="running", epoch=a["epoch"] + 1)
            self.g.stop_hub_agent(agent_id)
            self.g.launch_hub_agent(self.launch_spec(self.store.old_one(agent_id)))
        elif action == "delete":
            if a["desired"] != "stopped":
                raise ValueError("only a stopped old_one can be deleted")
            self.store.set_old_one(agent_id, desired="deleted")
        else:
            raise ValueError(f"unknown action {action}")
        await self.push_roster()
        return {"id": agent_id, "desired": self.store.old_one(agent_id)["desired"]}

    async def drain(self, enabled: bool | None) -> list[dict]:
        if enabled is not None:
            self.draining = enabled
        out = []
        for aid in list(self.conns):
            try:
                state = await self.agent_rpc(aid, "drain" if enabled is not None else "drain_status", timeout=10,
                                             **({"enabled": enabled} if enabled is not None else {}))
                out.append({"id": aid, **state})
            except Exception:
                out.append({"id": aid, "safe": False, "reason": "drain status unavailable"})
            if enabled is False:
                for m in self.store.pending_for(aid):
                    await self.deliver(m)
        return out

    async def latest(self, agent_id: str) -> dict:
        a = self.store.old_one(agent_id)
        last = self.store.turn_log(agent_id, limit=1)
        logged = {"last_reply": clip(last[0]["reply"], 3000), "last_status": last[0]["status"],
                  "finished_at": last[0]["created_at"]} if last else {}
        if agent_id not in self.conns:
            return {"agent_id": agent_id, "name": a and a["name"], "online": False} | logged
        try:
            r = await self.agent_rpc(agent_id, "latest_result")
        except Exception as e:
            r = {"error": str(e)}
        # The logged turn survives restarts; a live result newer than it wins.
        if logged and logged["finished_at"] > (r.get("finished_at") or 0):
            r = r | logged
        return {"agent_id": agent_id, "name": a["name"], "online": True} | r

    async def rpc(self, method: str, p: dict):
        """Calls from the temple."""
        if method == "hub_assign":
            return await self.assign_task(p["task_id"], p["description"], p.get("acceptance", ""), p.get("cwd"))
        if method == "hub_close":
            return await self.close_task(p["task_id"], p.get("note", ""))
        if method == "hub_continue":
            return await self.continue_task(p["task_id"], p["instruction"])
        if method == "hub_cancel":
            return await self.cancel_task(p["task_id"], p.get("reason", ""))
        if method == "hub_task":
            return self.view(p["task_id"])
        if method == "hub_deliver":
            return await self.deliver_from_temple(p["envelope"])
        if method == "hub_control":
            return await self.control(p["action"], p["agent_id"])
        if method == "hub_drain":
            return await self.drain(p.get("enabled"))
        if method == "hub_latest":
            return await self.latest(p["agent_id"])
        if method == "hub_messages":
            return self.store.messages(p.get("agent_id"), p.get("task_id"), p.get("search"), min(int(p.get("limit", 100)), 500))
        if method == "hub_turns":
            return self.store.turn_log(p["agent_id"], limit=min(int(p.get("limit", 20)), 100))
        raise ValueError(f"unknown hub rpc {method}")

    async def frame(self, frame: dict) -> None:
        """One-way frames from the temple."""
        if frame.get("type") == "bindings":
            self.bindings = frame.get("rows", [])
        elif frame.get("type") == "runtime" and frame.get("agent_id"):
            self.store.set_setting("runtimes", (self.store.setting("runtimes") or {}) | {frame["agent_id"]: frame.get("config")})
            await self.send_agent(frame["agent_id"], {"type": "runtime", "config": frame.get("config")})
