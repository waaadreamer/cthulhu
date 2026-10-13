"""Role subclasses: what each agent remembers, which tools it holds, and how
it is woken. All authority is still enforced by the temple; tools here only
shape what the model is offered.
"""
from __future__ import annotations

import asyncio
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
BOOL = {"type": "boolean"}
TASK_ID = {"task_id": STR}
# Task discipline (tasks.py). Each role is offered only the calls the temple lets it make.
TASK = {
    "get": tool("task_get", "读取任务合同、状态、探针历史、延期、事件和消息时间线。", TASK_ID, ["task_id"]),
    "create": tool("task_create", "创建任务并把任务说明发给执行者。程序强制：执行者、验收标准必填，截止默认 120 分钟。",
                   {"executor": {"type": "string", "description": "执行者 agent ID"}, "title": STR,
                    "text": {"type": "string", "description": "任务说明：目标、边界、交付物"},
                    "acceptance": {"type": "string", "description": "验收标准：可核验的完成条件"},
                    "deadline": {"type": "string", "description": "分钟数，或 'YYYY-MM-DD HH:MM'；默认 120"}},
                   ["executor", "title", "text", "acceptance"]),
    "probe": tool("task_probe", "登记本任务的进度探针：一条 shell 命令，输出单个数字，数字增长即进展（如 `wc -l < out.jsonl`、"
                  "`stat -c %s report.md`、`git -C repo rev-list --count HEAD`）。由所在星系每个检查点执行，是唯一的进度证据；"
                  "实际执行后的首检查点前没有有效探针，任务作废、执行者被停止。",
                  {"task_id": STR, "command": STR}, ["task_id", "command"]),
    "extend": tool("task_extend", "申请延期。第 1 次由直接上级批准，第 2 次阿撒托斯，第 3 次只能主人；延长不超过原时长；"
                   "静默工作须提供命令/进程/产物证据与预计完成；manager可代申请但不能自批。",
                   {"task_id": STR, "deadline": {"type": "string", "description": "从现在或截止（取较晚）延长的分钟数，或 'YYYY-MM-DD HH:MM'"},
                    "reason": STR, "evidence": STR, "expected_completion": STR}, ["task_id", "deadline", "reason"]),
    "block": tool("task_block", "任务卡在只有主人能做的决定上：暂停计时并立即通知主人。", {"task_id": STR, "question": STR},
                  ["task_id", "question"]),
    "submit": tool("task_submit", "提交验收：附上可核验的证据（产物路径、命令输出、提交号）。证据提交后冻结。",
                   {"task_id": STR, "evidence": STR}, ["task_id", "evidence"]),
    "extension": tool("task_extension", "批准或拒绝等你决定的延期申请。", {"task_id": STR, "approve": BOOL, "reason": STR},
                      ["task_id", "approve", "reason"]),
    "verdict": tool("task_verdict", "验收裁决：通过则任务关闭；驳回则任务回到进行中，计时不重置。",
                    {"task_id": STR, "passed": BOOL, "reason": STR}, ["task_id", "passed", "reason"]),
    "probe_verdict": tool("task_probe_verdict", "判断探针是否真实度量了朝验收标准的进展。判为无效则任务作废。",
                          {"task_id": STR, "ok": BOOL, "reason": STR}, ["task_id", "ok", "reason"]),
    "unblock": tool("task_unblock", "解除任务的主人阻塞并恢复计时（程序抄送主人）。", {"task_id": STR, "note": STR}, ["task_id", "note"]),
    "replace": tool("task_replace", "命令换人；manager可填executor绑定已有worker，须先清算原执行者。", {"task_id": STR, "reason": STR, "executor": STR}, ["task_id", "reason"]),
    "cancel": tool("task_cancel", "取消有权管理的任务，保留部分证据与历史；仅清理该任务的队列和回合，不停止其他任务或分离进程。",
                   {"task_id": STR, "reason": STR}, ["task_id", "reason"]),
}
EXECUTOR_TOOLS = [TASK[k] for k in ("get", "probe", "extend", "block", "submit")]


