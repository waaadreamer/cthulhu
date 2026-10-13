"""Real authenticated WebSockets and HTTP streaming; upstream is a local fixture."""
import asyncio
import json
import re
import socket
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from contextlib import contextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import StreamingResponse, JSONResponse
from starlette.routing import Route

from cthulhu.temple import Temple
from cthulhu.protocol import Link
from cthulhu.api_provider import ProviderWorker
from cthulhu.galaxy_api_runtime import GalaxyAPIRuntime
from cthulhu.runtime import tool

ROOT = Path(__file__).resolve().parent.parent
CODEX = ROOT / "codex-native/vendor/x86_64-unknown-linux-musl/bin/codex"


@contextmanager
def no_signals():
    yield


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, app):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        server.capture_signals = no_signals
        task = asyncio.create_task(server.serve(sockets=[sock]))
        self.servers.append((server, task))
        while not server.started:
            await asyncio.sleep(.01)
        return sock.getsockname()[1]

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.servers, self.links = [], []
        self.requests = []
        self.rt = None
        config = {"secret": "test-secret", "db": str(Path(self.tmp.name) / "db"),
                  "galaxies": [{"id": "ali", "name": "ali", "token": "provider-token"},
                               {"id": "central", "name": "central", "token": "caller-token"}],
                  "api_providers": {"ali": {"models": ["gpt-6-astra", "gpt-6.1-sol"], "consumers": ["central"]}}}
        self.temple = Temple(config)
        port = await self.serve(self.temple.app())
        gateway = await self.serve(Starlette(routes=[Route("/responses", self.gateway, methods=["POST"]),
                                                    Route("/messages", self.messages_gateway, methods=["POST"])]))
        url = f"ws://127.0.0.1:{port}/ws"

        async def welcome(_):
            pass

        async def provider_frame(frame):
            if frame.get("type") in ("api_request", "api_cancel"):
                await self.worker.frame(frame)

        self.provider_link = Link(url, {"kind": "galaxy", "id": "ali", "token": "provider-token", "capabilities": ["galaxy_api_v1", "galaxy_api_claude_v1"]}, welcome, provider_frame)
        self.worker = ProviderWorker(self.provider_link, {"models": ["gpt-6-astra", "gpt-6.1-sol"]})
        self.worker.local_transport = lambda: (f"http://127.0.0.1:{gateway}/responses", "never-leak-test-key")
        self.provider_link.on_disconnect = self.worker.disconnect
        self.links.append(asyncio.create_task(self.provider_link.run()))
        await asyncio.wait_for(self.provider_link.connected.wait(), 3)
        await self.worker.register("test")

        async def frame(_):
            pass
        self.caller = Link(url, {"kind": "galaxy", "id": "central", "token": "caller-token", "capabilities": ["galaxy_api_v1", "galaxy_api_claude_v1"]}, welcome, frame)
        self.links.append(asyncio.create_task(self.caller.run()))
        await asyncio.wait_for(self.caller.connected.wait(), 3)

    async def asyncTearDown(self):
        if self.rt:
            await self.rt.close()
        for task in self.links:
            task.cancel()
        await asyncio.gather(*self.links, return_exceptions=True)
        for server, task in self.servers:
            server.should_exit = True
        await asyncio.gather(*(t for _, t in self.servers), return_exceptions=True)
        self.tmp.cleanup()

    async def gateway(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer never-leak-test-key")
        if getattr(self, "upstream_status", 200) != 200:
            return JSONResponse({"error": "never-leak-test-key"}, status_code=self.upstream_status)
        body = await request.json()
        self.requests.append(body)
        number = len(self.requests)
        rid = f"resp_{number}"
        history = body["input"]
        if isinstance(history, str):
            history = [{"role": "user", "content": history}]
        tool_output = any(i.get("type") == "function_call_output" for i in history)
        wants_tool = (bool(body.get("tools")) or getattr(self, "codex_mode", False)) and not tool_output
        nonce = re.search(r"echo_[0-9a-f]{8}", json.dumps(history))
        value = nonce.group(0) if nonce else "echoed"
        answer = f"Completed {value} result."
        tool_name = body.get("tools", [{}])[0].get("name", "echo")
        item = ({"id": f"fc_{number}", "type": "function_call", "call_id": f"call_{number}",
                 "name": tool_name, "arguments": json.dumps({"value": value}), "status": "completed"} if wants_tool else
                {"id": f"msg_{number}", "type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": answer, "annotations": []}]})
        response = {"id": rid, "object": "response", "created_at": 1, "model": "openai/" + body["model"],
                    "service_tier": "default", "status": "in_progress", "output": []}
        events = [{"type": "response.created", "response": response},
                  {"type": "response.in_progress", "response": response},
                  {"type": "response.output_item.added", "output_index": 0,
                   "item": item | {"status": "in_progress", **({"arguments": ""} if wants_tool else {})}}]
        if wants_tool:
            events += [{"type": "response.function_call_arguments.delta", "item_id": item["id"],
                        "output_index": 0, "delta": item["arguments"]}]
        else:
            events += [{"type": "response.output_text.delta", "item_id": item["id"],
                        "output_index": 0, "content_index": 0, "delta": answer}]
        events += [{"type": "response.output_item.done", "output_index": 0, "item": item},
                   {"type": "response.completed", "response": response | {"status": "completed", "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}}]

        async def stream():
            for event in events:
                if event["type"] == "response.completed" and hasattr(self, "terminal_gate"):
                    await self.terminal_gate.wait()
                if getattr(self, "hold_stream", False):
                    await asyncio.sleep(30)
                wire = ("data: " + json.dumps(event) + "\n\n").encode()
                # Intentionally split SSE blocks and JSON across HTTP chunks.
                yield wire[:17]
                await asyncio.sleep(.002)
                yield wire[17:]
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def messages_gateway(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer never-leak-test-key")
        self.assertEqual(request.headers["anthropic-version"], "2023-06-01")
        body = await request.json()
        self.requests.append(body)
        status = 429 if body["model"] == getattr(self, "messages_fail_model", None) else getattr(self, "messages_status", 200)
        if status != 200:
            return JSONResponse({"error": "never-leak-test-key"}, status_code=status)
        has_result = any(b.get("type") == "tool_result" for m in body["messages"]
                         if isinstance(m.get("content"), list) for b in m["content"])
        name = next((t["name"] for t in body.get("tools", []) if t["name"].endswith("__echo")), None)
        use_tool = name and not has_result
        nonce = re.search(r"echo_[0-9a-f]{8}", json.dumps(body["messages"]))
        value = nonce.group(0) if nonce else "native-echo"
        tool_answer = re.search(r"tool_result_[0-9a-f]{16}", json.dumps(body["messages"]))
        answer = tool_answer.group(0) if tool_answer else "Native Claude received native-echo"
        message = {"id": "msg_native", "type": "message", "role": "assistant", "content": [],
                   "model": getattr(self, "messages_returned_model", body["model"]), "stop_reason": None, "stop_sequence": None,
                   "usage": {"input_tokens": 10, "output_tokens": 0}}
        content = ({"type": "tool_use", "id": "toolu_native", "name": name, "input": {}} if use_tool else
                   {"type": "text", "text": ""})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps({"value": value})} if use_tool else
                 {"type": "text_delta", "text": answer})
        events = [{"type": "message_start", "message": message},
                  {"type": "content_block_start", "index": 0, "content_block": content},
                  {"type": "content_block_delta", "index": 0, "delta": delta},
                  {"type": "content_block_stop", "index": 0},
                  {"type": "message_delta", "delta": {"stop_reason": "tool_use" if use_tool else "end_turn",
                                                        "stop_sequence": None}, "usage": {"output_tokens": 10}},
                  {"type": "message_stop"}]
        async def stream():
            for event in events:
                yield ("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                if getattr(self, "messages_hold", False):
                    await asyncio.sleep(30)
                await asyncio.sleep(.02)
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def enable_claude(self):
        model = "claude-opus-5-5"
        self.temple.config["api_providers"]["ali"]["models"].append(model)
        self.worker.config["models"].append(model)
        self.worker.config["model_backends"] = {model: "claude"}
        url, key = self.worker.local_transport()
        self.worker.local_transport = lambda protocol="responses": (url.replace("/responses", "/messages") if protocol == "messages" else url, key)
        await self.worker.register("test-native")
        return model

    async def consume(self, body):
        opened = await self.caller.call("api_open", provider="ali", body=body)
        import base64
        content = b""
        meta = {}
        while True:
            part = await self.caller.call("api_next", id=opened["id"])
            self.assertFalse(part.get("error"), part)
            content += base64.b64decode(part.get("data", ""))
            meta.update(part.get("meta", {}))
            if part.get("done"):
                break
        return content, meta

    async def test_real_ws_http_tool_history_concurrency_and_tier(self):
        a, b = await asyncio.gather(
            self.consume({"model": "gpt-6-astra", "input": "hello", "service_tier": "priority"}),
            self.consume({"model": "gpt-6.1-sol", "input": [
                {"type": "function_call", "call_id": "previous", "name": "echo", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "previous", "output": "safe result"}]}))
        self.assertIn(b"response.completed", a[0])
        self.assertEqual(a[1]["returned_tier"], "default")
        self.assertEqual(b[1]["returned_model"], "openai/gpt-6.1-sol")
        self.assertNotIn(b"never-leak-test-key", a[0] + b[0])
        self.assertFalse(self.temple.api_broker.streams)

    @unittest.skipUnless(CODEX.exists(), "bundled Codex is unavailable")
    async def test_real_codex_dynamic_tool_and_second_turn(self):
        self.codex_mode = True
        self.rt = GalaxyAPIRuntime(str(CODEX), self.tmp.name, link=self.caller, provider="ali",
                                   state_dir=Path(self.tmp.name), model="gpt-6-astra", service_tier="fast")
        calls = []
        async def handler(name, args):
            calls.append((name, args))
            return "echoed"
        await self.rt.start()
        thread = await self.rt.start_thread("Use echo when asked.", [tool("echo", "Echo a value",
                                            {"value": {"type": "string"}}, ["value"])], handler)
        result = await self.rt.run_turn(thread, "Call echo once, then answer.", timeout=20)
        self.assertEqual(result.status, "completed", str((result.error, len(self.requests), result.items)))
        self.assertEqual(calls, [("echo", {"value": "echoed"})])
        self.assertGreaterEqual(len(self.requests), 2)
        self.assertIn("Echo a value", json.dumps(self.requests[0]))
        self.assertTrue(any(i.get("type") == "function_call_output" for i in self.requests[1]["input"]))
        result = await self.rt.run_turn(thread, "Summarize the previous result.", timeout=20)
        self.assertEqual(result.status, "completed", str((result.error, len(self.requests), result.items)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.rt.api_result["returned_tier"], "default")
        self.assertEqual(self.requests[0]["service_tier"], "priority")


    async def test_upstream_error_is_sanitized(self):
        self.upstream_status = 401
        opened = await self.caller.call("api_open", provider="ali", body={"model": "gpt-6-astra", "input": "hello"})
        part = await self.caller.call("api_next", id=opened["id"])
        self.assertEqual(part["error"], "upstream_auth")
        self.assertNotIn("never-leak-test-key", json.dumps(part))

    async def test_ws_disconnect_fails_outstanding_stream_and_reconnects_cleanly(self):
        opened = await self.caller.call("api_open", provider="ali", body={"model": "gpt-6-astra", "input": "hello"})
        await self.provider_link.ws.close()
        for _ in range(100):
            if not self.temple.api_broker.catalog():
                break
            await asyncio.sleep(.01)
        part = await self.caller.call("api_next", id=opened["id"])
        self.assertEqual(part.get("error"), "provider_disconnected", part)
        self.assertFalse(self.temple.api_broker.streams)
        for _ in range(200):
            if self.provider_link.connected.is_set():
                break
            await asyncio.sleep(.01)
        await self.worker.register("reconnected")
        content, meta = await self.consume({"model": "gpt-6-astra", "input": "new request"})
        self.assertIn(b"response.completed", content)


    async def test_concurrent_streams_queue_for_slot_instead_of_local_503(self):
        from cthulhu import api_provider
        from cthulhu.galaxy_api_runtime import GalaxyAPITransport
        import httpx2 as httpx
        self.terminal_gate = asyncio.Event()
        transport = GalaxyAPITransport(link=self.caller, provider="ali", state_dir=self.tmp.name, model="gpt-6-astra")
        await transport.start()
        cfg = transport.api_transport
        url, headers = cfg["base_url"] + "/responses", {"authorization": "Bearer " + cfg["token"]}
        body = {"model": "gpt-6-astra", "input": "hello"}
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
                async def one():
                    async with client.stream("POST", url, json=body, headers=headers) as r:
                        return r.status_code, await r.aread()
                # Former per-caller cap (2) and per-provider cap (4) produced bare 503s here.
                tasks = [asyncio.create_task(one()) for _ in range(6)]
                await asyncio.sleep(.5)
                self.terminal_gate.set()
                results = await asyncio.gather(*tasks)
                self.assertTrue(all(code == 200 and b"response.completed" in data for code, data in results), results)
                # Exhaustion that outlasts the wait is a named, retryable 429, never a bare route 503.
                with patch.object(api_provider, "SLOT_WAIT", 0), patch.dict(self.temple.config, {"api_limits": {"per_caller": 0}}):
                    r = await client.post(url, json=body, headers=headers)
                self.assertEqual(r.status_code, 429)
                self.assertIn("API capacity exhausted", r.text)
        finally:
            self.terminal_gate.set()
            await transport.close()

    async def test_open_waits_for_provider_reregistration(self):
        await self.provider_link.ws.close()
        for _ in range(200):
            if not self.temple.api_broker.catalog():
                break
            await asyncio.sleep(.01)
        self.assertFalse(self.temple.api_broker.catalog())

        async def reregister():
            await asyncio.wait_for(self.provider_link.connected.wait(), 5)
            await self.worker.register("restarted")
        task = asyncio.create_task(reregister())
        data, _ = await self.consume({"model": "gpt-6-astra", "input": "during restart"})
        await task
        self.assertIn(b"response.completed", data)

    async def test_cancel_ack_closes_only_target_request(self):
        self.hold_stream = True
        opened = await self.caller.call("api_open", provider="ali", body={"model": "gpt-6-astra", "input": "cancel me"})
        for _ in range(100):
            if opened["id"] in self.worker.tasks:
                break
            await asyncio.sleep(.01)
        result = await self.caller.call("api_cancel", id=opened["id"])
        self.assertTrue(result["upstream_closed"], result)
        self.assertNotIn(opened["id"], self.worker.tasks)
        self.assertFalse(self.temple.api_broker.streams)
        self.hold_stream = False
        data, _ = await self.consume({"model": "gpt-6-astra", "input": "still connected"})
        self.assertIn(b"response.completed", data)

    async def test_local_maintenance_is_scoped_and_legacy_cannot_select_api(self):
        from cthulhu.protocol import maintenance_token, CallFailed

        async def ignore(_):
            pass
        maint = Link(self.caller.url, {"kind": "maintenance", "token": maintenance_token("test-secret")},
                     ignore, ignore)
        task = asyncio.create_task(maint.run())
        self.links.append(task)
        await asyncio.wait_for(maint.connected.wait(), 3)
        snapshot = await maint.call("maintenance_snapshot")
        self.assertIn("version", snapshot)
        with self.assertRaises(CallFailed):
            await maint.call("send", to="someone", text="not allowed")
        with self.assertRaises(CallFailed):
            await maint.call("api_register", version=1, protocol="responses", models=["gpt-6-astra"])
        # No executing agents: scoped drain can be confirmed without touching a process.
        result = await maint.call("maintenance_drain", galaxy="central", enabled=True)
        self.assertTrue(result["safe_to_restart"])
        result = await maint.call("maintenance_drain", galaxy="central", enabled=False)
        self.assertFalse(result["draining"])
        self.temple.galaxies["central"].capabilities = []
        with self.assertRaises(CallFailed):
            await self.caller.call("api_open", provider="ali", body={"model": "gpt-6-astra", "input": "must not send"})


    async def test_maintenance_runtime_updates_only_existing_old_one(self):
        from cthulhu.protocol import maintenance_token, agent_token, CallFailed

        async def ignore(_):
            pass

        frames = asyncio.Queue()
        self.temple.store.upsert_agent("old_test", "test", "old_one", "central")
        agent = Link(self.caller.url, {"kind": "agent", "id": "old_test",
                     "token": agent_token("test-secret", "old_test", 1),
                     "capabilities": ["galaxy_api_v1"]}, ignore, frames.put)
        maint = Link(self.caller.url, {"kind": "maintenance", "token": maintenance_token("test-secret")},
                     ignore, ignore)
        for link in (agent, maint):
            self.links.append(asyncio.create_task(link.run()))
            await asyncio.wait_for(link.connected.wait(), 3)
        cfg = {"backend": "galaxy_api", "provider": "ali", "model": "gpt-6-astra", "effort": "medium"}
        result = await maint.call("maintenance_runtime", agent_id="old_test", config=cfg)
        self.assertEqual(result["config"], cfg)
        async def runtime_frame():
            while True:
                frame = await frames.get()
                if frame.get("type") == "runtime":
                    return frame
        self.assertEqual((await asyncio.wait_for(runtime_frame(), 3))["config"], cfg)
        self.assertEqual(self.temple.store.setting("runtimes")["old_test"], cfg)
        for aid, bad in (("missing", cfg), ("old_one", cfg),
                         ("old_test", cfg | {"provider": "unavailable"})):
            with self.assertRaises(CallFailed):
                await maint.call("maintenance_runtime", agent_id=aid, config=bad)
        self.assertEqual(self.temple.store.setting("runtimes")["old_test"], cfg)
        with self.assertRaises(CallFailed):
            await agent.call("maintenance_runtime", agent_id="old_test", config=cfg)
        await maint.call("maintenance_runtime", agent_id="old_test", config=None)
        self.assertIsNone((await asyncio.wait_for(runtime_frame(), 3))["config"])
        self.assertNotIn("old_test", self.temple.store.setting("runtimes"))

    async def test_task_calls_authenticated_over_the_wire(self):
        from cthulhu.protocol import agent_token, CallFailed

        async def ignore(_):
            pass
        clients = {}
        for role in ('azathoth', 'shub', 'yog', 'old_one'):
            self.temple.store.upsert_agent(role, role, role, 'central', parent_id='shub' if role == 'old_one' else None)
            link = Link(self.caller.url, {'kind': 'agent', 'id': role,
                        'token': agent_token('test-secret', role, 1)}, ignore, ignore)
            self.links.append(asyncio.create_task(link.run()))
            await asyncio.wait_for(link.connected.wait(), 3)
            clients[role] = link
        with self.assertRaises(CallFailed):     # no acceptance criteria
            await clients['shub'].call('task_create', executor='old_one', title='t', text='do it')
        with self.assertRaises(CallFailed):     # an old_one may not create tasks
            await clients['old_one'].call('task_create', executor='old_one', title='t', text='x', acceptance='y')
        made = await clients['shub'].call('task_create', executor='old_one', title='t', text='do it', acceptance='file exists')
        task = self.temple.store.task(made['task_id'])
        self.assertEqual((task['creator'], task['manager'], task['executor']), ('shub', 'shub', 'old_one'))
        with self.assertRaises(CallFailed):     # only the executor registers the probe
            await clients['shub'].call('task_probe', task_id=task['id'], command='echo 1')
        with self.assertRaises(CallFailed):     # yog judges only submitted tasks
            await clients['yog'].call('task_verdict', task_id=task['id'], passed=True, reason='x')
        for gone in ('task_state', 'patrol', 'patrol_log_recent'):
            with self.assertRaises(CallFailed):
                await clients['yog'].call(gone, task_id=task['id'])

    async def test_first_sse_reaches_caller_before_upstream_completion(self):
        import base64
        self.terminal_gate = asyncio.Event()
        opened = await self.caller.call("api_open", provider="ali", body={"model": "gpt-6-astra", "input": "stream"})
        try:
            part = await asyncio.wait_for(self.caller.call("api_next", id=opened["id"]), 2)
            self.assertIn(b"response.created", base64.b64decode(part["data"]))
            self.assertFalse(part.get("done"))
            self.assertIn(opened["id"], self.worker.tasks)
        finally:
            self.terminal_gate.set()
        while True:
            part = await self.caller.call("api_next", id=opened["id"])
            if part.get("done"):
                break


    async def test_bounded_live_canary_contract_against_fixture(self):
        from cthulhu.api_canary import run
        result = await run(self.caller, "ali", "full")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["generated_requests"], 5)
        self.assertEqual(result["echo_executions"], 1)
        self.assertEqual(len(result["tool_ledger"]), 1)
        self.assertTrue(result["results"][-1]["started_event_observed"])
        self.assertTrue(result["results"][-1]["upstream_closed"])

    async def test_canary_selected_model_stream_tools_attribution_and_cancel(self):
        from cthulhu.api_canary import run
        model = "fixture-opus"
        self.temple.config["api_providers"]["ali"]["models"].append(model)
        self.worker.config["models"].append(model)
        await self.worker.register("test")
        result = await run(self.caller, "ali", "full", [model])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["generated_requests"], 4)
        self.assertEqual(result["echo_executions"], 1)
        self.assertEqual({r["model"] for r in self.requests}, {model})
        for check in result["results"][:-1]:
            self.assertEqual(check["requested_model"], model)
            self.assertEqual(check["returned_model"], "openai/" + model)
            self.assertEqual(check["provider"], "ali")
        self.assertTrue(result["results"][-1]["upstream_closed"])

    async def test_canary_missing_selected_model_sends_nothing(self):
        from cthulhu.api_canary import run
        result = await run(self.caller, "ali", "full", ["gpt-6-astra", "not-advertised"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["generated_requests"], 0)
        self.assertEqual(self.requests, [])

    async def test_native_claude_over_provider_stream_and_actual_mcp_tool(self):
        from cthulhu import backends
        from cthulhu.claude_runtime import ClaudeRuntime
        model = await self.enable_claude()
        self.rt = backends.build({"backend": "claude", "provider": "ali", "model": model},
                                 codex="must-not-run-codex", cwd=self.tmp.name, log_dir=Path(self.tmp.name),
                                 sub_agents=False, api_link=self.caller)
        self.assertIsInstance(self.rt, ClaudeRuntime)
        calls, deltas = [], []
        async def handler(name, args):
            calls.append((name, args))
            return args["value"]
        def event(method, params):
            if method == "item/agentMessage/delta":
                deltas.append(params["delta"])
        await self.rt.start()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "personal-must-not-send",
                                      "ANTHROPIC_BASE_URL": "https://must-not-call.invalid",
                                      "CLAUDE_CODE_USE_BEDROCK": "1", "CTHULHU_AGENT_TOKEN": "harness-only"}):
            thread = await self.rt.start_thread("Call echo once when asked, then report its result.",
                                                [tool("echo", "Echo without side effects", {"value": {"type": "string"}}, ["value"])], handler)
        env = self.rt.threads[thread].client.options.env
        self.assertEqual(env["ANTHROPIC_API_KEY"], self.rt.api_transport.local_token)
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "")
        self.assertEqual(env["CTHULHU_AGENT_TOKEN"], "")
        self.assertEqual(env["CLAUDE_CODE_MAX_RETRIES"], "0")
        result = await self.rt.run_turn(thread, "Call echo once and report its result.", on_event=event, timeout=45)
        self.assertEqual(result.status, "completed", (result.error, self.requests))
        self.assertEqual(calls, [("echo", {"value": "native-echo"})])
        self.assertIn("native-echo", result.text)
        self.assertTrue(deltas)
        self.assertTrue(any(i.get("tool") == "echo" for i in result.items))
        self.assertEqual(self.rt.api_result["provider"], "ali")
        self.assertEqual(self.rt.api_result["requested_model"], model)
        self.assertEqual(self.rt.api_result["returned_model"], model)
        self.assertTrue(any(b.get("type") == "tool_result" for m in self.requests[-1]["messages"]
                            if isinstance(m.get("content"), list) for b in m["content"]))

    async def test_native_messages_error_cancel_and_legacy_rejection(self):
        from cthulhu.protocol import CallFailed
        model = await self.enable_claude()
        body = {"model": model, "messages": [{"role": "user", "content": "read only"}], "max_tokens": 64}
        for status, error in [(401, "upstream_auth"), (429, "upstream_rate_limit"), (503, "upstream_unavailable")]:
            self.messages_status = status
            opened = await self.caller.call("api_open", provider="ali", protocol="messages", body=body)
            part = await self.caller.call("api_next", id=opened["id"])
            self.assertEqual(part["error"], error)
            self.assertNotIn("never-leak-test-key", json.dumps(part))
        self.messages_status, self.messages_hold = 200, True
        opened = await self.caller.call("api_open", provider="ali", protocol="messages", body=body)
        part = await self.caller.call("api_next", id=opened["id"])
        self.assertTrue(part["data"])
        cancelled = await self.caller.call("api_cancel", id=opened["id"])
        self.assertTrue(cancelled["upstream_closed"])
        self.assertNotIn(opened["id"], self.worker.tasks)
        self.temple.galaxies["central"].capabilities.remove("galaxy_api_claude_v1")
        with self.assertRaises(CallFailed):
            await self.caller.call("api_open", provider="ali", protocol="messages", body=body)

    async def test_registered_model_selects_claude_without_changing_defaults(self):
        from cthulhu.protocol import agent_token
        model = await self.enable_claude()
        self.temple.store.upsert_agent("old_native", "native", "old_one", "central")
        async def ignore(_): pass
        link = Link(self.caller.url, {"kind": "agent", "id": "old_native",
                    "token": agent_token("test-secret", "old_native", 1),
                    "capabilities": ["galaxy_api_v1", "galaxy_api_claude_v1"]}, ignore, ignore)
        self.links.append(asyncio.create_task(link.run()))
        await asyncio.wait_for(link.connected.wait(), 3)
        await self.temple.set_runtime("old_native", {"backend": "galaxy_api", "provider": "ali", "model": model})
        self.assertEqual(self.temple.store.setting("runtimes"), {
            "old_native": {"backend": "claude", "provider": "ali", "model": model}})
        self.temple.agents["old_native"].capabilities.remove("galaxy_api_claude_v1")
        with self.assertRaises(ValueError):
            await self.temple.set_runtime("old_native", {"backend": "claude", "provider": "ali", "model": model})

    async def test_native_claude_shared_two_attempts_then_configured_claude_fallback(self):
        from cthulhu import backends
        primary = await self.enable_claude()
        fallback = "claude-sonnet-5-5"
        self.worker.config["models"].append(fallback)
        self.worker.config["model_backends"][fallback] = "claude"
        self.temple.config["api_providers"]["ali"]["models"].append(fallback)
        await self.worker.register("native-recovery")
        self.messages_fail_model = primary
        pair = self.rt = backends.Backends({"backend": "claude", "provider": "ali", "model": primary,
                    "fallback": {"backend": "claude", "provider": "ali", "model": fallback}},
                    codex="must-not-run-codex", cwd=self.tmp.name, log_dir=Path(self.tmp.name),
                    sub_agents=False, api_link=self.caller)
        threads, effects, statuses, checked = {}, [], [], []
        async def handler(name, args):
            effects.append(args["value"])
            return args["value"]
        async def attempt(rt, note):
            if rt not in threads:
                await rt.start()
                threads[rt] = await rt.start_thread("Call echo then answer.",
                    [tool("echo", "Echo", {"value": {"type": "string"}}, ["value"])], handler)
            return await rt.run_turn(threads[rt], "Call echo once, then answer. " + note, timeout=20)
        async def prepare(rt, result, note):
            checked.append(await rt.recovery_checkpoint(threads[rt], True))
            return "Previous attempt settled; no tool executed."
        async def report(state): statuses.append(state)
        with patch.object(backends, "RETRY_DELAYS", (0,)):
            result = await pair.recover(attempt, prepare=prepare, report=report)
        self.assertEqual(result.status, "completed", (result.error, statuses))
        self.assertEqual([r["model"] for r in self.requests], [primary] * 2 + [fallback] * 2)
        self.assertEqual(effects, ["native-echo"])
        self.assertEqual(len(checked), 2)
        self.assertEqual(statuses[-1]["attempt"], 3)
        self.assertEqual(statuses[-1]["retries"], 1)
        self.assertTrue(statuses[-1]["fallback"])
        self.assertEqual(pair.active_cfg["backend"], "claude")

    async def test_native_claude_unoffered_fallback_fails_without_model_request(self):
        from cthulhu import backends
        primary = await self.enable_claude()
        fallback = "claude-sonnet-5-5"
        self.messages_fail_model = primary
        pair = self.rt = backends.Backends({"backend": "claude", "provider": "ali", "model": primary,
                    "fallback": {"backend": "claude", "provider": "ali", "model": fallback}},
                    codex="must-not-run-codex", cwd=self.tmp.name, log_dir=Path(self.tmp.name),
                    sub_agents=False, api_link=self.caller)
        calls, statuses = [], []
        threads = {}
        async def attempt(rt, note):
            calls.append(rt)
            await rt.start()
            if rt not in threads:
                threads[rt] = await rt.start_thread("Reply ready.", [], None)
            return await rt.run_turn(threads[rt], "Reply ready. " + note, timeout=20)
        async def report(state): statuses.append(state)
        with patch.object(backends, "RETRY_DELAYS", (0,)):
            result = await pair.recover(attempt, report=report)
        self.assertEqual(result.status, "failed")
        self.assertIn("does not offer this model", result.error)
        self.assertIn("configured fallback exhausted", result.error)
        self.assertEqual(calls, [pair.primary] * 2 + [pair.fallback] * 2)
        self.assertEqual([r["model"] for r in self.requests], [primary] * 2)
        self.assertEqual(pair.fallback.api_result, {})
        self.assertEqual(statuses[-1]["phase"], "failed")
        self.assertEqual(statuses[-1]["attempt"], 4)
        self.assertEqual(statuses[-1]["retries"], 1)

    async def test_native_canary_real_cli_and_tool_result_not_in_prompt(self):
        from cthulhu.api_canary import run_native_claude, run
        model = await self.enable_claude()
        actual = self.messages_returned_model = "Claude-Opus-5.5-P017"
        rejected = await run(self.caller, "ali", "full", [model])
        self.assertEqual(rejected["generated_requests"], 0)
        result = await run_native_claude(self.caller, "ali", model, expected_returned_model=actual)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["backend"], "claude")
        self.assertEqual(result["runtime"], "ClaudeRuntime")
        self.assertEqual(result["generated_requests"], 3)
        self.assertTrue(result["echo_matches"])
        self.assertTrue(result["native_tool_observed"])
        self.assertTrue(result["streaming_observed"])
        self.assertNotIn("tool_result_", json.dumps(self.requests[0]["messages"]))
        self.assertIn("tool_result_", json.dumps(self.requests[1]["messages"]))
        self.assertEqual(result["results"][1]["returned_model"], actual)
        self.assertEqual(result["results"][1]["requested_model"], model)
        self.assertTrue(result["returned_model_matches"])

    async def test_native_canary_does_not_accept_unexpected_actual_model(self):
        from cthulhu.api_canary import run_native_claude
        model = await self.enable_claude()
        self.messages_returned_model = "unexpected-model"
        result = await run_native_claude(self.caller, "ali", model, expected_returned_model="Claude-Opus-5.5-P017")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["returned_model_matches"])
        self.assertEqual(result["generated_requests"], 2)
        self.assertTrue(result["echo_matches"])
        self.assertEqual(result["results"][1]["returned_model"], "unexpected-model")

    async def test_native_auth_failure_is_terminal_without_fallback(self):
        from cthulhu import backends
        model = await self.enable_claude()
        self.messages_status = 401
        self.rt = pair = backends.Backends({"backend": "claude", "provider": "ali", "model": model,
                    "fallback": {"backend": "claude", "provider": "ali", "model": model}},
                    codex="must-not-run-codex", cwd=self.tmp.name, log_dir=Path(self.tmp.name),
                    sub_agents=False, api_link=self.caller)
        async def attempt(rt, note):
            await rt.start()
            thread = await rt.start_thread("Reply ready.", [], None)
            return await rt.run_turn(thread, "Reply ready.", timeout=20)
        result = await pair.recover(attempt)
        self.assertEqual(result.status, "failed")
        self.assertIn("upstream_auth", result.error)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(pair.using_fallback)
