"""Retired central company backend must fail closed; personal behavior stays intact."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cthulhu import backends
from cthulhu.temple import validate_runtime


class BackendMigrationTests(unittest.TestCase):
    def test_central_company_backend_is_no_longer_selectable(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                backends.build({"backend": "company_codex"}, codex="unused", cwd=root,
                               log_dir=Path(root), sub_agents=False)
        with self.assertRaises(ValueError):
            validate_runtime({"backend": "company_codex", "model": "gpt-6-astra"})

    def test_personal_environment_and_identity_preserved(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CODEX_HOME": "/personal",
                "OPENAI_API_KEY": "personal", "CTHULHU_AGENT_TOKEN": "private", "COMPANY_CODEX_API_KEY": "obsolete"}):
            rt = backends.build({"backend": "codex", "model": "gpt-6-astra"}, codex="unused",
                                cwd=root, log_dir=Path(root), sub_agents=False)
            args, env = rt.launch_config()
            self.assertEqual(args, [])
            self.assertEqual(env["CODEX_HOME"], "/personal")
            self.assertEqual(env["OPENAI_API_KEY"], "personal")
            self.assertNotIn("CTHULHU_AGENT_TOKEN", env)
            self.assertNotIn("COMPANY_CODEX_API_KEY", env)
            self.assertEqual(backends.label({"backend": "codex", "model": "gpt-6-astra"}), "codex:gpt-6-astra")

    def test_provider_part_of_runtime_and_thread_identity(self):
        cfg = validate_runtime({"backend": "galaxy_api", "provider": "ali", "model": "gpt-6-astra",
                                "fallback": {"backend": "codex", "model": "gpt-6-astra"}})
        self.assertNotEqual(backends.label(cfg), backends.label(cfg["fallback"]))
        self.assertNotEqual(backends.label(cfg), backends.label(cfg | {"provider": "other"}))
        self.assertFalse(backends.same(cfg, cfg | {"provider": "other"}))
        self.assertTrue(backends.same(None, None))
