"""Each agent's own Chrome: a real window on a virtual screen, driven only by the daemon."""

import http.client
import json
import shutil
import time
import unittest
import urllib.parse

from commandcenter import browsers, clock, config
from tests import test_voice
from tests.harness import Case

PAGE = ("data:text/html," + urllib.parse.quote(
    "<title>Login test</title><h1>Sign in</h1>"
    "<label>Email <input id=u type=email value='me@example.com'></label>"
    "<label>Password <input id=p type=password value='hunter2'></label>"
    "<button onclick=\"document.title='clicked'\">Continue</button>"))

REAL = shutil.which("Xvfb") and shutil.which("google-chrome")


@unittest.skipUnless(REAL, "needs Xvfb and Chrome")
class RealChrome(unittest.TestCase):
    def setUp(self):
        import tempfile, pathlib
        self.var, config.VAR = config.VAR, pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(setattr, config, "VAR", self.var)
        self.addCleanup(shutil.rmtree, config.VAR, True)
        self.addCleanup(browsers.close_all)

    def test_a_snapshot_names_what_to_act_on_and_hides_a_password(self):
        got = browsers.run("tester", "open", {"url": PAGE})
        self.assertEqual(got["title"], "Login test")
        lines = got["elements"].splitlines()
        self.assertTrue(any('button "Continue"' in l for l in lines), lines)
        self.assertTrue(any('heading "Sign in"' in l for l in lines), lines)
        self.assertNotIn("hunter2", got["elements"])
        self.assertIn("me@example.com", got["elements"])

    def test_a_click_by_ref_works_and_a_dead_ref_is_refused(self):
        got = browsers.run("tester", "open", {"url": PAGE})
        ref = next(l.split()[0] for l in got["elements"].splitlines() if '"Continue"' in l)
        # This click only sets the title, so no load comes: waiting 3 s for one checks nothing.
        self.addCleanup(setattr, browsers, "CLICK_LOAD_WAIT", browsers.CLICK_LOAD_WAIT)
        browsers.CLICK_LOAD_WAIT = 0.3
        browsers.run("tester", "act", {"ref": ref})
        time.sleep(0.3)
        self.assertEqual(browsers.run("tester", "snapshot", {})["title"], "clicked")
        with self.assertRaises(RuntimeError):
            browsers.run("tester", "act", {"ref": "e999"})

    def test_a_screenshot_is_a_jpeg(self):
        browsers.run("tester", "open", {"url": PAGE})
        import base64
        data = base64.b64decode(browsers.run("tester", "screenshot", {})["image"])
        self.assertEqual(data[:2], b"\xff\xd8")

    def test_while_owner_has_control_the_agent_waits(self):
        browsers.run("tester", "open", {"url": PAGE})
        browsers.control("tester", True)
        with self.assertRaises(RuntimeError):
            browsers.run("tester", "snapshot", {})
        browsers.control("tester", False)
        self.assertIn("elements", browsers.run("tester", "snapshot", {}))


class Idle(unittest.TestCase):
    def test_status_says_when_an_idle_browser_closes(self):
        b = browsers.Browser("x")
        b.alive = True
        b.used = clock.now() - 120
        browsers._browsers["x"] = b
        self.addCleanup(browsers._browsers.pop, "x", None)
        row = browsers.status()["x"]
        self.assertAlmostEqual(row["closes_in"], browsers.IDLE_SECONDS - 120, delta=2)
        b.watchers = 1
        self.assertIsNone(browsers.status()["x"]["closes_in"])


class Route(test_voice.Routes):
    def test_a_request_through_the_tailnet_proxy_cannot_drive_a_browser(self):
        self.agent("job-search") if hasattr(self, "agent") else None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/browser/job-search/snapshot", body="{}",
                     headers={"Content-Type": "application/json", "X-Forwarded-For": "100.85.1.2"})
        reply = conn.getresponse()
        reply.read()
        conn.close()
        self.assertEqual(reply.status, 403)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(REAL, "needs Xvfb and Chrome")
class OneChromePerSide(Case):
    """Every agent shares one Chrome and its sign-ins, in windows of its own."""

    def setUp(self):
        super().setUp()
        self.addCleanup(browsers.close_all)
        self.agent("pa")
        self.agent("js")

    def cookie(self, agent, set_to=None):
        b = browsers.get(agent)
        if set_to:
            b.page("Network.enable")
            b.page("Network.setCookie", {"name": "who", "value": set_to, "domain": "example.com", "path": "/"})
        got = b.page("Network.getCookies", {"urls": ["https://example.com/"]})["cookies"]
        return next((c["value"] for c in got if c["name"] == "who"), None)

    def test_one_side_shares_one_chrome_and_its_cookies(self):
        self.cookie("pa", set_to="owner")
        self.assertIs(browsers.get("pa").chrome, browsers.get("js").chrome)
        self.assertEqual(self.cookie("js"), "owner")

    def test_an_agent_sees_only_its_own_tabs(self):
        browsers.run("pa", "open", {"url": PAGE})
        browsers.run("js", "open", {"url": "about:blank"})
        titles = [t["title"] for t in browsers.run("js", "tabs", {})["tabs"]]
        self.assertNotIn("Login test", titles)
        self.assertEqual(len(browsers.run("pa", "tabs", {})["tabs"]), 1)

    def test_closing_one_agent_leaves_the_others_page(self):
        browsers.run("pa", "open", {"url": PAGE})
        browsers.run("js", "open", {"url": "about:blank"})
        browsers.close("js")
        self.assertEqual(browsers.run("pa", "snapshot", {})["title"], "Login test")


@unittest.skipUnless(REAL, "needs Xvfb and Chrome")
class FollowsNewWindows(Case):
    def setUp(self):
        super().setUp()
        self.addCleanup(browsers.close_all)
        self.agent("pa")

    def test_a_window_the_page_opens_becomes_the_one_in_use(self):
        browsers.run("pa", "open", {"url": PAGE})
        b = browsers.get("pa")
        b.page("Runtime.evaluate", {"expression": "const w = window.open('about:blank'); w.document.title = 'second'", "userGesture": True})
        # Look until the new window is the one in use, for up to 5 s, rather than a fixed 1.5 s.
        for _ in range(50):
            title = browsers.run("pa", "snapshot", {})["title"]
            if title == "second":
                break
            time.sleep(0.1)
        self.assertEqual(title, "second")


@unittest.skipUnless(REAL, "needs Xvfb and Chrome")
class TypesLikeAPerson(Case):
    def setUp(self):
        super().setUp()
        self.addCleanup(browsers.close_all)
        self.agent("pa")

    def test_every_character_is_a_key_press_the_page_can_hear(self):
        page = "data:text/html," + urllib.parse.quote(
            "<title>keys</title><input id=f><script>window.keys=0;"
            "document.getElementById('f').addEventListener('keydown',()=>window.keys++)</script>")
        browsers.run("pa", "open", {"url": page})
        b = browsers.get("pa")
        b.page("Runtime.evaluate", {"expression": "document.getElementById('f').focus()"})
        browsers.run("pa", "type", {"text": "Hi there 42"})
        got = b.page("Runtime.evaluate", {"expression": "[document.getElementById('f').value, window.keys]",
                                          "returnByValue": True})["result"]["value"]
        self.assertEqual(got, ["Hi there 42", 11])
