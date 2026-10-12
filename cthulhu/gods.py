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
from .store import estimate_tokens
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

STR = {"type": "string"}
TASK_STATE_TOOL = tool("temple_task_state", "读取任务最新指令、关联执行者实际状态和续办记录。", {"task_id": STR}, ["task_id"])

# Permanent memory holds high-level insights and rules only; details live in the temple's message history.
MEMORY_RULES = ("永久上下文只记录高层次、长期有效的内容，只有五类：主人的目标与关注点、偏好、决定与授权、约束与规则、"
                "世界的稳定事实（例如某个集群是什么、用来做什么）。"
                "一律不记：进度、指标、数值、路径、ID、任务名、调查结论、操作步骤、建议、待办和待确认的问题。"
                "这些细节都保存在中心神殿的消息历史里，需要时可以查阅。判断标准：能重新查到的、只对某一次任务有用的、"
                "过几天就会过时的，都不记。每条只写一件事，用中文写一句话，不超过 60 字。")
MAX_ENTRY_CHARS = 120
# Past this size the whole permanent context is consolidated in one pass.
COMPACT_AT_TOKENS = 50_000


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
            if self.draining:
                return
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
                          + MEMORY_RULES + "已有条目里不符合这些规则的，也用 forget 删掉。没有变化就全部返回空数组。")
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
                    if not text.strip():
                        continue
                    if len(text.strip()) > MAX_ENTRY_CHARS:
                        log.warning("%s memory entry rejected as too long: %s", self.name, clip(text, 200))
                        continue
                    await self.link.call("remember", text=text.strip(), source=thread)
            except Exception:
                log.exception("%s memory distillation failed", self.name)
            finally:
                if fork:
                    await rt.drop_thread(fork)
            await self.compact_if_needed(rt, thread)

    async def compact_if_needed(self, rt, thread: str) -> None:
        current = await self.link.call("memory")
        size = sum(estimate_tokens(m["text"]) for m in current)
        if size < COMPACT_AT_TOKENS:
            return
        log.info("%s permanent memory at ~%d tokens; consolidating", self.name, size)
        fork = None
        try:
            fork = await self.fork_for_memory(rt, thread)
            listing = "\n".join(f"#{m['id']}（{stamp(m)}）{m['text']}" for m in current)
            prompt = (f"【程序层记忆整理，不是新消息，不要调用任何工具】你的永久上下文已经约 {size} tokens，超过上限，"
                      f"需要整体整理到一半以下。\n{MEMORY_RULES}\n\n当前永久上下文：\n{listing}\n\n"
                      "合并重复和相近的条目（update 保留其中一条的编号写合并后的全文，forget 其余），删除不符合规则或已过时的条目。"
                      "主人的意志、偏好、决定、授权和约束必须保留。add 返回空数组。")
            r = await self.run(fork, prompt, output_schema=MEMORY_SCHEMA, track=False, timeout=900, runtime=rt)
            data = json.loads(r.text or "{}")
            known = {m["id"] for m in current}
            for u in data.get("update", []):
                if u.get("id") in known and u.get("text", "").strip():
                    await self.link.call("revise", memory_id=u["id"], text=u["text"].strip(), source="compact")
            for mid in data.get("forget", []):
                if mid in known:
                    await self.link.call("forget", memory_id=mid)
        except Exception:
            log.exception("%s memory consolidation failed", self.name)
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


HISTORY_TOOLS = [
    tool("temple_messages", "查阅中心神殿的消息历史：调查结果、数据、路径等细节都在这里。可按 agent、任务或关键词过滤。",
         {"agent_id": STR, "task_id": STR, "search": {"type": "string", "description": "关键词"},
          "limit": {"type": "integer"}}),
    tool("temple_tasks", "列出阿撒托斯交给黑山羊的任务及其状态（open / done），可按关键词搜索。",
         {"search": STR, "limit": {"type": "integer"}}),
]


