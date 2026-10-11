"""Temple persistence: one SQLite database owned by the temple process."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS galaxies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    galaxy_id TEXT NOT NULL,
    parent_id TEXT,
    spec TEXT NOT NULL DEFAULT '{}',      -- init params: task, cwd, model
    epoch INTEGER NOT NULL DEFAULT 1,     -- bumping it revokes the old token
    desired TEXT NOT NULL DEFAULT 'running',  -- running | stopped
    thread_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    receiver TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message',
    priority INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    reply_to TEXT,
    state TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | handled
    created_at REAL NOT NULL,
    handled_at REAL
);
CREATE INDEX IF NOT EXISTS messages_receiver ON messages(receiver, state);
CREATE INDEX IF NOT EXISTS messages_created ON messages(created_at);
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_agent ON memory(agent_id);
CREATE TABLE IF NOT EXISTS chat_turns (
    id TEXT PRIMARY KEY,                  -- equals the owner message id
    agent_id TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '',
    activity TEXT NOT NULL DEFAULT '[]',  -- tool/command summaries for the UI
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | interrupted | failed
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_agent ON chat_turns(agent_id, created_at);
CREATE TABLE IF NOT EXISTS patrols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turn_log (       -- backend-neutral record of every agent turn
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
CREATE INDEX IF NOT EXISTS turn_log_agent ON turn_log(agent_id, id);
CREATE TABLE IF NOT EXISTS accounts (
    username TEXT PRIMARY KEY,
    salt TEXT NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def estimate_tokens(text: str) -> int:
    # Rough: one token per CJK character, four characters per token otherwise.
    cjk = sum(1 for c in text if "\u3000" <= c <= "\u9fff" or "\uff00" <= c <= "\uffef")
    return cjk + (len(text) - cjk + 3) // 4


def _row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = _row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        for ddl in ("ALTER TABLE chat_turns ADD COLUMN model TEXT", "ALTER TABLE chat_turns ADD COLUMN origin TEXT",
                    "ALTER TABLE memory ADD COLUMN updated_at REAL"):
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.db.execute("UPDATE memory SET updated_at=created_at WHERE updated_at IS NULL")
        self.lock = threading.Lock()
        path.chmod(0o600)

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    # galaxies

    def add_galaxy(self, galaxy_id: str, name: str, token_hash: str) -> None:
        self.x("INSERT OR REPLACE INTO galaxies VALUES (?,?,?,?)", (galaxy_id, name, token_hash, time.time()))

    def galaxy(self, galaxy_id: str) -> dict | None:
        return self.one("SELECT * FROM galaxies WHERE id=?", (galaxy_id,))

    def galaxies(self) -> list[dict]:
        return self.q("SELECT id, name, created_at FROM galaxies ORDER BY created_at")

    # agents

    def upsert_agent(self, agent_id: str, name: str, role: str, galaxy_id: str,
                     parent_id: str | None = None, spec: dict | None = None) -> dict:
        now = time.time()
        if self.agent(agent_id):
            self.x("UPDATE agents SET name=?, galaxy_id=?, spec=?, updated_at=? WHERE id=?",
                   (name, galaxy_id, json.dumps(spec or {}, ensure_ascii=False), now, agent_id))
        else:
            self.x("INSERT INTO agents (id, name, role, galaxy_id, parent_id, spec, created_at, updated_at)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   (agent_id, name, role, galaxy_id, parent_id, json.dumps(spec or {}, ensure_ascii=False), now, now))
        return self.agent(agent_id)

    def agent(self, agent_id: str) -> dict | None:
        a = self.one("SELECT * FROM agents WHERE id=?", (agent_id,))
        if a:
            a["spec"] = json.loads(a["spec"])
        return a

    def agents(self, galaxy_id: str | None = None, include_stopped=True) -> list[dict]:
        sql, args = "SELECT * FROM agents WHERE 1=1", []
        if galaxy_id:
            sql += " AND galaxy_id=?"
            args.append(galaxy_id)
        if not include_stopped:
            sql += " AND desired='running'"
        rows = self.q(sql + " ORDER BY created_at", args)
        for a in rows:
            a["spec"] = json.loads(a["spec"])
        return rows

    def set_agent(self, agent_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE agents SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), agent_id))

    # messages

    def add_message(self, sender: str, receiver: str, text: str, kind="message",
                    priority=0, reply_to=None, message_id=None) -> dict:
        mid = message_id or new_id("msg")
        self.x("INSERT INTO messages (id, sender, receiver, kind, priority, text, reply_to, created_at)"
               " VALUES (?,?,?,?,?,?,?,?)", (mid, sender, receiver, kind, priority, text, reply_to, time.time()))
        return self.message(mid)

    def message(self, mid: str) -> dict | None:
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def pending_for(self, receiver: str) -> list[dict]:
        return self.q("SELECT * FROM messages WHERE receiver=? AND state!='handled' ORDER BY created_at", (receiver,))

    def mark(self, mid: str, state: str) -> None:
        self.x("UPDATE messages SET state=?, handled_at=? WHERE id=?",
               (state, time.time() if state == "handled" else None, mid))

    def messages(self, agent_id: str | None = None, limit=100, before: float | None = None) -> list[dict]:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if agent_id:
            sql += " AND (sender=? OR receiver=?)"
            args += [agent_id, agent_id]
        if before:
            sql += " AND created_at<?"
            args.append(before)
        rows = self.q(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
        return rows[::-1]

    def pending_counts(self) -> dict[str, int]:
        return {r["receiver"]: r["n"] for r in
                self.q("SELECT receiver, COUNT(*) n FROM messages WHERE state!='handled' GROUP BY receiver")}

    # permanent memory

    def remember(self, agent_id: str, text: str, source: str | None = None) -> int:
        with self.lock:
            now = time.time()
            cur = self.db.execute("INSERT INTO memory (agent_id, text, source, created_at, updated_at) VALUES (?,?,?,?,?)",
                                  (agent_id, text, source, now, now))
            return cur.lastrowid

    def revise(self, agent_id: str, memory_id: int, text: str, source: str | None = None) -> bool:
        """Rewrite an entry in place; its id stays, updated_at moves."""
        return self.x("UPDATE memory SET text=?, source=COALESCE(?, source), updated_at=? WHERE agent_id=? AND id=?",
                      (text, source, time.time(), agent_id, memory_id)) > 0

    def memory(self, agent_id: str) -> list[dict]:
        return self.q("SELECT * FROM memory WHERE agent_id=? ORDER BY id", (agent_id,))

    def memory_stats(self) -> dict[str, dict]:
        stats = {}
        for r in self.q("SELECT agent_id, text FROM memory"):
            st = stats.setdefault(r["agent_id"], {"entries": 0, "tokens": 0})
            st["entries"] += 1
            st["tokens"] += estimate_tokens(r["text"])
        return stats

    def forget(self, agent_id: str, memory_id: int) -> bool:
        return self.x("DELETE FROM memory WHERE agent_id=? AND id=?", (agent_id, memory_id)) > 0

    # owner chat

    def add_turn(self, turn_id: str, agent_id: str, user_text: str) -> dict:
        now = time.time()
        self.x("INSERT INTO chat_turns (id, agent_id, user_text, created_at, updated_at) VALUES (?,?,?,?,?)",
               (turn_id, agent_id, user_text, now, now))
        return self.turn(turn_id)

    def turn(self, turn_id: str) -> dict | None:
        t = self.one("SELECT * FROM chat_turns WHERE id=?", (turn_id,))
        if t:
            t["activity"] = json.loads(t["activity"])
        return t

    def update_turn(self, turn_id: str, **fields) -> None:
        if "activity" in fields:
            fields["activity"] = json.dumps(fields["activity"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE chat_turns SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), turn_id))

    def turns(self, agent_id: str, limit=50, before: float | None = None, search: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM chat_turns WHERE agent_id=?", [agent_id]
        if before:
            sql += " AND created_at<?"
            args.append(before)
        if search:
            sql += " AND (user_text LIKE ? OR reply LIKE ?)"
            args += [f"%{search}%"] * 2
        rows = self.q(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
        for t in rows:
            t["activity"] = json.loads(t["activity"])
        return rows[::-1]

    # settings

    def setting(self, key: str):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else None

    def set_setting(self, key: str, value) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    # turn log

    def log_turn(self, agent_id: str, backend: str, model: str | None, thread: str, input: str, reply: str,
                 status: str) -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO turn_log (agent_id, backend, model, thread, input, reply, status, created_at)"
                                  " VALUES (?,?,?,?,?,?,?,?)",
                                  (agent_id, backend, model, thread, input, reply, status, time.time()))
            return cur.lastrowid

    def turn_log(self, agent_id: str, after: int = 0, limit: int = 20) -> list[dict]:
        rows = self.q("SELECT * FROM turn_log WHERE agent_id=? AND id>? ORDER BY id DESC LIMIT ?", (agent_id, after, limit))
        return rows[::-1]

    # yog patrols

    def add_patrol(self, summary: str) -> None:
        self.x("INSERT INTO patrols (summary, created_at) VALUES (?,?)", (summary, time.time()))

    def patrols(self, limit=3) -> list[dict]:
        return self.q("SELECT * FROM patrols ORDER BY id DESC LIMIT ?", (limit,))[::-1]

    # web accounts

    def account(self, username: str) -> dict | None:
        return self.one("SELECT * FROM accounts WHERE username=?", (username,))

    def set_account(self, username: str, salt: str, password_hash: str) -> None:
        self.x("INSERT OR REPLACE INTO accounts VALUES (?,?,?)", (username, salt, password_hash))

    def add_session(self, token_hash: str, username: str, ttl: float) -> None:
        self.x("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
        self.x("INSERT INTO sessions VALUES (?,?,?)", (token_hash, username, time.time() + ttl))

    def session(self, token_hash: str) -> dict | None:
        return self.one("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash, time.time()))

    def drop_session(self, token_hash: str) -> None:
        self.x("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
