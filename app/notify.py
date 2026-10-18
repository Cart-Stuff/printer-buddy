"""Push notifications (ntfy and/or Discord webhook), configured by environment variables."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self):
        self.ntfy_url = os.getenv("NTFY_URL")            # e.g. https://ntfy.sh/my-printers
        self.ntfy_token = os.getenv("NTFY_TOKEN")
        self.discord = os.getenv("DISCORD_WEBHOOK")
        events = os.getenv("NOTIFY_EVENTS", "started,paused,resumed,finished,failed,cancelled")
        self.events = {e.strip() for e in events.split(",") if e.strip()}

    @property
    def enabled(self) -> bool:
        return bool(self.ntfy_url or self.discord)

    def wants(self, event: str) -> bool:
        return event in self.events

    async def send(self, title: str, message: str):
        jobs = []
        if self.ntfy_url:
            headers = {"Title": title.encode("utf-8").decode("latin-1", "replace")}
            if self.ntfy_token:
                headers["Authorization"] = f"Bearer {self.ntfy_token}"
            jobs.append(asyncio.to_thread(_post, self.ntfy_url, message.encode(), headers))
        if self.discord:
            body = json.dumps({"content": f"**{title}**\n{message}"}).encode()
            jobs.append(asyncio.to_thread(_post, self.discord, body, {"Content-Type": "application/json"}))
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                log.warning("notification failed: %s", res)


def _post(url: str, body: bytes, headers: dict):
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "printer-monitor", **headers}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
