"""A file the owner sent an agent can be shown back to him in the chat, from that agent's inbox only."""

import http.client
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from commandcenter import server
from tests.harness import Case


class InboxFile(Case):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        self.rec = self.agent("personal-assistant")
        box = Path(self.rec["cwd"]) / ".platform" / "inbox" / self.rec["id"]
        box.mkdir(parents=True)
        (box / "shot.jpg").write_bytes(b"\xff\xd8 a screenshot")

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        reply = conn.getresponse()
        data = reply.read()
        conn.close()
        return reply.status, data

    def test_the_inbox_file_is_served_only_when_asked_for_the_inbox(self):
        self.assertEqual(self.get("/file/personal-assistant/shot.jpg?box=inbox"),
                         (200, b"\xff\xd8 a screenshot"))
        self.assertEqual(self.get("/file/personal-assistant/shot.jpg")[0], 404)
        self.assertEqual(self.get("/file/personal-assistant/..%2F..%2Fsecrets?box=inbox")[0], 404)

    def test_a_thumbnail_is_a_small_jpeg_and_the_tap_gets_the_whole_file(self):
        import io
        from PIL import Image
        big = Image.new("RGB", (2400, 1600), (40, 160, 120))
        buf = io.BytesIO()
        big.save(buf, "PNG")
        box = Path(self.rec["cwd"]) / ".platform" / "inbox" / self.rec["id"]
        (box / "big.png").write_bytes(buf.getvalue())
        code, small = self.get("/file/personal-assistant/big.png?box=inbox&thumb=1")
        self.assertEqual(code, 200)
        with Image.open(io.BytesIO(small)) as got:
            self.assertEqual((got.format, got.size[0]), ("JPEG", 480))
        code, whole = self.get("/file/personal-assistant/big.png?box=inbox")
        self.assertEqual(whole, buf.getvalue())
        # Not a picture: the thumb asks for the file itself.
        self.assertEqual(self.get("/file/personal-assistant/shot.jpg?box=inbox&thumb=1")[0], 200)
