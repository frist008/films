"""Local receiver for data collected in the owner's Chrome (CLAUDE.md, «Сбор через Chrome массово»).

Run: python -X utf8 -m films bridge [--port 8765]

A page tab POSTs text/JSON to http://127.0.0.1:8765/ingest/{kind}; the body is saved as
data/cache/ingest/{kind}-{timestamp}.txt (gitignored) and the path is printed. Listens on 127.0.0.1 only.
CORS + Access-Control-Allow-Private-Network headers let an https page reach it (Chrome may ask the owner once
to allow local network access). Nothing else is served; the bodies never pass through Claude's context.
"""
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db

OUT = db.ROOT / "data" / "cache" / "ingest"
MAX_BYTES = 50 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        m = re.fullmatch(r"/ingest/([a-z0-9_\-]{1,40})", self.path)
        n = int(self.headers.get("Content-Length") or 0)
        if not m or n > MAX_BYTES:
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        body = self.rfile.read(n)
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{m.group(1)}-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path.write_bytes(body)
        print(f"saved {len(body)} bytes -> {path}", flush=True)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def main(port: int = 8765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"bridge listening on http://127.0.0.1:{port}/ingest/<kind>", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
