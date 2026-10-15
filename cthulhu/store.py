"""Temple persistence: one SQLite database owned by the temple process."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .task_display import short_name

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
    desired TEXT NOT NULL DEFAULT 'running',  -- running | stopped | deleted (history kept)
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
    state TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | handled | cancelled (receiver force-stopped)
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
CREATE TABLE IF NOT EXISTS tasks (          -- v2 task contracts; unused since v3 (see task_index)
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active',    -- see tasks.STATES
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_index (     -- v3: the temple's view of every task; details live on the hub
    id TEXT PRIMARY KEY,
    galaxy_id TEXT NOT NULL,
    description TEXT NOT NULL,
    acceptance TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'assigning',
    executor TEXT,
    executor_name TEXT,
    line TEXT NOT NULL DEFAULT '',            -- one-line status for the bindings table
    summary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS roster (           -- v3: old_ones as reported by their hubs
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    galaxy_id TEXT NOT NULL,
    desired TEXT NOT NULL DEFAULT 'running',
    state TEXT NOT NULL DEFAULT 'offline',
    task_id TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS digest (           -- checkpoint results waiting for the next 10-minute digest
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    attention INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    sent_in TEXT
);
CREATE TABLE IF NOT EXISTS approvals (        -- azathoth's sudo requests, decided by the owner in the web UI
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    reason TEXT NOT NULL,
    cwd TEXT,
    timeout REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | rejected | expired | done | failed
    note TEXT,
    result TEXT,
    created_at REAL NOT NULL,
    decided_at REAL,
    finished_at REAL
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
CREATE TABLE IF NOT EXISTS channel_events (
    agent_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    receipt TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (agent_id, app_id, event_id)
);
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


TASK_FIELDS = {"state", "executor", "executor_name", "line", "summary", "closed_at", "acceptance"}


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
                    "ALTER TABLE memory ADD COLUMN updated_at REAL", "ALTER TABLE messages ADD COLUMN task_id TEXT",
                    "ALTER TABLE messages ADD COLUMN status TEXT", "ALTER TABLE chat_turns ADD COLUMN task_id TEXT",
                    "ALTER TABLE chat_turns ADD COLUMN kind TEXT",
                    "ALTER TABLE chat_turns ADD COLUMN report TEXT",
                    "ALTER TABLE task_index ADD COLUMN short_name TEXT",
                    "ALTER TABLE digest ADD COLUMN event_id TEXT",
                    "ALTER TABLE digest ADD COLUMN details TEXT",
                    "ALTER TABLE turn_log ADD COLUMN scope TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE tasks ADD COLUMN creator TEXT", "ALTER TABLE tasks ADD COLUMN manager TEXT",
                    "ALTER TABLE tasks ADD COLUMN executor TEXT", "ALTER TABLE tasks ADD COLUMN deadline REAL",
                    "ALTER TABLE tasks ADD COLUMN acceptance TEXT NOT NULL DEFAULT ''", "ALTER TABLE tasks ADD COLUMN probe TEXT",
                    "ALTER TABLE tasks ADD COLUMN data TEXT NOT NULL DEFAULT '{}'"):  # checkpoints, extensions, events (tasks.py)
            try:
                self.db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists
        self.db.execute("UPDATE memory SET updated_at=created_at WHERE updated_at IS NULL")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS digest_event ON digest(event_id) WHERE event_id IS NOT NULL")
        for row in self.db.execute("SELECT id, description FROM task_index WHERE short_name IS NULL").fetchall():
            self.db.execute("UPDATE task_index SET short_name=? WHERE id=?", (short_name(row["description"]), row["id"]))
        self.lock = threading.RLock()
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
        sql, args = "SELECT * FROM agents WHERE desired!='deleted'", []
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
                    priority=0, reply_to=None, message_id=None, task_id=None, status=None) -> dict:
        mid = message_id or new_id("msg")
        self.x("INSERT INTO messages (id, sender, receiver, kind, priority, text, reply_to, created_at, task_id, status)"
               " VALUES (?,?,?,?,?,?,?,?,?,?)",
               (mid, sender, receiver, kind, priority, text, reply_to, time.time(), task_id, status))
        return self.message(mid)

    def message(self, mid: str) -> dict | None:
        return self.one("SELECT * FROM messages WHERE id=?", (mid,))

    def pending_for(self, receiver: str) -> list[dict]:
        return self.q("SELECT * FROM messages WHERE receiver=? AND state NOT IN ('handled','cancelled')"
                      " ORDER BY priority DESC, created_at", (receiver,))

    def mark(self, mid: str, state: str) -> None:
        self.x("UPDATE messages SET state=?, handled_at=? WHERE id=?",
               (state, time.time() if state == "handled" else None, mid))

    def messages(self, agent_id: str | None = None, limit=100, before: float | None = None,
                 search: str | None = None, task_id: str | None = None, before_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM messages WHERE 1=1", []
        if agent_id:
            sql += " AND (sender=? OR receiver=?)"
            args += [agent_id, agent_id]
        if search:
            sql += " AND text LIKE ?"
            args.append(f"%{search}%")
        if task_id:
            sql += " AND task_id=?"
            args.append(task_id)
        if before_id:
            cursor = self.message(before_id)
            if cursor:
                sql += " AND (created_at, id) < (?, ?)"
                args += [cursor['created_at'], cursor['id']]
        elif before:
            sql += " AND created_at<?"
            args.append(before)
        rows = self.q(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit))
        for row in rows:
            task = self.task(row["task_id"]) if row.get("task_id") else None
            row["task_title"] = task and task["short_name"]
        return rows[::-1]

    def pending_counts(self) -> dict[str, int]:
        return {r["receiver"]: r["n"] for r in
                self.q("SELECT receiver, COUNT(*) n FROM messages WHERE state NOT IN ('handled','cancelled') GROUP BY receiver")}

    # v3 task index (the hub owns task details)

    def index_task(self, task_id: str, galaxy_id: str, description: str, acceptance: str = "", name: str = "") -> dict:
        now = time.time()
        self.x("INSERT INTO task_index (id, galaxy_id, description, acceptance, short_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
               (task_id, galaxy_id, description, acceptance, short_name(description, name), now, now))
        return self.task(task_id)

    def task(self, task_id: str) -> dict | None:
        return self.one("SELECT * FROM task_index WHERE id=?", (task_id,))

    def set_task(self, task_id: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE task_index SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), task_id))

    def tasks(self, search: str | None = None, limit=50, state: str | None = None, galaxy_id: str | None = None,
              open_only=False, before_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM task_index WHERE 1=1", []
        if search:
            sql += " AND (description LIKE ? OR id LIKE ? OR summary LIKE ?)"
            args += [f"%{search}%"] * 3
        if state:
            sql += " AND state=?"
            args.append(state)
        if galaxy_id:
            sql += " AND galaxy_id=?"
            args.append(galaxy_id)
        if open_only:
            sql += " AND state NOT IN ('closed','cancelled','assign_failed')"
        if before_id and (cursor := self.task(before_id)):
            sql += " AND (created_at, id) < (?, ?)"
            args += [cursor['created_at'], cursor['id']]
        return self.q(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", (*args, limit))

    # v3 old_one roster (the hub owns old_ones)

    def set_roster(self, galaxy_id: str, old_ones: list[dict]) -> None:
        """A hub's full list of its old_ones replaces what the temple knew for that galaxy."""
        now = time.time()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("DELETE FROM roster WHERE galaxy_id=?", (galaxy_id,))
                for a in old_ones:
                    self.db.execute("INSERT OR REPLACE INTO roster (id, name, galaxy_id, desired, state, task_id, data, updated_at)"
                                    " VALUES (?,?,?,?,?,?,?,?)",
                                    (a["id"], a["name"], galaxy_id, a.get("desired", "running"), a.get("state", "offline"),
                                     a.get("task_id"), json.dumps(a.get("data") or {}, ensure_ascii=False), now))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def roster_status(self, agent_id: str, status: dict) -> None:
        """A live status report from the old_one's hub: keep the directory row current between roster pushes."""
        r = self.old_one(agent_id)
        if not r:
            return
        data = r["data"] | {k: status.get(k) for k in ("current", "context_fill", "runtime")}
        self.x("UPDATE roster SET state=?, data=?, updated_at=? WHERE id=?",
               (status.get("state", r["state"]), json.dumps(data, ensure_ascii=False), time.time(), agent_id))

    def old_one(self, agent_id: str) -> dict | None:
        r = self.one("SELECT * FROM roster WHERE id=?", (agent_id,))
        if r:
            r["data"] = json.loads(r["data"])
        return r

    def roster(self, galaxy_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM roster WHERE desired!='deleted'", []
        if galaxy_id:
            sql += " AND galaxy_id=?"
            args.append(galaxy_id)
        rows = self.q(sql + " ORDER BY name", args)
        for r in rows:
            r["data"] = json.loads(r["data"])
        return rows

    # v3 digest of checkpoint results for azathoth

    def add_digest(self, task_id: str | None, text: str, attention: bool, event_id: str | None = None, details: dict | None = None) -> None:
        self.x("INSERT OR IGNORE INTO digest (task_id, attention, text, created_at, event_id, details) VALUES (?,?,?,?,?,?)",
               (task_id, int(attention), text, time.time(), event_id, json.dumps(details or {}, ensure_ascii=False)))

    def unsent_digest(self) -> list[dict]:
        return self.q("SELECT * FROM digest WHERE sent_in IS NULL ORDER BY attention DESC, id")

    def mark_digest(self, ids: list[int], message_id: str) -> None:
        with self.lock:
            self.db.executemany("UPDATE digest SET sent_in=? WHERE id=?", [(message_id, i) for i in ids])

    # v3 sudo approvals

    def add_approval(self, command: str, reason: str, cwd: str | None, timeout: float) -> dict:
        aid = new_id("sudo")
        self.x("INSERT INTO approvals (id, command, reason, cwd, timeout, created_at) VALUES (?,?,?,?,?,?)",
               (aid, command, reason, cwd, timeout, time.time()))
        return self.approval(aid)

    def approval(self, approval_id: str) -> dict | None:
        return self.one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def set_approval(self, approval_id: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE approvals SET {cols} WHERE id=?", (*fields.values(), approval_id))

    def approvals(self, state: str | None = None, limit=50) -> list[dict]:
        if state:
            return self.q("SELECT * FROM approvals WHERE state=? ORDER BY created_at DESC LIMIT ?", (state, limit))
        return self.q("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))

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
        for r in self.q("SELECT agent_id, text, updated_at FROM memory"):
            st = stats.setdefault(r["agent_id"], {"entries": 0, "tokens": 0, "updated_at": 0})
            st["entries"] += 1
            st["tokens"] += estimate_tokens(r["text"])
            st["updated_at"] = max(st["updated_at"], r["updated_at"])
        return stats

    def forget(self, agent_id: str, memory_id: int) -> bool:
        return self.x("DELETE FROM memory WHERE agent_id=? AND id=?", (agent_id, memory_id)) > 0

    # owner chat

    def add_turn(self, turn_id: str, agent_id: str, user_text: str) -> dict:
        now = time.time()
        self.x("INSERT INTO chat_turns (id, agent_id, user_text, created_at, updated_at) VALUES (?,?,?,?,?)",
               (turn_id, agent_id, user_text, now, now))
        return self.turn(turn_id)

    TURN_SQL = ("SELECT c.*, t.short_name AS task_title, t.state AS task_state, e.receipt AS channel_receipt"
                " FROM chat_turns c LEFT JOIN task_index t ON t.id = c.task_id LEFT JOIN channel_events e ON e.message_id=c.id")

    def turn(self, turn_id: str) -> dict | None:
        t = self.one(self.TURN_SQL + " WHERE c.id=?", (turn_id,))
        if t:
            t["activity"] = json.loads(t["activity"])
            t["report"] = json.loads(t["report"]) if t.get("report") else None
            t['channel_receipt'] = json.loads(t['channel_receipt']) if t['channel_receipt'] is not None else None
        return t

    def update_turn(self, turn_id: str, **fields) -> None:
        if "report" in fields:
            fields["report"] = json.dumps(fields["report"], ensure_ascii=False)
        if "activity" in fields:
            fields["activity"] = json.dumps(fields["activity"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE chat_turns SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), turn_id))

    def turns(self, agent_id: str, limit=50, before: float | None = None, search: str | None = None) -> list[dict]:
        sql, args = self.TURN_SQL + " WHERE c.agent_id=?", [agent_id]
        if before:
            sql += " AND c.created_at<?"
            args.append(before)
        if search:
            sql += " AND (c.user_text LIKE ? OR c.reply LIKE ?)"
            args += [f"%{search}%"] * 2
        rows = self.q(sql + " ORDER BY c.created_at DESC LIMIT ?", (*args, limit))
        for t in rows:
            t["activity"] = json.loads(t["activity"])
            t["report"] = json.loads(t["report"]) if t.get("report") else None
            t['channel_receipt'] = json.loads(t['channel_receipt']) if t['channel_receipt'] is not None else None
        return rows[::-1]

    # settings

    def setting(self, key: str):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(r["value"]) if r else None

    def set_setting(self, key: str, value) -> None:
        self.x("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))

    # turn log

    def log_turn(self, agent_id: str, backend: str, model: str | None, thread: str, input: str, reply: str,
                 status: str, scope: str = "") -> int:
        with self.lock:
            cur = self.db.execute("INSERT INTO turn_log (agent_id, backend, model, thread, input, reply, status, created_at, scope)"
                                  " VALUES (?,?,?,?,?,?,?,?,?)",
                                  (agent_id, backend, model, thread, input, reply, status, time.time(), scope))
            return cur.lastrowid

    def turn_log(self, agent_id: str, after: int = 0, limit: int = 20, scope: str = "") -> list[dict]:
        rows = self.q("SELECT * FROM turn_log WHERE agent_id=? AND scope=? AND id>? ORDER BY id DESC LIMIT ?", (agent_id, scope, after, limit))
        return rows[::-1]

    def channel_accept(self, agent_id: str, app_id: str, event_id: str, scope: str, payload: dict,
                       sender: str = 'owner', origin: str = '飞书') -> tuple[dict, bool]:
        """One transaction: external event -> existing message/chat, never a second queue."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM channel_events WHERE agent_id=? AND app_id=? AND event_id=?",
                                      (agent_id, app_id, event_id)).fetchone()
                if row:
                    if row['scope'] != scope:
                        raise ValueError('event scope mismatch')
                    self.db.execute("COMMIT")
                    return row, False
                mid, now = new_id('msg'), time.time()
                self.db.execute("INSERT INTO messages (id,sender,receiver,kind,text,created_at) VALUES (?,?,?,?,?,?)",
                                (mid, sender, agent_id, 'channel', payload['prompt'], now))
                self.db.execute("INSERT INTO chat_turns (id,agent_id,user_text,origin,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                                (mid, agent_id, payload['text'], origin, now, now))
                self.db.execute("INSERT INTO channel_events (agent_id,app_id,event_id,scope,message_id,payload) VALUES (?,?,?,?,?,?)",
                                (agent_id, app_id, event_id, scope, mid, json.dumps(payload, ensure_ascii=False)))
                row = self.db.execute("SELECT * FROM channel_events WHERE message_id=?", (mid,)).fetchone()
                self.db.execute("COMMIT")
                return row, True
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

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