async def task_call(link, name: str, args: dict):
    if name in {t["name"] for t in TASK.values()}:
        return await link.call(name, **args)
    return None

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
                r = await self.run_background(fork, prompt, output_schema=MEMORY_SCHEMA, timeout=600, runtime=rt)
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
            r = await self.run_background(fork, prompt, output_schema=MEMORY_SCHEMA, timeout=900, runtime=rt)
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
        if self.preempted:
            return                        # left queued behind the urgent message
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
            if self.preempted and result.status == "interrupted":
                status, reply = "queued", ""     # requeued behind the urgent message
        except Exception as e:
            result, status, reply = TurnResult("failed", error=str(e)), "failed", f"（出错：{e}）"
        self.complete([m], result)
        await self.link.call("chat_update", turn_id=turn_id, reply=reply, status=status, activity=activity[-40:],
                             model=self.latest.get("model"))
        await self.after_turn([m], result)


HISTORY_TOOLS = [
    tool("temple_messages", "查阅中心神殿的消息历史：调查结果、数据、路径等细节都在这里。可按 agent、任务或关键词过滤。",
         {"agent_id": STR, "task_id": STR, "search": {"type": "string", "description": "关键词"},
          "limit": {"type": "integer"}}),
    tool("temple_tasks", "列出任务及其状态（active / blocked_on_owner / submitted / accepted / failed），可按关键词搜索。",
         {"search": STR, "limit": {"type": "integer"}}),
]


async def history_call(link, name: str, args: dict):
    if name == "temple_messages":
        msgs = await link.call("messages", agent_id=args.get("agent_id"), task_id=args.get("task_id"),
                               search=args.get("search"), limit=args.get("limit", 30))
        return [{k: m.get(k) for k in ("id", "sender", "receiver", "task_id", "status", "text", "created_at")} for m in msgs]
    if name == "temple_tasks":
        return await link.call("tasks", search=args.get("search"), limit=args.get("limit", 20))
    return await task_call(link, name, args)


# How a report to the owner is labelled, from the status a manager (or a verdict) attached to its message.
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
            self.send_tool({"task_id": {"type": "string", "description": "跟进已有任务时填它的任务 ID；新工作用 task_create"}}),
            *super().tools()[1:],
            *HISTORY_TOOLS,
            *(TASK[k] for k in ("create", "get", "verdict", "extension", "unblock", "replace", "cancel")),
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
        # Task-discipline notices were already shown to the owner by the program: no forced tell_owner.
        self.reporting = {"task_id": task_id, "kind": kind, "told": all(m.get("kind") == "discipline" for m in batch),
                          "origin": title or "、".join(sorted({m["sender_name"] for m in batch}))}
        guide = {
            "progress": "这是任务过程中的进展。tell_owner 只写一两句话的进展（像思考过程里的一步：做了什么、发现了什么、下一步），不要展开细节。"
                        "例外：如果这条消息交付的正是主人要的东西（报告、答复、结论），就写完整内容并设 deliverable=true，它会作为总结展开显示。",
            "blocked": "任务受阻，需要主人决定。tell_owner 写一份阶段总结，会在界面上展开显示（之前的进展折叠在上面）："
                       "先列出到目前为止已完成的结果和结论，再说清卡在哪里，最后用主人可以直接回答的具体问题结尾。",
            "summary": "这个任务已验收通过。先核对结果是否满足主人的意图，再用 tell_owner 写给主人的完整总结："
                       "结论先行，然后是关键结果、遗留问题；过程不用重复，主人可以展开查看之前的进展。",
            "notice": "这是下属、尤格或神殿的消息（神殿的军纪通知程序已告诉主人，你只需采取行动，不必再 tell_owner）。"
                      "其他消息 tell_owner 用一两句话告诉主人要点；需要主人决定时说清楚。"
                      "如果它交付的正是主人要的东西，就写完整内容并设 deliverable=true。",
        }[kind]
        try:
            text = (f"【以下是宇宙内部的消息，不是主人说的。你可以用 temple_send 回复或继续安排下属，也可以不回复；"
                    f"但必须调用一次 tell_owner 告诉主人。{guide}】\n\n" + self.render(batch))
            result = await self.run_main(text)
            if result.status == "completed" and not self.reporting["told"]:
                result = await self.run_main(f"【程序提示】你还没有调用 tell_owner。现在调用它。{guide}")
            self.complete(batch, result)
            if not self.reporting["told"] and not (self.preempted and result.status == "interrupted"):
                reply = (result.text or "").strip() if result.status == "completed" else \
                    f"（处理{self.reporting['origin']}的消息时出错：{result.error or result.status}）"
                if reply:
                    await self.tell_owner(reply)
            if result.status == "completed" and self.last_turn:
                asyncio.create_task(self.distill(*self.last_turn, correct_only=True))
        finally:
            self.reporting = None


