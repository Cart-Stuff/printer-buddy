"""HTTP API + web UI.  Run:  uvicorn app.main:app --host 0.0.0.0 --port 8080"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import discovery
from .manager import Manager
from .net import http_get, http_stream
from .notify import ALL_EVENTS, PROGRESS_STEPS, SERVICES, Notifier, advanced as notify_advanced
from .store import Store

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

store = Store(os.getenv("DB_PATH", "data/monitor.db"))
manager = Manager(store, Notifier(store))
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
    # no-cache: the browser always checks for a newer page after an update
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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
    b = await body(request)
    if "notify" in b:
        manager.set_notify(pid, bool(b["notify"]))
    if "name" not in b and "group" not in b and "camera" not in b and "notify" not in b:
        return err("nothing to change")
    if "camera" in b:
        c = b.get("camera")
        if c is None:
            cam = None
        elif isinstance(c, dict) and isinstance(c.get("cams") or {}, dict):
            cams = {}
            for k, v in (c.get("cams") or {}).items():
                if not str(k).isdigit() or not isinstance(v, dict):
                    return err("bad camera settings")
                try:
                    rotation = int(v.get("rotation") or 0)
                except (TypeError, ValueError):
                    return err("bad camera settings")
                refresh = v.get("refresh")
                if rotation not in (0, 90, 180, 270) or refresh not in (None, 1, 2, 5, 10):
                    return err("bad camera settings")
                cams[str(int(k))] = {"flip_h": bool(v.get("flip_h")), "flip_v": bool(v.get("flip_v")),
                                     "rotation": rotation, "refresh": refresh}
            try:
                main_cam = max(0, int(c.get("main") or 0))
            except (TypeError, ValueError):
                return err("bad camera settings")
            cam = {"main": main_cam, "cams": cams}
        else:
            return err("bad camera settings")
        manager.set_camera(pid, cam)
    if "name" in b:
        name = str(b.get("name") or "").strip()
        if not name:
            return err("name is required")
        manager.rename(pid, name)
    if "group" in b:
        manager.set_group(pid, str(b.get("group") or "").strip()[:40] or None)
    return JSONResponse(manager.view(pid))


async def set_order(request: Request):
    ids = (await body(request)).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
        return err("ids must be a list of printer ids")
    manager.set_order(ids)
    return JSONResponse({"ok": True})


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
    try:
        cam = int(request.query_params.get("cam", 0))
    except ValueError:
        return err("bad camera number")
    url = manager.clients[pid].snapshot_url(cam)
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


async def live_stream(request: Request):
    """Live MJPEG from the printer's camera, passed through this box."""
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    if setup()["video"] not in ("live", "timelapse"):
        return err("live video is off (installer: Video)", 404)
    try:
        cam = int(request.query_params.get("cam", 0))
    except ValueError:
        return err("bad camera number")
    url = manager.clients[pid].stream_url(cam)
    if not url or not url.startswith("http://"):
        return err("this camera has no MJPEG stream", 404)
    try:
        upstream = await http_stream(url)
    except Exception as e:
        return err(f"camera unreachable: {e}", 502)
    if upstream.status != 200:
        upstream.close()
        return err(f"camera returned {upstream.status}", 502)

    async def relay():
        try:
            async for chunk in upstream.chunks():
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(relay(), media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
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


GCODE_EXT = (".gcode", ".gco", ".g")
MAX_UPLOAD = 1024 * 1024 * 1024  # 1 GB


def _online_client(request: Request):
    pid = _pid(request)
    if pid is None:
        return None, err("not found", 404)
    client = manager.clients[pid]
    if client.connection != "online":
        return None, err("printer is offline", 409)
    return client, None


async def list_files(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        return JSONResponse({"files": await client.list_files()})
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def print_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    name = str((await body(request)).get("path", "")).strip()
    if not name:
        return err("path is required")
    try:
        await client.start_print(name)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return JSONResponse({"ok": True})


async def delete_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    name = request.query_params.get("path", "").strip()
    if not name or ".." in name.split("/"):
        return err("bad path")
    try:
        await client.delete_file(name)
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return Response(status_code=204)


async def upload_file(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    name = request.query_params.get("name", "").replace("\\", "/").split("/")[-1].strip()
    if not name or not name.lower().endswith(GCODE_EXT) or '"' in name:
        return err("pick a .gcode file")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_UPLOAD:
            return err("file is too big (1 GB max)", 413)
    if not data:
        return err("the file is empty")
    try:
        await client.upload(name, bytes(data), start=request.query_params.get("print") == "1")
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)
    return JSONResponse({"ok": True, "name": name})


async def history(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        start = max(0, int(request.query_params.get("start", 0)))
        limit = min(100, max(1, int(request.query_params.get("limit", 30))))
    except ValueError:
        return err("bad start/limit")
    try:
        return JSONResponse(await client.history(start, limit))
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def history_totals(request: Request):
    client, e = _online_client(request)
    if e:
        return e
    try:
        return JSONResponse(await client.history_totals())
    except Exception as ex:
        return err(str(ex) or type(ex).__name__, 502)


async def printer_gcode(request: Request):
    pid = _pid(request)
    if pid is None:
        return err("not found", 404)
    script = str((await body(request)).get("script", "")).strip()
    if not script:
        return err("script is required")
    client = manager.clients[pid]
    if client.connection != "online":
        return err("printer is offline", 409)
    try:
        await client.gcode(script)
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


async def get_notify_settings(request: Request):
    return JSONResponse({**manager.notifier.config(), "all_events": ALL_EVENTS, "progress_steps": PROGRESS_STEPS,
                         "advanced": notify_advanced(), "active": manager.notifier.targets()})


async def save_notify_settings(request: Request):
    b = await body(request)
    values = {}
    for k in ("ntfy_url", "ntfy_token", "discord_webhook"):
        if k in b:
            v = str(b[k] or "").strip()
            if v and k != "ntfy_token" and not v.startswith(("http://", "https://")):
                return err(f"{k.replace('_', ' ')} must start with http:// or https://")
            values[k] = v
    if "events" in b:
        if not isinstance(b["events"], list):
            return err("events must be a list")
        values["events"] = [e for e in b["events"] if e in ALL_EVENTS]
    if "attach_snapshot" in b:
        values["attach_snapshot"] = bool(b["attach_snapshot"])
    for k in ("telegram_token", "telegram_chat", "pushover_user", "pushover_token",
              "smtp_host", "smtp_user", "smtp_pass", "smtp_from", "smtp_to"):
        if k in b:
            values[k] = str(b[k] or "").strip()
    if "smtp_port" in b:
        try:
            values["smtp_port"] = int(b["smtp_port"] or 587)
        except (TypeError, ValueError):
            return err("SMTP port must be a number")
    if "smtp_tls" in b:
        if b["smtp_tls"] not in ("starttls", "ssl", "none"):
            return err("bad SMTP security option")
        values["smtp_tls"] = b["smtp_tls"]
    if "progress_step" in b:
        if b["progress_step"] not in PROGRESS_STEPS:
            return err("bad progress step")
        values["progress_step"] = b["progress_step"]
    store.save_settings(values)
    return await get_notify_settings(request)


async def test_notification(request: Request):
    only = (await body(request)).get("service")
    if only is not None and only not in SERVICES:
        return err("unknown service")
    active = manager.notifier.targets()
    if only and only not in active:
        return err(f"fill in the {only} settings and save first")
    if not active:
        return err("add a notification service first")
    image = None
    for pid in list(manager.clients):
        if image := await manager.snapshot(pid):
            break
    errors = await manager.notifier.send("Printer Buddy", "Test notification - it works!", image, only=only)
    if errors:
        return err("; ".join(errors), 502)
    return JSONResponse({"ok": True, "with_snapshot": bool(image)})


# Setup chosen in install.sh (printer-buddy.env). First choice = default.
SETUP = {
    "ui": ("PB_UI", ("basic", "advanced")),
    "ai": ("PB_AI", ("off", "basic", "heavy")),
    "video": ("PB_VIDEO", ("snapshots", "live", "timelapse")),
    "notify": ("PB_NOTIFY", ("basic", "advanced")),
    "remote": ("PB_REMOTE", ("off", "on")),
    "users": ("PB_USERS", ("single", "multi")),
}
BUILT = {"ui.basic", "ui.advanced", "ai.off", "video.snapshots", "notify.basic", "notify.advanced", "video.live", "remote.off", "users.single"}


def setup() -> dict:
    out = {}
    for key, (env, choices) in SETUP.items():
        value = os.getenv(env, choices[0]).strip().lower()
        out[key] = value if value in choices else choices[0]
    return out


STARTED = str(time.time())  # changes on every restart; open pages reload when it does


async def get_config(request: Request):
    chosen = setup()
    return JSONResponse({"started": STARTED, "setup": chosen, "not_built": [k for k, v in chosen.items() if f"{k}.{v}" not in BUILT]})


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
    Route("/api/config", get_config),
    Route("/api/scan", scan, methods=["POST"]),
    Route("/api/probe", probe, methods=["POST"]),
    Route("/api/printers", list_printers),
    Route("/api/printers", add_printer, methods=["POST"]),
    Route("/api/printers/order", set_order, methods=["POST"]),
    Route("/api/printers/{pid:int}", update_printer, methods=["PATCH"]),
    Route("/api/printers/{pid:int}", delete_printer, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/snapshot", snapshot),
    Route("/api/printers/{pid:int}/stream", live_stream),
    Route("/api/printers/{pid:int}/gcode", printer_gcode, methods=["POST"]),
    Route("/api/printers/{pid:int}/files", list_files),
    Route("/api/printers/{pid:int}/history", history),
    Route("/api/printers/{pid:int}/history/totals", history_totals),
    Route("/api/printers/{pid:int}/files", delete_file, methods=["DELETE"]),
    Route("/api/printers/{pid:int}/files/print", print_file, methods=["POST"]),
    Route("/api/printers/{pid:int}/files/upload", upload_file, methods=["POST"]),
    Route("/api/printers/{pid:int}/{action}", printer_action, methods=["POST"]),
    Route("/api/events", events),
    Route("/api/notify/settings", get_notify_settings),
    Route("/api/notify/settings", save_notify_settings, methods=["PUT"]),
    Route("/api/notify/test", test_notification, methods=["POST"]),
], lifespan=lifespan)
