"""Owns the Moonraker connections for the selected printers, detects print events, fans out updates."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from .moonraker import MoonrakerClient
from .net import http_get
from .notify import Notifier
from .store import Store

log = logging.getLogger(__name__)
OFFLINE_GRACE = float(os.getenv("OFFLINE_GRACE", "60"))  # seconds a printer must stay gone mid-print before alerting


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
        self._last: dict[int, dict] = {}             # last view per printer, for transition detection
        self._offline_timers: dict[int, asyncio.Task] = {}
        self._milestone: dict[int, int] = {}         # last progress % announced per printer
        self._subs: set[asyncio.Queue] = set()

    async def start(self):
        for row in self.store.list():
            self._spawn(row)

    async def stop(self):
        for t in self._offline_timers.values():
            t.cancel()
        await asyncio.gather(*(c.stop() for c in self.clients.values()))

    def _spawn(self, row: dict):
        self.rows[row["id"]] = row
        c = MoonrakerClient(row["id"], row["host"], row["port"], row["api_key"], self._on_change)
        c.on_gcode = self._on_gcode
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

    def set_group(self, pid: int, group: str | None):
        self.store.update(pid, grp=group)
        self.rows[pid]["grp"] = group
        self._broadcast("printer", self.view(pid))

    @staticmethod
    def _cam_settings(row: dict) -> dict | None:
        if not row.get("cam"):
            return None
        cfg = json.loads(row["cam"])
        if "cams" not in cfg:  # settings saved before multi-camera: they were for camera 0
            cfg = {"main": 0, "cams": {"0": cfg}}
        return cfg

    def set_camera(self, pid: int, cfg: dict | None):
        value = json.dumps(cfg) if cfg else None
        self.store.update(pid, cam=value)
        self.rows[pid]["cam"] = value
        self._broadcast("printer", self.view(pid))

    def set_notify(self, pid: int, on: bool):
        self.store.update(pid, notify=1 if on else 0)
        self.rows[pid]["notify"] = 1 if on else 0
        self._broadcast("printer", self.view(pid))

    def set_order(self, ids: list[int]):
        ids = [i for i in ids if i in self.rows]
        self.store.set_order(ids)
        for pos, pid in enumerate(ids):
            self.rows[pid]["position"] = pos
            self._broadcast("printer", self.view(pid))

    async def remove(self, pid: int):
        c = self.clients.pop(pid, None)
        self.rows.pop(pid, None)
        self._last.pop(pid, None)
        if t := self._offline_timers.pop(pid, None):
            t.cancel()
        self.store.delete(pid)
        if c:
            await c.stop()
        self._broadcast("removed", {"id": pid})

    # views --------------------------------------------------------------
    def view(self, pid: int) -> dict:
        row = self.rows[pid]
        return {
            "id": pid, "name": row["name"], "host": row["host"], "port": row["port"],
            "has_api_key": bool(row["api_key"]), "group": row.get("grp"), "position": row.get("position"),
            "cam_settings": self._cam_settings(row), "notify": row.get("notify") != 0,
            **self.clients[pid].view(),
        }

    def views(self) -> list[dict]:
        return [self.view(pid) for pid in self.clients]

    # updates ------------------------------------------------------------
    def _on_change(self, client: MoonrakerClient):
        pid = client.id
        if pid not in self.rows:
            return
        v = self.view(pid)
        prev = self._last.get(pid)
        self._last[pid] = v
        if prev:
            self._detect(pid, prev, v)
        self._broadcast("printer", v)

    def _detect(self, pid: int, prev: dict, v: dict):
        if event := _event_for(prev["state"], v["state"]):
            self._emit(pid, event, v)
            if event == "started":
                self._milestone[pid] = 0
        # progress updates (advanced notifications): every N % while printing
        step = self.notifier.progress_step()
        if step and v["state"] == "printing" and prev["state"] in ("printing", "paused"):
            reached = int(v["progress"] * 100) // step * step
            last = self._milestone.setdefault(pid, reached)
            if step <= reached < 100 and reached > last:
                self._milestone[pid] = reached
                self._emit(pid, "progress", {**v, "milestone": reached})
        # Klipper shut down / errored (MCU shutdown, thermal runaway, ...): wait a moment for the reason
        if v["connection"] == "online" and prev["klippy_state"] == "ready" and v["klippy_state"] in ("shutdown", "error"):
            asyncio.get_running_loop().call_later(
                2, lambda: self._emit(pid, "shutdown", self.view(pid)) if pid in self.rows else None)
        # lost the printer mid-print: alert only if it stays gone for OFFLINE_GRACE seconds
        if prev["connection"] == "online" and v["connection"] != "online" and prev["state"] in ("printing", "paused"):
            if pid not in self._offline_timers:
                self._offline_timers[pid] = asyncio.create_task(self._offline_check(pid, prev))
        elif v["connection"] == "online" and (t := self._offline_timers.pop(pid, None)):
            t.cancel()

    async def _offline_check(self, pid: int, before: dict):
        try:
            await asyncio.sleep(OFFLINE_GRACE)
            c = self.clients.get(pid)
            if c and c.connection != "online":
                self._emit(pid, "offline", before)
        finally:
            self._offline_timers.pop(pid, None)

    def _emit(self, pid: int, event: str, v: dict):
        self._broadcast("event", {"id": pid, "event": event, "name": v["name"]})
        if self.rows.get(pid, {}).get("notify") == 0:   # notifications switched off for this printer
            return
        if self.notifier.enabled and self.notifier.wants(event):
            asyncio.get_running_loop().create_task(self._notify(pid, event, v))

    def _on_gcode(self, client: MoonrakerClient, line: str):
        if client.id in self.rows:
            self._broadcast("gcode", {"id": client.id, "line": line})

    async def _notify(self, pid: int, event: str, v: dict):
        name, file = v["name"], v.get("filename") or "unknown file"
        if event == "offline":
            title, parts = f"{name}: went offline mid-print", [file, f"{v['progress'] * 100:.0f}% done"]
        elif event == "progress":
            title, parts = f"{name}: {v['milestone']}% done", [file]
            if v.get("eta"):
                m = int(v["eta"] // 60)
                parts.append(f"{m // 60}h {m % 60}m left" if m >= 60 else f"{m}m left")
        elif event == "shutdown":
            title, parts = f"{name}: Klipper {v['klippy_state']}", [v.get("klippy_message") or "no reason given"]
        else:
            title, parts = f"{name}: print {event}", [file]
            if event == "failed" and v.get("message"):
                parts.append(v["message"])
            if event in ("paused", "cancelled", "failed"):
                parts.append(f"{v['progress'] * 100:.0f}% done")
            if event == "finished" and v.get("print_duration"):
                m = int(v["print_duration"] // 60)
                parts.append(f"took {m // 60}h {m % 60}m" if m >= 60 else f"took {m}m")
        image = await self.snapshot(pid) if event != "offline" else None
        await self.notifier.send(title, " · ".join(parts), image)

    async def snapshot(self, pid: int) -> bytes | None:
        """Picture from the printer's main camera (the one shown on its dashboard card)."""
        c, row = self.clients.get(pid), self.rows.get(pid)
        if not c or not row:
            return None
        cam = (self._cam_settings(row) or {}).get("main", 0)
        url = c.snapshot_url(cam)
        if not url or not url.startswith("http://"):
            return None
        try:
            status, _, data = await http_get(url, timeout=6)
            return data if status == 200 and data else None
        except Exception as e:
            log.info("snapshot for printer %s failed: %s", pid, e)
            return None

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
