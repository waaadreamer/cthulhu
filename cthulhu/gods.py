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
INT = {"type": "integer"}

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
    tool("temple_messages", "查阅中心神殿的消息历史（你和旧日支配者之间、汇总、汇报）。可按 agent、任务或关键词过滤。",
         {"agent_id": STR, "task_id": STR, "search": {"type": "string", "description": "关键词"}, "limit": INT}),
    tool("list_tasks", "列出任务（绑定表）：状态、执行者、最新一句总结。可按关键词、状态、星系过滤；默认只列在途任务。",
         {"search": STR, "state": STR, "galaxy_id": STR, "all": {"type": "boolean", "description": "包含已结束的任务"},
          "limit": INT}),
    tool("task_view", "查看任务全部明细：探针与样本、检查点、事件时间线、AI 总结、提交记录、相关消息。", {"task_id": STR}, ["task_id"]),
]


async def history_call(link, name: str, args: dict):
    if name == "temple_messages":
        msgs = await link.call("messages", agent_id=args.get("agent_id"), task_id=args.get("task_id"),
                               search=args.get("search"), limit=args.get("limit", 30))
        return [{k: m.get(k) for k in ("id", "sender", "receiver", "task_id", "text", "created_at")} for m in msgs]
    if name == "list_tasks":
        return await link.call("list_tasks", **args)
    if name == "task_view":
        return await link.call("task_view", task_id=args["task_id"])
    return None


