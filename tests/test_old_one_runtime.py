"""Newly spawned old_ones get the owner's default runtime; existing old_ones keep theirs."""
import tempfile
import unittest
from pathlib import Path

from cthulhu.roles import Role
from cthulhu.temple import Temple


class OldOneRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.temple = Temple({"secret": "s", "db": str(Path(self.tmp.name) / "db"),
                              "galaxies": [{"id": "g1", "name": "g1", "token": "t"}]})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_new_old_one_gets_default_runtime_and_existing_ones_are_unchanged(self):
        store = self.temple.store
        plain = store.upsert_agent("old_plain", "plain", Role.OLD_ONE, "g1", "sh", {"task": "t"})
        tuned = store.upsert_agent("old_tuned", "tuned", Role.OLD_ONE, "g1", "sh", {"task": "t"})
        own = {"backend": "codex", "model": "gpt-6-luna", "effort": "high"}
        store.set_setting("runtimes", {"old_tuned": own})
        before = {a["id"]: self.temple.runtime_for(a) for a in (plain, tuned)}

        new = await self.temple.spawn({"name": "n", "galaxy_id": "g1", "task": "t", "skill": "s",
                                       "title": "t", "acceptance": "a"}, "sh")

        expected = {"backend": "claude", "provider": "ali", "model": "claude-opus-5-5", "effort": "xhigh",
                    "fallback": {"backend": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}}
        runtimes = store.setting("runtimes")
        self.assertEqual(runtimes, {"old_tuned": own, new["id"]: expected})
        self.assertEqual(self.temple.runtime_for(store.agent(new["id"])), expected)
        self.assertEqual({a["id"]: self.temple.runtime_for(a) for a in (plain, tuned)}, before)
        self.assertIsNone(before["old_plain"])
        self.assertEqual(before["old_tuned"], own)


if __name__ == "__main__":
    unittest.main()