async def history_call(link, name: str, args: dict):
    if name == "temple_messages":
        msgs = await link.call("messages", agent_id=args.get("agent_id"), task_id=args.get("task_id"),
                               search=args.get("search"), limit=args.get("limit", 30))
        return [{k: m.get(k) for k in ("id", "sender", "receiver", "task_id", "status", "text", "created_at")} for m in msgs]
    if name == "temple_tasks":
        return await link.call("tasks", search=args.get("search"), limit=args.get("limit", 20))
    return None


# How a report to the owner is labelled, from the status shub attached to its message.
NOTE_KIND = {"progress": "progress", "blocked": "blocked", "done": "summary"}


class Azathoth(OwnerChat, MemoryAgent):
    role = Role.AZATHOTH
    memory_sources = {OWNER}
    # Reports from shub/yog may correct or drop entries, never add: details stay in the message history.
    correction_sources = {Role.SHUB, Role.YOG}
    memory_brief = "刚才是你和主人的对话。只提取主人表达的意志、偏好、决定、授权、约束，以及主人告诉你的世界的稳定事实。"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reporting: dict | None = None   # set while handling messages from shub/yog

    def tools(self) -> list[dict]:
        return [
            self.send_tool({
                "task_id": {"type": "string", "description": "发给黑山羊时：跟进已有任务就填它的任务 ID，不填则新建任务。"
                            "发给格赫罗斯的工作属于某个已有任务时，也填它的任务 ID"},
                "title": {"type": "string", "description": "发给黑山羊的新任务：十几个字的任务标题"}}),
            *super().tools()[1:],
            *HISTORY_TOOLS,
            tool("temple_agent", "读取某个 agent 的完整注册信息（含初始化参数）。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_owner_history", "检索你和主人的历史聊天。", {"search": STR, "limit": {"type": "integer"}}),
            tool("temple_deploy", "一键部署：把仓库 main 分支的最新版本部署到中心神殿和各星系。程序会先排空（等正在进行的回合结束）、"
                 "更新代码、重启、确认上线，失败自动回滚；结果会作为消息发回给你。"
                 "也用于批准或拒绝格赫罗斯的部署请求（request_id）。",
                 {"galaxies": {"type": "array", "items": STR, "description": "星系 ID 列表；不填或 [\"all\"] 表示中心神殿和全部星系"},
                  "reason": STR, "task_id": STR,
                  "recover_job_id": {"type": "string", "description": "重新确认失败部署的回滚，仅恢复，不重新发布目标版本"},
                  "request_id": {"type": "string", "description": "格赫罗斯部署请求的 ID"},
                  "reject": {"type": "boolean", "description": "拒绝该请求"},
                  "status_only": {"type": "boolean", "description": "只查看各星系当前版本和最近的部署记录"}}),
            tool("tell_owner", "只在处理黑山羊/尤格的消息时使用：在主人的聊天窗口里告诉主人当前情况，每次都必须调用；"
                 "进展只写一两句话，任务完成时写完整总结。",
                 {"text": STR,
                  "deliverable": {"type": "boolean", "description": "这条消息就是主人要的东西（报告、答复、结论），"
                                  "要作为总结展开显示，而不是折叠成过程中的一步"}}, ["text"]),
        ]

    async def call_tool(self, name: str, args: dict):
        found = await history_call(self.link, name, args)
        if found is not None:
            return found
        if name == "temple_deploy":
            if args.get("status_only"):
                return await self.link.call("deploy_status")
            return await self.link.call("deploy", timeout=120, **{k: args[k] for k in
                                        ("galaxies", "reason", "task_id", "request_id", "reject", "recover_job_id") if k in args})
        if name == "temple_agent":
            return await self.link.call("agent", agent_id=args["agent_id"])
        if name == "temple_owner_history":
            turns = await self.link.call("owner_chat", search=args.get("search"), limit=args.get("limit", 10))
            return [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created_at"])),
                     "owner": t["user_text"] or f"（你主动告知：{t.get('task_title') or t.get('origin') or '内部消息'}）",
                     "you": clip(t["reply"], 2000)} for t in turns]
        if name == "tell_owner":
            if self.reporting is None:
                return "现在是主人在和你对话，直接在最终回复里说即可，不需要 tell_owner。"
            await self.tell_owner(args["text"], deliverable=bool(args.get("deliverable")))
            return "已送达主人聊天窗口"
        return await super().call_tool(name, args)

    async def tell_owner(self, text: str, deliverable: bool = False) -> None:
        r = self.reporting or {}
        r["told"] = True
        # A deliverable the owner asked for is shown as a summary even while its task stays open.
        kind = "summary" if deliverable and r.get("kind") in ("progress", "notice") else r.get("kind")
        await self.link.call("notify_owner", text=text, origin=r.get("origin"), task_id=r.get("task_id"),
                             kind=kind, model=self.latest.get("model"))

    async def receive(self, batch: list[dict]) -> None:
        owner = [m for m in batch if m["sender"] == OWNER]
        rest = [m for m in batch if m["sender"] != OWNER]
        for m in owner:
            await self.chat(m)
        # One report turn per task, so each note to the owner belongs to exactly one task.
        groups: dict[str | None, list[dict]] = {}
        for m in rest:
            groups.setdefault(m.get("task_id"), []).append(m)
        for task_id, msgs in groups.items():
            await self.internal(task_id, msgs)

    async def internal(self, task_id: str | None, batch: list[dict]) -> None:
        """Reports from shub (tagged with a task and its status) or yog. Azathoth may answer them or not,
        but the owner is told every time; the program decides whether it is a step or the final summary."""
        statuses = [m.get("status") for m in batch if m.get("status")]
        status = "done" if "done" in statuses else ("blocked" if "blocked" in statuses else statuses[-1] if statuses else None)
        kind = NOTE_KIND.get(status, "notice")
        title = next((m.get("task_title") for m in batch if m.get("task_title")), None)
        self.reporting = {"task_id": task_id, "kind": kind, "told": False,
                          "origin": title or "、".join(sorted({m["sender_name"] for m in batch}))}
        guide = {
            "progress": "这是任务过程中的进展。tell_owner 只写一两句话的进展（像思考过程里的一步：做了什么、发现了什么、下一步），不要展开细节。"
                        "例外：如果这条消息交付的正是主人要的东西（报告、答复、结论），就写完整内容并设 deliverable=true，它会作为总结展开显示。",
            "blocked": "任务受阻，需要主人决定。tell_owner 写一份阶段总结，会在界面上展开显示（之前的进展折叠在上面）："
                       "先列出到目前为止已完成的结果和结论，再说清卡在哪里，最后用主人可以直接回答的具体问题结尾。",
            "summary": "黑山羊标记这个任务已完成。先核对结果是否满足主人的意图，再用 tell_owner 写给主人的完整总结："
                       "结论先行，然后是关键结果、遗留问题；过程不用重复，主人可以展开查看之前的进展。",
            "notice": "这是尤格或神殿的报告。tell_owner 用一两句话告诉主人要点；需要主人决定时说清楚。"
                      "如果它交付的正是主人要的东西，就写完整内容并设 deliverable=true。",
        }[kind]
        try:
            text = (f"【以下是宇宙内部的消息，不是主人说的。你可以用 temple_send 回复或继续安排下属，也可以不回复；"
                    f"但必须调用一次 tell_owner 告诉主人。{guide}】\n\n" + self.render(batch))
            result = await self.run_main(text)
            if result.status == "completed" and not self.reporting["told"]:
                result = await self.run_main(f"【程序提示】你还没有调用 tell_owner。现在调用它。{guide}")
            if not self.reporting["told"]:
                reply = (result.text or "").strip() if result.status == "completed" else \
                    f"（处理{self.reporting['origin']}的消息时出错：{result.error or result.status}）"
                if reply:
                    await self.tell_owner(reply)
            if result.status == "completed" and self.last_turn:
                asyncio.create_task(self.distill(*self.last_turn, correct_only=True))
        finally:
            self.reporting = None


