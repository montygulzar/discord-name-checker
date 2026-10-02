import io
import json
import unittest
import urllib.error

from checker.notifier import Notifier, build_payload, is_discord_webhook, parse_header

HOOK = "https://discord.com/api/webhooks/123/abc"
SERVER = "https://monitor.example.com/hits"


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


class Opener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResp()


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError(SERVER, code, "err", {}, io.BytesIO(body))


class NotifierTests(unittest.TestCase):
    def test_discord_webhook_payload(self):
        self.assertTrue(is_discord_webhook(HOOK))
        self.assertEqual(build_payload(HOOK, "abc"), {"content": "Available: `abc` (3L)"})

    def test_generic_payload(self):
        self.assertFalse(is_discord_webhook(SERVER))
        payload = build_payload(SERVER, "1234")
        self.assertEqual(payload["username"], "1234")
        self.assertEqual(payload["length"], 4)
        self.assertEqual(payload["type"], "4N")
        self.assertIn("found_at", payload)

    def test_parse_header(self):
        self.assertEqual(parse_header("Authorization: Bearer x"), ("Authorization", "Bearer x"))
        with self.assertRaises(ValueError):
            parse_header("nocolon")

    def test_delivers_with_custom_header(self):
        opener = Opener([None])
        n = Notifier(SERVER, headers={"Authorization": "Bearer x"}, opener=opener, sleep=lambda s: None)
        n.send("abc")
        n.close()
        req = opener.requests[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer x")
        self.assertEqual(json.loads(req.data)["username"], "abc")

    def test_retries_then_succeeds(self):
        sleeps = []
        opener = Opener([urllib.error.URLError("down"), http_error(429, b'{"retry_after": 0.7}'), None])
        n = Notifier(HOOK, opener=opener, sleep=sleeps.append)
        self.assertTrue(n.deliver("abc"))
        n.close()
        self.assertEqual(sleeps, [1, 0.7])

    def test_gives_up_on_4xx(self):
        errors = []
        opener = Opener([http_error(404)])
        n = Notifier(SERVER, opener=opener, sleep=lambda s: None, on_error=errors.append)
        self.assertFalse(n.deliver("abc"))
        n.close()
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
