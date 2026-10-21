# discord-name-checker

Pomelo availability sniper. Hits Discord's unauthed username-attempt endpoint
concurrently, rotates proxies, logs claimable names to disk.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# wordlist mode
python checker.py --wordlist names.txt --concurrency 50 --out hits.txt

# brute-force mode: all 4-char lowercase names
python checker.py --generate 4 --charset alpha --concurrency 100

# with proxies (one URL per line: http://user:pass@host:port or socks5://host:port)
python checker.py --wordlist names.txt --proxies proxies.txt --concurrency 200

# quick inline check
python checker.py --names rac0 zero void null
```

Hits stream to `hits.txt` as they come in. Live stats panel shows
checked / available / taken / errors / rate.

## Flags

| flag | default | notes |
|------|---------|-------|
| `--wordlist, -w` | — | candidates file, one per line |
| `--generate, -g N` | — | emit every name of length N |
| `--charset, -c` | `alpha` | `alpha` / `digits` / `alnum` / `full` (adds `_`) |
| `--names, -n` | — | inline names |
| `--concurrency, -j` | 25 | parallel workers |
| `--proxies, -p` | — | proxy pool file |
| `--out, -o` | `hits.txt` | append-mode hit log |

Names are validated against Pomelo rules (2–32 chars, `[a-z0-9_.]`, no leading/
trailing/doubled dots) before hitting the wire — invalid entries don't burn a
request.

## Free proxies

`fetch_proxies.py` scrapes a dozen public SOCKS5/HTTP lists, tests every entry
against Discord in parallel, and writes the survivors to `proxies.txt`
sorted fastest-first.

```bash
python fetch_proxies.py --out proxies.txt --timeout 5 --concurrency 500
```

Free proxies die fast — refresh every ~10 min while a long run is going:

```bash
# bash: refresh loop in a side terminal
while true; do python fetch_proxies.py --out proxies.txt; sleep 600; done
```

Workflow:

```bash
# terminal 1 — keep the pool fresh
while true; do python fetch_proxies.py -o proxies.txt; sleep 600; done

# terminal 2 — run the checker
python checker.py --wordlist names.txt --proxies proxies.txt -j 200
```

Expect maybe 5–20% of scraped proxies to actually reach Discord, so start the
checker's `-j` around the live count (shown at end of the fetch run).

## Notes

- 429s are honoured via `retry_after` and the name is requeued.
- Each worker holds its own session bound to one proxy from the pool.
- Ctrl-C flushes and exits cleanly.
