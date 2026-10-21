#!/usr/bin/env python3
"""
discord name checker — pomelo availability sniper (v2)

async checker against discord's unauthed pomelo endpoint. shared session with
per-request proxy, circuit breaker on connection floods, webhook batching,
proxy scoring + auto-prune.

usage:
    python checker.py --wordlist names.txt
    python checker.py --generate 4 --charset alnum --shuffle --proxies proxies.txt -j 400 -d 2
    python checker.py --wordlist names.txt --proxies proxies.txt --webhook https://discord.com/api/webhooks/.../...
"""

from __future__ import annotations

# ─── py 3.13+ SSL EOF fix (lifted from Cloudzik's engine) ─────────────────
import asyncio.sslproto as _sslproto

_orig_eof = _sslproto.SSLProtocol.eof_received


def _safe_eof(self):
    try:
        return _orig_eof(self)
    except RuntimeError:
        return False


_sslproto.SSLProtocol.eof_received = _safe_eof
# ──────────────────────────────────────────────────────────────────────────

import argparse
import asyncio
import itertools
import json
import logging
import random
import re
import signal
import string
import sys
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# silence noisy windows proactor tracebacks
warnings.filterwarnings("ignore", category=ResourceWarning)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
if sys.platform == "win32":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

ENDPOINT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"

VALID = re.compile(r"^(?!.*\.\.)(?!\.)(?!.*\.$)[a-z0-9_.]{2,32}$")

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]


@dataclass
class Stats:
    requests: int = 0          # every HTTP attempt (incl. 429s)
    checked: int = 0           # names with a verdict (taken/available)
    available: int = 0
    taken: int = 0
    invalid: int = 0
    errors: int = 0
    rate_limited: int = 0
    circuit_opens: int = 0
    started: float = field(default_factory=time.monotonic)
    recent_hits: list = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 1e-6)

    @property
    def rps(self) -> float:
        return self.requests / self.elapsed

    @property
    def check_rps(self) -> float:
        return self.checked / self.elapsed


@dataclass
class ProxyPool:
    """Round-robin pool with per-proxy scoring and auto-dead-marking."""
    proxies: list = field(default_factory=list)
    idx: int = 0
    dead: set = field(default_factory=set)
    score: dict = field(default_factory=lambda: defaultdict(int))

    def next(self) -> Optional[str]:
        if not self.proxies:
            return None
        alive = [p for p in self.proxies if p not in self.dead]
        if not alive:
            self.dead.clear()
            alive = self.proxies
        p = alive[self.idx % len(alive)]
        self.idx += 1
        return p

    def hit(self, proxy: Optional[str]) -> None:
        if proxy:
            self.score[proxy] = min(self.score[proxy] + 1, 10)

    def miss(self, proxy: Optional[str], kill_at: int = -6) -> None:
        if not proxy:
            return
        self.score[proxy] -= 1
        if self.score[proxy] <= kill_at:
            self.dead.add(proxy)

    @property
    def alive_count(self) -> int:
        return max(len(self.proxies) - len(self.dead), 0)


class CircuitBreaker:
    """Pauses all workers briefly after a burst of failures (Cloudzik-style)."""

    def __init__(self, threshold: int = 20, window: float = 2.0, cooldown: float = 2.0) -> None:
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._fails: list = []
        self._open_until = 0.0

    def record_failure(self) -> bool:
        now = time.monotonic()
        self._fails.append(now)
        self._fails = [t for t in self._fails if now - t < self.window]
        if len(self._fails) >= self.threshold:
            self._open_until = now + self.cooldown
            self._fails.clear()
            return True
        return False

    async def wait_if_open(self) -> None:
        now = time.monotonic()
        if now < self._open_until:
            await asyncio.sleep(self._open_until - now)


