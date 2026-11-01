"""The logins: a vault per side with its key in the TPM, and the one door to a page."""

import http.server
import os
import shutil
import threading
import unittest
import urllib.parse

from commandcenter import browsers, vault
from tests.harness import Case

REAL = all(shutil.which(b) for b in ("keepassxc-cli", "tpm2_createprimary", "Xvfb", "google-chrome"))


class Code(unittest.TestCase):
    def test_the_authenticator_code_matches_the_standard(self):
        # RFC 6238, SHA-1, the 20 byte test key, T = 59 s.
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(vault.code(secret, at=59, digits=8), "94287082")


class Side(unittest.TestCase):
    def test_there_is_one_side_whatever_the_folder(self):
        self.assertEqual(vault.side_of({"cwd": "/home/user/work/globex"}), vault.SIDE)
        self.assertEqual(vault.side_of({"cwd": "/home/user/ergo/job-search"}), vault.SIDE)


class LoginPage(http.server.BaseHTTPRequestHandler):
    posted = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (b"<title>Sign in</title><form method=post action=/login>"
                b"<input type=email name=email><input type=password name=pw><button>Go</button></form>")
        if self.path.startswith("/home"):
            body = b"<title>Welcome</title><h1>Signed in</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        LoginPage.posted.append(urllib.parse.parse_qs(self.rfile.read(n).decode()))
        self.send_response(303)
        self.send_header("Location", "/home")
        self.end_headers()


@unittest.skipUnless(REAL, "needs KeePassXC, tpm2-tools, Xvfb and Chrome")
class RealLogin(Case):
    def setUp(self):
        super().setUp()
        vault._keys.clear()
        self.addCleanup(vault._keys.clear)
        self.addCleanup(browsers.close_all)
        self.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LoginPage)
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(self.site.shutdown)
        self.port = self.site.server_address[1]
        LoginPage.posted = []
        self.agent("job-search")

    def save(self, url):
        return vault.add({"agent": "job-search", "title": "Test", "url": url,
                          "username": "me@example.com", "password": "s3cret-pw"})

    def test_a_login_is_saved_listed_without_secrets_and_typed_only_by_the_daemon(self):
        self.save(f"http://127.0.0.1:{self.port}/")
        rows = vault.listing("personal")
        self.assertEqual(rows, [{"agent": "job-search", "title": "Test", "url": f"http://127.0.0.1:{self.port}/"}])
        got = vault.login("job-search", "Test")
        self.assertEqual(got["title"], "Welcome")
        self.assertNotIn("s3cret-pw", repr(got))
        self.assertEqual(LoginPage.posted[0]["pw"], ["s3cret-pw"])
        self.assertEqual(LoginPage.posted[0]["email"], ["me@example.com"])
        # The key file is never left on disk.
        base = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}") + "/ergo-vault"
        self.assertEqual([p for p in os.listdir(base)] if os.path.isdir(base) else [], [])

    def test_a_page_on_another_site_gets_nothing_typed(self):
        # Saved for localhost, while the page lives on 127.0.0.1: a different site by its address.
        self.save(f"http://localhost:{self.port}/")
        b = browsers.get("job-search")
        b.page("Page.navigate", {"url": f"http://127.0.0.1:{self.port}/"})
        # The login goes to the saved address first, which is the right site, so to test the refusal
        # the saved site must be unreachable from here: point it at a port nobody serves.
        vault.remove({"agent": "job-search", "title": "Test"})
        vault.add({"agent": "job-search", "title": "Elsewhere", "url": "http://localhost:1/",
                   "username": "u", "password": "p"})
        with self.assertRaises(RuntimeError):
            vault.login("job-search", "Elsewhere")
        self.assertEqual(LoginPage.posted, [])

    def test_another_agent_cannot_use_the_login(self):
        self.save(f"http://127.0.0.1:{self.port}/")
        self.agent("personal-assistant")
        with self.assertRaises(RuntimeError):
            vault.login("personal-assistant", "Test")


