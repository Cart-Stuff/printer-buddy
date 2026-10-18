"""SQLite storage for the printers the user chose to monitor."""
from __future__ import annotations

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    host       TEXT NOT NULL,
    port       INTEGER NOT NULL DEFAULT 7125,
    api_key    TEXT,
    created_at REAL NOT NULL,
    UNIQUE(host, port)
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def list(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM printers ORDER BY id")]

    def get(self, pid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM printers WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None

    def find(self, host: str, port: int) -> dict | None:
        r = self.db.execute("SELECT * FROM printers WHERE host=? AND port=?", (host, port)).fetchone()
        return dict(r) if r else None

    def add(self, name: str, host: str, port: int, api_key: str | None) -> dict:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO printers(name, host, port, api_key, created_at) VALUES (?,?,?,?,?)",
                (name, host, port, api_key or None, time.time()))
        return self.get(cur.lastrowid)

    def update(self, pid: int, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("name", "api_key")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self.db:
            self.db.execute(f"UPDATE printers SET {sets} WHERE id=?", (*allowed.values(), pid))

    def delete(self, pid: int):
        with self.db:
            self.db.execute("DELETE FROM printers WHERE id=?", (pid,))
