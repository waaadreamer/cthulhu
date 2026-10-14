"""Galaxy AI: stateless judgments for the hub (design §4.3).

Not an agent: no thread, no memory. Each call is one Messages request whose whole
context the hub passes in, answered with one JSON object. Every call has a
program fallback in the hub, so a failure here never stalls a task.

Requests go through the temple's Galaxy API broker (provider galaxy, "messages"
protocol), or, when the galaxy config names an `anthropic` endpoint, straight to it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT = 60
DEFAULT_MODEL = "claude-haiku-5-5"

CHOOSE = ("你是任务派发程序的一个判断函数。给定新任务和若干空闲的旧日支配者（每个附最近做过的任务），"
          "判断是否有一个的已有上下文对新任务有明显帮助（同一仓库、同一系统、同一条工作线）。"
          "只有明显相关才复用，否则新建。输出 {\"reuse\": \"<agent id>\" 或 null, \"reason\": \"一句中文理由\"}。")
PROBE = ("你是任务纪律程序的一个判断函数。旧日支配者为任务登记了进度探针：一条 shell 命令，输出单个数字，数字增长代表进展。"
         "判断它是否真实度量了朝任务目标的进展（例如产物文件大小、结果行数、提交数、训练步数），而不是与任务无关、"
         "可以不干活就增长（如 date、随机数、自己往文件里追加无意义内容）、或永远不会增长的数字。"
         "输出 {\"ok\": true/false, \"reason\": \"一句中文理由\"}。")
PROBE_UPDATE = (PROBE + "\n这是一次换阶段：旧探针已在用，执行者申请换新探针。还要判断换阶段是否真实："
                "上一阶段确实做完或确实转向，而不是旧探针停滞后想换一个逃避催办。"
                "输出 {\"ok\": true/false, \"stage_change_genuine\": true/false, \"reason\": \"一句中文理由\"}。"
                "两者都为 true 才算通过。")
PROGRESS = ("你是任务纪律程序的一个判断函数。根据探针样本和执行者这一期的工作记录，判断进度并写一句话总结。"
            "pace：none（数值没有增长）、slow（有增长但按任务规模明显太慢或在原地打转）、ok。"
            "summary：一到两句中文，写这一期做了什么、到了哪一步，给阿撒托斯看。"
            "note_to_azathoth：需要阿撒托斯注意的事，没有就空字符串。"
            "probe_stale：执行者显然已进入下一阶段、当前探针不再反映进展时为 true。"
            "输出 {\"pace\": \"none|slow|ok\", \"summary\": \"...\", \"note_to_azathoth\": \"...\", \"probe_stale\": false}。")
HANDOVER = ("你是交接程序的一个函数。前任旧日支配者被替换，给出交接摘要，供接任者接续。"
            "输出 {\"done\": \"已经完成的工作\", \"artifacts\": \"产物路径与位置\", \"stuck_on\": \"卡在哪里\", "
            "\"next_steps\": \"建议的下一步\"}，均为中文。")


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return data


class GalaxyAI:
    def __init__(self, link, config: dict):
        self.link = link
        cfg = config.get("galaxy_ai") or {}
        self.provider = cfg.get("provider")
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.anthropic = cfg.get("anthropic")        # {"base_url": ..., "api_key_env": ...}
        self.timeout = float(cfg.get("timeout", TIMEOUT))

    async def ask(self, system: str, payload: dict) -> dict:
        """One request, retried once; raises if both fail (the caller falls back)."""
        content = json.dumps(payload, ensure_ascii=False)
        last = None
        for _ in range(2):
            try:
                text = await asyncio.wait_for(self.complete(system, content), self.timeout)
                return extract_json(text)
            except Exception as e:
                last = e
                log.warning("galaxy AI call failed: %s", e)
        raise RuntimeError(f"galaxy AI unavailable: {last}")

    async def complete(self, system: str, content: str) -> str:
        if os.environ.get("CTHULHU_FAKE_GALAXY_AI"):
            from tests.fake_galaxy_ai import reply
            return reply(system, content)
        body = {"model": self.model, "max_tokens": 1024, "system": system + "\n只输出一个 JSON 对象，不要任何其他文字。",
                "messages": [{"role": "user", "content": content}]}
        if self.anthropic:
            return await asyncio.to_thread(self.direct, body)
        if not self.provider:
            raise RuntimeError("no galaxy AI provider configured")
        return await self.via_broker(body)

    def direct(self, body: dict) -> str:
        key = os.environ.get(self.anthropic.get("api_key_env", "ANTHROPIC_API_KEY"), "")
        req = urllib.request.Request(self.anthropic["base_url"].rstrip("/") + "/v1/messages",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "x-api-key": key,
                                              "authorization": f"Bearer {key}", "anthropic-version": "2023-06-01",
                                              # Relays commonly reject urllib's default agent with a bare 403.
                                              "user-agent": "cthulhu-galaxy-ai/1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    async def via_broker(self, body: dict) -> str:
        opened = await self.link.call("api_open", provider=self.provider, protocol="messages",
                                      body=body | {"stream": True}, timeout=30)
        pending, text = b"", ""
        try:
            while True:
                part = await self.link.call("api_next", id=opened["id"], timeout=100)
                if part.get("error"):
                    raise RuntimeError(part["error"])
                pending += base64.b64decode(part.get("data", ""))
                while b"\n\n" in pending:
                    block, pending = pending.split(b"\n\n", 1)
                    data = b"\n".join(x[5:].lstrip() for x in block.split(b"\n") if x.startswith(b"data:"))
                    if not data:
                        continue
                    event = json.loads(data)
                    if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                        text += event["delta"]["text"]
                    elif event.get("type") == "error":
                        raise RuntimeError(event.get("error", {}).get("message", "upstream error"))
                if part.get("done"):
                    return text
        finally:
            if self.link.connected.is_set():
                try:
                    await self.link.call("api_cancel", id=opened["id"], timeout=5)
                except Exception:
                    pass

    # the four judgments

    async def choose_executor(self, description: str, candidates: list[dict]) -> dict:
        r = await self.ask(CHOOSE, {"task": description, "candidates": candidates})
        return {"reuse": r.get("reuse") or None, "reason": str(r.get("reason", ""))}

    async def judge_probe(self, payload: dict, update: bool) -> dict:
        r = await self.ask(PROBE_UPDATE if update else PROBE, payload)
        ok = bool(r.get("ok")) and (not update or bool(r.get("stage_change_genuine")))
        return {"ok": ok, "reason": str(r.get("reason", "")), "raw": r}

    async def judge_progress(self, payload: dict) -> dict:
        r = await self.ask(PROGRESS, payload)
        pace = r.get("pace") if r.get("pace") in ("none", "slow", "ok") else "ok"
        return {"pace": pace, "summary": str(r.get("summary", "")), "note_to_azathoth": str(r.get("note_to_azathoth", "")),
                "probe_stale": bool(r.get("probe_stale"))}

    async def summarize_for_handover(self, payload: dict) -> dict:
        r = await self.ask(HANDOVER, payload)
        return {k: str(r.get(k, "")) for k in ("done", "artifacts", "stuck_on", "next_steps")}
