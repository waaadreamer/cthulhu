"""One-click deploy against a real checkout: a bare repo stands in for GitHub."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cthulhu import deploy  # noqa: E402


def sh(*cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class UpdateCode(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.origin = self.dir / "origin.git"
        self.work = self.dir / "work"          # the developer's clone that pushes
        self.node = self.dir / "node"          # a galaxy's checkout
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.dir)
        shutil.copytree(ROOT / "cthulhu", self.work / "cthulhu", ignore=shutil.ignore_patterns("static", "__pycache__"))
        (self.work / "pyproject.toml").write_text("[project]\nname='x'\nversion='1'\n")
        for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1"],
                  ["remote", "add", "origin", str(self.origin)], ["push", "-q", "origin", "main"]):
            sh("git", *c, cwd=self.work)
        sh("git", "clone", "-q", str(self.origin), str(self.node), cwd=self.dir)
        self.v1 = sh("git", "rev-parse", "HEAD", cwd=self.node)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def commit(self, path, text, msg):
        (self.work / path).write_text(text)
        sh("git", "add", "-A", cwd=self.work)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=self.work)
        sh("git", "push", "-q", "origin", "main", cwd=self.work)
        return sh("git", "rev-parse", "HEAD", cwd=self.work)

    def update(self, rev):
        return asyncio.run(deploy.update_code(self.node, rev))

    def test_fast_forward_to_pushed_revision(self):
        v2 = self.commit("cthulhu/NOTE.txt", "v2", "v2")
        r = self.update(v2)
        self.assertEqual((r["prev"], r["revision"], r["deps"]), (self.v1, v2, False))
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), v2)
        self.assertTrue(self.update(v2)["unchanged"])

    def test_rejects_unpushed_dirty_and_broken(self):
        with self.assertRaises(RuntimeError):
            self.update("0" * 40)                               # not a commit on main
        (self.node / "cthulhu" / "NOTE.txt").write_text("local edit")
        sh("git", "add", "-A", cwd=self.node)
        v2 = self.commit("cthulhu/OTHER.txt", "x", "v2")
        with self.assertRaisesRegex(RuntimeError, "未提交"):
            self.update(v2)
        sh("git", "reset", "-q", "--hard", self.v1, cwd=self.node)
        # A revision whose code does not import is rolled back.
        bad = self.commit("cthulhu/agent.py", "this is not python(", "broken")
        with self.assertRaises(RuntimeError):
            self.update(bad)
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), self.v1)

    def test_remote_rollback_rpc_preserves_unrelated_checkout(self):
        from cthulhu.galaxy import Galaxy
        from unittest.mock import patch
        from types import SimpleNamespace
        v2 = self.commit("cthulhu/NOTE.txt", "v2", "v2")
        self.update(v2)
        frame = {"type": "rpc", "method": "deploy_rollback", "params": {"prev": self.v1, "target": v2}}
        with patch.object(deploy, "ROOT", self.node):
            self.assertEqual(asyncio.run(Galaxy._frame(SimpleNamespace(), frame)), {"revision": self.v1})
            asyncio.run(Galaxy._frame(SimpleNamespace(), frame))  # Lost response: safe to reconcile again.
            v3 = self.commit("cthulhu/NOTE.txt", "v3", "v3")
            self.update(v3)
            with self.assertRaisesRegex(RuntimeError, "checkout changed"):
                asyncio.run(Galaxy._frame(SimpleNamespace(), frame))
            self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), v3)

    def test_startup_guard_rolls_back_after_three_failed_starts(self):
        v2 = self.commit("cthulhu/NOTE.txt", "v2", "v2")
        self.update(v2)
        state = self.dir / "state"
        state.mkdir()
        cfg = self.dir / "galaxy.json"
        cfg.write_text(json.dumps({"state": str(state)}))
        (state / deploy.PENDING).write_text(json.dumps({"prev": self.v1, "target": v2}))
        old_root, deploy.ROOT = deploy.ROOT, self.node
        try:
            for _ in range(3):
                deploy.guard_startup(str(cfg))
            self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), v2)
            deploy.guard_startup(str(cfg))
            self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), self.v1)
            self.assertFalse((state / deploy.PENDING).exists())
        finally:
            deploy.ROOT = old_root


if __name__ == "__main__":
    unittest.main()


class EndToEnd(unittest.TestCase):
    """A temple and a galaxy running from a checkout of a local 'GitHub'; deploy a new pushed commit."""

    @classmethod
    def setUpClass(cls):
        import secrets, socket, time, urllib.request
        from cthulhu.store import Store
        from cthulhu.temple import password_hash
        cls.time, cls.urllib = time, urllib
        cls.dir = Path(tempfile.mkdtemp())
        origin, work, cls.node = cls.dir / "origin.git", cls.dir / "work", cls.dir / "node"
        sh("git", "init", "-q", "--bare", "-b", "main", str(origin), cwd=cls.dir)
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "venv", "node_modules", "web", "__pycache__", "*.egg-info"))
        for c in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1"],
                  ["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "main"]):
            sh("git", *c, cwd=work)
        sh("git", "clone", "-q", str(origin), str(cls.node), cwd=cls.dir)
        cls.work, cls.origin = work, origin
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        gtoken = secrets.token_hex(16)
        temple = {"secret": secrets.token_hex(16), "db": str(cls.dir / "temple.db"), "listen": ["127.0.0.1", cls.port],
                  "galaxies": [{"id": "g1", "name": "test", "token": gtoken}],
                  "gods": [{"id": v, "role": k, "galaxy": "g1"} for k, v in
                           {"azathoth": "az", "shub": "sh", "yog": "yo", "ghroth": "gh"}.items()],
                  "deploy": {"repo": str(origin)}}
        fake = str(cls.node / "tests" / "fake_codex.py")
        galaxy = {"id": "g1", "token": gtoken, "temple_ws": f"ws://127.0.0.1:{cls.port}/ws", "state": str(cls.dir / "state"),
                  "codex": fake, "cwd": str(cls.dir), "patrol_interval": 3600, "runtimes": {}}
        (cls.dir / "temple.json").write_text(json.dumps(temple))
        (cls.dir / "galaxy.json").write_text(json.dumps(galaxy))
        os.chmod(fake, 0o755)
        salt = secrets.token_hex(16)
        Store(cls.dir / "temple.db").set_account("owner", salt, password_hash("pw", salt))
        cls.env = os.environ | {"PYTHONPATH": str(cls.node), "CTHULHU_FAKE_CLAUDE": "1"}
        # Supervisors that restart whatever exits, like systemd Restart=always / run.sh.
        cls.procs = [cls.supervise("temple", "--config", str(cls.dir / "temple.json"), delay=1)]
        time.sleep(2)
        cls.procs.append(cls.supervise("galaxy", "--config", str(cls.dir / "galaxy.json"), delay=1))
        cls.cookie = cls.login()
        cls.wait(lambda: all(a["online"] for a in cls.api("directory")["agents"]), 40)

    @classmethod
    def supervise(cls, *args, delay):
        py = str(ROOT / "venv" / "bin" / "python")
        loop = f"while true; do {py} -m cthulhu {' '.join(args)} >> {cls.dir}/{args[0]}.log 2>&1; sleep {delay}; done"
        return subprocess.Popen(["bash", "-c", loop], cwd=cls.node, env=cls.env, start_new_session=True)

    @classmethod
    def tearDownClass(cls):
        import signal
        # Freeze the supervisor loops, then SIGTERM so the galaxy stops its agents (each in its own session).
        for p in cls.procs:
            try:
                os.killpg(p.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass
        subprocess.run(["pkill", "-TERM", "-f", f"cthulhu (temple|galaxy) --config {cls.dir}/"])
        deadline = time.time() + 20
        while time.time() < deadline and subprocess.run(["pgrep", "-f", f"{cls.dir}/"], capture_output=True).stdout:
            time.sleep(0.5)
        subprocess.run(["pkill", "-KILL", "-f", f"{cls.dir}/"])
        for p in cls.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=10)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def login(cls):
        while True:
            try:
                req = cls.urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/login", method="POST",
                                                 data=json.dumps({"username": "owner", "password": "pw"}).encode(),
                                                 headers={"content-type": "application/json"})
                with cls.urllib.request.urlopen(req) as r:
                    return r.headers["set-cookie"].split(";")[0]
            except OSError:
                cls.time.sleep(0.5)

    @classmethod
    def api(cls, path, body=None):
        req = cls.urllib.request.Request(f"http://127.0.0.1:{cls.port}/api/{path}",
                                         data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET",
                                         headers={"cookie": cls.cookie, "x-cthulhu": "1", "content-type": "application/json"})
        try:
            with cls.urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except cls.urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read() or b"{}")}

    @classmethod
    def wait(cls, cond, timeout=60):
        end = cls.time.time() + timeout
        while cls.time.time() < end:
            try:
                v = cond()
                if v:
                    return v
            except Exception:
                pass
            cls.time.sleep(0.5)
        raise AssertionError("timed out")

    def push(self, name):
        (self.work / "cthulhu" / name).write_text(name)
        sh("git", "add", "-A", cwd=self.work)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name, cwd=self.work)
        sh("git", "push", "-q", "origin", "main", cwd=self.work)
        return sh("git", "rev-parse", "HEAD", cwd=self.work)

    def finished(self, rev):
        def check():
            jobs = self.api("deploy").get("jobs", [])
            return jobs and jobs[0]["revision"] == rev and jobs[0]["state"] in ("done", "failed") and jobs[0]
        return self.wait(check, 120)

    def chat(self, text):
        msg = self.api("send", {"to": "az", "text": text})
        return self.wait(lambda: next(t for t in self.api("chat?agent_id=az") if t["id"] == msg["id"] and t["status"] == "done"))

    def test_1_owner_deploys_all(self):
        rev = self.push("NOTE1.txt")
        self.assertEqual(self.api("deploy_latest")["revision"], rev)
        job = self.api("deploy", {"galaxies": "all"})
        self.assertEqual(job["revision"], rev)
        job = self.finished(rev)
        self.assertEqual(job["state"], "done", job["steps"])
        self.assertEqual(sh("git", "rev-parse", "HEAD", cwd=self.node), rev)
        # The galaxy came back on the new revision; the result was reported to azathoth.
        self.wait(lambda: self.api("directory")["galaxies"][0]["revision"] == rev)
        self.wait(lambda: any(m["sender"] == "temple" and "[deploy" in m["text"] for m in self.api("messages?agent_id=az")))
        self.assertEqual(self.api("deploy")["temple"]["revision"], rev)

    def test_2_ghroth_requests_azathoth_approves(self):
        rev = self.push("NOTE2.txt")
        self.wait(lambda: all(a["online"] for a in self.api("directory")["agents"]), 60)
        self.api("send", {"to": "gh", "text": 'CALL temple_deploy_request {"reason": "ship NOTE2"}'})
        req = self.wait(lambda: next(m for m in self.api("messages?agent_id=az") if "deploy request" in m["text"]))
        rid = req["text"].split("deploy request ")[1].split("]")[0]
        turn = self.chat('CALL temple_deploy {"request_id": "%s"}' % rid)
        self.assertIn(rev, turn["reply"])
        self.assertEqual(self.finished(rev)["state"], "done")

    def test_3_permissions(self):
        self.wait(lambda: all(a["online"] for a in self.api("directory")["agents"]), 60)
        # Ghroth cannot deploy directly; it has no such tool.
        turn = self.chat("CALL temple_deploy_status {}")       # azathoth lacks this ghroth-only tool name
        self.assertIn("error", turn["reply"])
        req = self.urllib.request.Request(f"http://127.0.0.1:{self.port}/api/deploy", method="POST", data=b"{}",
                                          headers={"content-type": "application/json", "x-cthulhu": "1"})
        with self.assertRaises(self.urllib.error.HTTPError) as cm:
            self.urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)
