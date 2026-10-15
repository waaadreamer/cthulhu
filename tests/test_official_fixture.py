"""Local-only canary shutdown: no credentials, model request or live state."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cthulhu.api_provider import ProviderWorker
from cthulhu.temple import Temple
from scripts.verify_official_old_one import verify


class PlannedExit(Exception):
    pass


class FixtureCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_callbacks_finish_before_database_close(self):
        disconnected = []
        errors = []
        original = Temple._disconnect
        async def disconnect(temple, conn):
            await asyncio.sleep(.05)
            await original(temple, conn)
            # Force a database read after an asynchronous disconnect callback.
            temple.directory()
            disconnected.append(conn.id)
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _, context: errors.append(context))
        try:
            with tempfile.TemporaryDirectory() as root:
                config = Path(root) / "galaxy.json"
                config.write_text(json.dumps({"codex": "never-launched", "api_provider": {
                    "models": ["claude-opus-5-5"], "model_backends": {"claude-opus-5-5": "claude"}}}))
                output = Path(root) / "result.json"
                with patch.object(ProviderWorker, "local_transport", return_value=("https://unused.invalid/responses", "fixture-key")), \
                     patch.object(Temple, "new_task", new=AsyncMock(side_effect=PlannedExit)), \
                     patch.object(Temple, "_disconnect", new=disconnect):
                    with self.assertRaises(PlannedExit):
                        await verify(config, output)
                await asyncio.sleep(.1)
                self.assertEqual(disconnected, ["ali"])
                self.assertFalse(errors, errors)
                self.assertEqual(json.loads(output.read_text())["requests"], [])
        finally:
            loop.set_exception_handler(previous)
