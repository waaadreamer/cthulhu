"""Isolation and fallback regression checks; no external model requests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cthulhu import backends
from cthulhu.agent import Agent
from cthulhu.runtime import CodexRuntime, RuntimeError_, TurnResult
from cthulhu.temple import validate_runtime


class CompanyBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "company"
        self.home.mkdir()
        self.key = self.root / "key"
        self.key.write_text("test-company-token")
        self.key.chmod(0o600)
        self.company = dict(codex_home=str(self.home), api_key_file=str(self.key),
                            base_url="https://gateway.example/v1")
        self.kw = dict(codex="unused", cwd=str(self.root), log_dir=self.root,
                       sub_agents=False, company_codex=self.company)

    def runtime(self):
        return backends.build(dict(backend="company_codex", model="gpt-6.1-sol",
                                   service_tier="fast"), **self.kw)

    def test_environment_provider_and_personal_untouched(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.root / "personal"),
                                     "OPENAI_API_KEY": "personal-token", "CTHULHU_AGENT_TOKEN": "temple-token",
                                     "COMPANY_CODEX_API_KEY": "stale-token"}):
            rt = self.runtime()
            args, env = rt.launch_config()
            self.assertEqual(env["CODEX_HOME"], str(self.home))
            self.assertEqual(env["COMPANY_CODEX_API_KEY"], "test-company-token")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CTHULHU_AGENT_TOKEN", env)
            self.assertNotIn("test-company-token", " ".join(args))
            self.assertIn('model_provider="company_codex"', args)
            personal = backends.build(dict(backend="codex", model="gpt-6-astra"), **self.kw)
            personal_args, personal_env = personal.launch_config()
            self.assertEqual(personal_args, [])
            self.assertEqual(personal_env["CODEX_HOME"], str(self.root / "personal"))
            self.assertEqual(personal_env["OPENAI_API_KEY"], "personal-token")
            self.assertNotIn("COMPANY_CODEX_API_KEY", personal_env)
            self.assertEqual(os.environ["COMPANY_CODEX_API_KEY"], "stale-token")
            self.assertEqual(list(self.home.iterdir()), [])

    def test_unsafe_or_missing_local_config_rejected(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            with self.assertRaises(RuntimeError_):
                self.runtime().launch_config()
        (self.home / "auth.json").write_text("{}")
        with self.assertRaises(RuntimeError_):
            self.runtime().launch_config()
        (self.home / "auth.json").unlink()
        self.key.chmod(0o644)
        with self.assertRaises(RuntimeError_):
            self.runtime().launch_config()
        missing = backends.build(dict(backend="company_codex"), **(self.kw | {"company_codex": None}))
        with self.assertRaises(RuntimeError_):
            missing.launch_config()

    def test_selection_validation_and_thread_identity(self):
        cfg = validate_runtime(dict(backend="company_codex", model="gpt-6-astra",
                                    service_tier="fast",
                                    fallback=dict(backend="codex", model="gpt-6-astra")))
        self.assertEqual(cfg["service_tier"], "fast")
        self.assertNotEqual(backends.label(cfg), backends.label(cfg["fallback"]))
        self.assertFalse(backends.same(cfg, cfg | {"service_tier": "default"}))
        self.assertTrue(backends.same(None, None))
        with self.assertRaises(ValueError):
            validate_runtime(dict(backend="claude", model="claude-opus-5-5", service_tier="fast"))
        with self.assertRaises(ValueError):
            backends.build(dict(backend="typo"), **self.kw)
        agent = object.__new__(Agent)
        agent.default_runtime = cfg
        agent._load_threads("legacy-thread")
        self.assertEqual(agent.threads, {"codex:gpt-6-astra": "legacy-thread"})

    async def test_fast_turn_and_dynamic_tools(self):
        rt = self.runtime()
        calls = []

        async def request(method, params):
            calls.append((method, params))
            if method == "thread/start":
                return {"thread": {"id": "company-thread"}}
            if method == "turn/start":
                rt._notify("item/completed", {"threadId": "company-thread", "turnId": "turn",
                           "item": {"type": "agentMessage", "text": "done"}})
                rt._notify("turn/completed", {"threadId": "company-thread",
                           "turn": {"id": "turn", "status": "completed"}})
                return {"turn": {"id": "turn"}}
            return {}
        rt.request = request
        handler = AsyncMock(return_value={"ok": True})
        await rt.start_thread("instructions", [{"name": "test"}], handler)
        rt._write = AsyncMock()
        await rt._serve({"id": 9, "method": "item/tool/call",
                         "params": {"threadId": "company-thread", "tool": "test", "arguments": {}}})
        handler.assert_awaited_once_with("test", {})
        self.assertTrue(rt._write.call_args.args[0]["result"]["success"])
        result = await rt.run_turn("company-thread", "hello")
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls[0][1]["modelProvider"], "company_codex")
        self.assertEqual(calls[-1][1]["serviceTier"], "fast")
        self.assertEqual(calls[0][1]["dynamicTools"], [{"name": "test"}])

    async def test_startup_fallback_and_primary_retry(self):
        cfg = dict(backend="company_codex", model="gpt-6-astra",
                   fallback=dict(backend="codex", model="gpt-6-astra"))
        pair = backends.Backends(cfg, **self.kw)
        pair.primary.start = AsyncMock(side_effect=RuntimeError_("unavailable"))
        pair.primary.close = AsyncMock()
        pair.fallback.start = AsyncMock()
        await pair.start()
        self.assertTrue(pair.using_fallback)
        pair.fallback.start.assert_awaited_once()
        self.assertEqual(pair.active_cfg["backend"], "codex")
        with patch("cthulhu.backends.time.time", return_value=pair.failed_at + backends.FALLBACK_HOLD + 1):
            self.assertIs(pair.active, pair.primary)

    async def test_failed_switch_keeps_working_backend(self):
        agent = object.__new__(Agent)
        agent.name = "test"
        agent.pending_runtime = dict(backend="company_codex", model="gpt-6-astra")
        agent.build_kw = self.kw | {"company_codex": None}
        agent.backends = backends.Backends(dict(backend="codex", model="gpt-6-astra"), **self.kw)
        old = agent.backends
        await agent.apply_runtime()
        self.assertIs(agent.backends, old)

    async def test_watchdog_ignores_unstarted_fallback(self):
        import asyncio
        agent = object.__new__(Agent)
        agent.backends = backends.Backends(dict(backend="codex", model="gpt-6-astra",
                                               fallback=dict(backend="company_codex", model="gpt-6-astra")),
                                           **self.kw)
        with patch("cthulhu.agent.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await agent.watchdog()
