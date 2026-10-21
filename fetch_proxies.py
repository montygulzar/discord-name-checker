#!/usr/bin/env python3
"""
fetch_proxies — scrape free proxy lists, test each against discord, write survivors.

usage:
    python fetch_proxies.py --out proxies.txt --timeout 5 --concurrency 500
    python fetch_proxies.py --out proxies.txt --test-url https://discord.com/api/v9/gateway

run it on a loop (cron / watch) because free proxies die fast:
    watch -n 600 python fetch_proxies.py --out proxies.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp_socks import ProxyConnector
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn

console = Console()

# every entry is (url, scheme). scheme is prepended to each raw host:port line.
SOURCES = [
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt", "socks4"),
    ("https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt", "http"),
    ("https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt", "socks5"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt", "http"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/socks5.txt", "socks5"),
    ("https://raw.githubusercontent.com/zevtyardt/proxy-list/main/http.txt", "http"),
    ("https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=socks5&timeout=5000&format=text", "socks5"),
    ("https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&protocol=http&timeout=5000&format=text", "http"),
    ("https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
    ("https://www.proxy-list.download/api/v1/get?type=http", "http"),
]


async def pull(session: aiohttp.ClientSession, url: str, scheme: str) -> list[str]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return []
            text = await r.text()
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        # strip anything after host:port (some sources append country/etc)
        host_port = line.split()[0].split(",")[0]
        if host_port.count(":") != 1:
            continue
        out.append(f"{scheme}://{host_port}")
    return out


async def gather_sources() -> set[str]:
    console.print("[cyan]pulling proxy lists...[/]")
    async with aiohttp.ClientSession() as s:
        results = await asyncio.gather(*(pull(s, u, sc) for u, sc in SOURCES))
    pool: set[str] = set()
    for r in results:
        pool.update(r)
    console.print(f"  [green]{len(pool):,}[/] unique proxies pulled")
    return pool


async def test_one(url: str, test_url: str, timeout: float, sem: asyncio.Semaphore) -> tuple[str, bool, float]:
    async with sem:
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession(
                connector=ProxyConnector.from_url(url),
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as s:
                async with s.get(test_url) as r:
                    ok = r.status < 500 and r.status != 403
                    return url, ok, time.monotonic() - t0
        except Exception:
            return url, False, timeout


async def run(args: argparse.Namespace) -> int:
    pool = await gather_sources()
    if not pool:
        console.print("[red]no proxies pulled — check your network[/]")
        return 1

    sem = asyncio.Semaphore(args.concurrency)
    tasks = [test_one(p, args.test_url, args.timeout, sem) for p in pool]

    live: list[tuple[str, float]] = []
    with Progress(
        TextColumn("[cyan]testing"),
        BarColumn(bar_width=40),
        TextColumn("{task.completed}/{task.total}"),
        TextColumn("· [green]live: {task.fields[live]}[/]"),
        TimeElapsedColumn(),
        console=console,
    ) as prog:
        task = prog.add_task("test", total=len(tasks), live=0)
        for coro in asyncio.as_completed(tasks):
            url, ok, elapsed = await coro
            if ok:
                live.append((url, elapsed))
                prog.update(task, live=len(live))
            prog.update(task, advance=1)

    live.sort(key=lambda x: x[1])  # fastest first
    out = Path(args.out)
    out.write_text("\n".join(u for u, _ in live) + ("\n" if live else ""))
    console.print(
        f"[bold green]done.[/] {len(live):,}/{len(pool):,} alive — "
        f"saved to [cyan]{out}[/] (sorted fastest-first)"
    )
    if live:
        median = live[len(live) // 2][1]
        console.print(f"  median latency: [yellow]{median*1000:.0f}ms[/]")
    return 0 if live else 2


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="fetch + filter free proxies for discord use")
    p.add_argument("--out", "-o", default="proxies.txt", help="output file (overwritten)")
    p.add_argument("--timeout", "-t", type=float, default=5.0, help="per-proxy test timeout (s)")
    p.add_argument("--concurrency", "-j", type=int, default=500, help="parallel tests")
    p.add_argument(
        "--test-url",
        default="https://discord.com/api/v9/gateway",
        help="URL to hit through each proxy to verify reachability",
    )
    return p.parse_args()


def main() -> int:
    try:
        return asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
