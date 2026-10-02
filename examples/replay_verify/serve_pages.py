"""Serve the example's static pages with byte-identical responses.

Verify mode compares each live response with the recorded one exactly,
headers included. An ordinary web server adds a ``Date`` header (and often
``Server`` and ``Last-Modified``), so its responses differ on every request
and verify reports every call as a mismatch. This fixture sends only
``Content-Type`` and ``Content-Length``, so an unchanged page produces an
identical response each time.

Every request is appended to ``--access-log`` so the walkthrough can count
how many requests each run made.

Usage:
    python examples/replay_verify/serve_pages.py --port 8204 --access-log PATH [--pages-dir DIR]
"""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGES_DIR = Path(__file__).resolve().parent / "pages"


def make_handler(access_log: Path, pages_dir: Path) -> type[BaseHTTPRequestHandler]:
    class DeterministicPageHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with access_log.open("a", encoding="utf-8") as log:
                log.write(f"GET {self.path}\n")
            name = self.path.lstrip("/")
            page = pages_dir / name
            if "/" in name or not name.endswith(".html") or not page.is_file():
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            self._send(200, page.read_bytes(), "text/html; charset=utf-8")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            # send_response_only() skips the Server and Date headers that
            # send_response() would add.
            self.send_response_only(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return DeterministicPageHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8204)
    parser.add_argument("--access-log", type=Path, required=True)
    parser.add_argument("--pages-dir", type=Path, default=PAGES_DIR)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.access_log, args.pages_dir))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
