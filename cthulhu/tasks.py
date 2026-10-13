"""Task discipline: the contract every task carries and the clock that enforces it.

A task binds an executor to a deadline and acceptance criteria. Its only progress
evidence is a probe: a shell command the executor's galaxy runs at every checkpoint,
whose stdout is one number; progress means the number grew. The temple runs the
checkpoints on its own clock and walks a fixed escalation ladder with program-written
messages. Models decide nothing in the ladder: Yog is only asked whether a probe
measures the acceptance criteria, for a diagnosis, and for acceptance verdicts.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, fields

from .roles import OWNER, Denied, Role

log = logging.getLogger(__name__)

STATES = ("active", "blocked_on_owner", "submitted", "accepted", "failed")
OPEN = ("active", "blocked_on_owner", "submitted")
DEFAULT_MINUTES = 120
PRIORITY = 9                  # program escalations: high, but never the turn-interrupting 10
SYSTEM = "temple"
VOIDER = "军纪：任务作废"


def clip(text: str | None, n: int = 300) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def stamp(t: float | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"


def parse_deadline(value, base: float) -> float | None:
    """Minutes after `base`, or an absolute local 'YYYY-MM-DD HH:MM'."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return base + float(value) * 60
    except (TypeError, ValueError):
        return time.mktime(time.strptime(str(value).strip(), "%Y-%m-%d %H:%M"))


@dataclass
class Task:
    id: str
    title: str
    state: str
    creator: str | None
    manager: str | None
    executor: str | None
    deadline: float | None
    acceptance: str
    probe: str | None
    data: dict          # duration, next_check, checkpoint, misses, samples, extensions, events, submissions, ...
    created_at: float
    updated_at: float

    @classmethod
    def load(cls, store, task_id: str | None) -> Task | None:
        row = task_id and store.task(task_id)
        return cls(**{f.name: row[f.name] for f in fields(cls)}) if row else None

    def save(self, store) -> None:
        store.set_task(self.id, state=self.state, executor=self.executor, deadline=self.deadline,
                       probe=self.probe, data=self.data)

    def log(self, kind: str, text: str, by: str = SYSTEM, **extra) -> None:
        self.data.setdefault("events", []).append({"at": time.time(), "kind": kind, "by": by, "text": text, **extra})

    @property
    def samples(self) -> list[dict]:
        return self.data.setdefault("samples", [])

    @property
    def extensions(self) -> list[dict]:
        return self.data.setdefault("extensions", [])

    def last_value(self) -> float | None:
        return next((s["value"] for s in reversed(self.samples) if s["value"] is not None), None)

    def history(self, n: int = 6) -> str:
        return ", ".join(f"{stamp(s['at'])}={s['value'] if s['value'] is not None else 'n/a'}" for s in self.samples[-n:]) or "none"

    def brief(self) -> str:
        return (f"Task {self.id} \"{self.title}\"\nExecutor: {self.executor}; deadline {stamp(self.deadline)}\n"
                f"Acceptance: {self.acceptance}\nProbe: {self.probe or '(none)'}\nProbe history: {self.history()}")


