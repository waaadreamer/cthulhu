"""Hub store: the galaxy-local database the hub is authoritative for (design §1.2, D1).

Task details, old_ones, their messages and turn logs, and the outbox of events
still to be delivered to the temple all live here, so a temple restart or a
dropped link never loses a checkpoint.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS old_ones (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    desired TEXT NOT NULL DEFAULT 'running',   -- running | stopped | deleted
    epoch INTEGER NOT NULL DEFAULT 1,
    thread_id TEXT,
    spec TEXT NOT NULL DEFAULT '{}',            -- cwd, task history
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    executor TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_role TEXT NOT NULL DEFAULT '',
    receiver TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message',
    priority INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    reply_to TEXT,
    task_id TEXT,
    state TEXT NOT NULL DEFAULT 'queued',       -- queued | delivered | handled | cancelled
    replied INTEGER NOT NULL DEFAULT 0,         -- its one rate-limit-exempt reply was used
    created_at REAL NOT NULL,
    handled_at REAL
);
CREATE INDEX IF NOT EXISTS hub_messages_receiver ON messages(receiver, state);
CREATE TABLE IF NOT EXISTS turn_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT,
    thread TEXT NOT NULL,
    input TEXT NOT NULL,
    reply TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS hub_turn_log_agent ON turn_log(agent_id, id);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    params TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class HubStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = _row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        try:
            self.db.execute("ALTER TABLE turn_log ADD COLUMN task_id TEXT")
        except sqlite3.OperationalError:
            pass
        self.lock = threading.Lock()
        path.chmod(0o600)

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # settings

    def setting(self, key: str, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def set_setting(self, key: str, value) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    # old_ones

    def add_old_one(self, name: str, spec: dict) -> dict:
        aid, now = new_id("old"), time.time()
        self.x("INSERT INTO old_ones (id, name, spec, created_at, updated_at) VALUES (?,?,?,?,?)",
               (aid, name, json.dumps(spec, ensure_ascii=False), now, now))
        return self.old_one(aid)

    def old_one(self, aid: str) -> dict | None:
        a = self.one("SELECT * FROM old_ones WHERE id=?", (aid,))
        if a:
            a["spec"] = json.loads(a["spec"])
        return a

    def old_ones(self, include_stopped=True) -> list[dict]:
        sql = "SELECT * FROM old_ones WHERE desired!='deleted'" + ("" if include_stopped else " AND desired='running'")
        rows = self.q(sql + " ORDER BY created_at")
        for a in rows:
            a["spec"] = json.loads(a["spec"])
        return rows

    def set_old_one(self, aid: str, **fields) -> None:
        if "spec" in fields:
            fields["spec"] = json.dumps(fields["spec"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE old_ones SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), aid))

    # tasks

    def add_task(self, task_id: str, description: str, acceptance: str, state: str, executor: str | None, data: dict) -> None:
        now = time.time()
        self.x("INSERT INTO tasks (id, description, acceptance, state, executor, data, created_at, updated_at)"
               " VALUES (?,?,?,?,?,?,?,?)",
               (task_id, description, acceptance, state, executor, json.dumps(data, ensure_ascii=False), now, now))

    def task(self, task_id: str) -> dict | None:
        t = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if t:
            t["data"] = json.loads(t["data"])
        return t

    def tasks(self, states: tuple | None = None, executor: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if states:
            sql += f" AND state IN ({','.join('?' * len(states))})"
            args += list(states)
        if executor:
            sql += " AND executor=?"
            args.append(executor)
        rows = self.q(sql + " ORDER BY created_at", args)
        for t in rows:
            t["data"] = json.loads(t["data"])
        return rows

    def save_task(self, t: dict) -> None:
        self.x("UPDATE tasks SET state=?, executor=?, acceptance=?, data=?, updated_at=? WHERE id=?",
               (t["state"], t["executor"], t["acceptance"], json.dumps(t["data"], ensure_ascii=False), time.time(), t["id"]))

    # messages

    def add_message(self, m: dict) -> dict:
        m = {"id": m.get("id") or new_id("msg"), "created_at": time.time(), "kind": "message", "priority": 0,
             "sender_name": "", "sender_role": "", "reply_to": None, "task_id": None} | m
        self.x("INSERT OR IGNORE INTO messages (id, sender, sender_name, sender_role, receiver, kind, priority, text, reply_to,"
               " task_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (m["id"], m["sender"], m["sender_name"], m["sender_role"], m["receiver"], m["kind"], int(m["priority"]),
                m["text"], m["reply_to"], m["task_id"], m["created_at"]))
        return self.message(m["id"])

    def message(self, mid: str) -> dict | None:
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def pending_for(self, receiver: str) -> list[dict]:
        return self.q("SELECT * FROM messages WHERE receiver=? AND state NOT IN ('handled','cancelled')"
                      " ORDER BY priority DESC, created_at", (receiver,))

    def mark(self, mid: str, state: str) -> None:
        self.x("UPDATE messages SET state=?, handled_at=? WHERE id=?",
               (state, time.time() if state == "handled" else None, mid))

    def messages(self, agent_id: str | None = None, task_id: str | None = None, search: str | None = None,
                 limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if agent_id:
            sql += " AND (sender=? OR receiver=?)"
            args += [agent_id, agent_id]
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if search:
            sql += " AND text LIKE ?"
            args.append(f"%{search}%")
        return self.q(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))[::-1]

    def last_sent(self, sender: str, receiver: str) -> float | None:
        r = self.one("SELECT MAX(created_at) t FROM messages WHERE sender=? AND receiver=? AND kind IN ('message','relayed')"
                     " AND reply_to IS NULL", (sender, receiver))
        return r and r["t"]

    # turn log

    def log_turn(self, agent_id, backend, model, thread, input, reply, status, task_id=None) -> int:
        return self.x("INSERT INTO turn_log (agent_id, backend, model, thread, input, reply, status, created_at, task_id)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (agent_id, backend, model, thread, input, reply, status, time.time(), task_id)).lastrowid

    def turn_log(self, agent_id: str, after: int = 0, limit: int = 20) -> list[dict]:
        return self.q("SELECT * FROM turn_log WHERE agent_id=? AND id>? ORDER BY id DESC LIMIT ?",
                      (agent_id, after, limit))[::-1]

    # outbox (hub -> temple events while the link is down)

    def push_outbox(self, method: str, params: dict) -> None:
        self.x("INSERT INTO outbox (method, params, created_at) VALUES (?,?,?)",
               (method, json.dumps(params, ensure_ascii=False), time.time()))

    def outbox(self) -> list[dict]:
        rows = self.q("SELECT * FROM outbox ORDER BY id")
        for r in rows:
            r["params"] = json.loads(r["params"])
        return rows

    def drop_outbox(self, oid: int) -> None:
        self.x("DELETE FROM outbox WHERE id=?", (oid,))
