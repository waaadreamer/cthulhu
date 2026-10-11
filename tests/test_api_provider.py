import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cthulhu.api_provider import ProviderBroker, ProviderWorker, MAX_REQUEST, MAX_CHUNK, QUEUE_SIZE
from cthulhu.roles import Denied
from cthulhu.galaxy_api_runtime import GalaxyAPIRuntime
from cthulhu import backends
from cthulhu.agent import Agent
from cthulhu.runtime import TurnResult


class Conn:
    def __init__(self, kind, id):
        self.kind, self.id = kind, id
        self.capabilities = ["galaxy_api_v1"]
        self.send = AsyncMock(return_value=True)


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = Conn("galaxy", "ali")
        self.caller = Conn("agent", "worker")
        self.temple = SimpleNamespace(config={"api_providers": {"ali": {
            "models": ["gpt-6-astra", "gpt-6.1-sol"], "consumers": ["central"]}}},
            galaxies={"ali": self.provider}, agents={"worker": self.caller},
            store=SimpleNamespace(agent=lambda _: {"desired": "running", "galaxy_id": "central"}))
        self.broker = ProviderBroker(self.temple)
        self.broker.register(self.provider, {"version": 1, "protocol": "responses",
                                            "models": ["gpt-6-astra", "gpt-6.1-sol", "not-permitted"]})
        self.body = {"model": "gpt-6-astra", "input": [{"role": "user", "content": "hello"}],
                     "tools": [{"type": "function", "name": "echo", "parameters": {"type": "object"}}]}

    async def asyncTearDown(self):
        for s in list(self.broker.streams.values()):
            self.broker.remove(s)

    async def open(self, **kw):
        return await self.broker.open(self.caller, {"provider": "ali", "body": self.body | kw})

    async def test_scoped_discovery_stale_and_unknown_registration(self):
        self.assertEqual(self.broker.catalog("elsewhere"), [])
        self.assertEqual(self.broker.catalog("central")[0]["models"], ["gpt-6-astra", "gpt-6.1-sol"])
        with self.assertRaises(Denied):
            self.broker.register(Conn("galaxy", "ali"), {"version": 1})
        with self.assertRaises(Denied):
            self.broker.register(self.caller, {"version": 1})
        self.temple.galaxies["ali"] = Conn("galaxy", "ali")
        self.assertEqual(self.broker.catalog("central"), [])
        with self.assertRaises(Denied):
            await self.open()

    async def test_concurrent_correlation_and_history_passthrough(self):
        a, b = await self.open(), await self.open(model="gpt-6.1-sol")
        sent = self.provider.send.call_args.args[0]
        self.assertEqual(sent["body"]["tools"], self.body["tools"])
        self.assertFalse(sent["body"]["store"])
        for opened, text in ((b, b"B"), (a, b"A")):
            await self.broker.push(self.provider, {"id": opened["id"], "seq": 0,
                                   "data": base64.b64encode(text).decode(), "done": True,
                                   "meta": {"returned_tier": "default", "secret": "ignored"}})
        self.assertEqual((await self.broker.next(self.caller, a))["data"], "QQ==")
        part = await self.broker.next(self.caller, b)
        self.assertEqual(part["data"], "Qg==")
        self.assertEqual(part["meta"], {"returned_tier": "default"})
        self.assertFalse(self.broker.streams)

    async def test_disconnect_and_cancel_cleanup(self):
        opened = await self.open()
        await self.broker.disconnect(self.provider)
        self.assertEqual((await self.broker.next(self.caller, opened))["error"], "provider_disconnected")
        self.assertFalse(self.broker.catalog())
        self.broker.register(self.provider, {"version": 1, "protocol": "responses", "models": ["gpt-6-astra"]})
        opened = await self.open()
        await self.broker.disconnect(self.caller)
        self.assertFalse(self.broker.streams)
        self.assertEqual(self.provider.send.call_args.args[0]["type"], "api_cancel")

    async def test_limits_sequence_ownership_and_backpressure(self):
        opened = await self.open()
        with self.assertRaises(Denied):
            await self.broker.push(Conn("galaxy", "forged"), {"id": opened["id"], "seq": 0})
        with self.assertRaises(ValueError):
            await self.broker.push(self.provider, {"id": opened["id"], "seq": 1})
        for seq in range(QUEUE_SIZE):
            await self.broker.push(self.provider, {"id": opened["id"], "seq": seq, "data": "eA=="})
        task = asyncio.create_task(self.broker.push(self.provider, {"id": opened["id"], "seq": QUEUE_SIZE, "data": "eA=="}))
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        await self.broker.next(self.caller, opened)
        await asyncio.wait_for(task, 1)
        with patch("cthulhu.api_provider.CANCEL_TIMEOUT", .01):
            await self.broker.cancel(self.caller, opened)
        self.assertFalse(self.broker.streams)
        with self.assertRaises(ValueError):
            await self.open(input="x" * MAX_REQUEST)

    async def test_timeout_and_no_error_body_leak(self):
        opened = await self.open()
        with patch("cthulhu.api_provider.IDLE_TIMEOUT", .01):
            part = await self.broker.next(self.caller, opened)
        self.assertEqual(part["error"], "timeout")
        opened = await self.open()
        await self.broker.push(self.provider, {"id": opened["id"], "seq": 0, "error": "secret bearer token"})
        self.assertEqual((await self.broker.next(self.caller, opened))["error"], "provider_error")

    async def test_forbidden_models_headers_history_handles(self):
        with self.assertRaises(Denied):
            await self.open(model="not-permitted")
        for field in ("headers", "base_url", "previous_response_id"):
            with self.assertRaises(ValueError):
                await self.open(**{field: "private"})


class RuntimeSafetyTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, root):
        return GalaxyAPIRuntime("unused", root, link=SimpleNamespace(connected=asyncio.Event()),
                                provider="ali", state_dir=Path(root), model="gpt-6-astra")

    async def test_native_and_dynamic_tools_disable_replay(self):
        with tempfile.TemporaryDirectory() as root:
            rt = self.runtime(root)
            self.assertTrue(rt.retry_safe)
            rt._notify("item/started", {"threadId": "t", "item": {"type": "reasoning"}})
            self.assertTrue(rt.retry_safe)
            rt._notify("item/started", {"threadId": "t", "item": {"type": "commandExecution"}})
            self.assertFalse(rt.retry_safe)
            rt.retry_safe = True
            rt.tool_handlers["t"] = AsyncMock(return_value="done")
            rt._write = AsyncMock()
            await rt._serve({"id": 1, "method": "item/tool/call",
                             "params": {"threadId": "t", "tool": "side_effect", "arguments": {}}})
            self.assertFalse(rt.retry_safe)
            rt.tool_handlers["t"].assert_awaited_once()

    async def test_agent_does_not_replay_failed_side_effects(self):
        agent = object.__new__(Agent)
        runtime = SimpleNamespace(retry_safe=False)
        agent.backends = SimpleNamespace(active=runtime, trip=lambda: self.fail("must not trip after tools"),
                                          active_cfg={"backend": "galaxy_api", "model": "gpt-6-astra"})
        agent.threads = {}
        agent.ensure_thread = AsyncMock(return_value="t")
        agent.handoff_context = AsyncMock(return_value="")
        agent.memory_delta = AsyncMock(return_value="")
        agent.run = AsyncMock(return_value=TurnResult("failed", error="disconnected after tool"))
        agent.link = SimpleNamespace(call=AsyncMock())
        result = await agent.run_main("execute once")
        self.assertEqual(result.status, "failed")
        agent.run.assert_awaited_once()

    async def test_startup_fallback_and_failed_switch_keep_old(self):
        with tempfile.TemporaryDirectory() as root:
            kw = dict(codex="unused", cwd=root, log_dir=Path(root), sub_agents=False)
            pair = backends.Backends({"backend": "galaxy_api", "provider": "offline", "model": "gpt-6-astra",
                                      "fallback": {"backend": "codex", "model": "gpt-6-astra"}}, **kw)
            pair.fallback.start = AsyncMock()
            await pair.start()
            self.assertTrue(pair.using_fallback)
            self.assertEqual(pair.active_cfg["backend"], "codex")
            pair.fallback.start.assert_awaited_once()
            # A startup failure is retried after a short hold, not the 30-minute one.
            self.assertEqual(pair.hold, backends.STARTUP_RETRY)
            pair.failed_at -= backends.STARTUP_RETRY + 1
            self.assertFalse(pair.using_fallback)


class DrainTests(unittest.TestCase):
    def test_drain_waits_for_foreground_memory_and_patrol(self):
        agent = object.__new__(Agent)
        agent.draining = True
        agent.inflight_runs = 0
        agent.state = "idle"
        agent.queue = []
        agent.memory_lock = SimpleNamespace(locked=lambda: False)
        self.assertTrue(agent.drain_status()["safe"])
        agent.inflight_runs = 1
        self.assertFalse(agent.drain_status()["safe"])
        agent.inflight_runs = 0
        agent.memory_lock = SimpleNamespace(locked=lambda: True)
        self.assertFalse(agent.drain_status()["safe"])
        agent.memory_lock = SimpleNamespace(locked=lambda: False)
        agent.patrol_busy = True
        self.assertFalse(agent.drain_status()["safe"])


class ProviderConfigTests(unittest.TestCase):
    def test_existing_private_toml_stays_local(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.toml"
            path.write_text('model_provider = "company"\n[model_providers.company]\n'
                            'base_url = "https://gateway.example/v1"\nwire_api = "responses"\n'
                            'experimental_bearer_token = "local-test-key"\n')
            path.chmod(0o600)
            worker = ProviderWorker(None, {"codex_config_file": str(path)})
            url, token = worker.local_transport()
            self.assertEqual(url, "https://gateway.example/v1/responses")
            self.assertEqual(token, "local-test-key")
            self.assertEqual(path.read_text().count("local-test-key"), 1)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                worker.local_transport()