class Discipline:
    """The one task state machine. Agents reach it through task_* calls; the owner through the web API."""

    def __init__(self, temple):
        self.t = temple
        cfg = temple.config
        self.interval = float(cfg.get("checkpoint_interval", 600))
        self.tick = float(cfg.get("task_tick", min(15.0, self.interval / 4)))
        self.grace = float(cfg.get("task_grace", 600))
        self.probe_timeout = float(cfg.get("probe_timeout", 30))
        self.lock = asyncio.Lock()

    @property
    def store(self):
        return self.t.store

    def get(self, task_id: str) -> Task:
        task = Task.load(self.store, task_id)
        if not task:
            raise ValueError(f"unknown task {task_id}")
        return task

    def role(self, agent_id: str | None) -> str | None:
        return self.t.role_of(agent_id) if agent_id else None

    def god(self, role: str) -> str | None:
        return (self.t.god(role) or {}).get("id")

    def superior(self, task: Task) -> str | None:
        """Who answers for the executor: its manager for an old_one, azathoth for a manager."""
        return task.manager if self.role(task.executor) == Role.OLD_ONE else self.god(Role.AZATHOTH)

    # notifications: program-written, never waiting on a model

    async def tell(self, to: str | None, text: str, task: Task, *, kind="discipline", status=None) -> None:
        if not to or to == OWNER:
            return
        try:
            await self.t.route(SYSTEM, to, text, kind=kind, priority=PRIORITY, task_id=task.id, status=status)
        except Exception as e:
            log.warning("task %s: could not notify %s: %s", task.id, to, e)

    async def tell_owner(self, text: str, task: Task, kind="blocked") -> None:
        try:
            await self.t.note_owner(text, task_id=task.id, kind=kind, origin=task.title)
        except Exception:
            log.exception("task %s: could not notify the owner", task.id)

    async def changed(self, task: Task) -> None:
        await self.t.broadcast({"type": "task", "task_id": task.id})

    # creation

    def contract(self, p: dict) -> tuple[str, str, float]:
        title, acceptance = str(p.get("title") or "").strip(), str(p.get("acceptance") or "").strip()
        if not title or not acceptance:
            raise ValueError("a task needs title, executor, acceptance and a deadline (default 120 minutes)")
        now = time.time()
        deadline = parse_deadline(p.get("deadline"), now) or now + DEFAULT_MINUTES * 60
        if deadline <= now:
            raise ValueError("deadline must be in the future")
        return title[:80], acceptance, deadline

    def new(self, creator: str, manager: str, executor: str, p: dict) -> Task:
        title, acceptance, deadline = self.contract(p)
        now = time.time()
        row = self.store.add_task(title, creator=creator, manager=manager, executor=executor, deadline=deadline,
                                  acceptance=acceptance, data={"duration": deadline - now, "next_check": now + self.interval,
                                                               "brief": str(p.get("text") or p.get("task") or ""),
                                                               "parent": p.get("parent_task_id")})
        task = Task.load(self.store, row["id"])
        task.log("created", f"由 {creator} 创建，执行者 {executor}，截止 {stamp(deadline)}", by=creator)
        task.save(self.store)
        return task

    async def create(self, caller: str, p: dict) -> dict:
        """azathoth -> shub/ghroth, or a manager -> one of its own old_ones. The brief goes out as a message."""
        role, executor = self.role(caller), p.get("executor")
        target = executor and self.store.agent(executor)
        if not target:
            raise ValueError("executor must be an existing agent id")
        if role == Role.AZATHOTH and target["role"] in (Role.SHUB, Role.GHROTH):
            manager = executor
        elif role in (Role.SHUB, Role.GHROTH) and target["role"] == Role.OLD_ONE and target["parent_id"] == caller:
            manager = caller
        else:
            raise Denied(f"{role} may not assign tasks to {target['role']} {executor}")
        if not str(p.get("text") or "").strip():
            raise ValueError("text (the task brief) is required")
        task = self.new(caller, manager, executor, p)
        await self.t.route(caller, executor, p["text"], task_id=task.id)
        await self.changed(task)
        return {"task_id": task.id, "deadline": stamp(task.deadline)}

    def bind_spawn(self, caller: str, p: dict, agent_id: str) -> Task:
        """An old_one is always bound to a task: a new one from the contract, or (replacement) an
        existing task of this manager whose executor it takes over."""
        if p.get("task_id"):
            task = self.get(p["task_id"])
            if task.manager != caller or self.role(task.executor) != Role.OLD_ONE or task.state not in OPEN:
                raise Denied("only an open old_one task of your own can be handed to a replacement")
            task.log("executor", f"执行者由 {task.executor} 换为 {agent_id}", by=caller)
            task.executor = agent_id
            task.data["brief"] = p["task"]
            task.save(self.store)
            return task
        return self.new(caller, caller, agent_id, p | {"text": p["task"]})

    # executor calls

    async def set_probe(self, caller: str, task_id: str, command: str) -> dict:
        task = self.get(task_id)
        if caller != task.executor:
            raise Denied("only the executor registers the probe")
        if task.state not in OPEN:
            raise ValueError(f"task is {task.state}")
        task.probe = str(command or "").strip()
        value, note = await self.sample(task)
        if value is None:
            raise ValueError(f"probe output must be a single number ({note}); e.g. `stat -c %s out.jsonl 2>/dev/null || echo 0`")
        async with self.lock:
            fresh = self.get(task_id)
            fresh.probe = task.probe
            fresh.samples.append({"at": time.time(), "value": value, "progress": None, "note": "registered"})
            fresh.data["probe_ok"] = None
            # A probe changed after review is reviewed again at the next checkpoint.
            fresh.data["probe_review"] = False
            fresh.log("probe", f"探针：{fresh.probe}（当前值 {value:g}）", by=caller)
            fresh.save(self.store)
        await self.changed(task)
        return {"value": value, "next_checkpoint": stamp(fresh.data["next_check"])}

    async def block(self, caller: str, task_id: str, question: str) -> dict:
        async with self.lock:
            task = self.get(task_id)
            if caller not in (task.executor, task.manager):
                raise Denied("only the executor or its manager can block a task on the owner")
            if task.state != "active" or not str(question or "").strip():
                raise ValueError("an active task and a concrete question for the owner are required")
            task.state = "blocked_on_owner"
            task.data["paused_at"] = time.time()
            task.log("block", question, by=caller)
            task.save(self.store)
        await self.tell_owner(f"【任务等待你的决定】{task.title}\n{question}\n\n计时已暂停。在任务页点“已解决”恢复，或回复阿撒托斯。", task)
        await self.tell(self.god(Role.AZATHOTH), f"[blocked on owner] {task.brief()}\nQuestion: {question}\n"
                        "The owner has been told; the timer is paused. If you can answer within the owner's will, "
                        "call task_unblock(task_id, note).", task)
        await self.changed(task)
        return {"state": task.state}

    async def resume(self, by: str, task_id: str, note: str = "") -> dict:
        async with self.lock:
            task = self.get(task_id)
            if task.state != "blocked_on_owner":
                raise ValueError("task is not blocked on the owner")
            now = time.time()
            paused = now - task.data.pop("paused_at", now)
            task.state = "active"
            task.deadline = (task.deadline or now) + paused
            task.data["next_check"] = now + self.interval
            task.log("resume", note or "阻塞已解决", by=by)
            task.save(self.store)
        await self.tell(task.executor, f"[unblocked] The blocker on task {task.id} is resolved by {by}: {note or '(no note)'}\n"
                        f"Timer resumed; deadline now {stamp(task.deadline)}.", task)
        await self.changed(task)
        return {"state": task.state, "deadline": stamp(task.deadline)}

    async def extend(self, caller: str, task_id: str, deadline, reason: str) -> dict:
        async with self.lock:
            task = self.get(task_id)
            if caller != task.executor:
                raise Denied("only the executor requests an extension")
            if task.state not in ("active", "blocked_on_owner"):
                raise ValueError(f"task is {task.state}")
            if any(e["state"] == "pending" for e in task.extensions):
                raise ValueError("an extension request is already pending")
            until = parse_deadline(deadline, task.deadline)
            if not until or until <= task.deadline:
                raise ValueError("the new deadline must be later than the current one")
            last = next((s for s in reversed(task.samples) if s["progress"] is not None), None)
            refusal = ("the probe showed no progress in the last checkpoint period" if not last or not last["progress"] else
                       "an extension may not be longer than the original duration" if until - task.deadline > task.data["duration"] + 1
                       else None)
            granted = sum(e["state"] == "approved" for e in task.extensions)
            approver = [self.superior(task), self.god(Role.AZATHOTH), OWNER][min(granted, 2)]
            ext = {"at": time.time(), "by": caller, "until": until, "reason": reason, "approver": approver,
                   "state": "refused" if refusal else "pending", **({"decision": refusal} if refusal else {})}
            task.extensions.append(ext)
            task.log("extension", f"申请延期至 {stamp(until)}：{reason}" + (f"（程序拒绝：{refusal}）" if refusal else ""), by=caller)
            task.save(self.store)
        await self.changed(task)
        if refusal:
            raise Denied(f"extension refused: {refusal}")
        text = (f"[extension request #{granted + 1}] {task.brief()}\nRequested new deadline {stamp(until)}. Reason: {reason}\n"
                "Decide with task_extension(task_id, approve, reason).")
        if approver == OWNER:
            await self.tell_owner(f"【第 {granted + 1} 次延期申请，只能由你批准】{task.title}\n申请截止改为 {stamp(until)}。理由：{reason}\n"
                                  f"探针历史：{task.history()}\n请在任务页批准或拒绝。", task)
        else:
            await self.tell(approver, text, task)
        return {"state": "pending", "approver": approver}

    async def decide_extension(self, caller: str, task_id: str, approve: bool, reason: str = "") -> dict:
        async with self.lock:
            task = self.get(task_id)
            ext = next((e for e in task.extensions if e["state"] == "pending"), None)
            if not ext:
                raise ValueError("no pending extension request")
            if caller != ext["approver"] or caller == task.executor:
                raise Denied(f"this extension is decided by {ext['approver']}, never by the executor")
            ext.update(state="approved" if approve else "denied", decision=reason, decided_at=time.time())
            if approve:
                task.deadline = ext["until"]
            task.log("extension", f"延期{'批准' if approve else '拒绝'}：{reason}", by=caller)
            task.save(self.store)
        await self.tell(task.executor, f"[extension {'approved' if approve else 'denied'}] task {task.id}: {reason or '-'}; "
                        f"deadline {stamp(task.deadline)}.", task)
        await self.changed(task)
        return {"deadline": stamp(task.deadline), "state": ext["state"]}

    async def submit(self, caller: str, task_id: str, evidence: str) -> dict:
        async with self.lock:
            task = self.get(task_id)
            if caller not in (task.executor, task.manager):
                raise Denied("only the executor or its manager submits")
            if task.state != "active" or not str(evidence or "").strip():
                raise ValueError("an active task and concrete evidence are required")
            task.state = "submitted"
            task.data.setdefault("submissions", []).append({"at": time.time(), "by": caller, "evidence": evidence})
            task.data["verdict_due"] = time.time() + self.interval
            task.data["owner_verdict"] = False
            task.log("submit", clip(evidence, 2000), by=caller)
            task.save(self.store)
        if self.role(task.executor) == Role.OLD_ONE:
            await self.tell(self.god(Role.YOG), f"[acceptance review] {task.brief()}\nBrief: {clip(task.data.get('brief'), 2000)}\n"
                            f"Submitted evidence (frozen): {evidence}\nVerify it against the acceptance criteria yourself, then call "
                            "task_verdict(task_id, passed, reason). End with a one-line Chinese conclusion.", task)
        else:
            await self.tell(self.god(Role.AZATHOTH), f"[submitted for acceptance] {task.brief()}\nEvidence: {evidence}\n"
                            "Check it against the owner's intent, then call task_verdict(task_id, passed, reason).",
                            task, kind="message", status="submitted")
        await self.changed(task)
        return {"state": task.state}

    async def verdict(self, caller: str, task_id: str, passed: bool, reason: str) -> dict:
        async with self.lock:
            task = self.get(task_id)
            if task.state != "submitted":
                raise ValueError(f"task is {task.state}, not submitted")
            role, worker_task = self.role(caller), self.role(task.executor) == Role.OLD_ONE
            if not (caller == OWNER or (role == Role.YOG and worker_task) or (role == Role.AZATHOTH and not worker_task)):
                raise Denied("old_one tasks are accepted by yog, manager tasks by azathoth; the owner may decide any")
            task.state = "accepted" if passed else "active"
            task.log("verdict", f"{'通过' if passed else '驳回'}：{reason}", by=caller)
            task.save(self.store)
        text = (f"[{'accepted' if passed else 'rejected'}] task {task.id} \"{task.title}\" by {caller}: {reason}" +
                ("" if passed else f"\nThe task is active again; the clock was not reset (deadline {stamp(task.deadline)})."))
        for to in dict.fromkeys((task.executor, task.manager, task.creator)):
            if to != caller:
                await self.tell(to, text, task)
        if role == Role.AZATHOTH:
            await self.tell_owner(f"阿撒托斯{'验收通过' if passed else '驳回'}了「{task.title}」：{reason}", task,
                                  kind="summary" if passed else "progress")
        await self.changed(task)
        return {"state": task.state}

    async def probe_verdict(self, caller: str, task_id: str, ok: bool, reason: str) -> dict:
        if self.role(caller) != Role.YOG:
            raise Denied("only yog judges probes")
        async with self.lock:
            task = self.get(task_id)
            if task.state not in OPEN:
                raise ValueError(f"task is {task.state}")
            task.data["probe_ok"] = bool(ok)
            task.log("probe_verdict", f"探针{'有效' if ok else '无效'}：{reason}", by=caller)
            task.save(self.store)
            if not ok:
                await self.void(task, f"Yog judged the probe invalid: {reason}")
        await self.changed(task)
        return {"state": task.state}

    # azathoth's interventions (each copied to the owner)

    async def replace(self, caller: str, task_id: str, reason: str) -> dict:
        task = self.get(task_id)
        if self.role(task.executor) != Role.OLD_ONE or task.state not in OPEN:
            raise ValueError("only an open old_one task can have its executor replaced")
        task.log("replace", f"阿撒托斯要求更换执行者：{reason}", by=caller)
        task.save(self.store)
        await self.tell(task.manager, f"[order from azathoth] Replace the executor {task.executor} of task {task.id}: {reason}\n"
                        f"Spawn the replacement with temple_spawn(task_id=\"{task.id}\", ...) and stop the old one.", task)
        await self.changed(task)
        return {"ordered": task.manager}

    # the clock

    async def run(self) -> None:
        await self.migrate()
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("task checkpoint sweep failed")
            await asyncio.sleep(self.tick)

    async def sweep(self) -> None:
        now = time.time()
        rows = self.store.q("SELECT id FROM tasks WHERE state IN ('active','submitted')")
        tasks = [Task.load(self.store, r["id"]) for r in rows]
        due = [t for t in tasks if t.state == "active" and t.data.get("next_check", 0) <= now]
        await asyncio.gather(*(self.checkpoint(t) for t in due))
        for task in tasks:
            if task.state == "submitted" and not task.data.get("owner_verdict") and task.data.get("verdict_due", now) <= now:
                async with self.lock:
                    task = self.get(task.id)
                    if task.state != "submitted" or task.data.get("owner_verdict"):
                        continue
                    task.data["owner_verdict"] = True
                    task.log("verdict_timeout", "验收超过一个检查点未裁决，转交主人")
                    task.save(self.store)
                last = task.data["submissions"][-1]
                await self.tell_owner(f"【验收超时，请你裁决】{task.title}\n验收标准：{task.acceptance}\n提交的证据：{clip(last['evidence'], 1500)}\n"
                                      "请在任务页点“通过”或“驳回”。", task)
                await self.changed(task)

    async def sample(self, task: Task) -> tuple[float | None, str]:
        agent = self.store.agent(task.executor) if task.executor else None
        conn = agent and self.t.galaxies.get(agent["galaxy_id"])
        if not conn:
            return None, "executor's galaxy is offline"
        try:
            r = await conn.rpc("probe", timeout=self.probe_timeout + 10, agent_id=agent["id"],
                               command=task.probe, timeout_s=self.probe_timeout)
        except Exception as e:
            return None, f"probe failed: {e}"
        if not isinstance(r, dict):
            return None, "galaxy does not support probes; upgrade it"
        out = (r.get("stdout") or "").strip()
        try:
            value = float(out)
            if math.isfinite(value):
                return value, ""
        except ValueError:
            pass
        return None, f"exit {r.get('code')}, output {clip(out or r.get('stderr'), 120)!r} is not a number"

    async def checkpoint(self, task: Task) -> None:
        agent = self.store.agent(task.executor) if task.executor else None
        if agent and (agent["galaxy_id"] in self.t.maintenance or agent["galaxy_id"] not in self.t.galaxies):
            async with self.lock:       # a deploy drain or an offline galaxy is not the executor's stall
                fresh = self.get(task.id)
                fresh.data["next_check"] = time.time() + self.interval
                fresh.save(self.store)
            return
        value, note = await self.sample(task) if task.probe else (None, "no probe")
        async with self.lock:
            task = self.get(task.id)
            now = time.time()
            if task.state != "active" or task.data.get("next_check", 0) > now:
                return
            d = task.data
            d["checkpoint"] = n = d.get("checkpoint", 0) + 1
            d["next_check"] = now + self.interval
            if not task.probe:
                await self.void(task, "no probe was registered by the first checkpoint")
                await self.changed(task)
                return
            prev = task.last_value()
            progress = value is not None and prev is not None and value > prev
            task.samples.append({"at": now, "value": value, "progress": progress, "note": note})
            d["misses"] = 0 if progress else d.get("misses", 0) + 1
            review = not d.get("probe_review")
            d["probe_review"] = True
            overdue = now > task.deadline
            alarm = (d["misses"] >= 3 or overdue) and not d.get("quiet") and n - d.get("alarm", -99) >= 3
            if alarm:
                d["alarm"] = n
            if not progress:
                task.log("stall", f"第 {n} 个检查点无进展（连续 {d['misses']}）" + (f"：{note}" if note else ""))
            task.save(self.store)
        await self.changed(task)
        yog, misses = self.god(Role.YOG), d["misses"]
        if review:
            await self.tell(yog, f"[probe review] {task.brief()}\nBrief: {clip(d.get('brief'), 2000)}\n"
                            "Does this probe really measure progress toward the acceptance criteria (not a clock, a log "
                            "that grows anyway, or a constant)? Call task_probe_verdict(task_id, ok, reason). An invalid probe "
                            "voids the task. End with a one-line Chinese conclusion.", task)
        if misses == 1:
            await self.tell(task.executor, f"[checkpoint {n}: no progress] Your probe for task {task.id} did not grow "
                            f"({task.history(3)}{'; ' + note if note else ''}). Make measurable progress before the next "
                            f"checkpoint at {stamp(d['next_check'])}, or call task_block if you need the owner.", task)
        if misses == 2:
            await self.tell(self.superior(task), f"[2 checkpoints without progress] {task.brief()}\nWithin 10 minutes: unblock the "
                            "executor, replace it, or have it request an extension. The next stalled checkpoint goes to the owner.", task)
        if misses == 2 or alarm:
            await self.tell(yog, f"[diagnosis] {task.brief()}\nThe program has already escalated. Read the task timeline "
                            "(task_get) and the executor's state (temple_latest_result), and write a short Chinese diagnosis "
                            "as your final reply: what is stuck and what would unblock it.", task)
        if alarm:
            why = "已超过截止时间" if overdue else f"连续 {misses} 个检查点没有进展"
            await self.tell_owner(f"【任务告警】「{task.title}」{why}。\n执行者 {task.executor}，截止 {stamp(task.deadline)}，"
                                  f"探针 `{task.probe}`\n指标：{task.history()}\n每 3 个检查点重复提醒，可在任务页暂停。", task)
            await self.tell(self.god(Role.AZATHOTH), f"[escalation: {'deadline passed' if overdue else f'{misses} checkpoints without progress'}] "
                            f"{task.brief()}\nThe owner has been told. Act: task_unblock, task_replace, or decide the pending "
                            "extension.", task)

    async def void(self, task: Task, reason: str) -> None:
        """Fail the task, stop its old_one executor (unless it still owes another task), tell everyone."""
        task.state = "failed"
        task.log("void", reason)
        task.save(self.store)
        ex = self.store.agent(task.executor) if task.executor else None
        others = self.store.q("SELECT id FROM tasks WHERE executor=? AND id!=? AND state IN ('active','blocked_on_owner','submitted')",
                              (task.executor, task.id))
        if ex and ex["role"] == Role.OLD_ONE and ex["desired"] == "running" and not others:
            try:
                await self.t.halt(ex["id"], VOIDER)
            except Exception:
                log.exception("task %s: could not stop executor", task.id)
        await self.tell_owner(f"【任务作废】「{task.title}」：{reason}。执行者 {task.executor} 已停止，管理者须重新派发带探针的新任务。", task)
        text = (f"[task voided] {task.brief()}\nReason: {reason}. The executor was stopped. "
                "Re-dispatch the work as a new task and have its executor register a probe right away (task_probe).")
        for to in dict.fromkeys((self.god(Role.AZATHOTH), task.manager)):
            await self.tell(to, text, task)

    async def migrate(self) -> None:
        """Pre-discipline open tasks: a grace period to register a deadline and probe, else voided."""
        self.store.x("UPDATE tasks SET state='accepted' WHERE state='done'")
        self.store.x("DELETE FROM settings WHERE key IN ('continuations','worker_nudges')")
        legacy = self.store.q("SELECT id FROM tasks WHERE state='open'")
        if not legacy:
            return
        now, az = time.time(), self.god(Role.AZATHOTH)
        by_executor: dict[str, list[Task]] = {}
        for row in legacy:
            first = self.store.one("SELECT receiver FROM messages WHERE task_id=? AND sender=? AND receiver IN "
                                   "(SELECT id FROM agents WHERE role IN ('shub','ghroth')) ORDER BY created_at LIMIT 1", (row["id"], az))
            executor = first["receiver"] if first else self.god(Role.SHUB)
            self.store.set_task(row["id"], state="active", creator=az, manager=executor, executor=executor,
                                deadline=now + DEFAULT_MINUTES * 60, acceptance="(legacy task: none recorded)",
                                data={"duration": DEFAULT_MINUTES * 60, "next_check": now + self.grace, "legacy": True,
                                      "events": [{"at": now, "kind": "created", "by": SYSTEM, "text": "军纪上线：旧任务获得宽限期"}]})
            by_executor.setdefault(executor, []).append(Task.load(self.store, row["id"]))
        for executor, tasks in by_executor.items():
            lines = "\n".join(f"- {t.id} \"{t.title}\"" for t in tasks)
            try:
                await self.t.route(SYSTEM, executor, f"[task discipline is now enforced] These open tasks have "
                                   f"{int(self.grace // 60)} minutes to register a probe (task_probe) or they are voided at the "
                                   f"first checkpoint. Deadline: {stamp(now + DEFAULT_MINUTES * 60)}. Better: submit finished ones, "
                                   f"and re-dispatch live work as new tasks with full contracts.\n{lines}", kind="discipline", priority=PRIORITY)
            except Exception as e:
                log.warning("migration notice to %s failed: %s", executor, e)
        log.info("task discipline: %d legacy tasks given %ss grace", len(legacy), self.grace)

    # views and the call surface

    def view(self, task_id: str) -> dict:
        row = self.store.task(task_id)
        if not row:
            raise ValueError(f"unknown task {task_id}")
        return row | {"messages": self.store.messages(task_id=task_id, limit=40)}

    async def call(self, caller: str, method: str, p: dict):
        role = self.role(caller)
        if method == "task_get":
            task = self.get(p["task_id"])
            if role not in (Role.AZATHOTH, Role.SHUB, Role.YOG, Role.GHROTH) and caller != task.executor:
                raise Denied("not your task")
            return self.view(task.id)
        if method == "task_create":
            return await self.create(caller, p)
        if method == "task_probe":
            return await self.set_probe(caller, p["task_id"], p["command"])
        if method == "task_extend":
            return await self.extend(caller, p["task_id"], p.get("deadline"), str(p.get("reason", "")))
        if method == "task_block":
            return await self.block(caller, p["task_id"], p.get("question", ""))
        if method == "task_submit":
            return await self.submit(caller, p["task_id"], p.get("evidence", ""))
        if method == "task_verdict":
            return await self.verdict(caller, p["task_id"], bool(p["passed"]), str(p.get("reason", "")))
        if method == "task_probe_verdict":
            return await self.probe_verdict(caller, p["task_id"], bool(p["ok"]), str(p.get("reason", "")))
        if method == "task_extension":
            result = await self.decide_extension(caller, p["task_id"], bool(p["approve"]), str(p.get("reason", "")))
        elif method in ("task_unblock", "task_replace"):
            if role != Role.AZATHOTH:
                raise Denied(f"{method} is azathoth's")
            result = await (self.resume(caller, p["task_id"], str(p.get("note", ""))) if method == "task_unblock"
                            else self.replace(caller, p["task_id"], str(p.get("reason", ""))))
        else:
            raise Denied(f"unknown task call {method}")
        if role == Role.AZATHOTH:
            task = self.get(p["task_id"])
            what = {"task_extension": "延期决定", "task_unblock": "解除阻塞", "task_replace": "要求更换执行者"}[method]
            detail = p.get("reason") or p.get("note") or ""
            await self.tell_owner(f"阿撒托斯对「{task.title}」{what}" + (f"：{detail}" if detail else ""), task, kind="progress")
        return result

    async def owner(self, p: dict) -> dict:
        """The owner's buttons: extension decisions, verdicts, blocker resolution, pausing escalation."""
        action, task_id = p["action"], p["task_id"]
        if action == "extension":
            return await self.decide_extension(OWNER, task_id, bool(p["approve"]), p.get("reason", ""))
        if action == "verdict":
            return await self.verdict(OWNER, task_id, bool(p["passed"]), p.get("reason", "") or "主人裁决")
        if action == "resolve":
            return await self.resume(OWNER, task_id, p.get("note", ""))
        if action == "quiet":
            async with self.lock:
                task = self.get(task_id)
                task.data["quiet"] = bool(p["quiet"])
                task.log("quiet", "主人暂停了告警" if p["quiet"] else "主人恢复了告警", by=OWNER)
                task.save(self.store)
            await self.changed(task)
            return {"quiet": task.data["quiet"]}
        raise ValueError(f"unknown action {action}")