class Shub(MemoryAgent):
    role = Role.SHUB
    memory_sources = {Role.AZATHOTH}
    correction_sources = {Role.OLD_ONE}
    memory_brief = "刚才是你和阿撒托斯的交流。只提取阿撒托斯定下的长期规则、授权，以及世界的稳定事实；具体任务的内容不记。"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.roster_seen: str | None = None

    async def extra_context(self) -> str:
        text = await self.roster()
        self.roster_seen = text
        return text

    def take_batch(self, ready=None):
        # Keep automatically inherited task IDs unambiguous when several tasks wake Shub together.
        candidates = self.queue if ready is None else ready
        first = min(candidates, key=lambda m: (-m['priority'], m['created_at']))
        return super().take_batch([m for m in candidates if m.get('task_id') == first.get('task_id')])

    async def receive(self, batch: list[dict]) -> None:
        # The roster is part of shub's permanent context; refresh it whenever it changed.
        text = self.render(batch)
        for task_id in dict.fromkeys(m.get("task_id") for m in batch if m.get("sender_role") == Role.YOG):
            if task_id:
                state = await self.link.call("task_state", task_id=task_id)
                text += "\n【续办前最新状态，以此为准；已完成、仍在执行或真正待决策时不要重试】\n" + json.dumps(state, ensure_ascii=False)
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
                  "task_id": STR,
                  "cwd": {"type": "string", "description": "工作目录，可选"}},
                 ["name", "galaxy_id", "task", "skill"]),
            tool("temple_stop", "终止一个旧日支配者。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_restart", "重启一个旧日支配者（保留其上下文）。", {"agent_id": STR}, ["agent_id"]),
            *HISTORY_TOOLS,
            TASK_STATE_TOOL,
        ]

    def send_tool(self, extra: dict | None = None) -> dict:
        return super().send_tool({
            "task_id": {"type": "string", "description": "给阿撒托斯汇报时必填：这条汇报所属的任务 ID"},
            "status": {"type": "string", "enum": ["progress", "blocked", "done"],
                       "description": "给阿撒托斯汇报时必填：progress 进行中；blocked 受阻、需要决定；"
                                      "done 整个任务已经完成并验收，不会再有后续"}})

    async def send(self, to, text, priority=0, kind="message", **task):
        if self.current_task and not task.get("task_id"):
            task["task_id"] = self.current_task
        return await super().send(to, text, priority, kind, **task)

    async def call_tool(self, name: str, args: dict):
        found = await history_call(self.link, name, args)
        if found is not None:
            return found
        if name == "temple_spawn":
            if self.current_task and not args.get("task_id"):
                args = args | {"task_id": self.current_task}
            return await self.link.call("spawn", **args)
        if name == "temple_task_state":
            return await self.link.call("task_state", task_id=args["task_id"])
        if name == "temple_stop":
            return await self.link.call("stop", agent_id=args["agent_id"])
        if name == "temple_restart":
            return await self.link.call("restart", agent_id=args["agent_id"])
        return await super().call_tool(name, args)


