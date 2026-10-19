"""HTTP API + web UI.  Run:  uvicorn app.main:app --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import discovery
from .manager import Manager
from .net import http_get
from .notify import Notifier
from .store import Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

store = Store(os.getenv("DB_PATH", "data/monitor.db"))
manager = Manager(store, Notifier())
scan_lock = asyncio.Lock()


def err(msg: str, status: int = 400):
    return JSONResponse({"error": msg}, status_code=status)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _pid(request: Request) -> int | None:
    pid = int(request.path_params["pid"])
    return pid if pid in manager.clients else None


# ---- pages -------------------------------------------------------------------
async def index(request: Request):
    return FileResponse(STATIC / "index.html")


# ---- discovery -----------------------------------------------------------------
async def scan(request: Request):
    """Find printers on the LAN. Lists them only; nothing gets subscribed."""
    if scan_lock.locked():
        return err("a scan is already running", 409)
    async with scan_lock:
        found = await discovery.scan()
    for p in found:
        row = store.find(p["host"], p["port"])
        p["added_id"] = row["id"] if row else None
    return JSONResponse({"printers": found})


async def probe(request: Request):
    """Check a single host (manual add by IP)."""
    b = await body(request)
    host = str(b.get("host", "")).strip()
    if not host:
        return err("host is required")
    ports = [int(b["port"])] if b.get("port") else discovery.PROBE_PORTS
    for port in ports:
        if info := await discovery.probe(host, port, b.get("api_key") or None):
            row = store.find(host, port)
            info["added_id"] = row["id"] if row else None
            return JSONResponse(info)
    return err(f"no Moonraker found at {host}", 404)


# ---- selected printers ---------------------------------------------------------
async def list_printers(request: Request):
    return JSONResponse({"printers": manager.views()})


async def add_printer(request: Request):
    b = await body(request)
    host = str(b.get("host", "")).strip()
    try:
        port = int(b.get("port") or 7125)
    except ValueError:
        return err("bad port")
    api_key = (b.get("api_key") or "").strip() or None
    if not host:
        return err("host is required")
    if store.find(host, port):
        return err("that printer is already added", 409)
    info = await discovery.probe(host, port, api_key)
    if not info:
        return err(f"no Moonraker found at {host}:{port}", 404)
    if info["needs_auth"] and not info["auth_ok"]:
        return err("this printer needs a valid API key" if api_key else "this printer needs an API key", 401)
    name = (b.get("name") or "").strip() or info["name"]
    return JSONResponse(manager.add(name, host, port, api_key), status_code=201)


async def update_printer(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    name = str((await body(request)).get("name", "")).strip()
    if not name:
        return err("name is required")
    manager.rename(pid, name)
    return JSONResponse(manager.view(pid))


async def delete_printer(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    await manager.remove(pid)
    return Response(status_code=204)


async def snapshot(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    url = manager.clients[pid].snapshot_url()
    if not url or not url.startswith("http://"):
        return err("no webcam", 404)
    try:
        status, headers, data = await http_get(url, timeout=6)
    except Exception as e:
        return err(f"webcam unreachable: {e}", 502)
    if status != 200:
        return err(f"webcam returned {status}", 502)
    return Response(data, media_type=headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "no-store"})


async def printer_action(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    action = request.path_params["action"]
    if action not in ("pause", "resume", "cancel", "estop"):
        return err("unknown action")
    client = manager.clients[pid]
    if client.connection != "online":
        return err("printer is offline", 409)
    try:
        await client.command(action)
    except Exception as e:
        return err(str(e) or type(e).__name__, 502)
    return JSONResponse({"ok": True})


# ---- live updates (Server-Sent Events) -----------------------------------------
async def events(request: Request):
    q = manager.subscribe()

    async def stream():
        try:
            yield f"event: snapshot\ndata: {json.dumps(manager.views())}\n\n"
            while True:
                try:
                    kind, data = await asyncio.wait_for(q.get(), 15)
                    yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def test_notification(request: Request):
    if not manager.notifier.enabled:
        return err("no notification target set (NTFY_URL or DISCORD_WEBHOOK)")
    await manager.notifier.send("Printer monitor", "Test notification")
    return JSONResponse({"ok": True})


async def health(request: Request):
    return JSONResponse({"ok": True, "printers": len(manager.clients)})


@contextlib.asynccontextmanager
async def lifespan(app):
    await manager.start()
    yield
    await manager.stop()


app = Starlette(routes=[
    Route("/", index),
    Route("/healthz", health),
    Route("/api/scan", scan, methods=["POST"]),
    Route("/api/probe", probe, methods=["POST"]),
    Route("/api/printers", list_printers),
    Route("/api/printers", add_printer, methods=["POST"]),
    Route("/api/printers/{pid:int}", update_printer, methods=["PATCH"]),
    Route("/api/printers/{pid:int}", delete_printer, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/snapshot", snapshot),
    Route("/api/printers/{pid:int}/{action}", printer_action, methods=["POST"]),
    Route("/api/events", events),
    Route("/api/notify/test", test_notification, methods=["POST"]),
], lifespan=lifespan)
