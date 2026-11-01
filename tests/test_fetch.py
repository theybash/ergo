"""The daemon fetching an address for a screen. Answer 30: any address.

Both limits here exist to protect the tab a screen is drawn in, not to decide what a screen may
reach. That distinction is the whole design, so the tests say which is which.
"""

from commandcenter import config, fetch
from tests.harness import Case, json_route, text_route


class Addresses(Case):
    def test_only_http_and_https_are_sent(self):
        for bad in ("file:///etc/passwd", "ftp://host/x", "gopher://x", "not a url", "", None,
                    "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                fetch.get(bad)

    def test_a_caller_error_is_not_a_fetch_failure(self):
        """The status code a screen sees. 502 for its own typo sends you looking at the network."""
        with self.assertRaises(ValueError) as caught:
            fetch.get("file:///etc/passwd")
        self.assertNotIsInstance(caught.exception, fetch.Failed)

    def test_a_dead_host_is_a_fetch_failure(self):
        with self.assertRaises(fetch.Failed):
            fetch.get("http://127.0.0.1:1/nothing", timeout=1)

    def test_a_method_it_will_not_send(self):
        with self.assertRaises(ValueError):
            fetch.get("http://127.0.0.1/", method="TRACE")


class Answers(Case):
    def test_json_comes_back_parsed(self):
        srv = self.http({"/q": json_route({"price": 42})})
        got = fetch.get(srv.base + "/q")
        self.assertEqual(got["json"], {"price": 42})
        self.assertEqual(got["status"], 200)

    def test_text_comes_back_as_text(self):
        srv = self.http({"/q": text_route("plain words")})
        self.assertEqual(fetch.get(srv.base + "/q")["text"], "plain words")

    def test_bytes_that_are_not_text_come_back_as_data(self):
        srv = self.http({"/q": lambda h, b: (200, "image/png", b"\x89PNG\x00\x01")})
        got = fetch.get(srv.base + "/q")
        self.assertIn("base64", got)
        self.assertIsNone(got["text"])

    def test_an_error_status_is_an_answer_not_an_exception(self):
        """A screen showing "429, try later" is more use than one showing "the fetch failed"."""
        srv = self.http({"/q": text_route("slow down", code=429)})
        got = fetch.get(srv.base + "/q")
        self.assertEqual(got["status"], 429)
        self.assertIn("slow down", got["text"])

    def test_broken_json_falls_back_to_text_rather_than_failing(self):
        srv = self.http({"/q": lambda h, b: (200, "application/json", "{not json")})
        got = fetch.get(srv.base + "/q")
        self.assertEqual(got["text"], "{not json")

    def test_how_long_it_took_is_reported(self):
        srv = self.http({"/q": json_route({})})
        self.assertGreaterEqual(fetch.get(srv.base + "/q")["ms"], 0)


class Limits(Case):
    def test_a_body_over_the_limit_is_cut_and_says_so(self):
        big = "x" * (config.FETCH_MAX_BYTES + 5000)
        srv = self.http({"/q": text_route(big)})
        got = fetch.get(srv.base + "/q")
        self.assertEqual(got["bytes"], config.FETCH_MAX_BYTES)
        self.assertTrue(got["truncated"], "a screen drawing three quarters of a table quietly "
                                          "is worse than one saying it was cut off")

    def test_a_body_exactly_at_the_limit_is_not_called_truncated(self):
        srv = self.http({"/q": text_route("x" * config.FETCH_MAX_BYTES)})
        got = fetch.get(srv.base + "/q")
        self.assertEqual(got["bytes"], config.FETCH_MAX_BYTES)
        self.assertFalse(got["truncated"])

    def test_a_caller_cannot_ask_for_more_than_the_limit(self):
        big = "x" * (config.FETCH_MAX_BYTES + 5000)
        srv = self.http({"/q": text_route(big)})
        got = fetch.get(srv.base + "/q", max_bytes=config.FETCH_MAX_BYTES * 10)
        self.assertEqual(got["bytes"], config.FETCH_MAX_BYTES)

    def test_a_caller_cannot_ask_for_more_time_than_the_limit(self):
        """A screen asking for ten minutes gets the platform's ten seconds.

        The limit is lowered to 0.3 s for the test. Asserting the real ten would mean a test
        suite that takes ten seconds to prove one clamp, and the clamp is the same either way.
        """
        import time
        config.FETCH_TIMEOUT = 0.3
        started = time.time()
        with self.assertRaises(fetch.Failed):
            fetch.get("http://10.255.255.1/", timeout=600)
        self.assertLess(time.time() - started, 3.3)


class Headers(Case):
    """It forwards nothing of the operator's, and everything the screen asked for."""

    def echo(self):
        return self.http({"*": lambda h, b: (200, "application/json",
                                             {k: v for k, v in h.headers.items()})})

    def test_a_cookie_is_never_forwarded(self):
        srv = self.echo()
        got = fetch.get(srv.base + "/q", headers={"Cookie": "session=secret"})
        self.assertNotIn("Cookie", got["json"])

    def test_the_host_header_is_the_real_host(self):
        srv = self.echo()
        got = fetch.get(srv.base + "/q", headers={"Host": "somewhere.else"})
        self.assertEqual(got["json"]["Host"], f"127.0.0.1:{srv.port}")

    def test_the_screens_own_header_is_kept(self):
        """A screen is its own agent's code, so a key in it is that agent's key."""
        srv = self.echo()
        got = fetch.get(srv.base + "/q", headers={"X-Api-Key": "the agents own"})
        self.assertEqual(got["json"]["X-Api-Key"], "the agents own")

    def test_a_dict_body_is_sent_as_json(self):
        srv = self.http({"/q": lambda h, b: (200, "application/json",
                                             {"got": b.decode(),
                                              "type": h.headers.get("Content-Type")})})
        got = fetch.get(srv.base + "/q", method="POST", body={"a": 1})
        self.assertEqual(got["json"]["got"], '{"a": 1}')
        self.assertEqual(got["json"]["type"], "application/json")
