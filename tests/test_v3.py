"""v3 end-to-end: real temple + galaxy (with hub) + agent processes, fake codex and fake Galaxy AI.

Checkpoints run every few seconds instead of 10 minutes. Old_ones are steered with
fake-codex `CALL <tool> <json>` lines sent by the owner (who may message any old_one).
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cthulhu.store import Store  # noqa: E402
from cthulhu.temple import password_hash  # noqa: E402
from tests.test_universe import FAKE, free_port  # noqa: E402

CHECK = 4.0


class V3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.chmod(FAKE, 0o755)
        cls.dir = Path(tempfile.mkdtemp())
        cls.port = free_port()
        gtoken = secrets.token_hex(16)
        temple = {"secret": secrets.token_hex(16), "db": str(cls.dir / "temple.db"), "listen": ["127.0.0.1", cls.port],
                  "digest_every": 3, "galaxies": [{"id": "g1", "name": "test", "token": gtoken,
                                                   "description": "测试用星系：跑测试任务"}],
                  "gods": [{"id": "az", "role": "azathoth", "galaxy": "g1"}],
                  "old_one_runtime": {"backend": "codex", "model": "fake"}}
        (cls.dir / "work").mkdir()
        galaxy = {"id": "g1", "token": gtoken, "temple_ws": f"ws://127.0.0.1:{cls.port}/ws", "state": str(cls.dir / "galaxy"),
                  "codex": FAKE, "cwd": str(cls.dir / "work"), "check_every": CHECK, "start_grace": 60}
        (cls.dir / "temple.json").write_text(json.dumps(temple))
        (cls.dir / "galaxy.json").write_text(json.dumps(galaxy))
        salt = secrets.token_hex(16)
        Store(cls.dir / "temple.db").set_account("owner", salt, password_hash("pw", salt))
        py = str(ROOT / "venv" / "bin" / "python")
        if not Path(py).exists():
            py = "/opt/cthulhu/venv/bin/python"
        env = os.environ | {"PYTHONPATH": str(ROOT), "CTHULHU_FAKE_GALAXY_AI": "1"}
        cls.logs = open(cls.dir / "procs.log", "wb")
        cls.temple = subprocess.Popen([py, "-m", "cthulhu", "temple", "--config", str(cls.dir / "temple.json")], env=env,
                                      stdout=cls.logs, stderr=cls.logs)
        cls.galaxy = subprocess.Popen([py, "-m", "cthulhu", "galaxy", "--config", str(cls.dir / "galaxy.json")], env=env,
                                      stdout=cls.logs, stderr=cls.logs)
        cls.cookie = cls.wait(cls.login, 30)
        cls.wait(lambda: next(g for g in cls.api("directory")["galaxies"] if g["id"] == "g1")["hub"], 30)
        cls.wait(lambda: next(a for a in cls.api("directory")["agents"] if a["id"] == "az")["online"], 30)

    @classmethod
    def tearDownClass(cls):
        cls.galaxy.terminate()
        cls.galaxy.wait(20)
        cls.temple.terminate()
        cls.temple.wait(5)

    @classmethod
    def login(cls):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/login", method="POST",
                                     data=json.dumps({"username": "owner", "password": "pw"}).encode(),
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return r.headers["set-cookie"].split(";")[0]

    @classmethod
    def api(cls, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET",
                                     headers={"cookie": cls.cookie, "x-cthulhu": "1", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}

    @staticmethod
    def wait(cond, timeout=30):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            try:
                v = cond()
                if v:
                    return v
            except Exception as e:
                last = e
            time.sleep(0.3)
        raise AssertionError(f"timed out ({last!r})")

    def chat(self, text):
        msg = self.api("send", {"to": "az", "text": text})
        return self.wait(lambda: next(t for t in self.api("chat?agent_id=az") if t["id"] == msg["id"] and t["status"] == "done"), 60)

    def new_task(self, description, acceptance="ok"):
        turn = self.chat("CALL new_task " + json.dumps({"galaxy": "g1", "description": description, "short_name": description[:24], "acceptance": acceptance},
                                                       ensure_ascii=False))
        self.assertIn("task_id", turn["reply"], turn["reply"])
        return json.loads(turn["reply"].removeprefix("tool: "))

    def task(self, task_id):
        return self.api(f"task?task_id={task_id}")

    def tell(self, agent_id, call, args):
        """Make an old_one call one of its tools (the owner may message any old_one)."""
        r = self.api("send", {"to": agent_id, "text": f"CALL {call} " + json.dumps(args, ensure_ascii=False)})
        self.assertNotIn("error", r, r)
        # One tool call per turn: wait until the old_one has handled it before the next one.
        self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={agent_id}") if m["id"] == r["id"])["state"] == "handled", 60)

    def az_messages(self, kind=None):
        return [m for m in self.api("messages?agent_id=az&limit=500") if m["receiver"] == "az" and (kind is None or m["kind"] == kind)]

    def test_1_full_lifecycle_probe_digest_finish_review_close_reuse(self):
        work = self.dir / "work" / "out.txt"
        work.write_text("")
        t = self.new_task("写 out.txt REPOalpha")
        tid, aid = t["task_id"], t["agent_id"]
        self.assertFalse(t["reused"])
        self.wait(lambda: self.task(tid)["state"] == "awaiting_probe")
        # The new old_one comes up and receives its brief.
        self.wait(lambda: next(a for a in self.api("directory")["agents"] if a["id"] == aid)["online"], 60)
        # A probe with a non-number output is refused synchronously; a bad one is rejected by the AI.
        self.tell(aid, "register_task_probe", {"command": "echo hello", "rationale": "x"})
        self.tell(aid, "register_task_probe", {"command": "echo 1 # BADPROBE", "rationale": "x"})
        self.wait(lambda: any(e["kind"] == "probe_rejected" for e in self.task(tid)["data"]["events"]))
        self.assertEqual(self.task(tid)["state"], "awaiting_probe")
        self.tell(aid, "register_task_probe", {"command": f"wc -c < {work}", "rationale": "bytes written"})
        self.wait(lambda: self.task(tid)["state"] == "running")
        # No growth -> nudge at P9 with the probe footer, and an attention line in the digest.
        nudge = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={aid}")
                                       if m["kind"] == "checkpoint" and "催办" in m["text"]), 30)
        self.assertEqual(nudge["priority"], 9)
        self.assertIn("[probe] 当前 stage 1", nudge["text"])
        digest = self.wait(lambda: next(m for m in self.az_messages("digest")
                                        if m.get("task_id") == tid and "无增长" in m["text"]), 30)
        self.assertEqual(digest["priority"], 6)
        self.assertIn(tid, digest["id"])
        # The durable digest is also exactly one task-owned process row; no raw
        # mixed-task owner note plus a second judgement message.
        rows = [row for row in self.api("chat?agent_id=az&limit=200") if row["id"] == digest["id"]]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], tid)
        self.assertTrue(rows[0]["report"]["event_ids"])
        self.assertNotIn("###", rows[0]["reply"])
        # Growth -> ok in the digest.
        work.write_text("hello world")
        self.wait(lambda: any(e["kind"] == "checkpoint" and " ok " in e["text"] for e in self.task(tid)["data"]["events"]), 30)
        # finish -> review (checkpoints paused), direct P8 message to azathoth.
        self.tell(aid, "finish_task", {"evidence": f"{work} has 11 bytes"})
        self.wait(lambda: self.task(tid)["state"] == "review")
        review = self.wait(lambda: next(m for m in self.az_messages("task_finished") if tid in m["text"]))
        self.assertEqual(review["priority"], 8)
        n = self.task(tid)["data"]["checkpoint"]["n"]
        time.sleep(CHECK * 1.5)
        self.assertEqual(self.task(tid)["data"]["checkpoint"]["n"], n)
        # Rejected review: continue_task resumes checkpoints and nudges the old_one with a strong footer.
        r = self.chat("CALL continue_task " + json.dumps({"task_id": tid, "instruction": "还要写第二行"}, ensure_ascii=False))
        self.assertIn("running", r["reply"])
        cont = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={aid}") if m["kind"] == "task_control"))
        self.assertIn("新指令可能改变了工作内容", cont["text"])
        self.tell(aid, "finish_task", {"evidence": "done again"})
        self.wait(lambda: self.task(tid)["state"] == "review")
        self.chat("CALL close_task " + json.dumps({"task_id": tid, "note": "好"}, ensure_ascii=False))
        self.wait(lambda: self.task(tid)["state"] == "closed")
        # The galaxy table and the bindings table are in azathoth's turn header.
        turn = self.chat("SHOW_CONTEXT")
        self.assertIn("<bindings>", turn["reply"])
        self.assertIn(tid, turn["reply"])
        self.assertIn("<galaxies>", turn["reply"])
        self.assertIn("g1（test）", turn["reply"])
        self.assertIn("用途：测试用星系", turn["reply"])
        self.assertIn(f"{aid}", turn["reply"].split("</galaxies>")[0])
        # A related task reuses the now-free old_one; an unrelated one would not.
        t2 = self.new_task("继续 REPOalpha 的工作")
        self.assertTrue(t2["reused"], t2)
        self.assertEqual(t2["agent_id"], aid)
        self.chat("CALL cancel_task " + json.dumps({"task_id": t2["task_id"], "reason": "测试"}, ensure_ascii=False))
        self.wait(lambda: self.task(t2["task_id"])["state"] == "cancelled")

    def test_2_no_probe_demand_then_respawn_then_suspend(self):
        t = self.new_task("一个从不登记探针的任务 REPObeta")
        tid, first = t["task_id"], t["agent_id"]
        demand = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={first}")
                                        if m["kind"] == "checkpoint" and m["priority"] == 10), 60)
        self.assertIn("register_task_probe", demand["text"])
        second = self.wait(lambda: (x := self.task(tid))["executor"] != first and x["executor"], 60)
        task = self.task(tid)
        self.assertEqual(task["data"]["respawns"], 1)
        brief = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={second}") if m["kind"] == "task_brief"))
        self.assertIn("<handover>", brief["text"])
        old = next(a for a in self.api("directory")["agents"] if a["id"] == first)
        self.assertEqual(old["desired"], "stopped")
        self.wait(lambda: self.task(tid)["state"] == "suspended", 60)
        self.wait(lambda: any(tid in m["text"] and "挂起" in m["text"] for m in self.az_messages("digest")), 30)

    def test_3_stage_update_report_rate_limits_and_peer_messages(self):
        work = self.dir / "work" / "stage.txt"
        work.write_text("abc")
        a = self.new_task("阶段任务 REPOgamma")
        b = self.new_task("另一个任务 REPOdelta")
        self.assertNotEqual(a["agent_id"], b["agent_id"])
        aid, bid = a["agent_id"], b["agent_id"]
        self.wait(lambda: next(x for x in self.api("directory")["agents"] if x["id"] == aid)["online"], 60)
        self.tell(aid, "register_task_probe", {"command": f"wc -c < {work}", "rationale": "bytes"})
        self.wait(lambda: self.task(a["task_id"])["state"] == "running")
        # An update without stage_note is refused; a dodge is rejected and the old probe stays.
        self.tell(aid, "register_task_probe", {"command": "echo 5", "rationale": "x", "stage_note": "DODGE 换个数字"})
        self.wait(lambda: any(e["kind"] == "probe_rejected" for e in self.task(a["task_id"])["data"]["events"]))
        self.assertEqual(self.task(a["task_id"])["data"]["probe"]["stage"], 1)
        self.tell(aid, "register_task_probe", {"command": "echo 7", "rationale": "steps", "stage_note": "下载完成，开始训练"})
        self.wait(lambda: self.task(a["task_id"])["data"]["probe"]["stage"] == 2)
        self.assertEqual(self.task(a["task_id"])["data"]["probe"]["baseline"], 7)
        # report goes straight to azathoth; a second within 10 minutes is rate limited.
        self.tell(aid, "report", {"text": "进展：阶段一完成"})
        self.wait(lambda: any("阶段一完成" in m["text"] and m["kind"] == "report" for m in self.az_messages()))
        self.tell(aid, "report", {"text": "第二次汇报", "blocker": True})
        self.wait(lambda: any("rate_limited" in (r.get("last_reply") or "") for r in [self.api(f"latest?agent_id={aid}")]))
        self.assertFalse(any("第二次汇报" in m["text"] for m in self.az_messages()))
        # Peer messages are a separate bucket: allowed right after a report, then limited per pair.
        self.tell(aid, "send", {"to": bid, "text": "问题：你的输出在哪？"})
        q = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={bid}") if m["sender"] == aid))
        self.tell(aid, "send", {"to": bid, "text": "再问一次"})
        self.wait(lambda: "rate_limited" in (self.api(f"latest?agent_id={aid}").get("last_reply") or ""))
        # The reply is exempt once per incoming message.
        self.tell(bid, "send", {"to": aid, "text": "在 /tmp", "reply_to": q["id"]})
        self.wait(lambda: any(m["sender"] == bid for m in self.api(f"hub_messages?agent_id={aid}")))
        # Answering azathoth's question is exempt once, even with the report bucket used up.
        self.chat("CALL temple_send " + json.dumps({"to": aid, "text": "问题：产物在哪？"}, ensure_ascii=False))
        q = self.wait(lambda: next(m for m in self.api(f"hub_messages?agent_id={aid}") if m["text"].startswith("问题：产物在哪")))
        self.tell(aid, "report", {"text": "在 stage.txt", "reply_to": q["id"]})
        self.wait(lambda: any("在 stage.txt" in m["text"] and "回复" in m["text"] for m in self.az_messages()))
        self.tell(aid, "report", {"text": "再答一次", "reply_to": q["id"]})
        self.wait(lambda: "rate_limited" in (self.api(f"latest?agent_id={aid}").get("last_reply") or ""))
        # Old_ones cannot message azathoth directly.
        self.tell(aid, "send", {"to": "az", "text": "hi"})
        self.wait(lambda: "report" in (self.api(f"latest?agent_id={aid}").get("last_reply") or ""))
        for t in (a, b):
            self.chat("CALL cancel_task " + json.dumps({"task_id": t["task_id"], "reason": "测试结束"}, ensure_ascii=False))

    def test_4_owner_can_not_be_bypassed_for_sudo(self):
        # Without a configured sudo galaxy the request is still queued for the owner; rejecting returns to azathoth.
        msg = self.api("send", {"to": "az", "text": "CALL sudo_request " + json.dumps({"command": "id", "reason": "测试"})})
        pending = self.wait(lambda: next(a for a in self.api("approvals?state=pending")))
        self.assertEqual(pending["command"], "id")
        self.api("approvals", {"id": pending["id"], "approve": False, "note": "不行"})
        turn = self.wait(lambda: next(t for t in self.api("chat?agent_id=az") if t["id"] == msg["id"] and t["status"] == "done"))
        self.assertIn("rejected", turn["reply"])
        self.assertEqual(self.api("approvals")[0]["state"], "rejected")


if __name__ == "__main__":
    unittest.main()