SPAWN_FIELDS = {
    "name": STR,
    "task": {"type": "string", "description": "任务说明：目标、边界、交付物，完成后 task_submit"},
    "skill": {"type": "string", "description": "它的职责设定（system prompt）：它是谁、负责什么领域、工作方式"},
    "title": STR, "acceptance": {"type": "string", "description": "验收标准（新任务必填）"},
    "deadline": {"type": "string", "description": "分钟数或 'YYYY-MM-DD HH:MM'，默认 120"},
    "task_id": {"type": "string", "description": "仅更换执行者时填：接手的已有任务"},
}


class Manager:
    """Mixin for shub and ghroth: they take tasks from azathoth and run their own old_ones."""

    def take_batch(self, ready=None):
        # Keep automatically inherited task IDs unambiguous when several tasks wake Shub together.
        candidates = self.queue if ready is None else ready
        first = min(candidates, key=lambda m: (-m['priority'], m['created_at']))
        return super().take_batch([m for m in candidates if m.get('task_id') == first.get('task_id')])

    def send_tool(self, extra: dict | None = None) -> dict:
        return super().send_tool({
            "task_id": {"type": "string", "description": "这条消息所属的任务 ID"},
            "status": {"type": "string", "enum": ["progress", "blocked"],
                       "description": "给阿撒托斯汇报时的标签。任务只由 task_submit 和验收关闭，不能靠汇报关闭"}})

    def tools(self) -> list[dict]:
        return super().tools() + [
            self.spawn_tool(),
            tool("temple_stop", "终止你自己的一个旧日支配者。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_restart", "重启你自己的一个旧日支配者（保留其上下文）。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_delete", "删除一个已终止的旧日支配者（历史保留）。", {"agent_id": STR}, ["agent_id"]),
            *(TASK[k] for k in ("create", "extension", "replace", "cancel")), *EXECUTOR_TOOLS,
        ]

    async def send(self, to, text, priority=0, kind="message", **task):
        if self.current_task and not task.get("task_id"):
            task["task_id"] = self.current_task
        return await super().send(to, text, priority, kind, **task)

    async def call_tool(self, name: str, args: dict):
        if name == "temple_spawn":
            # A new old_one task hangs under the task this manager is working on.
            return await self.link.call("spawn", **args, **({"parent_task_id": self.current_task}
                                                            if self.current_task and not args.get("task_id") else {}))
        if name in ("temple_stop", "temple_restart", "temple_delete"):
            return await self.link.call(name.removeprefix("temple_"), agent_id=args["agent_id"])
        found = await task_call(self.link, name, args)
        return found if found is not None else await super().call_tool(name, args)


class Shub(Manager, MemoryAgent):
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

    async def receive(self, batch: list[dict]) -> None:
        # The roster is part of shub's permanent context; refresh it whenever it changed.
        text = self.render(batch)
        roster = await self.roster()
        if roster != self.roster_seen:
            self.roster_seen = roster
            text = f"【名册已更新】\n{roster}\n\n{text}"
        result = await self.run_main(text)
        self.complete(batch, result)
        await self.after_turn(batch, result)

    async def roster(self) -> str:
        d = await self.link.call("directory")
        olds = [a for a in d["agents"] if a["role"] == Role.OLD_ONE and a["desired"] == "running" and a["parent_id"] == self.id]
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
        return super().tools() + HISTORY_TOOLS

    def spawn_tool(self) -> dict:
        return tool("temple_spawn", "在指定星系创建一个旧日支配者并同时建立它的任务合同；它会立即按 task 开始工作。"
                    "更换执行者时填已有 task_id（不建新任务）。", SPAWN_FIELDS | {
                        "galaxy_id": STR, "cwd": {"type": "string", "description": "工作目录，可选"}},
                    ["name", "galaxy_id", "task", "skill"])

    async def call_tool(self, name: str, args: dict):
        found = await history_call(self.link, name, args)
        return found if found is not None else await super().call_tool(name, args)


