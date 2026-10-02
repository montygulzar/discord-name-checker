# discord-name-checker

Finds available short Discord usernames:

| Type | Mode | Characters | 3-length | 4-length |
|------|------|------------|----------|----------|
| L (letters) | `letters` | `a-z` | 17,576 | 456,976 |
| N (numbers) | `numbers` | `0-9` | 1,000 | 10,000 |
| C (chars)   | `chars`   | `a-z 0-9 _ .` | 54,797 | ~2.08M |

It uses only the Python standard library (3.9+), so there's nothing to install.

## Quick start

```bash
git clone https://github.com/montygulzar/discord-name-checker
cd discord-name-checker
cp .env.example .env          # optional: token, proxy, webhook
python main.py --length 3 --mode letters      # 3L
python main.py --length 4 --mode numbers      # 4N
python main.py --length 3 4 --mode chars --shuffle   # 3C + 4C, random order
```

Available names are printed, appended to `available.txt`, and sent to your webhook if you set one.

## Options

| Flag | What it does |
|------|--------------|
| `-l/--length 3 4` | Name length(s) |
| `-m/--mode` | `letters`, `numbers`, `chars`, or `custom` (with `--charset abcxyz`) |
| `-w/--wordlist FILE` | Check names from a file (one per line) instead of generating them |
| `--shuffle` | Random order instead of `aaa, aab, ...` |
| `--limit N` | Stop after N checks |
| `-o/--output FILE` | Where hits go (default `available.txt`) |
| `--resume` | Skip names already in `checked.txt` from a previous run |
| `--min-delay SEC` | Force a minimum gap between requests (default 0) |
| `--proxy URL` | Route requests through an HTTP proxy |
| `--webhook URL` | Send each hit to your server or a Discord webhook |
| `--webhook-header 'Name: value'` | Extra header for the webhook (e.g. auth), repeatable |

## Token (optional)

With no token the checker uses the endpoint Discord's sign-up page uses. If you set
`DISCORD_TOKEN` in `.env`, it uses the logged-in endpoint
(`/users/@me/pomelo-attempt`) instead, which is usually more reliable.
**Never share your token.** Anyone who has it controls your account.
Consider using an alt account: automated checking is against Discord's Terms of Service.

## Speed and rate limits

- **One reused connection.** Every check goes over a single keep-alive HTTPS
  connection, so there's no new TLS handshake per name.
- **Paced by Discord's own headers.** After each response the checker reads
  `X-RateLimit-Remaining` / `X-RateLimit-Reset-After`. It sends at full speed while
  budget remains, then waits exactly as long as Discord says. In normal running that
  means no 429s, which matters because repeated 429s lead to longer temporary IP bans.
- **429 fallback.** If a 429 still happens, it waits `retry_after` and retries the same
  name, backing off harder if 429s repeat.
- **Resume.** `--resume` skips everything already checked, so restarts cost nothing.
  Names that errored aren't marked as checked, so they get retried.
- **Stops on repeated errors.** After 10 errors in a row (bad network, blocked IP)
  it stops instead of burning through the list.

The rate limit applies per IP and per account, so the fastest way to finish a big
list is to let it run. It doesn't rotate proxies or tokens.

## Proxy

A proxy is a middle server. Requests go to it, it forwards them to Discord, and Discord
sees the proxy's IP instead of yours. Use it to keep the traffic off your home IP.
It doesn't make checking faster.

```
PROXY=http://user:pass@host:port
```

Only HTTP proxies (the usual kind providers sell) are supported. The connection to
Discord is still TLS-encrypted end-to-end through the proxy's CONNECT tunnel.
SOCKS proxies aren't supported because they'd need a third-party library.

## Sending hits to your monitoring server

```
WEBHOOK_URL=https://your-server.example.com/hits
```

- **Discord webhook** (`https://discord.com/api/webhooks/...`): posts a message such as
  ``Available: `abc` (3L)``.
- **Any other URL**: gets a JSON `POST`:
  ```json
  {"username": "abc", "length": 3, "type": "3L", "found_at": "2026-10-02T12:00:00+00:00"}
  ```
  Add auth with `--webhook-header "Authorization: Bearer YOUR_KEY"`.

Sending runs on a background thread, so it never slows the checker. Failed sends are retried
up to 3 times, and anything still queued is sent before the program exits.

## Tests

```bash
python -m unittest discover tests
```
