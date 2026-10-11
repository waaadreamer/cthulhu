"""Role subclasses: what each agent remembers, which tools it holds, and how
it is woken. All authority is still enforced by the temple; tools here only
shape what the model is offered.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from .agent import Agent, clip, load_role, summarize_items
from .roles import OWNER, Role
from .runtime import TurnResult, tool

log = logging.getLogger(__name__)

MEMORY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["add", "update", "forget"],
    "properties": {
        "add": {"type": "array", "items": {"type": "string"}},
        "update": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text"],
            "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}}},
        "forget": {"type": "array", "items": {"type": "integer"}},
    },
}

# Azathoth's final reply to an internal message is shown to the owner unless it is exactly this.
SILENT = "[静默]"

STR = {"type": "string"}


class MemoryAgent(Agent):
    """An agent with a permanent context distilled from selected conversations.

    Messages from `memory_sources` are, after the turn, distilled by an extra
    model call on an ephemeral fork of the thread. Everything else stays in the
    temporary context (the native thread) only.
    """
    memory_sources: set[str] = set()
    # Senders whose messages may only correct or drop existing entries (verified results), never add.
    correction_sources: set[str] = set()
    memory_brief = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.memory_lock = asyncio.Lock()

    async def after_turn(self, batch: list[dict], result: TurnResult) -> None:
        if result.status != "completed" or not self.last_turn:
            return
        if any(m["sender_role"] in self.memory_sources for m in batch):
            asyncio.create_task(self.distill(*self.last_turn))
        elif self.correction_sources and any(m["sender_role"] in self.correction_sources for m in batch):
            asyncio.create_task(self.distill(*self.last_turn, correct_only=True))

    async def fork_for_memory(self, rt, thread: str) -> str:
        # Claude forks can run on a cheaper model / effort; Codex forks inherit the thread's.
        cfg = self.backends.cfg
        if hasattr(rt, "instructions") and (cfg.get("memory_model") or cfg.get("memory_effort")):
            return await rt.fork_thread(thread, model=cfg.get("memory_model"), effort=cfg.get("memory_effort"))
        return await rt.fork_thread(thread)

    async def distill(self, rt, thread: str, correct_only: bool = False) -> None:
        async with self.memory_lock:
            fork = None
            try:
                current = await self.link.call("memory")
                if correct_only and not current:
                    return
                listing = "\n".join(f"#{m['id']}（{stamp(m)}）{m['text']}" for m in current) or "（空）"
                fork = await self.fork_for_memory(rt, thread)
                prompt = (f"【程序层记忆整理，不是新消息，不要调用任何工具】现在是 {time.strftime('%Y-%m-%d %H:%M')}。\n"
                          f"{self.memory_brief}\n\n当前永久上下文（括号里是最后更新时间）：\n{listing}\n\n"
                          "只看最近这轮交流，维护上面的永久上下文：\n"
                          "- update：已有条目被这轮交流补充、修正或推翻时，给出该编号的新全文（改写原条目，不要另起一条）。\n"
                          "- forget：彻底失效、不再有任何参考价值的条目编号。\n"
                          + ("- add：必须返回空数组。这轮是下属的执行结果，只能用经过核实的结果修正或删除已有条目。\n"
                             if correct_only else
                             "- add：确实是新的长期信息才新增。\n")
                          + "每条只写一件事，一两句话，自包含、忠实原意；不要把多件事塞进一条，不要记待办、待确认的问题和操作步骤。"
                          "会随时间变化的状态（集群、星系、资源类事实）要写明截至的日期。没有变化就全部返回空数组。")
                r = await self.run(fork, prompt, output_schema=MEMORY_SCHEMA, track=False, timeout=600, runtime=rt)
                data = json.loads(r.text or "{}")
                log.info("%s memory%s: add %s update %s forget %s", self.name, " (correct only)" if correct_only else "",
                         len(data.get("add", [])), [u.get("id") for u in data.get("update", [])], data.get("forget", []))
                known = {m["id"] for m in current}
                for mid in data.get("forget", []):
                    if mid in known:
                        await self.link.call("forget", memory_id=mid)
                for u in data.get("update", []):
                    if u.get("id") in known and u.get("text", "").strip():
                        await self.link.call("revise", memory_id=u["id"], text=u["text"].strip(), source=thread)
                for text in [] if correct_only else data.get("add", []):
                    if text.strip():
                        await self.link.call("remember", text=text.strip(), source=thread)
            except Exception:
                log.exception("%s memory distillation failed", self.name)
            finally:
                if fork:
                    await rt.drop_thread(fork)


def stamp(m: dict) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(m.get("updated_at") or m["created_at"]))


class OwnerChat:
    """Mixin: owner messages are answered in the web chat, streamed live."""

    async def receive(self, batch: list[dict]) -> None:
        owner = [m for m in batch if m["sender"] == OWNER]
        rest = [m for m in batch if m["sender"] != OWNER]
        for m in owner:
            await self.chat(m)
        if rest:
            await Agent.receive(self, rest)

    async def chat(self, m: dict) -> None:
        turn_id = m["id"]
        await self.link.call("chat_update", turn_id=turn_id, status="running")
        final_items: set[str] = set()
        activity: list[str] = []

        def on_event(method: str, p: dict) -> None:
            item = p.get("item") or {}
            if method == "item/started" and item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                final_items.add(item["id"])
            elif method == "item/agentMessage/delta" and p.get("itemId") in final_items:
                asyncio.create_task(self.link.emit({"type": "stream", "turn_id": turn_id, "delta": p["delta"]}))
            elif method == "item/completed":
                if item.get("type") == "agentMessage" and item.get("phase") == "commentary":
                    activity.append("💭 " + clip(item.get("text", ""), 200))
                else:
                    activity.extend(summarize_items([item]))
                asyncio.create_task(self.link.emit({"type": "stream", "turn_id": turn_id, "activity": activity[-20:]}))

        try:
            result = await self.run_main(self.render([m]), on_event=on_event)
            status = {"completed": "done", "interrupted": "interrupted"}.get(result.status, "failed")
            reply = result.text or (f"（出错：{result.error}）" if result.error else "")
        except Exception as e:
            result, status, reply = TurnResult("failed", error=str(e)), "failed", f"（出错：{e}）"
        await self.link.call("chat_update", turn_id=turn_id, reply=reply, status=status, activity=activity[-40:],
                             model=self.latest.get("model"))
        await self.after_turn([m], result)


class Azathoth(OwnerChat, MemoryAgent):
    role = Role.AZATHOTH
    memory_sources = {OWNER}
    memory_brief = ("刚才是你和主人之间的交流（可能是主人发起的对话，也可能是你把下属的汇报转告给主人）。"
                    "提取值得长期保留的内容：主人的目标、意志、偏好、决定、约束、洞见，以及经过核实的、对以后判断有用的客观事实"
                    "（例如集群和星系是什么、在哪里）。不要记录寒暄、执行流水和一次性的进度细节。")

    def tools(self) -> list[dict]:
        return super().tools() + [
            tool("temple_messages", "读取中心神殿的消息记录（可按 agent 过滤）。", {"agent_id": STR, "limit": {"type": "integer"}}),
            tool("temple_agent", "读取某个 agent 的完整注册信息（含初始化参数）。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_owner_history", "检索你和主人的历史聊天。", {"search": STR, "limit": {"type": "integer"}}),
        ]

    async def call_tool(self, name: str, args: dict):
        if name == "temple_messages":
            msgs = await self.link.call("messages", agent_id=args.get("agent_id"), limit=args.get("limit", 30))
            return [{k: m[k] for k in ("id", "sender", "receiver", "text", "created_at")} for m in msgs]
        if name == "temple_agent":
            return await self.link.call("agent", agent_id=args["agent_id"])
        if name == "temple_owner_history":
            turns = await self.link.call("owner_chat", search=args.get("search"), limit=args.get("limit", 10))
            return [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created_at"])),
                     "owner": t["user_text"] or f"（你主动告知，起因：{t.get('origin') or '内部消息'}）",
                     "you": clip(t["reply"], 2000)} for t in turns]
        return await super().call_tool(name, args)

    async def receive(self, batch: list[dict]) -> None:
        owner = [m for m in batch if m["sender"] == OWNER]
        rest = [m for m in batch if m["sender"] != OWNER]
        for m in owner:
            await self.chat(m)
        if rest:
            await self.internal(rest)

    async def internal(self, batch: list[dict]) -> None:
        """Messages from shub/yog. The program, not the model, decides where the reply goes:
        the final reply is shown in the owner's chat unless it is exactly SILENT."""
        text = (f"【以下是宇宙内部的消息，不是主人说的。你这轮的最终回复会由程序作为主动消息直接显示在主人的聊天窗口。"
                f"交付的结果、需要主人决定的事、重大风险、纠正你之前告诉主人的错误信息，就写给主人；"
                f"纯过程性进展、主人不需要知道的，最终回复只写 {SILENT}。】\n\n" + self.render(batch))
        activity: list[str] = []

        def on_event(method: str, p: dict) -> None:
            if method == "item/completed":
                activity.extend(summarize_items([p.get("item") or {}]))

        result = await self.run_main(text, on_event=on_event)
        reply = (result.text or "").strip()
        if result.status != "completed":
            reply = f"（处理{', '.join(sorted({m['sender_name'] for m in batch}))}的消息时出错：{result.error or result.status}）"
        if reply and reply != SILENT and not reply.endswith(SILENT):
            origin = "、".join(sorted({m["sender_name"] for m in batch})) + "的消息"
            await self.link.call("notify_owner", text=reply, origin=origin, model=self.latest.get("model"),
                                 activity=activity[-40:])
            # What reached the owner is part of the owner conversation: distill it.
            if result.status == "completed" and self.last_turn:
                asyncio.create_task(self.distill(*self.last_turn))