class Azathoth(OwnerChat, MemoryAgent):
    """The single standing decision-maker (design §1.1.1). Owner chats are answered in the web chat;
    everything else (digests, reports, review requests) is handled as an internal turn whose outcome
    the owner sees through tell_owner."""
    role = Role.AZATHOTH
    memory_sources = {OWNER}
    memory_brief = "刚才是你和主人的对话。只提取主人表达的意志、偏好、决定、授权、约束，以及主人告诉你的世界的稳定事实。"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reporting: dict | None = None

    async def turn_header(self) -> str:
        try:
            return await self.link.call("azathoth_header", timeout=15)
        except Exception:
            return ""

    def tools(self) -> list[dict]:
        return [
            self.send_tool({"task_id": {"type": "string", "description": "这条消息所属的任务 ID（发给旧日支配者时填）"}}),
            *HISTORY_TOOLS,
            tool("new_task", "把一个任务派到某个星系。星系枢纽会决定复用空闲的旧日支配者还是新建一个，绑定结果会作为消息通知你。"
                 "描述要完整：目标、边界、交付物；验收标准写可核验的完成条件。",
                 {"galaxy": {"type": "string", "description": "星系 ID 或名称"}, "description": STR,
                  "short_name": {"type": "string", "description": "2–32字稳定任务短名，例如部署界面修复；不使用整段提示词"},
                  "acceptance": STR, "cwd": {"type": "string", "description": "工作目录，可选"}}, ["galaxy", "description", "short_name"]),
            tool("close_task", "验收通过：结束任务，执行者被释放、可被复用。", {"task_id": STR, "note": STR}, ["task_id"]),
            tool("continue_task", "验收不通过或需要继续：恢复检查点，并把指令加急发给执行者。", {"task_id": STR, "instruction": STR},
                 ["task_id", "instruction"]),
            tool("cancel_task", "中止任务（任何状态）。执行者当前回合被中断，进程保留可复用。", {"task_id": STR, "reason": STR},
                 ["task_id", "reason"]),
            tool("manage_old_one", "停止 / 恢复 / 删除（须先停止）/ 打断当前回合 一个旧日支配者。",
                 {"agent_id": STR, "action": {"type": "string", "enum": ["stop", "resume", "delete", "interrupt"]}},
                 ["agent_id", "action"]),
            tool("latest_result", "读取某个旧日支配者的最新工作状态与产出。", {"agent_id": STR}, ["agent_id"]),
            tool("temple_owner_history", "检索你和主人的历史聊天。", {"search": STR, "limit": INT}),
            tool("sudo_request", "需要 root 的命令只能走这里：主人在神殿网页上批准后，由程序以 root 执行原文并把结果返回给你。"
                 "等待期间工具会阻塞，最长 30 分钟；被拒或超时会如实返回。理由要写清楚，让主人能直接判断。",
                 {"command": {"type": "string", "description": "完整的 shell 命令，不要带 sudo 前缀"}, "reason": STR,
                  "cwd": STR, "timeout": {"type": "integer", "description": "执行超时秒数，默认 600"}}, ["command", "reason"]),
            tool("temple_deploy", "一键部署：把仓库 main 分支的最新版本部署到中心神殿和各星系。程序先排空、更新代码、重启、确认上线，"
                 "失败自动回滚；结果会作为消息发回给你。",
                 {"galaxies": {"type": "array", "items": STR, "description": "星系 ID 列表；不填或 [\"all\"] 表示全部"},
                  "reason": STR, "recover_job_id": STR, "status_only": {"type": "boolean"}}),
            tool("tell_owner", "只在处理宇宙内部消息（汇总、汇报、审核请求）时使用：在主人的聊天窗口里告诉主人当前情况。"
                 "每十分钟在当前任务的本期过程行补充一句判断和下一步；正常/停滞都要汇报。程序记录已执行操作结果。"
                 "交付主人要的东西时写完整并设 deliverable=true。",
                 {"text": STR, "deliverable": {"type": "boolean"}}, ["text"]),
        ]

    async def call_tool(self, name: str, args: dict):
        if self.reporting and name in ("send", "new_task", "close_task", "continue_task", "cancel_task", "manage_old_one"):
            params = args | {"report_source": self.reporting["source_message_id"]}
            return await self.link.call(name, timeout=180, **params)
        found = await history_call(self.link, name, args)
        if found is not None:
            return found
        if name in ("new_task", "close_task", "continue_task", "cancel_task", "manage_old_one", "latest_result"):
            return await self.link.call(name, timeout=120, **args)
        if name == "sudo_request":
            return await self.link.call("sudo_request", timeout=1800 + int(args.get("timeout") or 600) + 60, **args)
        if name == "temple_deploy":
            if args.get("status_only"):
                return await self.link.call("deploy_status")
            return await self.link.call("deploy", timeout=120, **{k: args[k] for k in ("galaxies", "reason", "recover_job_id") if k in args})
        if name == "temple_owner_history":
            turns = await self.link.call("owner_chat", search=args.get("search"), limit=args.get("limit", 10))
            return [{"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created_at"])),
                     "owner": t["user_text"] or f"（你主动告知：{t.get('origin') or '内部消息'}）",
                     "you": clip(t["reply"], 2000)} for t in turns]
        if name == "tell_owner":
            if self.reporting is None:
                return "现在是主人在和你对话，直接在最终回复里说即可，不需要 tell_owner。"
            await self.tell_owner(args["text"], deliverable=bool(args.get("deliverable")))
            return "已送达主人聊天窗口"
        return await super().call_tool(name, args)

    async def tell_owner(self, text: str, deliverable: bool = False) -> None:
        r = self.reporting or {}
        await self.link.call("notify_owner", text=text, origin=r.get("origin"), task_id=r.get("task_id"),
                             kind="summary" if deliverable else r.get("kind", "progress"), model=self.latest.get("model"),
                             source_message_id=r.get("source_message_id"))
        r["told"] = True

    async def receive(self, batch: list[dict]) -> None:
        owner = [m for m in batch if m["sender"] == OWNER]
        rest = [m for m in batch if m["sender"] != OWNER]
        for m in owner:
            await self.chat(m)
        if rest:
            await self.internal(rest)

    async def internal(self, batch: list[dict]) -> None:
        """A source message owns its response. Never infer a task from a mixed batch."""
        # One digest per task/period; handling separately also keeps late periods independent.
        for message in batch:
            digest = message.get("kind") == "digest"
            self.reporting = {"task_id": message.get("task_id"), "source_message_id": message["id"],
                              "kind": "summary" if message.get("kind") == "task_finished" else "progress",
                              "told": False, "origin": message.get("sender_name") or message["sender"]}
            try:
                text = ("【宇宙内部消息，不是主人发言。只处理下面消息明确所属的任务；无 task_id 是全局事件。"
                        "核实当前有效状态和本期证据，必要时采取管理操作；不复述旧错误或原始提示词。"
                        "十分钟汇总必须 tell_owner，用一句话写本期进展、判断和下一步；程序会把判断和真实操作结果"
                        "补全该任务本期的同一过程行。没有证据/没有操作/操作失败要如实说，不能编造已执行安排。】\n"
                        + self.render([message]))
                result = await self.run_main(text)
                if digest and not self.reporting["told"]:
                    reply = result.text.strip() if result.status == "completed" else ""
                    await self.tell_owner(reply or "总结不可用；管理判断与下一步尚未回传")
                self.complete([message], result)
                if getattr(self, "preempted", False):
                    break
                if self.last_turn:
                    asyncio.create_task(self.distill(*self.last_turn, correct_only=True))
            except Exception:
                # Preserve unhandled source messages in the existing queue on delivery/runtime failure.
                self.preempted = True
                raise
            finally:
                self.reporting = None


