import base64
import http.client
import json
import unittest
from unittest import mock

from checker import discord_api
from checker.discord_api import AuthError, DiscordChecker, ProxyError, parse_proxy


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status = status
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.headers = headers or {}

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


class FakeConn:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, json.loads(body), headers))

    def getresponse(self):
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_checker(responses, token=None, **kwargs):
    clock = Clock()
    checker = DiscordChecker(token=token, sleep=clock.sleep, clock=clock.time, **kwargs)
    conn = FakeConn(responses)
    checker._connect = lambda: conn
    return checker, conn, clock


class CheckerTests(unittest.TestCase):
    def test_available_and_taken(self):
        checker, conn, _ = make_checker([
            FakeResponse(200, {"taken": False}),
            FakeResponse(200, {"taken": True}),
        ])
        self.assertEqual(checker.check("abc").status, discord_api.AVAILABLE)
        self.assertEqual(checker.check("abd").status, discord_api.TAKEN)
        self.assertEqual(conn.requests[0][1], discord_api.UNAUTHED_PATH)
        self.assertEqual(conn.requests[0][2], {"username": "abc"})

    def test_token_uses_authed_endpoint(self):
        checker, conn, _ = make_checker([FakeResponse(200, {"taken": False})], token="tok")
        checker.check("abc")
        _, path, _, headers = conn.requests[0]
        self.assertEqual(path, discord_api.AUTHED_PATH)
        self.assertEqual(headers["Authorization"], "tok")

    def test_paces_when_bucket_empty(self):
        checker, _, clock = make_checker([
            FakeResponse(200, {"taken": True},
                         {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"}),
            FakeResponse(200, {"taken": True}),
        ])
        checker.check("abc")
        self.assertEqual(clock.sleeps, [])  # no sleep until the next request
        checker.check("abd")
        self.assertEqual(len(clock.sleeps), 1)
        self.assertAlmostEqual(clock.sleeps[0], 2.55)

    def test_no_sleep_while_budget_remains(self):
        checker, _, clock = make_checker([
            FakeResponse(200, {"taken": True},
                         {"X-RateLimit-Remaining": "3", "X-RateLimit-Reset-After": "2"}),
            FakeResponse(200, {"taken": True}),
        ])
        checker.check("abc")
        checker.check("abd")
        self.assertEqual(clock.sleeps, [])

    def test_429_retries_same_name(self):
        checker, conn, clock = make_checker([
            FakeResponse(429, {"retry_after": 1.5}),
            FakeResponse(200, {"taken": False}),
        ])
        result = checker.check("abc")
        self.assertEqual(result.status, discord_api.AVAILABLE)
        self.assertEqual(len(conn.requests), 2)
        self.assertGreaterEqual(clock.sleeps[0], 1.5)
        self.assertLess(clock.sleeps[0], 2.0)

    def test_network_error_reconnects(self):
        checker, conn, _ = make_checker([
            http.client.RemoteDisconnected("gone"),
            FakeResponse(200, {"taken": True}),
        ])
        self.assertEqual(checker.check("abc").status, discord_api.TAKEN)

    def test_401_raises(self):
        checker, _, _ = make_checker([FakeResponse(401, {"message": "401: Unauthorized"})], token="bad")
        with self.assertRaises(AuthError):
            checker.check("abc")

    def test_400_is_invalid(self):
        checker, _, _ = make_checker([FakeResponse(400, {"message": "Invalid Form Body"})])
        self.assertEqual(checker.check("abc").status, discord_api.INVALID)

    def test_gives_up_after_retries(self):
        checker, _, _ = make_checker([FakeResponse(503, b"") for _ in range(3)], max_retries=3)
        self.assertEqual(checker.check("abc").status, discord_api.ERROR)

    def test_min_delay(self):
        checker, _, clock = make_checker(
            [FakeResponse(200, {"taken": True}), FakeResponse(200, {"taken": True})], min_delay=0.5)
        checker.check("abc")
        checker.check("abd")
        self.assertEqual(clock.sleeps, [0.5])


class ProxyTests(unittest.TestCase):
    def test_parse_with_auth(self):
        host, port, headers = parse_proxy("http://user:p%40ss@1.2.3.4:3128")
        self.assertEqual((host, port), ("1.2.3.4", 3128))
        expected = "Basic " + base64.b64encode(b"user:p@ss").decode()
        self.assertEqual(headers, {"Proxy-Authorization": expected})

    def test_parse_without_scheme_or_auth(self):
        self.assertEqual(parse_proxy("proxy.local:8000"), ("proxy.local", 8000, {}))

    def test_rejects_socks(self):
        with self.assertRaises(ProxyError):
            parse_proxy("socks5://1.2.3.4:1080")

    def test_connect_tunnels_through_proxy(self):
        checker = DiscordChecker(proxy="http://u:p@1.2.3.4:3128")
        with mock.patch("http.client.HTTPSConnection") as conn_cls:
            checker._connect()
        conn_cls.assert_called_once()
        self.assertEqual(conn_cls.call_args.args[:2], ("1.2.3.4", 3128))
        tunnel = conn_cls.return_value.set_tunnel
        tunnel.assert_called_once()
        self.assertEqual(tunnel.call_args.args[:2], ("discord.com", 443))
        self.assertIn("Proxy-Authorization", tunnel.call_args.kwargs["headers"])


if __name__ == "__main__":
    unittest.main()
