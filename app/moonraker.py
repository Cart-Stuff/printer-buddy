"""One persistent Moonraker WebSocket connection per *selected* printer."""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Callable

from .net import HandshakeError, WebSocket, get_json

log = logging.getLogger(__name__)
VERSION = "0.1.0"

SUBSCRIBE = {
    "print_stats": None,
    "extruder": ["temperature", "target"],
    "heater_bed": ["temperature", "target"],
    "display_status": ["progress", "message"],
    "virtual_sdcard": ["progress", "is_active"],
    "webhooks": ["state", "state_message"],
}


class AuthError(Exception):
    pass


class RPCError(Exception):
    pass


def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


class MoonrakerClient:
    def __init__(self, printer_id: int, host: str, port: int, api_key: str | None,
                 on_change: Callable[["MoonrakerClient"], None]):
        self.id, self.host, self.port, self.api_key = printer_id, host, port, api_key
        self._on_change = on_change
        self.connection = "connecting"  # connecting | online | offline | auth_error
        self.error: str | None = None
        self.klippy_state: str | None = None
        self.status: dict = {}
        self.webcams: list[dict] = []
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws: WebSocket | None = None
        self._task: asyncio.Task | None = None

    # lifecycle -----------------------------------------------------------
    def start(self):
        self._task = asyncio.create_task(self._run(), name=f"moonraker-{self.id}")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self):
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                await self._session()
                self._set(connection="offline", error="connection closed")
            except asyncio.CancelledError:
                raise
            except AuthError as e:
                self._set(connection="auth_error", error=str(e))
                backoff = 60.0
            except Exception as e:
                self._set(connection="offline", error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            if time.monotonic() - started > 60:
                backoff = 1.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _session(self):
        url = f"ws://{self.host}:{self.port}/websocket"
        if self.api_key:
            status, data = await get_json(f"http://{self.host}:{self.port}/access/oneshot_token",
                                          {"X-Api-Key": self.api_key})
            if status in (401, 403):
                raise AuthError("API key rejected")
            if status == 200 and isinstance(data, dict):
                url += "?token=" + str(data.get("result", ""))
        try:
            ws = await WebSocket.connect(url)
        except HandshakeError as e:
            if e.status in (401, 403):
                raise AuthError("Moonraker requires an API key") from e
            raise
        self._ws = ws
        reader = asyncio.create_task(self._reader(ws))
        pinger = asyncio.create_task(self._pinger(ws))
        try:
            try:
                await self._call("server.connection.identify", {
                    "client_name": "printer-monitor", "version": VERSION, "type": "other",
                    "url": "https://github.com/",
                })
            except RPCError:
                pass  # very old Moonraker
            info = await self._call("server.info")
            self._set(connection="online", error=None, klippy_state=info.get("klippy_state"))
            await self._load_webcams()
            if info.get("klippy_state") == "ready":
                await self._subscribe()
            await reader
        finally:
            reader.cancel()
            pinger.cancel()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("disconnected"))
            self._pending.clear()
            await ws.close()
            self._ws = None

    async def _pinger(self, ws: WebSocket):
        while not ws.closed:
            await asyncio.sleep(20)
            if time.monotonic() - ws.last_rx > 65:
                log.info("printer %s: no data for 65s, reconnecting", self.id)
                ws.abort()
                return
            try:
                await ws.ping()
            except Exception:
                ws.abort()
                return

    # JSON-RPC -------------------------------------------------------------
    async def _call(self, method: str, params: dict | None = None, timeout: float = 10.0):
        if not self._ws:
            raise ConnectionError("not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params is not None:
            msg["params"] = params
        try:
            await self._ws.send_text(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def _reader(self, ws: WebSocket):
        while (raw := await ws.recv()) is not None:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending[rid]
                if fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(RPCError(err.get("message", str(err)) if isinstance(err, dict) else str(err)))
                else:
                    fut.set_result(msg.get("result"))
                continue
            self._notification(msg.get("method"), msg.get("params") or [])
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("disconnected"))

    def _notification(self, method: str | None, params: list):
        if method == "notify_status_update" and params and isinstance(params[0], dict):
            _merge(self.status, params[0])
            self._changed()
        elif method == "notify_klippy_ready":
            self._set(klippy_state="ready")
            asyncio.create_task(self._safe(self._subscribe()))
        elif method == "notify_klippy_shutdown":
            self._set(klippy_state="shutdown")
        elif method == "notify_klippy_disconnected":
            self._set(klippy_state="disconnected")
        elif method == "notify_webcams_changed":
            asyncio.create_task(self._safe(self._load_webcams()))

    async def _safe(self, coro):
        try:
            await coro
        except Exception as e:
            log.warning("printer %s: %s", self.id, e)

    async def _subscribe(self):
        res = await self._call("printer.objects.subscribe", {"objects": SUBSCRIBE})
        self.status = (res or {}).get("status", {})
        self._changed()

    async def _load_webcams(self):
        try:
            res = await self._call("server.webcams.list")
            self.webcams = [c for c in (res or {}).get("webcams", []) if c.get("enabled", True)]
        except (RPCError, asyncio.TimeoutError):
            self.webcams = []
        self._changed()

    async def command(self, action: str):
        """pause / resume / cancel / estop"""
        methods = {"pause": "printer.print.pause", "resume": "printer.print.resume",
                   "cancel": "printer.print.cancel", "estop": "printer.emergency_stop"}
        return await self._call(methods[action], timeout=15)

    # state ------------------------------------------------------------------
    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._changed()

    def _changed(self):
        try:
            self._on_change(self)
        except Exception:
            log.exception("on_change handler failed")

    def snapshot_url(self) -> str | None:
        for cam in self.webcams:
            url = cam.get("snapshot_url")
            if url:
                if url.startswith(("http://", "https://")):
                    return url
                # relative URLs are served by the web frontend (Mainsail/Fluidd nginx), not Moonraker's port
                return f"http://{self.host}{url if url.startswith('/') else '/' + url}"
        return None

    def view(self) -> dict:
        s = self.status
        ps = s.get("print_stats", {})
        ds = s.get("display_status", {})
        vs = s.get("virtual_sdcard", {})
        live = self.connection == "online" and self.klippy_state == "ready"
        state = ps.get("state") if live else None
        progress = ds.get("progress") or vs.get("progress") or 0.0
        duration = ps.get("print_duration") or 0.0
        eta = None
        if state == "printing" and progress > 0.01 and duration > 0:
            eta = duration / progress - duration
        return {
            "connection": self.connection,
            "error": self.error,
            "klippy_state": self.klippy_state,
            "state": state,
            "filename": ps.get("filename") or None,
            "message": ps.get("message") or ds.get("message") or None,
            "progress": progress,
            "print_duration": duration,
            "eta": eta,
            "nozzle": s.get("extruder", {}).get("temperature"),
            "nozzle_target": s.get("extruder", {}).get("target"),
            "bed": s.get("heater_bed", {}).get("temperature"),
            "bed_target": s.get("heater_bed", {}).get("target"),
            "has_webcam": self.snapshot_url() is not None,
        }