class VaultRoute(Case):
    def test_an_agent_on_the_laptop_cannot_change_the_logins(self):
        from tests import test_voice  # noqa: F401
        import http.client, json
        from http.server import ThreadingHTTPServer
        from commandcenter import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/api/vault", body=json.dumps({"agent": "x"}),
                     headers={"Content-Type": "application/json"})
        reply = conn.getresponse()
        reply.read()
        self.assertEqual(reply.status, 403)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(REAL, "needs KeePassXC, tpm2-tools, Xvfb and Chrome")
class SaveWhatHeTyped(Case):
    """A login the owner types while he has the page is offered for the vault, and nothing else is."""

    def setUp(self):
        super().setUp()
        vault._keys.clear()
        self.addCleanup(vault._keys.clear)
        self.addCleanup(browsers.close_all)
        self.site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), LoginPage)
        threading.Thread(target=self.site.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(self.site.shutdown)
        self.port = self.site.server_address[1]
        self.agent("job-search")
        self.told = []

    def type_and_submit(self, user, pw):
        b = browsers._browsers["job-search"]
        b.page("Runtime.evaluate", {"expression":
            f"document.querySelector('input[type=email]').value={user!r};"
            f"document.querySelector('input[type=password]').value={pw!r};"
            "document.querySelector('button').click()"})
        # The form is caught as it is sent, before the page leaves, so the page landing is enough.
        import time
        for _ in range(100):
            try:
                href = b.page("Runtime.evaluate", {"expression": "location.href",
                                                   "returnByValue": True})["result"].get("value") or ""
            except browsers.Gone:
                raise
            except RuntimeError as exc:
                # Chrome can refuse while the old page goes away. Look again, but a Chrome
                # that stopped answering fails now, not after 100 waits.
                if "did not answer" in str(exc):
                    raise
                href = ""
            if href.endswith("/home"):
                break
            time.sleep(0.05)
        time.sleep(0.1)

    def open(self):
        browsers.run("job-search", "open", {"url": f"http://127.0.0.1:{self.port}/"})
        b = browsers._browsers["job-search"]
        b.event_sinks.add(self.told.append)
        return b

    def test_a_login_he_types_is_offered_and_saved_when_he_says_yes(self):
        self.open()
        browsers.control("job-search", True)
        self.type_and_submit("me@example.com", "typed-by-hand")
        self.assertEqual(self.told[-1], {"t": "save_login", "site": "127.0.0.1", "user": "me@example.com"})
        self.assertNotIn("typed-by-hand", repr(self.told))
        got = browsers.answer_offer("job-search", True)
        self.assertEqual(got, {"saved": True, "title": "127.0.0.1", "user": "me@example.com"})
        self.assertEqual([r["agent"] for r in vault.listing("personal")], ["job-search"])
        # And it works: the daemon signs in with it, typing what he typed.
        browsers.control("job-search", False)
        LoginPage.posted = []
        browsers.run("job-search", "open", {"url": f"http://127.0.0.1:{self.port}/"})
        got = vault.login("job-search", "127.0.0.1")
        self.assertTrue(LoginPage.posted, got)
        self.assertEqual(LoginPage.posted[0]["pw"], ["typed-by-hand"])

    def test_a_two_step_login_pairs_the_user_name_with_the_password(self):
        b = self.open()
        browsers.control("job-search", True)
        browsers._captured(b, {"origin": f"http://127.0.0.1:{self.port}", "user": "first@step.com", "pw": ""})
        browsers._captured(b, {"origin": f"http://127.0.0.1:{self.port}", "user": "", "pw": "second-step"})
        self.assertEqual(self.told[-1]["user"], "first@step.com")

    def test_a_form_the_agent_fills_is_never_offered(self):
        self.open()
        self.type_and_submit("me@example.com", "agent-typed")
        self.assertEqual(self.told, [])
        self.assertEqual(browsers.answer_offer("job-search", True), {"saved": False})
        self.assertEqual(vault.listing("personal"), [])

    def test_not_now_forgets_it(self):
        self.open()
        browsers.control("job-search", True)
        self.type_and_submit("me@example.com", "typed-by-hand")
        self.assertEqual(browsers.answer_offer("job-search", False), {"saved": False})
        self.assertEqual(browsers.answer_offer("job-search", True), {"saved": False})
        self.assertEqual(vault.listing("personal"), [])