class Shub(MemoryAgent):
    role = Role.SHUB
    memory_sources = {Role.AZATHOTH}
    correction_sources = {Role.OLD_ONE}
    memory_brief = ("刚才是你和阿撒托斯的交流。只提取阿撒托斯给出的、以后组织任务时仍然有用的客观信息和长期约定："
                    "例如某个集群/星系是什么、有什么资源，阿撒托斯定下的长期规则和偏好。"
                    "不要记录具体某次任务的请求、要求、进度或结果（任务本身留在消息记录里，记下来会干扰以后的任务），"
                    "也不要记录 old_one 的汇报；old_one 的初始化参数由程序单独保存。")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.roster_seen: str | None = None

    async def extra_context(self) -> str:
        text = await self.roster()
        self.roster_seen = text
        return text

    async def receive(self, batch: list[dict]) -> None:
        # The roster is part of shub's permanent context; refresh it whenever it changed.
        text = self.render(batch)
        roster = await self.roster()
        if roster != self.roster_seen:
            self.roster_seen = roster
            text = f"【名册已更新】\n{roster}\n\n{text}"
        result = await self.run_main(text)
        await self.after_turn(batch, result)

    async def roster(self) -> str:
        d = await self.link.call("directory")
        olds = [a for a in d["agents"] if a["role"] == Role.OLD_ONE and a["desired"] == "running"]
        galaxies = "\n".join(f"- {g['name']}（ID {g['id']}，{'在线' if g['online'] else '离线'}"
                             f"{'' if g.get('spawn', True) else '，不可创建旧日支配者'}）" for g in d["galaxies"])
        lines = []
        for a in olds:
            full = await self.link.call("agent", agent_id=a["id"])
            spec = full["spec"]
            lines.append(f"## {a['name']}（ID {a['id']}，星系 {a['galaxy_id']}）\n任务：{spec.get('task', '')}\n"
                         f"职责设定：{clip(spec.get('skill', ''), 1500)}")
        return (f"# 星系\n{galaxies or '（无）'}\n\n# 现役旧日支配者（初始化参数）\n" +
                ("\n\n".join(lines) if lines else "（暂无）"))

    def tools(self) -> list[dict]:
        return super().tools() + [
            tool("temple_spawn", "在指定星系创建一个旧日支配者。创建后它会立即按 task 开始工作。",
                 {"name": STR, "galaxy_id": STR,
                  "task": {"type": "string", "description": "完整任务：目标、边界、验收标准、完成后向谁汇报"},
                  "skill": {"type": "string", "description": "它的职责设定（system prompt），描述它是谁、负责什么领域、工作方式"},
                  "cwd": {"type": "string", "description": "工作目录，可选"}},
                 ["name", "galaxy_id", "task", "skill"]),
            tool("temple_stop", "终止一个旧日支配者。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_restart", "重启一个旧日支配者（保留其上下文）。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_messages", "读取中心神殿的消息记录（可按 agent 过滤）。", {"agent_id": STR, "limit": {"type": "integer"}}),
        ]

    async def call_tool(self, name: str, args: dict):
        if name == "temple_spawn":
            return await self.link.call("spawn", **args)
        if name == "temple_stop":
            return await self.link.call("stop", agent_id=args["agent_id"])
        if name == "temple_restart":
            return await self.link.call("restart", agent_id=args["agent_id"])
        if name == "temple_messages":
            msgs = await self.link.call("messages", agent_id=args.get("agent_id"), limit=args.get("limit", 30))
            return [{k: m[k] for k in ("id", "sender", "receiver", "text", "created_at")} for m in msgs]
        return await super().call_tool(name, args)


