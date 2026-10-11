"""One-off import from the previous harness database (read-only).

- owner web account (same scrypt parameters, password unchanged)
- owner chat history with azathoth and with ghroth
- owner goals / notes become azathoth's initial permanent context
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from .store import Store


def migrate(old_db: str, store: Store, azathoth: str, ghroth: str) -> dict:
    old = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    counts = {}

    for a in old.execute("SELECT username, salt, password_hash FROM web_accounts"):
        if not store.account(a["username"]):
            store.set_account(a["username"], a["salt"], a["password_hash"])
    counts["accounts"] = len(store.q("SELECT * FROM accounts"))

    maintenance = {r["chat_id"] for r in old.execute("SELECT chat_id FROM maintenance_chats")}
    owner = {r["chat_id"] for r in old.execute("SELECT chat_id FROM owner_chats")} - maintenance
    n = 0
    for t in old.execute("SELECT * FROM chat_turns WHERE status IN ('completed', 'interrupted') ORDER BY created_at"):
        agent = ghroth if t["chat_id"] in maintenance else azathoth if t["chat_id"] in owner else None
        if agent is None or store.turn(t["id"]):
            continue
        store.add_turn(t["id"], agent, t["user_text"])
        store.update_turn(t["id"], reply=t["assistant_text"] or "",
                          status="done" if t["status"] == "completed" else "interrupted")
        store.x("UPDATE chat_turns SET created_at=?, updated_at=? WHERE id=?", (t["created_at"], t["updated_at"], t["id"]))
        n += 1
    counts["turns"] = n

    if not store.memory(azathoth):
        n = 0
        for row in old.execute("SELECT id, version, text FROM goals ORDER BY updated_at"):
            store.remember(azathoth, f"主人目标（原话）：{row['text']}", source=f"legacy:{row['id']}")
            n += 1
        for row in old.execute("SELECT kind, quote FROM owner_notes WHERE kind IN ('insight', 'decision', 'constraint') ORDER BY created_at"):
            label = {"insight": "主人洞见", "decision": "主人决定", "constraint": "主人约束"}[row["kind"]]
            store.remember(azathoth, f"{label}（原话）：{row['quote']}", source="legacy")
            n += 1
        counts["memory"] = n
    return counts


if __name__ == "__main__":
    import json
    cfg = json.loads(Path(sys.argv[2]).read_text())
    gods = {g["role"]: g["id"] for g in cfg["gods"]}
    print(migrate(sys.argv[1], Store(Path(cfg["db"])), gods["azathoth"], gods["ghroth"]))
