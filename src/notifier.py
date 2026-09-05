"""Discord webhook notifier. Never raises -- a failed notification must not fail a run."""
from __future__ import annotations

import json
import os
import urllib.request


def _webhook() -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def send(content: str, username: str = "tiktok-yt-bot") -> None:
    url = _webhook()
    if not url:
        print(f"[notifier] no DISCORD_WEBHOOK_URL set; would have sent: {content}")
        return
    payload = json.dumps({"content": content[:1900], "username": username}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as exc:  # noqa: BLE001  -- notifications are best-effort
        print(f"[notifier] failed: {exc}")
