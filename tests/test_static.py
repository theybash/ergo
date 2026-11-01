"""Which frontend the console serves, and how it serves it.

Two frontends now live in the repository: the old web/ folder and a React build under
web-react/dist. PLATFORM_WEB chooses between them at startup, so these tests run a real server on
a throwaway port and ask it for files, because the interesting behaviour is in the headers and a
test that called the function directly would not see them.
"""

import gzip
import http.client
import threading
from http.server import ThreadingHTTPServer

from commandcenter import config, server
from tests.harness import Case


class Static(Case):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        # The poll interval is the shutdown cost, not the request cost: shutdown() waits for the
        # loop to come round again, and the default half second would add eight seconds to the
        # suite for no reason at all.
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        # Registered in this order so they run in the other one: the loop has to be told to stop
        # before the socket closes underneath it.
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def get(self, path, gzip_ok=False):
        """One request, sent exactly as written.

        http.client does not tidy the path, which is the point: a browser would fold ../ away
        before sending it, and the traversal tests need the server to see it.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers={"Accept-Encoding": "gzip"} if gzip_ok else {})
        reply = conn.getresponse()
        body = reply.read()
        head = {k.lower(): v for k, v in reply.getheaders()}
        conn.close()
        return reply.status, head, body

    def dist(self):
        """A folder shaped like a Vite build, and the console pointed at it."""
        root = self.dir / "dist"
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "index.html").write_text("<!doctype html><title>React console</title>")
        (root / "assets" / "x-abc.js").write_text("export const hello = 1;\n" * 400)
        (root / "assets" / "x-abc.css").write_text("body{color:red}")
        (root / "manifest.webmanifest").write_text('{"name":"Command Center"}')
        (root / "sw.js").write_text("self.addEventListener('install', function () {});")
        (root / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        (root / "paste.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg bytes" * 200)
        config.WEB = root
        return root

    # -- the switch -------------------------------------------------------------

    def test_the_variable_is_the_only_thing_that_chooses(self):
        self.assertEqual(config.web_dir(""), config.ROOT / "web")
        self.assertEqual(config.web_dir("web-react/dist"), config.ROOT / "web-react" / "dist")
        self.assertEqual(config.web_dir("/tmp/build"), config.web_dir("/tmp/build"))
        self.assertTrue(config.web_dir("/tmp/build").is_absolute())

    def test_the_default_frontend_is_the_old_console(self):
        code, head, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Command center", body)
        self.assertIn(b"/app.js", body, "the old index still names its own script")
        self.assertTrue(head["content-type"].startswith("text/html"))

    def test_the_old_console_still_serves_its_own_files(self):
        self.assertEqual(self.get("/app.css")[0], 200)
        self.assertEqual(self.get("/app.js")[0], 200)
        # web/vendor is fetched by tools/fetch-vendor.sh and not committed, so a fresh clone has none.
        if (config.ROOT / "web" / "vendor" / "xterm.js").exists():
            self.assertEqual(self.get("/vendor/xterm.js")[0], 200)

    def test_the_variable_points_the_console_at_a_build(self):
        self.dist()
        code, head, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"React console", body)
        self.assertEqual(head["cache-control"], "no-cache")

    # -- assets -----------------------------------------------------------------

    def test_a_hashed_asset_is_kept_for_a_year(self):
        self.dist()
        code, head, body = self.get("/assets/x-abc.js")
        self.assertEqual(code, 200)
        self.assertTrue(head["content-type"].startswith("text/javascript"))
        self.assertEqual(head["cache-control"], "public, max-age=31536000, immutable")
        self.assertIn(b"export const hello", body)

    def test_every_kind_of_file_a_build_ships_has_a_name_on_the_wire(self):
        self.dist()
        wanted = {
            "/assets/x-abc.css": "text/css",
            "/manifest.webmanifest": "application/manifest+json",
            "/sw.js": "text/javascript",
            "/icon.svg": "image/svg+xml",
            "/paste.jpg": "image/jpeg",
        }
        for path, ctype in wanted.items():
            code, head, _ = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertTrue(head["content-type"].startswith(ctype), f"{path} said {head}")

    def test_the_page_and_the_worker_are_checked_every_time(self):
        """They name the hashed files, so a stale one means a new build never arrives."""
        self.dist()
        self.assertEqual(self.get("/")[1]["cache-control"], "no-cache")
        self.assertEqual(self.get("/sw.js")[1]["cache-control"], "no-cache")

    def test_fonts_come_from_beside_the_code_in_either_mode(self):
        for _ in range(2):
            code, head, body = self.get("/fonts/inter-400.woff2")
            self.assertEqual(code, 200)
            self.assertEqual(head["content-type"], "font/woff2")
            self.assertEqual(head["cache-control"], "public, max-age=31536000, immutable")
            self.assertGreater(len(body), 1000)
            self.dist()

    def test_the_view_wrapper_survives_a_build_that_has_none(self):
        """An agent screen has to keep working while the console UI is being replaced."""
        self.dist()
        self.assertEqual(config.view_wrapper(), config.ROOT / "web" / "view.html")
        self.assertTrue(config.view_wrapper().exists())
        (config.WEB / "view.html").write_text("<html>own wrapper</html>")
        self.assertEqual(config.view_wrapper(), config.WEB / "view.html")

    # -- deep links ---------------------------------------------------------------

    def test_a_deep_link_gets_the_page(self):
        self.dist()
        for path in ("/?screen=chat", "/nope", "/agent/box/chat"):
            code, _, body = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertIn(b"React console", body, path)

    def test_a_missing_file_is_still_a_missing_file(self):
        """Answering the page to a missing .js would hide a broken build behind a blank screen."""
        self.dist()
        for path in ("/nope.js", "/assets/gone-123.css", "/nope.svg"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_the_api_is_untouched_by_the_fallback(self):
        self.dist()
        self.assertEqual(self.get("/api/nonsense")[0], 404)
        self.assertEqual(self.get("/api/health")[0], 200)
        # A socket address answers 400 to an ordinary GET, which is its own business. What
        # matters here is that the page never answers for it.
        self.assertNotIn(b"React console", self.get("/ws/agent/box")[2])

    def test_a_path_that_climbs_out_is_refused(self):
        self.dist()
        for path in ("/../etc/passwd", "/assets/../../etc/passwd",
                     "/../../../../etc/passwd", "/assets/../../../etc/hosts.js"):
            code, _, _ = self.get(path)
            self.assertIn(code, (403, 404), path)
            self.assertNotEqual(code, 200, path)

    # -- gzip ----------------------------------------------------------------------

    def test_text_is_gzipped_when_the_browser_asks(self):
        """263 KB of bundle over Tailscale is the reason this exists."""
        self.dist()
        raw = (config.WEB / "assets" / "x-abc.js").read_bytes()
        code, head, body = self.get("/assets/x-abc.js", gzip_ok=True)
        self.assertEqual(code, 200)
        self.assertEqual(head["content-encoding"], "gzip")
        self.assertEqual(head["vary"], "Accept-Encoding")
        self.assertEqual(gzip.decompress(body), raw)
        self.assertLess(len(body), len(raw) / 2)

    def test_nothing_is_gzipped_when_the_browser_does_not_ask(self):
        self.dist()
        code, head, body = self.get("/assets/x-abc.js")
        self.assertEqual(code, 200)
        self.assertNotIn("content-encoding", head)
        self.assertEqual(body, (config.WEB / "assets" / "x-abc.js").read_bytes())

    def test_fonts_and_images_are_left_alone(self):
        """They are compressed already, so gzip spends time to make them bigger."""
        self.dist()
        for path in ("/paste.jpg", "/fonts/inter-400.woff2"):
            code, head, _ = self.get(path, gzip_ok=True)
            self.assertEqual(code, 200, path)
            self.assertNotIn("content-encoding", head, path)
            self.assertNotIn("vary", head, path)
