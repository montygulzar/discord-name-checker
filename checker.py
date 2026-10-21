#!/usr/bin/env python3
"""
discord name checker — pomelo availability sniper

hits discord's unauthed username-attempt endpoint concurrently, rotates proxies,
tracks hits/takens/errors live, dumps claimable names to disk.

usage:
    python checker.py --wordlist names.txt --concurrency 50
    python checker.py --generate 4 --charset alpha --concurrency 100
    python checker.py --wordlist names.txt --proxies proxies.txt --out hits.txt
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import random
import re
import signal
import string
import sys
import time
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

ENDPOINT = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"

# pomelo rules: 2-32 chars, lowercase letters, digits, underscore, period.
# periods can't lead/trail or repeat. we validate client-side before burning a request.
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
    checked: int = 0
    available: int = 0
    taken: int = 0
    invalid: int = 0
    errors: int = 0
    rate_limited: int = 0
    started: float = field(default_factory=time.monotonic)
    recent_hits: list[str] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return max(time.monotonic() - self.started, 1e-6)

    @property
    def rps(self) -> float:
        return self.checked / self.elapsed


@dataclass
class ProxyPool:
    proxies: list[str]
    idx: int = 0

    def next(self) -> Optional[str]:
        if not self.proxies:
            return None
        p = self.proxies[self.idx % len(self.proxies)]
        self.idx += 1
        return p


def valid_name(name: str) -> bool:
    return bool(VALID.match(name))


def load_lines(path: Path) -> list[str]:
    out = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def generate(length: int, charset: str) -> Iterable[str]:
    pool = {
        "alpha": string.ascii_lowercase,
        "digits": string.digits,
        "alnum": string.ascii_lowercase + string.digits,
        "full": string.ascii_lowercase + string.digits + "_",
    }.get(charset, string.ascii_lowercase)
    for combo in itertools.product(pool, repeat=length):
        yield "".join(combo)


def build_connector(proxy: Optional[str]) -> aiohttp.BaseConnector:
    if proxy:
        return ProxyConnector.from_url(proxy)
    return aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)


async def check_one(
    session: aiohttp.ClientSession,
    name: str,
    retries: int = 3,
) -> tuple[str, Optional[bool], Optional[str]]:
    """return (name, available_bool_or_None, error_or_None)."""
    payload = {"username": name}
    backoff = 1.0
    for attempt in range(retries):
        try:
            async with session.post(
                ENDPOINT,
                json=payload,
                headers={
                    "User-Agent": random.choice(UA_POOL),
                    "Content-Type": "application/json",
                    "Accept": "*/*",
                    "Origin": "https://discord.com",
                    "Referer": "https://discord.com/",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 429:
                    retry_after = 1.0
                    try:
                        body = await r.json()
                        retry_after = float(body.get("retry_after", 1.0))
                    except Exception:
                        pass
                    await asyncio.sleep(min(retry_after, 10.0))
                    return name, None, "ratelimit"
                if r.status >= 500:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if r.status != 200:
                    text = (await r.text())[:120]
                    return name, None, f"http{r.status}:{text}"
                data = await r.json()
                taken = bool(data.get("taken", True))
                return name, (not taken), None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            await asyncio.sleep(backoff)
            backoff *= 2
            if attempt == retries - 1:
                return name, None, f"net:{type(e).__name__}"
    return name, None, "exhausted"


def render(stats: Stats, config: dict) -> Panel:
    tbl = Table.grid(expand=True)
    tbl.add_column(justify="left")
    tbl.add_column(justify="right")

    def row(k, v, style="white"):
        tbl.add_row(Text(k, style="dim"), Text(str(v), style=style))

    row("checked", f"{stats.checked:,}", "cyan")
    row("available", f"{stats.available:,}", "bold green")
    row("taken", f"{stats.taken:,}", "red")
    row("invalid (skipped)", f"{stats.invalid:,}", "yellow")
    row("errors", f"{stats.errors:,}", "magenta")
    row("rate-limited", f"{stats.rate_limited:,}", "yellow")
    row("rate", f"{stats.rps:,.1f} req/s", "cyan")
    row("elapsed", f"{stats.elapsed:,.1f}s", "white")

    hits_tbl = Table.grid(expand=True)
    hits_tbl.add_column()
    if stats.recent_hits:
        for h in stats.recent_hits[-12:]:
            hits_tbl.add_row(Text(f"  ✓ {h}", style="bold green"))
    else:
        hits_tbl.add_row(Text("  (none yet)", style="dim"))

    header = Text.assemble(
        ("discord name checker", "bold white"),
        ("  ·  ", "dim"),
        (f"concurrency={config['concurrency']}", "cyan"),
        ("  ·  ", "dim"),
        (f"proxies={config['proxies']}", "cyan"),
        ("  ·  ", "dim"),
        (f"out={config['out']}", "cyan"),
    )

    return Panel(
        Group(header, Text(""), tbl, Text(""), Text("recent hits", style="bold"), hits_tbl),
        border_style="green" if stats.available else "cyan",
        title="[bold]pomelo sniper[/]",
    )


async def producer(
    names: AsyncIterator[str],
    queue: asyncio.Queue,
    stats: Stats,
) -> None:
    async for name in names:
        name = name.strip().lower()
        if not valid_name(name):
            stats.invalid += 1
            continue
        await queue.put(name)
    # sentinel per worker is handled by caller via None pushes


async def _new_session(pool: ProxyPool) -> tuple[aiohttp.ClientSession, Optional[str]]:
    proxy = pool.next()
    return aiohttp.ClientSession(connector=build_connector(proxy)), proxy


async def worker(
    worker_id: int,
    queue: asyncio.Queue,
    stats: Stats,
    pool: ProxyPool,
    out_file,
    stop: asyncio.Event,
) -> None:
    session, proxy = await _new_session(pool)
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
            name, available, err = await check_one(session, name)
            stats.checked += 1
            if err == "ratelimit":
                stats.rate_limited += 1
                await queue.put(name)  # requeue onto another worker
                consecutive_bad += 1
            elif err is not None:
                stats.errors += 1
                consecutive_bad += 1
            elif available:
                stats.available += 1
                stats.recent_hits.append(name)
                out_file.write(name + "\n")
                out_file.flush()
                consecutive_bad = 0
            else:
                stats.taken += 1
                consecutive_bad = 0
            queue.task_done()

            # rotate proxy if this one keeps getting slapped or erroring
            if consecutive_bad >= 2 and pool.proxies:
                await session.close()
                session, proxy = await _new_session(pool)
                consecutive_bad = 0
    finally:
        await session.close()


async def iter_source(args) -> AsyncIterator[str]:
    if args.wordlist:
        for line in load_lines(Path(args.wordlist)):
            yield line
    if args.generate:
        for n in generate(args.generate, args.charset):
            yield n
    if args.names:
        for n in args.names:
            yield n


async def run(args) -> int:
    stats = Stats()
    proxies = load_lines(Path(args.proxies)) if args.proxies else []
    pool = ProxyPool(proxies=proxies)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
    stop = asyncio.Event()

    out_path = Path(args.out)
    out_file = out_path.open("a", encoding="utf-8")
    console = Console()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    workers = [
        asyncio.create_task(worker(i, queue, stats, pool, out_file, stop))
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

    config = {
        "concurrency": args.concurrency,
        "proxies": len(proxies) or "none",
        "out": str(out_path),
    }

    with Live(render(stats, config), console=console, refresh_per_second=8, screen=False) as live:
        try:
            while not stop.is_set():
                live.update(render(stats, config))
                if feeder.done() and queue.empty():
                    break
                await asyncio.sleep(0.1)
        finally:
            stop.set()
            feeder.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            live.update(render(stats, config))

    out_file.close()
    console.print(
        f"[bold green]done.[/] {stats.available} available / {stats.checked} checked "
        f"in {stats.elapsed:.1f}s — saved to [cyan]{out_path}[/]"
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
        help="charset for --generate (default: alpha)",
    )
    src.add_argument("--names", "-n", nargs="*", help="inline names to check")

    net = p.add_argument_group("network")
    net.add_argument("--concurrency", "-j", type=int, default=25, help="concurrent workers")
    net.add_argument("--proxies", "-p", help="file of proxy URLs (http://, socks5://)")

    out = p.add_argument_group("output")
    out.add_argument("--out", "-o", default="hits.txt", help="file to append hits to")

    args = p.parse_args()
    if not (args.wordlist or args.generate or args.names):
        p.error("provide at least one of --wordlist, --generate, --names")
    return args


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
