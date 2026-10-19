"""SQLite storage for the printers the user chose to monitor."""
from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(printers)")}
        with self.db:
            if "grp" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN grp TEXT")
            if "position" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN position INTEGER")
            if "cam" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN cam TEXT")
            if "notify" not in cols:
                self.db.execute("ALTER TABLE printers ADD COLUMN notify INTEGER")

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
        allowed = {k: v for k, v in fields.items() if k in ("name", "api_key", "grp", "position", "cam", "notify")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        with self.db:
            self.db.execute(f"UPDATE printers SET {sets} WHERE id=?", (*allowed.values(), pid))

    def set_order(self, ids: list[int]):
        with self.db:
            self.db.executemany("UPDATE printers SET position=? WHERE id=?", [(i, pid) for i, pid in enumerate(ids)])

    def settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self.db.execute("SELECT key, value FROM settings")}

    def save_settings(self, values: dict):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                                [(k, json.dumps(v)) for k, v in values.items()])

    def delete(self, pid: int):
        with self.db:
            self.db.execute("DELETE FROM printers WHERE id=?", (pid,))