class Yog(MemoryAgent):
    """Independent QA. Woken only by the temple's task discipline: probe reviews, acceptance reviews and
    diagnoses of escalated tasks. It decides nothing in the escalation ladder."""
    role = Role.YOG
    memory_sources = {Role.AZATHOTH}
    correction_sources = {Role.OLD_ONE}
    memory_brief = "刚才是你和阿撒托斯的交流。只提取阿撒托斯对质检工作定下的长期要求和关注点；具体某次的审查不记。"

    def tools(self) -> list[dict]:
        return super().tools() + [
            tool("temple_latest_result", "远程读取某个 agent 的最新工作状态与产出。", {"agent_id": STR}, ["agent_id"]),
            *(TASK[k] for k in ("get", "probe_verdict", "verdict")),
        ]

    def send_tool(self, extra=None):
        return super().send_tool({"task_id": STR})

    async def call_tool(self, name: str, args: dict):
        if name == "temple_latest_result":
            return await self.link.call("latest_result", agent_id=args["agent_id"])
        found = await task_call(self.link, name, args)
        return found if found is not None else await super().call_tool(name, args)

    async def receive(self, batch: list[dict]) -> None:
        # Each review turn's final reply is its Chinese conclusion, kept as the patrol log the owner reads.
        result = await self.run_main(self.render(batch))
        self.complete(batch, result)
        if result.status == "completed" and (result.text or "").strip():
            await self.link.call("patrol_log", summary=result.text.strip())
        await self.after_turn(batch, result)


class Ghroth(Manager, OwnerChat, MemoryAgent):
    role = Role.GHROTH
    # Azathoth carries the owner's will, so its standing rules and feedback count, as for Shub and Yog.
    memory_sources = {OWNER, Role.AZATHOTH}
    memory_brief = ("刚才是你和主人或阿撒托斯的交流。提取主人对 harness 维护的要求、决定、约束和偏好，以及你们达成的结论；"
                    "阿撒托斯的消息只提取它转达或定下的长期规则、经验教训和应保持的做法，具体任务的内容不记。")

    def tools(self) -> list[dict]:
        # Maintenance can answer Nyarl only within an explicitly associated task.
        return super().tools() + [
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

    def spawn_tool(self) -> dict:
        return tool("temple_spawn", "创建一个旧日支配者做 harness 工作并同时建立任务合同。它在你的星系上、在 harness 仓库自己的 "
                    "git worktree（分支 old/<ID>）里工作，不能推送或部署；由你审查合并。", SPAWN_FIELDS, ["name", "task", "skill"])


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

    def tools(self) -> list[dict]:
        return super().tools() + EXECUTOR_TOOLS

    async def call_tool(self, name: str, args: dict):
        found = await task_call(self.link, name, args)
        return found if found is not None else await super().call_tool(name, args)

    def skill(self) -> str:
        deadline = self.spec.get("deadline")
        return (load_role("old_one") + f"\n\n# 你的管理者为你设定的职责\n{self.spec.get('skill', '')}\n\n"
                f"# 你的任务合同\n任务 ID：{self.spec.get('task_id')}\n"
                f"截止：{time.strftime('%Y-%m-%d %H:%M', time.localtime(deadline)) if deadline else '见 task_get'}\n"
                f"验收标准：{self.spec.get('acceptance', '')}\n\n# 你的初始任务\n{self.spec.get('task', '')}")

    async def on_ready(self) -> None:
        if self.thread_id is None:
            # A fresh old_one starts on its task without waiting for a message.
            self.enqueue({"id": f"start-{self.id}", "sender": "system", "sender_name": "中心神殿", "sender_role": "system",
                          "task_id": self.spec.get("task_id"),
                          "priority": 0, "created_at": time.time(),
                          "text": "你刚被创建。先用 task_probe 登记进度探针（第一个检查点前没有就作废），然后开始执行你的初始任务。"})


from .nyarl import NyarlBridge


class Nyarlathotep(NyarlBridge, OwnerChat, MemoryAgent):
    pass

CLASSES = {c.role: c for c in (Azathoth, Shub, Yog, Ghroth, OldOne, Nyarlathotep)}
