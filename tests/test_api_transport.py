"""Real authenticated WebSockets and HTTP streaming; upstream is a local fixture."""
import asyncio
import json
import re
import socket
import tempfile
import unittest
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
        gateway = await self.serve(Starlette(routes=[Route("/responses", self.gateway, methods=["POST"])]))
        url = f"ws://127.0.0.1:{port}/ws"

        async def welcome(_):
            pass

        async def provider_frame(frame):
            if frame.get("type") in ("api_request", "api_cancel"):
                await self.worker.frame(frame)

        self.provider_link = Link(url, {"kind": "galaxy", "id": "ali", "token": "provider-token", "capabilities": ["galaxy_api_v1"]}, welcome, provider_frame)
        self.worker = ProviderWorker(self.provider_link, {"models": ["gpt-6-astra", "gpt-6.1-sol"]})
        self.worker.local_transport = lambda: (f"http://127.0.0.1:{gateway}/responses", "never-leak-test-key")
        self.provider_link.on_disconnect = self.worker.disconnect
        self.links.append(asyncio.create_task(self.provider_link.run()))
        await asyncio.wait_for(self.provider_link.connected.wait(), 3)
        await self.worker.register("test")

        async def frame(_):
            pass
        self.caller = Link(url, {"kind": "galaxy", "id": "central", "token": "caller-token", "capabilities": ["galaxy_api_v1"]}, welcome, frame)
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