class WebhookBatcher:
    """Buffers hits and dispatches to a Discord webhook in rate-safe batches."""

    MAX_CHARS = 1900
    BATCH = 15
    INTERVAL = 3.0

    def __init__(self, url: str, session: aiohttp.ClientSession) -> None:
        self.url = url
        self.session = session
        self.queue: asyncio.Queue = asyncio.Queue()
        self._stop = asyncio.Event()

    def enqueue(self, name: str) -> None:
        try:
            self.queue.put_nowait(name)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        buf: list = []
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                name = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                buf.append(name)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if buf and (len(buf) >= self.BATCH or now - last >= self.INTERVAL):
                await self._flush(buf)
                buf = []
                last = now
        if buf:
            await self._flush(buf)

    async def _flush(self, names: list) -> None:
        msg = "\n".join(f"`{n}` is **available** <t:{int(time.time())}:R>" for n in names)
        if len(msg) > self.MAX_CHARS:
            msg = msg[: self.MAX_CHARS - 3] + "..."
        for _ in range(3):
            try:
                async with self.session.post(
                    self.url,
                    json={"content": msg, "username": "name-checker"},
                ) as r:
                    if r.status == 429:
                        try:
                            d = await r.json()
                            await asyncio.sleep(float(d.get("retry_after", 2)))
                        except Exception:
                            await asyncio.sleep(2)
                        continue
                    return
            except Exception:
                await asyncio.sleep(1)


def valid_name(name: str) -> bool:
    return bool(VALID.match(name))


def load_lines(path: Path) -> list:
    out = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def generate(length: int, charset: str, shuffle: bool = False) -> Iterable[str]:
    pool = {
        "alpha": string.ascii_lowercase,
        "digits": string.digits,
        "alnum": string.ascii_lowercase + string.digits,
        "full": string.ascii_lowercase + string.digits + "_",
    }.get(charset, string.ascii_lowercase)
    if shuffle:
        all_names = ["".join(c) for c in itertools.product(pool, repeat=length)]
        random.shuffle(all_names)
        for n in all_names:
            yield n
    else:
        for combo in itertools.product(pool, repeat=length):
            yield "".join(combo)


def _build_session(proxy: Optional[str]) -> aiohttp.ClientSession:
    """Session bound to one proxy. aiohttp_socks handles http:// and socks5://."""
    if proxy:
        connector = ProxyConnector.from_url(proxy)
    else:
        connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    return aiohttp.ClientSession(
        connector=connector,
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=30),
    )


async def check_one(
    session: aiohttp.ClientSession,
    name: str,
    timeout: float,
) -> tuple[str, Optional[bool], Optional[str], Optional[float]]:
    """
    return (name, available_bool_or_None, error_str_or_None, retry_after_or_None).
    """
    try:
        async with session.post(
            ENDPOINT,
            json={"username": name},
            headers={
                "User-Agent": random.choice(UA_POOL),
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": "https://discord.com",
                "Referer": "https://discord.com/",
            },
            timeout=aiohttp.ClientTimeout(total=timeout, sock_connect=5),
        ) as r:
            if r.status == 429:
                retry_after = 1.0
                try:
                    body = await r.json()
                    retry_after = float(body.get("retry_after", 1.0))
                except Exception:
                    pass
                return name, None, "ratelimit", retry_after
            if r.status in (200, 201, 204):
                try:
                    data = await r.json()
                except Exception:
                    data = {}
                taken = bool(data.get("taken", True))
                return name, (not taken), None, None
            if r.status == 400:
                return name, False, None, None  # discord said invalid; treat as taken
            return name, None, f"http{r.status}", None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        return name, None, f"net:{type(e).__name__}", None


