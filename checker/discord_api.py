"""Talks to Discord's username-availability endpoints.

Speed comes from two things: one reused keep-alive HTTPS connection (no TLS
handshake per name) and pacing from Discord's own rate-limit headers, so we
send at full speed while budget remains and sleep exactly as long as Discord
says once it runs out. In steady state that means no 429s at all.
"""

import base64
import http.client
import json
import random
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import unquote, urlsplit

HOST = "discord.com"
AUTHED_PATH = "/api/v9/users/@me/pomelo-attempt"
UNAUTHED_PATH = "/api/v9/unique-username/username-attempt-unauthed"
USER_AGENT = "DiscordNameChecker/1.0 (+https://github.com/montygulzar/discord-name-checker)"

AVAILABLE = "available"
TAKEN = "taken"
INVALID = "invalid"
ERROR = "error"


class AuthError(Exception):
    """The supplied token was rejected."""


class ProxyError(ValueError):
    """The proxy URL can't be used."""


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""


def parse_proxy(url: str) -> tuple[str, int, dict[str, str]]:
    """Turn http://user:pass@host:port into (host, port, tunnel headers)."""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    if parts.scheme != "http":
        raise ProxyError(
            f"unsupported proxy scheme {parts.scheme!r}: use an http:// proxy "
            "(traffic to Discord is still TLS-encrypted through the tunnel)"
        )
    if not parts.hostname:
        raise ProxyError(f"no host in proxy URL {url!r}")
    headers = {}
    if parts.username:
        creds = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
    return parts.hostname, parts.port or 8080, headers


class DiscordChecker:
    def __init__(
        self,
        token: Optional[str] = None,
        proxy: Optional[str] = None,
        proxies: Optional[list[str]] = None,
        min_delay: float = 0.0,
        max_retries: int = 6,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.token = token
        self.path = AUTHED_PATH if token else UNAUTHED_PATH
        # A list is for failover only: we move to the next proxy when the
        # current one can't be reached, never to dodge Discord's rate limits.
        self.proxies = [parse_proxy(p) for p in ([proxy] if proxy else []) + (proxies or [])]
        self._proxy_idx = 0
        self.min_delay = min_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._conn: Optional[http.client.HTTPSConnection] = None
        self._ready_at = 0.0  # earliest time the next request may go out
        self._ssl = ssl.create_default_context()
        self._headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Connection": "keep-alive",
        }
        if token:
            self._headers["Authorization"] = token

    # -- connection ---------------------------------------------------------

    def _connect(self) -> http.client.HTTPSConnection:
        if self.proxies:
            host, port, tunnel_headers = self.proxies[self._proxy_idx]
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ssl)
            conn.set_tunnel(HOST, 443, headers=tunnel_headers)
        else:
            conn = http.client.HTTPSConnection(HOST, 443, timeout=self.timeout, context=self._ssl)
        return conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _post(self, name: str) -> tuple[int, http.client.HTTPMessage, bytes]:
        if self._conn is None:
            self._conn = self._connect()
        body = json.dumps({"username": name})
        self._conn.request("POST", self.path, body=body, headers=self._headers)
        resp = self._conn.getresponse()
        data = resp.read()  # must drain fully to reuse the connection
        if resp.getheader("Connection", "").lower() == "close":
            self.close()
        return resp.status, resp.headers, data

    # -- pacing -------------------------------------------------------------

    def _wait_turn(self) -> None:
        delay = self._ready_at - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _block_for(self, seconds: float) -> None:
        self._ready_at = max(self._ready_at, self._clock() + seconds)

    def _apply_rate_headers(self, headers) -> None:
        remaining = headers.get("X-RateLimit-Remaining")
        reset_after = headers.get("X-RateLimit-Reset-After")
        if remaining is not None and reset_after is not None:
            try:
                if int(float(remaining)) <= 0:
                    # small pad so we don't arrive a hair before the reset
                    self._block_for(float(reset_after) + 0.05)
            except ValueError:
                pass
        if self.min_delay:
            self._block_for(self.min_delay)

    # -- public -------------------------------------------------------------

    def check(self, name: str) -> Result:
        consecutive_429 = 0
        last_detail = ""
        for attempt in range(self.max_retries):
            self._wait_turn()
            try:
                status, headers, raw = self._post(name)
            except (http.client.HTTPException, OSError) as exc:
                # dropped keep-alive, dead proxy, timeout: reconnect and retry
                self.close()
                if len(self.proxies) > 1:
                    self._proxy_idx = (self._proxy_idx + 1) % len(self.proxies)
                last_detail = f"network: {exc}"
                self._block_for(min(2 ** attempt, 30) * 0.5)
                continue

            self._apply_rate_headers(headers)
            payload = _json(raw)

            if status == 200:
                consecutive_429 = 0
                if "taken" not in payload:
                    return Result(name, ERROR, f"unexpected response: {raw[:200]!r}")
                return Result(name, TAKEN if payload["taken"] else AVAILABLE)

            if status == 429:
                consecutive_429 += 1
                retry_after = _float(payload.get("retry_after")) or _float(headers.get("Retry-After")) or 1.0
                # repeated 429s mean we're pushing too hard: back off harder
                backoff = retry_after * (2 ** (consecutive_429 - 1)) + random.uniform(0.05, 0.25)
                self._block_for(backoff)
                last_detail = f"rate limited ({retry_after:.2f}s)"
                continue

            if status == 401:
                raise AuthError("Discord rejected the token (401). Check DISCORD_TOKEN.")

            if status == 400:
                if "captcha_key" in payload:
                    return Result(name, ERROR, "captcha required by Discord")
                return Result(name, INVALID, _short(raw))

            if status >= 500:
                last_detail = f"server error {status}"
                self._block_for(min(2 ** attempt, 30) * 0.5)
                continue

            return Result(name, ERROR, f"HTTP {status}: {_short(raw)}")

        return Result(name, ERROR, last_detail or "gave up after retries")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _float(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _short(raw: bytes) -> str:
    return raw[:200].decode("utf-8", "replace")