OLD_ONE_TOOLS = [
    tool("register_task_probe", "登记（或换阶段时更新）当前任务的进度探针：一条 shell 命令，输出单个数字，数字增长即进展"
         "（如 `wc -l < out.jsonl`、`stat -c %s report.md`、`git -C repo rev-list --count HEAD`）。程序先试跑，再由 AI 判定是否合理。"
         "任务进入新阶段、旧探针不再增长时，用它更新并填写 stage_note。",
         {"command": STR, "rationale": {"type": "string", "description": "这个数字为什么能反映任务进展"},
          "stage_note": {"type": "string", "description": "仅更新时必填：上一阶段做到哪了、为什么换"}}, ["command", "rationale"]),
    tool("finish_task", "任务完成：提交可核验的证据（产物路径、命令输出、提交号），交阿撒托斯审核。检查点随即暂停。",
         {"evidence": STR}, ["evidence"]),
    tool("report", "向阿撒托斯汇报，或回答阿撒托斯的问题。blocker=true 表示卡住、需要它决定。每 10 分钟只能一次"
         "（和找同伴的限流互不影响），一次写完整；回答阿撒托斯发来的消息时填 reply_to（消息 ID），每条免限一次。",
         {"text": STR, "blocker": {"type": "boolean"}, "reply_to": {"type": "string", "description": "被回答的消息 ID"}}, ["text"]),
    tool("list_tasks", "列出全局在途任务及其执行者（找谁问问题用）。"),
    tool("task_view", "查看一个任务的明细（你自己的任务，或其他任务的公开状态）。", {"task_id": STR}, ["task_id"]),
]


class OldOne(Agent):
    role = Role.OLD_ONE
    persistent_thread = True

    def tools(self) -> list[dict]:
        return [tool("send", "给另一个旧日支配者发消息（不能发给阿撒托斯，找它用 report）。同一个对象每 10 分钟只能发一条，"
                     "所以问题要一次写完整：背景、你已经试过什么、你具体要什么答案、期望的格式。"
                     "回复别人发来的消息时填 reply_to，每条来信可免限流回复一次。",
                     {"to": {"type": "string", "description": "旧日支配者 ID"}, "text": STR,
                      "priority": {"type": "integer", "description": "0–8"}, "reply_to": {"type": "string", "description": "被回复的消息 ID"}},
                     ["to", "text"]), *OLD_ONE_TOOLS]

    async def call_tool(self, name: str, args: dict):
        if name == "send":
            return await self.send(args["to"], args["text"], priority=int(args.get("priority") or 0),
                                   **({"reply_to": args["reply_to"]} if args.get("reply_to") else {}))
        if name in ("register_task_probe", "finish_task", "report", "list_tasks", "task_view"):
            return await self.link.call(name, timeout=180, **args)
        raise ValueError(f"unknown tool {name}")

    async def turn_header(self) -> str:
        try:
            return await self.link.call("header", timeout=15)
        except Exception:
            return ""

    def skill(self) -> str:
        return load_role("old_one")


from .nyarl import NyarlBridge


class Nyarlathotep(NyarlBridge, OwnerChat, MemoryAgent):
    pass

CLASSES = {c.role: c for c in (Azathoth, OldOne, Nyarlathotep)}