class Yog(MemoryAgent):
    role = Role.YOG
    memory_sources = {Role.AZATHOTH}
    correction_sources = {Role.OLD_ONE}
    memory_brief = "刚才是你和阿撒托斯的交流。只提取阿撒托斯对监督工作定下的长期要求和关注点；具体某次的巡检和请求不记。"

    def __init__(self, *a, patrol_interval: float = 300, **kw):
        super().__init__(*a, **kw)
        self.patrol_interval = patrol_interval
        self.patrol_wake = asyncio.Event()
        self.patrol_pending = False
        self.azathoth_id = Role.AZATHOTH.value
        self.ghroth_task: str | None = None
        self.reported = False
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
            TASK_STATE_TOOL,
        ]

    def send_tool(self, extra=None):
        result = super().send_tool({"task_id": STR})
        result['description'] += " 任务提醒统一用此工具：等待收证发黑山羊，明确停滞发具体执行者，必须带task_id。正常后台进展不要催办。程序按任务去重，suppressed表示本次未发送。"
        return result

    async def _on_frame(self, frame: dict):
        if frame.get("type") == "patrol_requested":
            self.patrol_pending = True
            self.patrol_wake.set()
            return
        result = await super()._on_frame(frame)
        if not self.draining and self.patrol_pending:
            self.patrol_wake.set()
        return result

    async def _on_welcome(self, w: dict) -> None:
        await super()._on_welcome(w)
        if not self.draining and self.patrol_pending:
            self.patrol_wake.set()

    async def background(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self.patrol_wake.wait(), self.patrol_interval)
            except asyncio.TimeoutError:
                pass
            self.patrol_wake.clear()
            try:
                if not self.draining:
                    if self.patrol_pending:
                        # Coalesce bursts and let the finishing agent publish its idle state.
                        await asyncio.sleep(1)
                    if self.draining:
                        continue
                    self.patrol_pending = False
                    self.patrol_wake.clear()
                    self.patrol_busy = True
                    try:
                        await self.patrol()
                    finally:
                        self.patrol_busy = False
            except Exception:
                log.exception("patrol failed")

    async def on_sent(self, to: str, text: str) -> None:
        if to == self.azathoth_id:
            self.reported = True

    async def call_tool(self, name: str, args: dict):
        if name == "temple_latest_result":
            return await self.link.call("latest_result", agent_id=args["agent_id"])
        if name == "temple_task_state":
            return await self.link.call("task_state", task_id=args["task_id"])
        return await super().call_tool(name, args)

    async def patrol(self) -> None:
        started = time.time()
        log.info("patrol started at=%.3f", started)
        try:
            await self._patrol()
        finally:
            log.info("patrol ended start=%.3f end=%.3f", started, time.time())

    async def _patrol(self) -> None:
        scan = await self.link.call("patrol")
        snapshot, tasks = scan["agents"], scan["tasks"]
        log.info("patrol inputs %s", json.dumps([{
            'task_id': t['task']['id'], 'reminders': {aid: r.get('phase') for aid, r in t['reminders'].items()},
            'active': t['active'], 'pending': t['pending'], 'owner_blocked': t['owner_blocked'],
            'executors': [{k: e.get(k) for k in ('agent_id', 'state', 'last_task', 'finished_at', 'last_activity_at')}
                          for e in t['executors']]} for t in tasks], ensure_ascii=False))
        if not snapshot and not tasks:
            log.info("patrol suppressed: empty snapshot")
            return
        ghroth = next((s for s in snapshot if s.get("role") == Role.GHROTH), None)
        ghroth_busy = bool(ghroth and ghroth.get("state") == "working")
        # Volatile fields excluded so an idle universe does not cost a model call.
        stable = [{k: s.get(k) for k in ("agent_id", "state", "online", "finished_at", "last_status", "queued")}
                  for s in snapshot]
        stable_tasks = [(t['task']['id'], t['reminders']) for t in tasks]
        fingerprint = hashlib.sha256(json.dumps([stable, stable_tasks], sort_keys=True).encode()).hexdigest()
        busy = any(s.get("state") == "working" for s in snapshot)
        if not tasks and not ghroth_busy and fingerprint == self.last_fingerprint and \
                not ((busy or tasks) and time.time() - self.last_patrol > 3 * self.patrol_interval):
            log.info("patrol model suppressed: unchanged fingerprint; deadline scan already ran")
            return
        self.last_fingerprint, self.last_patrol = fingerprint, time.time()
        # Mark turns that finished since the previous patrol, so new results are reported exactly once.
        for snap in snapshot:
            done = snap.get("finished_at")
            snap["new_result"] = bool(done) and self.seen_results.get(snap["agent_id"]) != done
            if done:
                self.seen_results[snap["agent_id"]] = done
        self.seen_path.write_text(json.dumps(self.seen_results))
        # Ghroth answers no one, so while it works (or when it has just finished) azathoth must hear from yog
        # on every patrol; the program enforces it.
        must_report = bool(ghroth and (ghroth_busy or ghroth.get("new_result")))
        d = await self.link.call("directory")
        self.azathoth_id = next((a["id"] for a in d["agents"] if a["role"] == Role.AZATHOTH), self.azathoth_id)
        self.ghroth_task = ghroth.get("current_task") if ghroth_busy else None
        # Waiting tasks use exactly the same public send as model-authored reminders.
        from .continuation import INSPECT
        for row in tasks:
            for target, rec in row['reminders'].items():
                if rec.get('phase') == 'waiting' and any(a['id'] == target and a['role'] == Role.SHUB for a in d['agents']):
                    await self.send(target, INSPECT, task_id=row['task']['id'])
        self.reported = False
        rule = ("本次巡检中格赫罗斯" + ("正在工作" if ghroth_busy else "刚完成了新结果") +
                "：无论是否正常，都必须给阿撒托斯发一条消息，用一两句话说明它在做什么、做到哪一步、是否正常"
                "（已工作多久见 working_since）。" if must_report else "")
        rt = self.runtime
        thread = await rt.start_thread(await self.instructions(), self.tools(), self.call_tool, ephemeral=True)
        try:
            prompt = (f"【定时巡检 {time.strftime('%Y-%m-%d %H:%M')}】以下是所有旧日支配者与格赫罗斯的 latest_result：\n"
                      f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=1)}\n```\n"
                      f"未完成任务（最新指令、关联执行者、续办记录）：\n{json.dumps(tasks, ensure_ascii=False, indent=1)}\n"
                      f"实时部署注册表（本次中心采样，优先于旧last_reply和产物中的待部署描述）：{json.dumps(scan.get('deployment', {}), ensure_ascii=False)}\n"
                      f"最近的巡检结论：\n{await self.recent_patrols()}\n\n"
                      "逐任务检查：等待收证或具备续办条件时，用temple_send(to=黑山羊ID,task_id,text)请其核实已有工作并推进或收尾。"
                      "已有在途收证复用；程序每次五分钟巡检通过同一send继续检查等待任务。不要只凭agent空闲推断后台停止，不重启正常工作。"
                      "执行中具体责任worker有明确停滞证据时，也用temple_send直接提醒该worker，带task_id和证据；正常后台进展不发送催办。"
                      "suppressed是去重或状态保护，表示本次未发送，不可报告为送达。程序在15分钟无收证完成或worker响应时升级阿撒托斯。"
                      "真正需要主人决定的阻塞向阿撒托斯报告，并由黑山羊按既有blocked状态上报，不当作已批准重试。"
                      "报告必须区分本次实际收证与复用的采样时间；旧样本不是实时进度。"
                      "收证完成仍未结束的任务在下一次巡检继续检查；完成则交黑山羊验收并向上汇报。"
                      "新收证结果须确认黑山羊已向上报告；若尚未报告，带task_id向阿撒托斯报告实际结果。已送达的同一结果不重复上报。"
                      f"{rule}除此之外一切正常就什么都不发。发给其他 agent 的消息用英文。最后用一两句中文写下本次巡检结论。")
            r = await self.run(thread, prompt, track=False, timeout=1800, runtime=rt)
            if r.status == "failed" and getattr(rt, "retry_safe", True) and self.backends.trip():
                await rt.drop_thread(thread)
                rt = self.runtime
                thread = await rt.start_thread(await self.instructions(), self.tools(), self.call_tool, ephemeral=True)
                r = await self.run(thread, prompt, track=False, timeout=1800, runtime=rt)
            if must_report and not self.reported and r.status == "completed":
                r2 = await self.run(thread, "【程序提示】你还没有给阿撒托斯发格赫罗斯的情况。现在用 temple_send 发一条（英文，一两句话）。",
                                    track=False, timeout=600, runtime=rt)
                if r2.status != "completed":
                    log.warning("yog follow-up report turn failed: %s", r2.error)
            if must_report and not self.reported:
                await self.send(self.azathoth_id, self.ghroth_fallback(ghroth))
            self.latest = {"last_patrol": clip(r.text, 1500), "finished_at": time.time()}
            log.info("patrol model result status=%s reported=%s", r.status, self.reported)
            await self.link.call("patrol_log", summary=r.text or r.error or "")
        finally:
            self.ghroth_task = None
            await rt.drop_thread(thread)

    async def send(self, to: str, text: str, priority: int = 0, kind: str = "message", **task) -> dict:
        if to == self.azathoth_id and self.ghroth_task and not task.get("task_id"):
            task["task_id"] = self.ghroth_task
        return await super().send(to, text, priority, kind, **task)

    @staticmethod
    def ghroth_fallback(g: dict) -> str:
        """Program-written report when the model did not send one."""
        if g.get("state") == "working":
            mins = int((time.time() - (g.get("working_since") or time.time())) / 60)
            steps = "; ".join((g.get("live") or [])[-3:]) or "no tool activity yet"
            return f"[auto] Ghroth is working ({mins} min) on: {g.get('current') or '?'} Recent activity: {steps}"
        return f"[auto] Ghroth finished a turn ({g.get('last_status')}): {clip(g.get('last_reply') or '', 600)}"

    async def recent_patrols(self) -> str:
        rows = await self.link.call("patrol_log_recent")
        return "\n".join(f"- {time.strftime('%H:%M', time.localtime(p['created_at']))} {clip(p['summary'], 300)}"
                         for p in rows) or "（无）"


class Ghroth(OwnerChat, MemoryAgent):
    role = Role.GHROTH
    memory_sources = {OWNER}
    memory_brief = "刚才是你和主人的对话。提取主人对 harness 维护的要求、决定、约束和偏好，以及你们达成的结论。"

    def tools(self) -> list[dict]:
        # Ghroth talks to no one; its only channel is asking azathoth to approve a deploy.
        return [
            tool("temple_deploy_request", "请求阿撒托斯批准一键部署：把仓库 main 分支最新版本部署到中心神殿和各星系。"
                 "先把修改提交并 push 到 GitHub。阿撒托斯批准后由神殿执行（排空、更新、重启、确认、失败回滚）。",
                 {"galaxies": {"type": "array", "items": STR, "description": "星系 ID；不填表示全部"},
                  "reason": {"type": "string", "description": "这次部署包含什么改动、为什么需要部署"},
                  "task_id": STR}, ["reason"]),
            tool("temple_deploy_status", "查看各星系当前代码版本、是否支持远程部署，以及最近的部署记录和进度。"),
        ]

    async def call_tool(self, name: str, args: dict):
        if name == "temple_deploy_request":
            return await self.link.call("deploy_request", **{k: args[k] for k in ("galaxies", "reason", "task_id") if k in args})
        if name == "temple_deploy_status":
            return await self.link.call("deploy_status")
        return await super().call_tool(name, args)


class OldOne(Agent):
    role = Role.OLD_ONE

    def take_batch(self, ready=None):
        candidates = self.queue if ready is None else ready
        first = min(candidates, key=lambda m: (-m['priority'], m['created_at']))
        return super().take_batch([m for m in candidates if m.get('task_id') == first.get('task_id')])

    async def send(self, to, text, priority=0, kind="message", **task):
        if self.current_task and not task.get('task_id'):
            task['task_id'] = self.current_task
        return await super().send(to, text, priority, kind, **task)

    def skill(self) -> str:
        return (load_role("old_one") + f"\n\n# 黑山羊（ID shub）为你设定的职责\n{self.spec.get('skill', '')}\n\n"
                f"# 你的初始任务\n{self.spec.get('task', '')}")

    async def on_ready(self) -> None:
        if self.thread_id is None:
            # A fresh old_one starts on its task without waiting for a message.
            self.enqueue({"id": f"start-{self.id}", "sender": "system", "sender_name": "中心神殿", "sender_role": "system",
                          "task_id": self.spec.get("task_id"),
                          "priority": 0, "created_at": time.time(), "text": "你刚被创建。开始执行你的初始任务。"})


CLASSES = {c.role: c for c in (Azathoth, Shub, Yog, Ghroth, OldOne)}
