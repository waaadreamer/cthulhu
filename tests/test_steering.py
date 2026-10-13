"""Exercise actual agent/native turn methods with controlled RPC acknowledgement order."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cthulhu import backends
from cthulhu.agent import Agent
from cthulhu.claude_runtime import ClaudeRuntime
from cthulhu.roles import OWNER, Role
from cthulhu.runtime import CodexRuntime, RuntimeError_


class RecordingLink:
    def __init__(self):
        self.logs = []
        self.handled = []
        self.handled_events = asyncio.Queue()

    async def call(self, method, **params):
        if method == "memory":
            return []
        if method == "turn_log":
            return list(self.logs)
        if method == "log_turn":
            self.logs.append(params | {"created_at": time.time()})
        elif method == "handled":
            self.handled.append(params["id"])
            self.handled_events.put_nowait(params["id"])
        return {}

    async def emit(self, frame):
        pass


class ControlledRuntime(CodexRuntime):
    """Only the app-server RPC is fictional; turn, steering and checkpoint code is real."""
    def __init__(self, cwd):
        super().__init__("unused", cwd, model="fixture")
        self.started = asyncio.Queue()
        self.steering = asyncio.Queue()
        self.requests = []
        self.writes = []
        self.history = {}
        self.running = {}
        self.start_gate = None
        self.next_turn = 0

    @property
    def alive(self):
        return True

    async def start(self):
        pass

    async def _write(self, frame):
        self.writes.append(frame)

    async def request(self, method, params, timeout=120):
        self.requests.append((method, params, timeout))
        if method == "turn/start":
            self.next_turn += 1
            turn = f"turn-{self.next_turn}"
            call = {"id": turn, "thread": params["threadId"], "text": params["input"][0]["text"]}
            self.running[turn] = call
            self.history.setdefault(call["thread"], []).append({"id": turn, "status": "running", "items": []})
            self.started.put_nowait(call)
            if self.start_gate is not None:
                gate, self.start_gate = self.start_gate, None
                await gate
            return {"turn": {"id": turn}}
        if method == "turn/steer":
            ack = asyncio.get_running_loop().create_future()
            self.steering.put_nowait({"turn": params["expectedTurnId"], "thread": params["threadId"],
                                     "text": params["input"][0]["text"], "ack": ack})
            return await ack
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": self.history.get(params["threadId"], [])}}
        raise AssertionError(f"unexpected runtime RPC: {method}")

    def complete(self, turn, status="completed", text="done", error=None, items=()):
        thread = self.running[turn]["thread"]
        completed = list(items)
        if text:
            completed.append({"id": "answer", "type": "agentMessage", "text": text})
        row = next(row for row in self.history[thread] if row["id"] == turn)
        row.update(status=status, items=completed)
        for item in completed:
            self._notify("item/completed", {"threadId": thread, "turnId": turn, "item": item})
        self._notify("turn/completed", {"threadId": thread,
            "turn": {"id": turn, "status": status, "error": {"message": error} if error else None}})


class SteeringAgent(Agent):
    role = Role.GHROTH


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ControlledRuntime(self.tmp.name)
        self.link = RecordingLink()
        with patch("cthulhu.agent.Link", return_value=self.link), patch("cthulhu.backends.build", return_value=self.rt):
            self.agent = SteeringAgent("unused", {"id": "gh", "name": "fixture", "token": "fixture"},
                "unused", self.tmp.name, Path(self.tmp.name), runtime={"backend": "codex", "model": "fixture"})
        self.agent.threads = {"codex:fixture": "main"}
        self.agent.loaded = {"main"}
        self.agent.memory_seen = {"main": {}}
        self.jobs = []

    async def asyncTearDown(self):
        for job in self.jobs:
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.tmp.cleanup()

    def message(self, mid="correction", text="STOP_NEW_WRITES", **kw):
        return {"id": mid, "text": text, "sender": "az", "sender_name": "Azathoth",
                "sender_role": "azathoth", "priority": 5, "created_at": 1,
                "kind": "message", "task_id": "task-a", **kw}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.jobs.append(task)
        return task

    async def next_event(self, queue):
        return await asyncio.wait_for(queue.get(), 2)

    def set_batch(self, first=None, task="task-a"):
        self.agent.batch = [first or self.message("first", "ORIGINAL_WORK", task_id=task)]
        self.agent.in_batch = True
        self.agent.current_task = task

    async def start_main(self, first=None, task="task-a"):
        self.set_batch(first, task)
        main = self.spawn(self.agent.run_main(self.agent.render(self.agent.batch)))
        return main, await self.next_event(self.rt.started)

    async def dispatch(self, message=None):
        message = message or self.message()
        self.agent.queue.append(message)
        steering = self.spawn(self.agent.steer(message))
        rpc = await self.next_event(self.rt.steering)
        return message, steering, rpc

    async def test_guards_leave_unrelated_or_unsupported_messages_queued(self):
        claude = ClaudeRuntime(self.tmp.name, model="claude-opus-5-5")
        cases = [
            {"name": "different task", "incoming": {"task_id": "task-b"}},
            {"name": "untagged incoming", "incoming": {"task_id": None}},
            {"name": "tagged incoming to untagged turn", "task": None},
            {"name": "incoming chat", "incoming": {"kind": "chat"}},
            {"name": "incoming owner", "incoming": {"sender": OWNER}},
            {"name": "current chat", "first": {"kind": "chat"}},
            {"name": "current owner", "first": {"sender": OWNER}},
            {"name": "mixed task batch", "extra": self.message("other-task", task_id="task-b")},
            {"name": "mixed chat batch", "extra": self.message("other-chat", kind="chat")},
            {"name": "scoped thread", "thread": "feishu-scope"},
            {"name": "background has no tracked turn", "inactive": True},
            {"name": "draining", "draining": True},
            {"name": "guided owner reporting", "reporting": {"kind": "progress"}},
            {"name": "Claude has no steer", "runtime": claude},
            {"name": "empty batch", "empty": True},
            {"name": "no batch", "no_batch": True},
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                task = case.get("task", "task-a")
                first = self.message("first", "ORIGINAL_WORK", task_id=task, **case.get("first", {}))
                self.set_batch(first, task)
                if case.get("extra"):
                    self.agent.batch.append(case["extra"])
                if case.get("empty"):
                    self.agent.batch = []
                if case.get("no_batch"):
                    self.agent.batch = None
                message = self.message(**case.get("incoming", {}))
                self.agent.queue = [message]
                self.agent.draining = case.get("draining", False)
                self.agent.reporting = case.get("reporting")
                self.agent.active = None if case.get("inactive") else (
                    case.get("runtime", self.rt), case.get("thread", "main"), "turn")
                self.assertFalse(await self.agent.steer(message))
                self.assertEqual(self.agent.queue, [message])
                self.assertEqual(self.rt.requests, [])
                self.assertEqual(self.link.handled, [])

    async def test_accepted_message_logged_handed_off_and_handled_once(self):
        original = self.message("first", "ORIGINAL_HEAD " + "x" * 3000)
        self.agent.enqueue(original)
        loop = self.spawn(self.agent.loop())
        first = await self.next_event(self.rt.started)
        correction = self.message()
        self.agent.enqueue(correction)
        rpc = await self.next_event(self.rt.steering)
        self.assertEqual(rpc["turn"], first["id"])
        self.assertIn(correction, self.agent.queue)
        self.assertEqual(self.link.handled, [])
        rpc["ack"].set_result({})
        await asyncio.sleep(0)
        self.rt.complete(first["id"])
        self.assertEqual(await self.next_event(self.link.handled_events), "first")
        self.assertEqual(await self.next_event(self.link.handled_events), "correction")
        self.assertEqual(self.link.handled, ["first", "correction"])
        self.assertEqual(len(self.link.logs), 1)
        self.assertEqual(self.link.logs[0]["input"].count("STOP_NEW_WRITES"), 1)
        bridge = await self.agent.handoff_context("new-backend-thread")
        self.assertIn("ORIGINAL_HEAD", bridge)
        self.assertIn("STOP_NEW_WRITES", bridge)
        self.assertLess(len(bridge), 2100)
        self.assertEqual(self.agent.queue, [])
        self.assertTrue(self.rt.started.empty())
        self.assertFalse(loop.done())
        self.assertEqual(next(timeout for method, _, timeout in self.rt.requests if method == "turn/steer"), 10)

    async def test_delayed_accept_after_completion_blocks_log_and_handling_until_ack(self):
        self.agent.enqueue(self.message("first", "ORIGINAL_WORK"))
        self.spawn(self.agent.loop())
        first = await self.next_event(self.rt.started)
        correction = self.message()
        self.agent.enqueue(correction)
        rpc = await self.next_event(self.rt.steering)
        self.rt.complete(first["id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.link.logs, [])
        self.assertEqual(self.link.handled, [])
        self.assertEqual(self.agent.queue, [correction])
        rpc["ack"].set_result({})
        self.assertEqual(await self.next_event(self.link.handled_events), "first")
        self.assertEqual(await self.next_event(self.link.handled_events), "correction")
        self.assertIn("STOP_NEW_WRITES", self.link.logs[0]["input"])
        self.assertEqual(self.agent.queue, [])

    async def test_rejection_never_consumes_or_logs_message(self):
        main, first = await self.start_main()
        correction, steering, rpc = await self.dispatch()
        rpc["ack"].set_exception(RuntimeError_("expectedTurnId is not the active turn"))
        self.assertFalse(await steering)
        self.assertEqual(self.agent.queue, [correction])
        self.assertEqual([m["id"] for m in self.agent.batch], ["first"])
        self.rt.complete(first["id"])
        self.assertEqual((await main).status, "completed")
        self.assertNotIn("STOP_NEW_WRITES", self.link.logs[0]["input"])
        self.assertEqual(self.link.handled, [])
        self.assertEqual(self.agent.take_batch(), [correction])

    async def test_rejection_after_completion_stays_queued_for_next_batch(self):
        main, first = await self.start_main()
        correction, steering, rpc = await self.dispatch()
        self.rt.complete(first["id"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(main.done())
        self.assertEqual(self.link.logs, [])
        rpc["ack"].set_exception(RuntimeError_("expectedTurnId is not the active turn"))
        self.assertFalse(await steering)
        self.assertEqual((await main).status, "completed")
        self.assertEqual(self.agent.queue, [correction])
        self.assertNotIn("STOP_NEW_WRITES", self.link.logs[0]["input"])
        self.assertEqual(self.link.handled, [])

    async def test_rejected_failed_turn_reoffers_message_to_retry_without_loss(self):
        with patch.object(backends, "RETRY_DELAYS", (0,)):
            main, first = await self.start_main()
            correction, steering, rpc = await self.dispatch()
            self.rt.complete(first["id"], status="failed", text="", error="invalid_stream")
            await asyncio.sleep(0)
            self.assertEqual(self.agent.queue, [correction])
            rpc["ack"].set_exception(RuntimeError_("expectedTurnId is not the active turn"))
            self.assertFalse(await steering)
            retry = await self.next_event(self.rt.started)
            retry_rpc = await self.next_event(self.rt.steering)
            self.assertEqual(retry_rpc["turn"], retry["id"])
            self.assertEqual(self.agent.queue, [correction])
            self.assertNotIn("STOP_NEW_WRITES", self.link.logs[0]["input"])
            self.assertEqual(self.link.handled, [])
            retry_rpc["ack"].set_result({})
            await asyncio.sleep(0)
            self.rt.complete(retry["id"])
            self.assertEqual((await main).status, "completed")
        self.assertEqual(self.agent.queue, [])
        self.assertEqual([m["id"] for m in self.agent.batch], ["first", "correction"])
        self.assertEqual(self.link.logs[1]["input"].count("STOP_NEW_WRITES"), 1)

    async def test_duplicate_dispatch_sends_one_rpc_and_appends_message_once(self):
        main, first = await self.start_main()
        correction, steering, rpc = await self.dispatch()
        duplicate = self.spawn(self.agent.steer(correction))
        await asyncio.sleep(0)
        self.assertEqual(self.agent.queue, [correction])
        self.assertTrue(self.rt.steering.empty())
        rpc["ack"].set_result({})
        self.assertEqual(await asyncio.gather(steering, duplicate), [True, False])
        self.rt.complete(first["id"])
        self.assertEqual((await main).status, "completed")
        self.assertEqual([m["id"] for m in self.agent.batch], ["first", "correction"])
        self.assertEqual(self.link.logs[0]["input"].count("STOP_NEW_WRITES"), 1)
        self.assertEqual(len([m for m, _, _ in self.rt.requests if m == "turn/steer"]), 1)

    async def test_delayed_accept_after_failure_is_retained_in_retry_with_existing_effect(self):
        effects = []
        async def write_effect(name, args):
            effects.append("effect-written-once")
            return effects[-1]
        self.rt.tool_handlers["main"] = write_effect
        with patch.object(backends, "RETRY_DELAYS", (0,)):
            main, first = await self.start_main()
            await self.rt._serve({"id": 1, "method": "item/tool/call", "params": {
                "threadId": "main", "tool": "write_effect", "arguments": {}}})
            correction, steering, rpc = await self.dispatch()
            effect = {"id": "effect", "type": "dynamicToolCall", "tool": "write_effect",
                      "arguments": {}, "result": "effect-written-once", "status": "completed"}
            self.rt.complete(first["id"], status="failed", text="", error="invalid_stream", items=[effect])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertFalse(main.done())
            self.assertTrue(self.rt.started.empty())
            self.assertEqual(self.link.logs, [])
            rpc["ack"].set_result({})
            self.assertTrue(await steering)
            retry = await self.next_event(self.rt.started)
            self.assertIn("effect-written-once", retry["text"])
            self.assertEqual(retry["text"].split("</recovery>")[-1].count("STOP_NEW_WRITES"), 1)
            self.assertEqual(self.link.logs[0]["input"].count("STOP_NEW_WRITES"), 1)
            self.rt.complete(retry["id"])
            self.assertEqual((await main).status, "completed")
        self.assertEqual(effects, ["effect-written-once"])
        self.assertEqual([m["id"] for m in self.agent.batch], ["first", "correction"])
        self.assertEqual(self.agent.queue, [])
        self.assertEqual(len(self.link.logs), 2)
        self.assertTrue(all(row["input"].count("STOP_NEW_WRITES") == 1 for row in self.link.logs))

    async def test_arrival_before_turn_start_is_steered_once_when_turn_starts(self):
        gate = asyncio.get_running_loop().create_future()
        self.rt.start_gate = gate
        main, first = await self.start_main()
        self.assertIsNone(self.agent.active)
        correction = self.message()
        self.agent.enqueue(correction)
        await asyncio.sleep(0)
        self.assertEqual(self.agent.queue, [correction])
        self.assertTrue(self.rt.steering.empty())
        gate.set_result(None)
        rpc = await self.next_event(self.rt.steering)
        rpc["ack"].set_result({})
        await asyncio.sleep(0)
        self.rt.complete(first["id"])
        self.assertEqual((await main).status, "completed")
        self.assertEqual([m["id"] for m in self.agent.batch], ["first", "correction"])
        self.assertEqual(self.link.logs[0]["input"].count("STOP_NEW_WRITES"), 1)
        self.assertEqual(len([m for m, _, _ in self.rt.requests if m == "turn/steer"]), 1)

    async def test_untagged_turn_accepts_only_untagged_message(self):
        main, first = await self.start_main(task=None)
        correction, steering, rpc = await self.dispatch(self.message(task_id=None))
        rpc["ack"].set_result({})
        self.assertTrue(await steering)
        self.rt.complete(first["id"])
        self.assertEqual((await main).status, "completed")
        self.assertEqual(self.agent.queue, [])
        self.assertIn(correction, self.agent.batch)

    async def test_codex_failures_preserve_accepted_input_in_claude_and_queue_new_input(self):
        from tests.fake_claude import FakeClient
        queries = asyncio.Queue()
        class GatedClaudeClient(FakeClient):
            async def query(self, text):
                await super().query(text)
                self.gate = asyncio.get_running_loop().create_future()
                queries.put_nowait({"text": text, "gate": self.gate})

            async def receive_response(self):
                await self.gate
                async for message in super().receive_response():
                    yield message

        fallback = ClaudeRuntime(self.tmp.name, model="claude-opus-5-5")
        self.addAsyncCleanup(fallback.close)
        self.agent.backends.fallback = fallback
        self.agent.backends.cfg = self.agent.backends.cfg | {
            "fallback": {"backend": "claude", "model": "claude-opus-5-5"}}
        with patch("cthulhu.claude_runtime._client_class", return_value=GatedClaudeClient), \
                patch.object(backends, "RETRY_DELAYS", (0,)):
            self.agent.enqueue(self.message("first", "ORIGINAL_WORK"))
            self.spawn(self.agent.loop())
            first = await self.next_event(self.rt.started)
            correction = self.message()
            self.agent.enqueue(correction)
            rpc = await self.next_event(self.rt.steering)
            rpc["ack"].set_result({})
            await asyncio.sleep(0)
            self.rt.complete(first["id"], status="failed", text="", error="invalid_stream")
            retry = await self.next_event(self.rt.started)
            self.assertIn("STOP_NEW_WRITES", retry["text"])
            self.rt.complete(retry["id"], status="failed", text="", error="provider_disconnected")

            claude_turn = await self.next_event(queries)
            self.assertIsInstance(self.agent.active[0], ClaudeRuntime)
            self.assertIs(self.agent.active[0], fallback)
            self.assertTrue(self.agent.backends.using_fallback)
            self.assertFalse(hasattr(fallback, "steer"))
            self.assertIn("ORIGINAL_WORK", claude_turn["text"])
            self.assertEqual(claude_turn["text"].split("</recovery>")[-1].count("STOP_NEW_WRITES"), 1)
            self.assertEqual([row["status"] for row in self.link.logs], ["failed", "failed"])

            later = self.message("later-correction", "CLAUDE_WAIT_NEXT_BATCH")
            self.agent.enqueue(later)
            await asyncio.sleep(0)
            self.assertEqual(self.agent.queue, [later])
            self.assertEqual(self.link.handled, [])
            self.assertEqual([m["id"] for m in self.agent.batch], ["first", "correction"])
            self.assertTrue(self.rt.steering.empty())
            self.assertTrue(queries.empty())

            claude_turn["gate"].set_result(None)
            self.assertEqual(await self.next_event(self.link.handled_events), "first")
            self.assertEqual(await self.next_event(self.link.handled_events), "correction")
            next_batch = await self.next_event(queries)
            self.assertEqual(self.link.handled, ["first", "correction"])
            self.assertEqual([m["id"] for m in self.agent.batch], ["later-correction"])
            self.assertEqual(next_batch["text"].count("CLAUDE_WAIT_NEXT_BATCH"), 1)
            self.assertEqual(self.link.logs[2]["backend"], "claude")
            self.assertEqual(self.link.logs[2]["input"].count("STOP_NEW_WRITES"), 1)
            self.assertEqual(len([m for m, _, _ in self.rt.requests if m == "turn/start"]), 2)

            next_batch["gate"].set_result(None)
            self.assertEqual(await self.next_event(self.link.handled_events), "later-correction")
        self.assertEqual(self.link.handled, ["first", "correction", "later-correction"])
        self.assertEqual([row["backend"] for row in self.link.logs], ["codex", "codex", "claude", "claude"])
        self.assertEqual([row["status"] for row in self.link.logs], ["failed", "failed", "completed", "completed"])
        self.assertEqual(self.link.logs[3]["input"].count("CLAUDE_WAIT_NEXT_BATCH"), 1)
        self.assertEqual(self.agent.queue, [])

    async def test_scoped_and_background_runs_keep_message_for_normal_batch(self):
        for background in (False, True):
            with self.subTest(background=background):
                self.set_batch()
                self.agent.queue = []
                if background:
                    running = self.spawn(self.agent.run_background("main", "MEMORY_ONLY", runtime=self.rt))
                else:
                    running = self.spawn(self.agent.run("feishu-scope", "SCOPED_INPUT", runtime=self.rt))
                turn = await self.next_event(self.rt.started)
                correction = self.message()
                self.agent.queue.append(correction)
                self.assertFalse(await self.agent.steer(correction))
                self.assertEqual(self.agent.queue, [correction])
                self.assertTrue(self.rt.steering.empty())
                self.rt.complete(turn["id"])
                self.assertEqual((await running).status, "completed")
                self.assertEqual(self.agent.take_batch(), [correction])
                self.assertEqual(self.link.handled, [])
