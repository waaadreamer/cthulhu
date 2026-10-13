"""Task discipline: contracts, probes, the checkpoint ladder, extensions, acceptance, migration."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cthulhu.roles import Denied, Role
from cthulhu.temple import Conn, Temple
from test_universe import HarnessServer


class Galaxy:
    """Stands in for the executor's galaxy: the probe prints whatever `value` holds."""
    def __init__(self):
        self.value = "0"
        self.calls = []

    async def rpc(self, method, timeout=30, **p):
        self.calls.append((method, p))
        if method == 'agent_stopped':
            return {'agent_id': p['agent_id'], 'stopped': True}
        return {"code": 0, "stdout": str(self.value), "stderr": ""}

    async def send(self, frame):
        return True


class Discipline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gods = [{"id": r.value, "role": r.value, "galaxy": "g1"} for r in (Role.AZATHOTH, Role.SHUB, Role.YOG, Role.GHROTH)]
        self.t = Temple({"secret": "s", "db": str(Path(self.tmp.name) / "db"), "checkpoint_interval": 600,
                         "galaxies": [{"id": "g1", "name": "g1", "token": "t"}], "gods": gods})
        self.galaxy = self.t.galaxies["g1"] = Galaxy()
        self.t.broadcast = AsyncMock()
        self.t.deliver = AsyncMock()
        self.d = self.t.discipline
        self.t.store.upsert_agent("old_a", "a", Role.OLD_ONE, "g1", "shub", {"task": "t"})
        r = await self.call("shub", "task_create", executor="old_a", title="collect", text="collect rows",
                            acceptance="1000 rows in out.jsonl", deadline=60)
        self.tid = r["task_id"]

    async def asyncTearDown(self):
        self.t.store.db.close()
        self.tmp.cleanup()

    async def call(self, who, method, **p):
        return await self.t.handle_agent_call(Conn(None, "agent", who), method, p)

    def task(self):
        return self.t.store.task(self.tid)

    def inbox(self, who):
        return [m for m in self.t.store.messages(who, limit=500) if m["receiver"] == who and m["sender"] == "temple"]

    def owner_notes(self):
        return [t for t in self.t.store.turns("azathoth", limit=500) if not t["user_text"]]

    async def checkpoint(self, value=None):
        """Advance the clock to the next checkpoint and run the sweep."""
        if value is not None:
            self.galaxy.value = value
        task = self.d.get(self.tid)
        task.data["next_check"] = time.time() - 1
        task.save(self.t.store)
        await self.d.sweep()

    async def probe(self, value="0", command="wc -l < out.jsonl"):
        self.galaxy.value = value
        return await self.call("old_a", "task_probe", task_id=self.tid, command=command)

    async def test_queued_assignment_is_bounded_without_false_manager_stop(self):
        await self.checkpoint()
        self.assertEqual(self.task()['state'], 'active')
        self.assertIsNone(self.task()['data']['execution_started_at'])
        self.assertTrue(any('dispatch waiting' in m['text'] for m in self.inbox('shub')))
        r = await self.call('azathoth', 'task_create', executor='shub', title='queued report',
                            text='report', acceptance='facts', deadline=20)
        task = self.d.get(r['task_id'])
        task.deadline = time.time() - 1
        task.data['next_check'] = time.time() - 1
        task.save(self.t.store)
        await self.d.sweep()
        self.assertEqual(self.t.store.task(task.id)['state'], 'failed')
        self.assertEqual(self.t.store.agent('shub')['desired'], 'running')
        self.assertTrue(any('NOT stopped' in m['text'] for m in self.inbox('shub')))

    async def test_flat_overdue_extension_refresh_and_escalation(self):
        import datetime
        await self.probe('10')
        await self.checkpoint('10')
        task = self.d.get(self.tid)
        original = task.deadline = time.time() - 3 * 3600
        task.data['original_deadline'] = original
        task.save(self.t.store)
        history = list(task.samples)
        eta = (datetime.datetime.now() + datetime.timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M')
        async def request(who='old_a'):
            return await self.call(who, 'task_extend', task_id=self.tid, deadline=5,
                                   reason='existing bounded computation', evidence='PID 12, command aggregate, out.json',
                                   expected_completion=eta)
        r = await request()
        self.assertEqual(r['approver'], 'shub')
        before = time.time()
        await self.call('shub', 'task_extension', task_id=self.tid, approve=True, reason='verified process')
        self.assertGreater(self.task()['deadline'], before)
        self.assertEqual(self.task()['data']['original_deadline'], original)
        self.assertEqual(self.task()['data']['samples'], history)
        self.assertEqual(self.task()['data']['misses'], 0)
        self.assertGreater(self.task()['data']['next_check'], before)
        self.assertTrue(all(m['state'] == 'cancelled' for m in self.inbox('old_a') if m['kind'] == 'checkpoint'))
        self.assertEqual((await request())['approver'], 'azathoth')
        await self.call('azathoth', 'task_extension', task_id=self.tid, approve=True, reason='verified')
        self.assertEqual((await request())['approver'], 'owner')

    async def test_expired_approval_does_not_grant_or_keep_pending(self):
        await self.probe('1')
        await self.checkpoint('2')
        await self.call('old_a', 'task_extend', task_id=self.tid, deadline=5, reason='more verified rows')
        task = self.d.get(self.tid)
        deadline = task.deadline
        task.extensions[-1]['until'] = time.time() - 1
        task.save(self.t.store)
        r = await self.call('shub', 'task_extension', task_id=self.tid, approve=True, reason='late')
        self.assertEqual(r['state'], 'denied')
        self.assertEqual(self.task()['deadline'], deadline)
        self.assertFalse(any(e['state'] == 'pending' for e in self.task()['data']['extensions']))

    async def test_manager_on_behalf_cannot_approve_own_request(self):
        await self.probe('1')
        await self.checkpoint('2')
        r = await self.call('shub', 'task_extend', task_id=self.tid, deadline=5, reason='verified worker work')
        self.assertEqual(r['approver'], 'azathoth')
        with self.assertRaises(Denied):
            await self.call('shub', 'task_extension', task_id=self.tid, approve=True, reason='self')

    async def test_existing_producer_reassignment_and_qa_result_routing(self):
        self.t.store.upsert_agent('old_b', 'b', Role.OLD_ONE, 'g1', 'shub', {'task': 'other history'})
        with self.assertRaisesRegex(Denied, 'stop and settle'):
            await self.call('shub', 'task_replace', task_id=self.tid, executor='old_b', reason='handover')
        await self.call('shub', 'stop', agent_id='old_a')
        original = self.task()['deadline']
        await self.call('shub', 'task_replace', task_id=self.tid, executor='old_b', reason='saved producer output')
        self.assertEqual(self.task()['executor'], 'old_b')
        self.assertEqual(self.task()['deadline'], original)
        await self.call('old_b', 'task_submit', task_id=self.tid, evidence='out.json exit0 hash123')
        self.assertTrue(any('pending QA' in m['text'] and 'hash123' in m['text'] for m in self.inbox('shub')))
        with self.assertRaises(ValueError):
            await self.call('shub', 'task_replace', task_id=self.tid, executor='old_b', reason='during review')
        await self.call('yog', 'task_verdict', task_id=self.tid, passed=False, reason='need mapping')
        self.assertTrue(any('[rejected]' in m['text'] for m in self.inbox('old_b')))
        await self.call('old_b', 'task_submit', task_id=self.tid, evidence='mapping verified')
        await self.call('yog', 'task_verdict', task_id=self.tid, passed=True, reason='complete')
        self.assertTrue(any('[accepted]' in m['text'] for m in self.inbox('azathoth')))

    # contract

    async def test_contract_validation(self):
        t = self.task()
        self.assertEqual((t["creator"], t["manager"], t["executor"], t["state"]), ("shub", "shub", "old_a", "active"))
        self.assertAlmostEqual(t["deadline"] - t["created_at"], 3600, delta=5)
        with self.assertRaisesRegex(ValueError, "acceptance"):
            await self.call("shub", "task_create", executor="old_a", title="x", text="y")
        with self.assertRaisesRegex(ValueError, "executor"):
            await self.call("shub", "task_create", title="x", text="y", acceptance="z")
        r = await self.call("shub", "task_create", executor="old_a", title="x", text="y", acceptance="z")
        self.assertAlmostEqual(self.t.store.task(r["task_id"])["deadline"] - time.time(), 7200, delta=5)
        # Authority: azathoth assigns managers; managers only their own old_ones; nobody else creates.
        self.t.store.upsert_agent("old_g", "g", Role.OLD_ONE, "g1", "ghroth", {"task": "t"})
        for who, to in (("shub", "old_g"), ("old_a", "old_a"), ("yog", "old_a"), ("azathoth", "old_a"), ("shub", "ghroth")):
            with self.assertRaises(Denied, msg=(who, to)):
                await self.call(who, "task_create", executor=to, title="x", text="y", acceptance="z")
        r = await self.call("azathoth", "task_create", executor="ghroth", title="fix", text="fix it", acceptance="tests pass")
        self.assertEqual(self.t.store.task(r["task_id"])["manager"], "ghroth")
        # The brief goes to the executor as an ordinary message of that task.
        self.assertTrue(self.t.store.messages(task_id=r["task_id"])[0]["text"] == "fix it")

    async def test_spawn_requires_contract_and_binds_task(self):
        with self.assertRaisesRegex(ValueError, "acceptance"):
            await self.call("shub", "spawn", name="w", galaxy_id="g1", task="t", skill="s")
        self.assertEqual(len([a for a in self.t.store.agents() if a["role"] == Role.OLD_ONE]), 1)
        r = await self.call("shub", "spawn", name="w", galaxy_id="g1", task="t", skill="s", title="w", acceptance="done")
        task = self.t.store.task(r["task_id"])
        self.assertEqual((task["executor"], task["manager"]), (r["id"], "shub"))
        self.assertEqual(self.t.store.agent(r["id"])["spec"]["task_id"], task["id"])
        # Replacement: a new old_one takes over the existing task, which keeps its clock.
        with self.assertRaisesRegex(Denied, "stop and settle"):
            await self.call("shub", "spawn", name="blocked", galaxy_id="g1", task="t", skill="s", task_id=task["id"])
        await self.call("shub", "stop", agent_id=r["id"])
        r2 = await self.call("shub", "spawn", name="w2", galaxy_id="g1", task="t", skill="s", task_id=task["id"])
        self.assertEqual(self.t.store.task(task["id"])["executor"], r2["id"])
        self.assertEqual(self.t.store.task(task["id"])["deadline"], task["deadline"])
        # Ghroth's old_ones live on ghroth's galaxy in a worktree; a manager manages only its own.
        r3 = await self.call("ghroth", "spawn", name="h", task="t", skill="s", title="h", acceptance="merged")
        self.assertEqual(self.t.store.agent(r3["id"])["spec"]["worktree"], f"old/{r3['id']}")
        with self.assertRaises(Denied):
            await self.call("shub", "stop", agent_id=r3["id"])
        with self.assertRaises(Denied):
            await self.call("shub", "send", to=r3["id"], text="hi")
        await self.call("ghroth", "stop", agent_id=r3["id"])
        await self.call("ghroth", "delete", agent_id=r3["id"])
        self.assertIsNone(next((a for a in self.t.store.agents() if a["id"] == r3["id"]), None))

    # probe and the first checkpoint

    async def test_void_on_missing_probe(self):
        await self.call("old_a", "task_started", task_id=self.tid, turn_id="actual-turn")
        await self.checkpoint()
        t = self.task()
        self.assertEqual(t["state"], "failed")
        self.assertEqual(self.t.store.agent("old_a")["desired"], "stopped")
        self.assertTrue(any("任务作废" in n["reply"] for n in self.owner_notes()))
        for who in ("azathoth", "shub"):
            self.assertTrue(any("task voided" in m["text"] for m in self.inbox(who)), who)

    async def test_probe_must_be_numeric_and_only_from_executor(self):
        self.galaxy.value = "not a number"
        with self.assertRaisesRegex(ValueError, "single number"):
            await self.call("old_a", "task_probe", task_id=self.tid, command="echo hi")
        with self.assertRaises(Denied):
            await self.call("shub", "task_probe", task_id=self.tid, command="echo 1")
        r = await self.probe("5")
        self.assertEqual(r["value"], 5)
        self.assertEqual(self.galaxy.calls[-1][1]["agent_id"], "old_a")

    async def test_first_checkpoint_asks_yog_once_and_invalid_probe_voids(self):
        await self.probe("1")
        await self.checkpoint("2")
        await self.checkpoint("3")
        reviews = [m for m in self.inbox("yog") if "[probe review]" in m["text"]]
        self.assertEqual(len(reviews), 1)
        await self.call("yog", "task_probe_verdict", task_id=self.tid, ok=False, reason="counts log lines, not rows")
        self.assertEqual(self.task()["state"], "failed")
        self.assertEqual(self.t.store.agent("old_a")["desired"], "stopped")
        with self.assertRaises(Denied):
            await self.call("shub", "task_probe_verdict", task_id=self.tid, ok=True, reason="")

    # the ladder

    async def test_ladder_steps_and_repeat(self):
        await self.probe("10")
        await self.checkpoint("10")         # 1: nudge the executor
        self.assertEqual(len([m for m in self.inbox("old_a") if "no progress" in m["text"]]), 1)
        self.assertFalse([m for m in self.inbox("shub") if "2 checkpoints" in m["text"]])
        await self.checkpoint("10")         # 2: the superior and a yog diagnosis
        self.assertEqual(len([m for m in self.inbox("shub") if "2 checkpoints" in m["text"]]), 1)
        self.assertTrue([m for m in self.inbox("yog") if "[diagnosis]" in m["text"]])
        self.assertFalse([n for n in self.owner_notes() if "任务告警" in n["reply"]])
        await self.checkpoint("10")         # 3: owner + azathoth, program-written
        alarms = lambda: [n for n in self.owner_notes() if "任务告警" in n["reply"]]
        self.assertEqual(len(alarms()), 1)
        self.assertEqual(len([m for m in self.inbox("azathoth") if "[escalation" in m["text"]]), 1)
        self.assertTrue(all(m["priority"] == 9 for m in self.t.store.messages(limit=500) if m["sender"] == "temple"))
        await self.checkpoint("10")
        await self.checkpoint("10")
        self.assertEqual(len(alarms()), 1)
        await self.checkpoint("10")         # every 3 checkpoints after that
        self.assertEqual(len(alarms()), 2)
        # The owner pauses the alarm.
        await self.d.owner({"action": "quiet", "task_id": self.tid, "quiet": True})
        for _ in range(3):
            await self.checkpoint("10")
        self.assertEqual(len(alarms()), 2)

    async def test_progress_resets_the_ladder_and_non_numeric_is_no_progress(self):
        await self.probe("10")
        await self.checkpoint("10")
        await self.checkpoint("garbage")
        self.assertEqual(self.task()["data"]["misses"], 2)
        await self.checkpoint("11")
        self.assertEqual(self.task()["data"]["misses"], 0)
        await self.checkpoint("11")
        await self.checkpoint("11")
        self.assertFalse([n for n in self.owner_notes() if "任务告警" in n["reply"]])
        self.assertEqual(len([m for m in self.inbox("shub") if "2 checkpoints" in m["text"]]), 2)

    async def test_deadline_passed_escalates_even_with_progress(self):
        await self.probe("1")
        t = self.d.get(self.tid)
        t.deadline = time.time() - 1
        t.save(self.t.store)
        await self.checkpoint("2")
        self.assertTrue([n for n in self.owner_notes() if "已超过截止时间" in n["reply"]])

    # extensions

    async def test_extension_chain_and_refusals(self):
        await self.probe("1")
        with self.assertRaisesRegex(Denied, "no progress"):
            await self.call("old_a", "task_extend", task_id=self.tid, deadline=30, reason="more data")
        await self.checkpoint("2")
        with self.assertRaisesRegex(Denied, "original duration"):
            await self.call("old_a", "task_extend", task_id=self.tid, deadline=61, reason="too long")
        deadline = self.task()["deadline"]
        # 1st: the direct superior (shub); the executor can never decide its own.
        r = await self.call("old_a", "task_extend", task_id=self.tid, deadline=30, reason="more data")
        self.assertEqual(r["approver"], "shub")
        for who in ("old_a", "azathoth", "yog"):
            with self.assertRaises(Denied):
                await self.call(who, "task_extension", task_id=self.tid, approve=True, reason="")
        await self.call("shub", "task_extension", task_id=self.tid, approve=True, reason="ok")
        self.assertAlmostEqual(self.task()["deadline"], deadline + 1800, delta=1)
        # 2nd: azathoth, copied to the owner.
        self.assertEqual((await self.call("old_a", "task_extend", task_id=self.tid, deadline=30, reason="r"))["approver"], "azathoth")
        await self.call("azathoth", "task_extension", task_id=self.tid, approve=True, reason="ok")
        self.assertTrue([n for n in self.owner_notes() if "阿撒托斯对「collect」延期决定" in n["reply"]])
        # 3rd: only the owner.
        self.assertEqual((await self.call("old_a", "task_extend", task_id=self.tid, deadline=30, reason="r"))["approver"], "owner")
        with self.assertRaises(Denied):
            await self.call("azathoth", "task_extension", task_id=self.tid, approve=True, reason="")
        self.assertTrue([n for n in self.owner_notes() if "只能由你批准" in n["reply"]])
        await self.d.owner({"action": "extension", "task_id": self.tid, "approve": False, "reason": "no"})
        exts = self.task()["data"]["extensions"]
        self.assertEqual([e["state"] for e in exts], ["refused", "refused", "approved", "approved", "denied"])

    # blocked on the owner

    async def test_blocked_on_owner_pauses_the_clock(self):
        await self.probe("1")
        deadline = self.task()["deadline"]
        await self.call("old_a", "task_block", task_id=self.tid, question="Which bucket?")
        self.assertEqual(self.task()["state"], "blocked_on_owner")
        self.assertTrue([n for n in self.owner_notes() if "Which bucket?" in n["reply"]])
        for _ in range(4):
            await self.checkpoint("1")
        self.assertFalse([m for m in self.inbox("old_a") if "no progress" in m["text"]])
        t = self.d.get(self.tid)
        t.data["paused_at"] -= 300        # five minutes paused
        t.save(self.t.store)
        await self.d.owner({"action": "resolve", "task_id": self.tid, "note": "use bucket b"})
        self.assertEqual(self.task()["state"], "active")
        self.assertAlmostEqual(self.task()["deadline"], deadline + 300, delta=5)
        self.assertTrue([m for m in self.inbox("old_a") if "use bucket b" in m["text"]])

    # acceptance

    async def test_yog_acceptance_and_owner_takeover(self):
        await self.probe("1")
        with self.assertRaises(Denied):
            await self.call("yog", "task_submit", task_id=self.tid, evidence="x")
        await self.call("old_a", "task_submit", task_id=self.tid, evidence="out.jsonl has 1000 rows")
        self.assertTrue([m for m in self.inbox("yog") if "[acceptance review]" in m["text"]])
        for who in ("shub", "azathoth", "old_a"):
            with self.assertRaises(Denied):
                await self.call(who, "task_verdict", task_id=self.tid, passed=True, reason="")
        await self.call("yog", "task_verdict", task_id=self.tid, passed=False, reason="only 900 rows")
        self.assertEqual(self.task()["state"], "active")
        for who in ("old_a", "shub"):
            self.assertTrue([m for m in self.inbox(who) if "only 900 rows" in m["text"]])
        # Resubmitted; yog stays silent for a checkpoint, so the owner decides.
        await self.call("old_a", "task_submit", task_id=self.tid, evidence="now 1000")
        t = self.d.get(self.tid)
        t.data["verdict_due"] = time.time() - 1
        t.save(self.t.store)
        await self.d.sweep()
        await self.d.sweep()
        self.assertEqual(len([n for n in self.owner_notes() if "验收超时" in n["reply"]]), 1)
        await self.d.owner({"action": "verdict", "task_id": self.tid, "passed": True})
        self.assertEqual(self.task()["state"], "accepted")

    async def test_manager_task_accepted_by_azathoth_not_yog(self):
        r = await self.call("azathoth", "task_create", executor="shub", title="m", text="t", acceptance="a")
        await self.call("shub", "task_submit", task_id=r["task_id"], evidence="done")
        self.assertFalse([m for m in self.inbox("yog") if r["task_id"] == m["task_id"]])
        with self.assertRaises(Denied):
            await self.call("yog", "task_verdict", task_id=r["task_id"], passed=True, reason="")
        await self.call("azathoth", "task_verdict", task_id=r["task_id"], passed=True, reason="fine")
        self.assertEqual(self.t.store.task(r["task_id"])["state"], "accepted")

    # migration

    async def test_migration_grace(self):
        store = self.t.store
        legacy = store.add_task("legacy")["id"]
        store.x("UPDATE tasks SET state='open', data='{}' WHERE id=?", (legacy,))
        old_done = store.add_task("finished")["id"]
        store.x("UPDATE tasks SET state='done' WHERE id=?", (old_done,))
        store.set_setting("continuations", {"x": {}})
        store.add_message("azathoth", "ghroth", "fix it", task_id=legacy)
        await self.d.migrate()
        await self.d.migrate()              # a second restart changes nothing
        t = store.task(legacy)
        self.assertEqual((t["state"], t["executor"], t["manager"]), ("active", "ghroth", "ghroth"))
        self.assertAlmostEqual(t["data"]["next_check"] - time.time(), self.d.grace, delta=5)
        self.assertEqual(store.task(old_done)["state"], "accepted")
        self.assertIsNone(store.setting("continuations"))
        self.assertEqual(len([m for m in self.inbox("ghroth") if "task discipline is now enforced" in m["text"]]), 1)
        # Still no probe when the grace ends: voided (a god is never stopped).
        t = self.d.get(legacy)
        t.data["next_check"] = time.time() - 1
        t.save(store)
        await self.d.sweep()
        self.assertEqual(store.task(legacy)["state"], "failed")
        self.assertEqual(store.agent("ghroth")["desired"], "running")


class LiveTemple(HarnessServer):
    """The clock on a real temple + galaxy: the galaxy runs the probe, checkpoints run without any model."""
    temple_extra = {"checkpoint_interval": 10, "task_tick": 0.3}

    def test_probe_runs_on_the_galaxy_and_ladder_runs_without_yog(self):
        self.api("force_stop", {"agent_id": "yo"})      # the ladder never waits on yog's model
        self.addCleanup(self.api, "resume", {"agent_id": "yo"})
        out = self.dir / "work-out.txt"
        out.write_text("x")
        spawn = json.dumps({"name": "worker", "galaxy_id": "g1", "task": "collect", "skill": "s", "cwd": str(self.dir),
                            "title": "live", "acceptance": "work-out.txt grows", "deadline": 30})
        self.chat("az", "CALL temple_send " + json.dumps({"to": "sh", "text": "CALL temple_spawn " + spawn}))
        worker = self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["name"] == "worker"), 30)
        tid = next(t for t in self.api("tasks?search=live") if t["executor"] == worker["id"])["id"]
        # The executor registers its probe with its own tool call; the galaxy runs it in the executor's cwd.
        probe = "CALL task_probe " + json.dumps({"task_id": tid, "command": "wc -c < work-out.txt"})
        self.chat("az", "CALL temple_send " + json.dumps({"to": "sh", "text": "CALL temple_send " + json.dumps(
            {"to": worker["id"], "task_id": tid, "text": probe})}))
        task = lambda: self.api(f"task?task_id={tid}")
        self.wait(lambda: task()["probe"] == "wc -c < work-out.txt", 30)
        self.assertEqual(task()["data"]["samples"][0]["value"], 1)
        # No growth: the program walks the ladder up to the owner by itself.
        alarm = self.wait(lambda: next(t for t in self.api("chat?agent_id=az&limit=100")
                                       if t["task_id"] == tid and "任务告警" in t["reply"]), 60)
        self.assertIn("连续 3 个检查点没有进展", alarm["reply"])
        texts = [m["text"] for m in self.api(f"messages?task_id={tid}") if m["sender"] == "temple"]
        self.assertTrue(any("no progress" in t for t in texts) and any("2 checkpoints" in t for t in texts))
        # Growth resets the ladder.
        out.write_text("x" * 50)
        self.wait(lambda: task()["data"]["samples"][-1]["value"] == 50 and task()["data"]["misses"] == 0, 25)
