"""Command-line entry point."""

import argparse
import os
import sys
import time
from typing import Iterable, Optional

from . import discord_api
from .generator import CHARSETS, classify, count, generate, load_wordlist
from .notifier import Notifier, parse_header

MAX_ERROR_STREAK = 10


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=value lines; real env vars win."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:
                os.environ.setdefault(key.strip(), value)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discord-name-checker",
        description="Find available short Discord usernames (3L/4L, 3N/4N, 3C/4C).",
    )
    src = p.add_argument_group("what to check")
    src.add_argument("-l", "--length", type=int, nargs="+", default=[3],
                     help="name length(s), e.g. --length 3 4 (default: 3)")
    src.add_argument("-m", "--mode", choices=[*CHARSETS, "custom"], default="letters",
                     help="letters = a-z (L), numbers = 0-9 (N), chars = a-z0-9_. (C), custom = --charset")
    src.add_argument("--charset", help="characters to use with --mode custom")
    src.add_argument("-w", "--wordlist", help="check names from a file instead of generating")
    src.add_argument("--shuffle", action="store_true", help="randomise order")
    src.add_argument("--limit", type=int, help="stop after N checks")

    run = p.add_argument_group("running")
    run.add_argument("-o", "--output", default="available.txt", help="file hits are appended to")
    run.add_argument("--resume", action="store_true",
                     help="skip names already recorded in the progress file")
    run.add_argument("--progress-file", default="checked.txt", help="progress file for --resume")
    run.add_argument("--min-delay", type=float, default=0.0,
                     help="minimum seconds between requests (default 0: pace from Discord's headers)")
    run.add_argument("--proxy", default=None, help="http://user:pass@host:port (or PROXY in .env)")
    run.add_argument("--webhook", default=None,
                     help="send hits to this URL / Discord webhook (or WEBHOOK_URL in .env)")
    run.add_argument("--webhook-header", action="append", default=[],
                     help="extra header for --webhook, e.g. 'Authorization: Bearer X' (repeatable)")
    run.add_argument("--env-file", default=".env", help="path to .env file")
    return p


def pick_names(args) -> tuple[Iterable[str], Optional[int]]:
    if args.wordlist:
        names = load_wordlist(args.wordlist)
        return names, len(names)
    if args.mode == "custom":
        if not args.charset:
            raise SystemExit("--mode custom needs --charset")
        charset = args.charset
    else:
        charset = CHARSETS[args.mode]
    for length in args.length:
        if not 2 <= length <= 32:
            raise SystemExit("--length must be between 2 and 32")
    total = None
    if all(len(set(charset)) ** n <= 3_000_000 for n in args.length):
        total = count(args.length, charset)
    return generate(args.length, charset, shuffle=args.shuffle), total


def read_progress(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(args.env_file)

    token = os.environ.get("DISCORD_TOKEN") or None
    proxy = args.proxy or os.environ.get("PROXY") or None
    webhook = args.webhook or os.environ.get("WEBHOOK_URL") or None

    names, total = pick_names(args)
    done = read_progress(args.progress_file) if args.resume else set()
    if done:
        names = (n for n in names if n not in done)
        if total is not None:
            total = None  # unknown overlap; don't print a misleading total

    try:
        checker = discord_api.DiscordChecker(token=token, proxy=proxy, min_delay=args.min_delay)
    except discord_api.ProxyError as exc:
        raise SystemExit(f"proxy error: {exc}")

    notifier = None
    if webhook:
        try:
            headers = dict(parse_header(h) for h in args.webhook_header)
        except ValueError as exc:
            raise SystemExit(str(exc))
        notifier = Notifier(webhook, headers=headers,
                            on_error=lambda msg: print(f"\n[webhook] {msg}", file=sys.stderr))

    mode = "token" if token else "no token"
    print(f"Checking via Discord ({mode}{', proxy' if proxy else ''}"
          f"{', webhook' if webhook else ''}). Hits -> {args.output}. Ctrl+C to stop.")

    checked = hits = errors = streak = 0
    start = time.monotonic()
    progress = open(args.progress_file, "a", encoding="utf-8")
    output = open(args.output, "a", encoding="utf-8")
    try:
        for name in names:
            if args.limit is not None and checked >= args.limit:
                break
            result = checker.check(name)
            checked += 1
            if result.status == discord_api.AVAILABLE:
                hits += 1
                print(f"\r\033[K[+] {name} ({classify(name)}) available", flush=True)
                output.write(name + "\n")
                output.flush()
                if notifier:
                    notifier.send(name)
            elif result.status == discord_api.ERROR:
                errors += 1
                print(f"\r\033[K[!] {name}: {result.detail}", file=sys.stderr, flush=True)
            streak = streak + 1 if result.status == discord_api.ERROR else 0
            if streak >= MAX_ERROR_STREAK:
                print(f"\n{streak} errors in a row; stopping. Re-run with --resume once fixed.",
                      file=sys.stderr)
                break
            if result.status != discord_api.ERROR:
                progress.write(name + "\n")  # errors stay unchecked for --resume
                progress.flush()
            rate = checked / max(time.monotonic() - start, 1e-6)
            of_total = f"/{total}" if total else ""
            sys.stdout.write(f"\r\033[K{checked}{of_total} checked | {rate:.1f}/s | "
                             f"{hits} hits | {errors} errors | last: {name}")
            sys.stdout.flush()
    except discord_api.AuthError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        checker.close()
        progress.close()
        output.close()
        if notifier:
            notifier.close()

    print(f"\nDone: {checked} checked, {hits} available, {errors} errors.")
    return 0
