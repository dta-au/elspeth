"""Small deterministic government-style search fixture for the example pipeline."""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


class SearchHandler(BaseHTTPRequestHandler):
    access_log: Path

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            body = b"ok"
        elif parsed.path == "/directory":
            query = parse_qs(parsed.query).get("q", [""])[0]
            if query == "Commonwealth Bank":
                matches = ["Commonwealth Bank branch A", "Commonwealth Bank branch B"]
            elif query == "Australian Taxation Office":
                matches = ["Australian Taxation Office"]
            else:
                matches = []
            entries = "".join(f"<li>{html.escape(match)}</li>" for match in matches)
            body = f"<main><h1>Results for {html.escape(query)}</h1><ul>{entries}</ul></main>".encode()
            with self.access_log.open("a", encoding="utf-8") as log:
                log.write(query + "\n")
        else:
            self.send_error(404)
            return
        # Keep response headers stable so HTTP verify compares like with like.
        self.send_response_only(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--access-log", type=Path, required=True)
    args = parser.parse_args()
    SearchHandler.access_log = args.access_log
    HTTPServer(("127.0.0.1", 8213), SearchHandler).serve_forever()
