"""Small deterministic government-style search fixture for the example pipeline."""

from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit


class SearchHandler(BaseHTTPRequestHandler):
    access_log: Path

    @staticmethod
    def _matches(query: str) -> list[str]:
        if query == "Commonwealth Bank":
            return ["Commonwealth Bank branch A", "Commonwealth Bank branch B"]
        if query == "Australian Taxation Office":
            return ["Australian Taxation Office"]
        return []

    @classmethod
    def _directory_html(cls, query: str) -> bytes:
        entries = "".join(f"<li>{html.escape(match)}</li>" for match in cls._matches(query))
        return f"<main><h1>Results for {html.escape(query)}</h1><ul>{entries}</ul></main>".encode()

    def _send_html(self, body: bytes) -> None:
        self.send_response_only(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record_query(self, query: str) -> None:
        with self.access_log.open("a", encoding="utf-8") as log:
            log.write(query + "\n")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            body = b"ok"
        elif parsed.path in {"/directory", "/registry"}:
            query_key = "q" if parsed.path == "/directory" else "term"
            query = parse_qs(parsed.query).get(query_key, [""])[0]
            if parsed.path == "/directory":
                body = self._directory_html(query)
            else:
                entries = "".join(
                    f'<article><h2><a href="/registry/{index}">{html.escape(match)}</a></h2>'
                    '<p class="registration">Registered</p></article>'
                    for index, match in enumerate(self._matches(query), start=1)
                )
                body = f'<section class="results"><header>{html.escape(query)}</header>{entries}</section>'.encode()
            self._record_query(query)
        else:
            self.send_error(404)
            return
        # Keep response headers stable so HTTP verify compares like with like.
        self._send_html(body)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/form-search":
            self.send_error(404)
            return
        if self.headers.get("Content-Type") != "application/x-www-form-urlencoded; charset=utf-8":
            self.send_error(415)
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= 8192:
                raise ValueError("form length out of fixture bounds")
            fields = parse_qsl(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except (KeyError, ValueError, UnicodeError):
            self.send_error(400)
            return
        if [name for name, _ in fields] != ["q", "scope", "scope"] or [value for _, value in fields[1:]] != ["public", "active"]:
            self.send_error(400)
            return
        query = fields[0][1]
        self._record_query(query)
        self._send_html(self._directory_html(query))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--access-log", type=Path, required=True)
    args = parser.parse_args()
    SearchHandler.access_log = args.access_log
    HTTPServer(("127.0.0.1", 8213), SearchHandler).serve_forever()
