"""Owns the Moonraker connections for the selected printers, detects print events, fans out updates."""
from __future__ import annotations

import asyncio
import logging

from .moonraker import MoonrakerClient
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)


def _event_for(prev: str | None, new: str | None) -> str | None:
    if prev is None or new is None or prev == new:
        return None
    if new == "printing":
        return "resumed" if prev == "paused" else "started"
    return {"paused": "paused", "complete": "finished", "error": "failed", "cancelled": "cancelled"}.get(new)


class Manager:
    def __init__(self, store: Store, notifier: Notifier):
        self.store = store
        self.notifier = notifier
        self.clients: dict[int, MoonrakerClient] = {}
        self.rows: dict[int, dict] = {}
        self._last_state: dict[int, str | None] = {}
        self._subs: set[asyncio.Queue] = set()

    async def start(self):
        for row in self.store.list():
            self._spawn(row)

    async def stop(self):
        await asyncio.gather(*(c.stop() for c in self.clients.values()))

    def _spawn(self, row: dict):
        self.rows[row["id"]] = row
        c = MoonrakerClient(row["id"], row["host"], row["port"], row["api_key"], self._on_change)
        self.clients[row["id"]] = c
        c.start()

    # CRUD ---------------------------------------------------------------
    def add(self, name: str, host: str, port: int, api_key: str | None) -> dict:
        row = self.store.add(name, host, port, api_key)
        self._spawn(row)
        return self.view(row["id"])

    def rename(self, pid: int, name: str):
        self.store.update(pid, name=name)
        self.rows[pid]["name"] = name
        self._broadcast("printer", self.view(pid))

    async def remove(self, pid: int):
        c = self.clients.pop(pid, None)
        self.rows.pop(pid, None)
        self._last_state.pop(pid, None)
        self.store.delete(pid)
        if c:
            await c.stop()
        self._broadcast("removed", {"id": pid})

    # views --------------------------------------------------------------
    def view(self, pid: int) -> dict:
        row = self.rows[pid]
        return {
            "id": pid, "name": row["name"], "host": row["host"], "port": row["port"],
            "has_api_key": bool(row["api_key"]), **self.clients[pid].view(),
        }

    def views(self) -> list[dict]:
        return [self.view(pid) for pid in self.clients]

    # updates ------------------------------------------------------------
    def _on_change(self, client: MoonrakerClient):
        if client.id not in self.rows:
            return
        v = self.view(client.id)
        prev = self._last_state.get(client.id)
        self._last_state[client.id] = v["state"]
        event = _event_for(prev, v["state"])
        if event:
            self._broadcast("event", {"id": client.id, "event": event, "name": v["name"]})
            if self.notifier.enabled and self.notifier.wants(event):
                asyncio.get_running_loop().create_task(self._notify(event, v))
        self._broadcast("printer", v)

    async def _notify(self, event: str, v: dict):
        title = f"{v['name']}: print {event}"
        parts = [v["filename"] or "unknown file"]
        if event == "failed" and v["message"]:
            parts.append(v["message"])
        if event in ("paused", "cancelled", "failed"):
            parts.append(f"{v['progress'] * 100:.0f}% done")
        if event == "finished" and v["print_duration"]:
            m = int(v["print_duration"] // 60)
            parts.append(f"took {m // 60}h {m % 60}m")
        await self.notifier.send(title, " · ".join(parts))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def _broadcast(self, kind: str, data: dict):
        for q in list(self._subs):
            try:
                q.put_nowait((kind, data))
            except asyncio.QueueFull:
                pass  # slow browser tab; it gets a fresh snapshot on reconnect