class Yog(MemoryAgent):
    role = Role.YOG
    memory_sources = {Role.AZATHOTH}
    correction_sources = {Role.OLD_ONE}
    memory_brief = ("刚才是你和阿撒托斯的交流。只提取阿撒托斯对监督工作给出的长期要求、关注点，以及以后巡检仍然有用的客观信息。"
                    "不要记录具体某次的请求、巡检流水或一次性结论。")

    def __init__(self, *a, patrol_interval: float = 300, **kw):
        super().__init__(*a, **kw)
        self.patrol_interval = patrol_interval
        self.last_fingerprint = None
        self.last_patrol = 0.0
        # agent id -> finished_at already shown in a patrol; persisted so restarts don't re-report.
        self.seen_path = self.state_dir / "seen_results.json"
        try:
            self.seen_results: dict[str, float] = json.loads(self.seen_path.read_text())
        except (OSError, ValueError):
            self.seen_results = {}

    def tools(self) -> list[dict]:
        return super().tools() + [
            tool("temple_latest_result", "远程读取某个 agent 的最新工作状态与产出。", {"agent_id": STR}, ["agent_id"]),
        ]

    async def call_tool(self, name: str, args: dict):
        if name == "temple_latest_result":
            return await self.link.call("latest_result", agent_id=args["agent_id"])
        return await super().call_tool(name, args)

    async def background(self) -> None:
        while True:
            await asyncio.sleep(self.patrol_interval)
            try:
                await self.patrol()
            except Exception:
                log.exception("patrol failed")

    async def patrol(self) -> None:
        snapshot = await self.link.call("patrol")
        if not snapshot:
            return
        # Volatile fields excluded so an idle universe does not cost a model call.
        stable = [{k: s.get(k) for k in ("agent_id", "state", "online", "finished_at", "last_status", "queued")}
                  for s in snapshot]
        fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
        busy = any(s.get("state") == "working" for s in snapshot)
        if fingerprint == self.last_fingerprint and not (busy and time.time() - self.last_patrol > 3 * self.patrol_interval):
            return
        self.last_fingerprint, self.last_patrol = fingerprint, time.time()
        # Mark turns that finished since the previous patrol, so new results are reported exactly once.
        for snap in snapshot:
            done = snap.get("finished_at")
            snap["new_result"] = bool(done) and self.seen_results.get(snap["agent_id"]) != done
            if done:
                self.seen_results[snap["agent_id"]] = done
        self.seen_path.write_text(json.dumps(self.seen_results))
        rt = self.runtime
        thread = await rt.start_thread(await self.instructions(), self.tools(), self.call_tool, ephemeral=True)
        try:
            prompt = (f"【定时巡检 {time.strftime('%Y-%m-%d %H:%M')}】以下是所有旧日支配者与格赫罗斯的 latest_result：\n"
                      f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=1)}\n```\n"
                      f"最近的巡检结论：\n{await self.recent_patrols()}\n\n"
                      "判断每个 agent 是否在正常推进。需要催办就直接 temple_send 给该旧日支配者；需要阿撒托斯决策或纠偏才发给阿撒托斯。"
                      "一切正常就什么都不发。最后用一两句话写下本次巡检结论。")
            r = await self.run(thread, prompt, track=False, timeout=1800, runtime=rt)
            if r.status == "failed" and self.backends.trip():
                await rt.drop_thread(thread)
                rt = self.runtime
                thread = await rt.start_thread(await self.instructions(), self.tools(), self.call_tool, ephemeral=True)
                r = await self.run(thread, prompt, track=False, timeout=1800, runtime=rt)
            self.latest = {"last_patrol": clip(r.text, 1500), "finished_at": time.time()}
            await self.link.call("patrol_log", summary=r.text or r.error or "")
        finally:
            await rt.drop_thread(thread)

    async def recent_patrols(self) -> str:
        rows = await self.link.call("patrol_log_recent")
        return "\n".join(f"- {time.strftime('%H:%M', time.localtime(p['created_at']))} {clip(p['summary'], 300)}"
                         for p in rows) or "（无）"


class Ghroth(OwnerChat, MemoryAgent):
    role = Role.GHROTH
    memory_sources = {OWNER}
    memory_brief = "刚才是你和主人的对话。提取主人对 harness 维护的要求、决定、约束和偏好，以及你们达成的结论。"

    def tools(self) -> list[dict]:
        # Ghroth talks to no one; it only works.
        return []


class OldOne(Agent):
    role = Role.OLD_ONE

    def skill(self) -> str:
        return (load_role("old_one") + f"\n\n# 黑山羊（ID shub）为你设定的职责\n{self.spec.get('skill', '')}\n\n"
                f"# 你的初始任务\n{self.spec.get('task', '')}")

    async def on_ready(self) -> None:
        if self.thread_id is None:
            # A fresh old_one starts on its task without waiting for a message.
            self.enqueue({"id": f"start-{self.id}", "sender": "system", "sender_name": "中心神殿", "sender_role": "system",
                          "priority": 0, "created_at": time.time(), "text": "你刚被创建。开始执行你的初始任务。"})


CLASSES = {c.role: c for c in (Azathoth, Shub, Yog, Ghroth, OldOne)}