def render(stats: Stats, pool: ProxyPool, config: dict) -> Panel:
    tbl = Table.grid(expand=True)
    tbl.add_column(justify="left")
    tbl.add_column(justify="right")

    def row(k, v, style="white"):
        tbl.add_row(Text(k, style="dim"), Text(str(v), style=style))

    row("requests", f"{stats.requests:,}", "cyan")
    row("checked", f"{stats.checked:,}", "cyan")
    row("available", f"{stats.available:,}", "bold green")
    row("taken", f"{stats.taken:,}", "red")
    row("invalid (skipped)", f"{stats.invalid:,}", "yellow")
    row("errors", f"{stats.errors:,}", "magenta")
    row("rate-limited", f"{stats.rate_limited:,}", "yellow")
    row("circuit opens", f"{stats.circuit_opens:,}", "yellow")
    row("req rate", f"{stats.rps:,.1f}/s", "cyan")
    row("check rate", f"{stats.check_rps:,.1f}/s", "cyan")
    row("elapsed", f"{stats.elapsed:,.1f}s", "white")

    hits_tbl = Table.grid(expand=True)
    hits_tbl.add_column()
    if stats.recent_hits:
        for h in stats.recent_hits[-12:]:
            hits_tbl.add_row(Text(f"  ✓ {h}", style="bold green"))
    else:
        hits_tbl.add_row(Text("  (none yet)", style="dim"))

    proxies_str = (
        f"{pool.alive_count}/{len(pool.proxies)}" if pool.proxies else "none"
    )
    header = Text.assemble(
        ("discord name checker v2", "bold white"),
        ("  ·  ", "dim"),
        (f"workers={config['concurrency']}", "cyan"),
        ("  ·  ", "dim"),
        (f"proxies={proxies_str}", "cyan"),
        ("  ·  ", "dim"),
        (f"out={config['out']}", "cyan"),
    )

    return Panel(
        Group(header, Text(""), tbl, Text(""), Text("recent hits", style="bold"), hits_tbl),
        border_style="green" if stats.available else "cyan",
        title="[bold]pomelo sniper[/]",
    )


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    stats: Stats,
    pool: ProxyPool,
    cb: CircuitBreaker,
    out_file,
    stop: asyncio.Event,
    webhook: Optional[WebhookBatcher],
    delay: float,
    timeout: float,
    max_retries: int,
) -> None:
    """Each worker owns a session bound to one proxy (via aiohttp_socks).
    On rotate/kill the whole session is torn down and rebuilt."""
    proxy = pool.next()
    try:
        session = _build_session(proxy)
    except Exception:
        pool.miss(proxy)
        proxy = pool.next()
        session = _build_session(proxy)
    consecutive_bad = 0

    try:
        while not stop.is_set():
            try:
                name = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if name is None:
                queue.task_done()
                return

            attempt = 0
            resolved = False
            while attempt < max_retries and not stop.is_set():
                attempt += 1
                await cb.wait_if_open()
                stats.requests += 1
                _name, available, err, retry_after = await check_one(session, name, timeout)

                if err == "ratelimit":
                    stats.rate_limited += 1
                    pool.miss(proxy, kill_at=-10)
                    consecutive_bad += 1
                    if retry_after and retry_after < 3:
                        await asyncio.sleep(retry_after)
                elif err is not None:
                    stats.errors += 1
                    pool.miss(proxy)
                    consecutive_bad += 1
                    if cb.record_failure():
                        stats.circuit_opens += 1
                    await asyncio.sleep(min(attempt * 0.1, 2.0))
                else:
                    stats.checked += 1
                    pool.hit(proxy)
                    consecutive_bad = 0
                    resolved = True
                    if available:
                        stats.available += 1
                        stats.recent_hits.append(name)
                        out_file.write(name + "\n")
                        out_file.flush()
                        if webhook:
                            webhook.enqueue(name)
                    else:
                        stats.taken += 1
                    break

                # rotate on 2 consecutive bad
                if consecutive_bad >= 2 and pool.proxies:
                    await session.close()
                    proxy = pool.next()
                    try:
                        session = _build_session(proxy)
                    except Exception:
                        pool.miss(proxy)
                        proxy = pool.next()
                        session = _build_session(proxy)
                    consecutive_bad = 0

            if not resolved:
                try:
                    queue.put_nowait(name)
                except Exception:
                    pass

            queue.task_done()
            if delay > 0:
                await asyncio.sleep(delay)
    finally:
        try:
            await session.close()
        except Exception:
            pass


