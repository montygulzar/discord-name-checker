"""Sends hits to a monitoring server or Discord webhook on a background thread."""

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

from .generator import classify

USER_AGENT = "DiscordNameChecker/1.0 (+https://github.com/montygulzar/discord-name-checker)"


def is_discord_webhook(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    is_discord_host = host in {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
    return is_discord_host and parts.path.startswith("/api/webhooks/")


def build_payload(url: str, name: str, found_at: Optional[datetime] = None) -> dict:
    found_at = found_at or datetime.now(timezone.utc)
    label = classify(name)
    if is_discord_webhook(url):
        return {"content": f"Available: `{name}` ({label})"}
    return {
        "username": name,
        "length": len(name),
        "type": label,
        "found_at": found_at.isoformat(),
    }


def parse_header(header: str) -> tuple[str, str]:
    key, sep, value = header.partition(":")
    if not sep or not key.strip():
        raise ValueError(f"header must look like 'Name: value', got {header!r}")
    return key.strip(), value.strip()


class Notifier:
    def __init__(
        self,
        url: str,
        headers: Optional[dict[str, str]] = None,
        retries: int = 3,
        timeout: float = 10.0,
        opener: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        on_error: Callable[[str], None] = lambda msg: None,
    ):
        self.url = url
        self.headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
        self.retries = retries
        self.timeout = timeout
        self._open = opener
        self._sleep = sleep
        self._on_error = on_error
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def send(self, name: str) -> None:
        self._queue.put(name)

    def close(self, timeout: float = 15.0) -> None:
        """Flush pending hits, then stop the worker."""
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while True:
            name = self._queue.get()
            if name is None:
                return
            self.deliver(name)

    def deliver(self, name: str) -> bool:
        body = json.dumps(build_payload(self.url, name)).encode()
        for attempt in range(self.retries):
            req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
            try:
                with self._open(req, timeout=self.timeout) as resp:
                    resp.read()
                return True
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    self._sleep(_retry_after(exc) or 1.0)
                    continue
                if 400 <= exc.code < 500:
                    self._on_error(f"webhook rejected {name}: HTTP {exc.code}")
                    return False
            except (urllib.error.URLError, OSError):
                pass
            self._sleep(2 ** attempt)
        self._on_error(f"webhook failed for {name} after {self.retries} attempts")
        return False


def _retry_after(exc: urllib.error.HTTPError) -> Optional[float]:
    try:
        return float(json.loads(exc.read() or b"{}").get("retry_after"))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return float(exc.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
