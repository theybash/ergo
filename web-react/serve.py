"""Serve dist/ on the tailnet, fast on a phone.

`vite preview` sent the 263 KB bundle with Cache-Control: no-cache, so every open of the page
pulled it again over Tailscale. The bundle's filename carries a content hash, so it can be cached
for a year: a new build gets a new name. index.html is the one file that must always be fresh.
Text assets go out gzipped when the browser asks, which cuts the bundle to about 80 KB.
"""
import gzip, http.server, os, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
PORT = int(os.environ.get("PORT", "8083"))
TEXT = (".js", ".css", ".html", ".svg", ".json", ".webmanifest", ".txt", ".map")

class H(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
                      ".svg": "image/svg+xml", ".js": "text/javascript"}
    def do_GET(self):
        path = self.translate_path(self.path.split("?", 1)[0])
        if os.path.isdir(path): path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            # A deep link like /?screen=chat is the same page; anything else unknown is index too.
            path = os.path.join(ROOT, "index.html")
        with open(path, "rb") as f: body = f.read()
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", self.guess_type(path))
        if "/assets/" in path or ext == ".woff2":
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        if ext in TEXT and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6); self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, fmt, *a): sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

os.chdir(ROOT)
http.server.ThreadingHTTPServer.allow_reuse_address = True
with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H) as s:
    print(f"react mock on http://0.0.0.0:{PORT} from {ROOT}", flush=True); s.serve_forever()