async def iter_source(args) -> AsyncIterator[str]:
    if args.wordlist:
        for line in load_lines(Path(args.wordlist)):
            yield line
    if args.generate:
        for n in generate(args.generate, args.charset, shuffle=args.shuffle):
            yield n
    if args.names:
        for n in args.names:
            yield n


async def run(args) -> int:
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: None)

    stats = Stats()
    proxies = load_lines(Path(args.proxies)) if args.proxies else []
    pool = ProxyPool(proxies=list(proxies))
    cb = CircuitBreaker(threshold=args.circuit_threshold, window=2.0, cooldown=args.circuit_cooldown)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
    stop = asyncio.Event()

    out_path = Path(args.out)
    out_file = out_path.open("a", encoding="utf-8")
    console = Console()

    # tiny direct session purely for webhook posts (bypass proxies)
    webhook_session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=4),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(total=15),
    ) if args.webhook else None

    webhook = WebhookBatcher(args.webhook, webhook_session) if args.webhook else None
    webhook_task = asyncio.create_task(webhook.run()) if webhook else None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    workers = [
        asyncio.create_task(
            worker(
                i, queue, stats, pool, cb, out_file, stop, webhook,
                delay=args.delay,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )
        )
        for i in range(args.concurrency)
    ]

    async def feed():
        async for name in iter_source(args):
            if stop.is_set():
                break
            name = name.strip().lower()
            if not valid_name(name):
                stats.invalid += 1
                continue
            await queue.put(name)
        for _ in workers:
            await queue.put(None)

    feeder = asyncio.create_task(feed())

    config = {"concurrency": args.concurrency, "out": str(out_path)}

    with Live(render(stats, pool, config), console=console, refresh_per_second=6, screen=False) as live:
        try:
            while not stop.is_set():
                live.update(render(stats, pool, config))
                if feeder.done() and queue.empty():
                    break
                await asyncio.sleep(0.15)
        finally:
            stop.set()
            feeder.cancel()
            if webhook:
                webhook.stop()
            await asyncio.gather(*workers, return_exceptions=True)
            if webhook_task:
                await asyncio.gather(webhook_task, return_exceptions=True)
            live.update(render(stats, pool, config))

    out_file.close()
    if webhook_session:
        await webhook_session.close()
    console.print(
        f"[bold green]done.[/] {stats.available} available / {stats.checked} checked "
        f"({stats.requests} requests) in {stats.elapsed:.1f}s — saved to [cyan]{out_path}[/]"
    )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="discord pomelo username availability checker")
    src = p.add_argument_group("source")
    src.add_argument("--wordlist", "-w", help="file of candidate names, one per line")
    src.add_argument("--generate", "-g", type=int, help="generate all names of this length")
    src.add_argument(
        "--charset", "-c",
        choices=["alpha", "digits", "alnum", "full"],
        default="alpha",
    )
    src.add_argument("--names", "-n", nargs="*", help="inline names")
    src.add_argument("--shuffle", "-s", action="store_true", help="randomize generation order")

    net = p.add_argument_group("network")
    net.add_argument("--concurrency", "-j", type=int, default=50)
    net.add_argument("--proxies", "-p", help="file of proxy URLs")
    net.add_argument("--delay", "-d", type=float, default=0.0,
                     help="per-worker sleep between requests")
    net.add_argument("--timeout", "-t", type=float, default=10.0,
                     help="per-request total timeout (s)")
    net.add_argument("--max-retries", type=int, default=12,
                     help="attempts per name before requeue")
    net.add_argument("--circuit-threshold", type=int, default=30,
                     help="connection failures in 2s that trip the circuit")
    net.add_argument("--circuit-cooldown", type=float, default=2.0,
                     help="seconds all workers pause when circuit opens")

    out = p.add_argument_group("output")
    out.add_argument("--out", "-o", default="hits.txt")
    out.add_argument("--webhook", help="discord webhook URL for live hit notifications")

    args = p.parse_args()
    if not (args.wordlist or args.generate or args.names):
        p.error("provide at least one of --wordlist, --generate, --names")
    return args


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
