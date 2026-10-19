"""Push notifications (ntfy and/or Discord). Set up in the web page; env vars are only defaults."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import urllib.request
import uuid

log = logging.getLogger(__name__)

ALL_EVENTS = ["started", "paused", "resumed", "finished", "failed", "cancelled", "offline", "shutdown"]


def _hdr(text: str) -> str:
    """HTTP headers must be latin-1; use RFC 2047 (which ntfy understands) for anything else."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(text.encode()).decode() + "?="


class Notifier:
    def __init__(self, store):
        self.store = store

    def config(self) -> dict:
        s = self.store.settings()
        env_events = os.getenv("NOTIFY_EVENTS")
        default_events = [e.strip() for e in env_events.split(",") if e.strip()] if env_events else ALL_EVENTS
        return {
            "ntfy_url": s.get("ntfy_url", os.getenv("NTFY_URL", "")),
            "ntfy_token": s.get("ntfy_token", os.getenv("NTFY_TOKEN", "")),
            "discord_webhook": s.get("discord_webhook", os.getenv("DISCORD_WEBHOOK", "")),
            "events": s.get("events", default_events),
            "attach_snapshot": s.get("attach_snapshot", True),
        }

    @property
    def enabled(self) -> bool:
        c = self.config()
        return bool(c["ntfy_url"] or c["discord_webhook"])

    def wants(self, event: str) -> bool:
        return event in self.config()["events"]

    async def send(self, title: str, message: str, image: bytes | None = None) -> list[str]:
        c = self.config()
        if not c["attach_snapshot"]:
            image = None
        jobs = []
        if c["ntfy_url"]:
            jobs.append(asyncio.to_thread(_ntfy, c["ntfy_url"], c["ntfy_token"], title, message, image))
        if c["discord_webhook"]:
            jobs.append(asyncio.to_thread(_discord, c["discord_webhook"], title, message, image))
        errors = []
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("notification failed: %s", res)
                errors.append(str(res))
        return errors


def _request(url: str, body: bytes, headers: dict, method: str = "POST"):
    req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": "printer-buddy", **headers})
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def _ntfy(url: str, token: str, title: str, message: str, image: bytes | None):
    headers = {"Title": _hdr(title)}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if image:
        headers.update({"Message": _hdr(message), "Filename": "snapshot.jpg"})
        _request(url, image, headers, method="PUT")
    else:
        _request(url, message.encode(), headers)


def _discord(url: str, title: str, message: str, image: bytes | None):
    content = f"**{title}**\n{message}"
    if not image:
        _request(url, json.dumps({"content": content}).encode(), {"Content-Type": "application/json"})
        return
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": content, "embeds": [{"image": {"url": "attachment://snapshot.jpg"}}]})
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
        f"Content-Type: application/json\r\n\r\n{payload}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"snapshot.jpg\"\r\n"
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + image + f"\r\n--{boundary}--\r\n".encode()
    _request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
