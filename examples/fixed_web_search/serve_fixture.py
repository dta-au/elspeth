"""Small deterministic government-style search fixture for the example pipeline."""

from __future__ import annotations

import html
from email.parser import BytesParser
from email.policy import default
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
        path = urlsplit(self.path).path
        if path not in {"/form-search", "/multipart-search"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers["Content-Length"])
            if not 0 < length <= 8192:
                raise ValueError("form length out of fixture bounds")
            wire_body = self.rfile.read(length)
            if path == "/form-search":
                if self.headers.get("Content-Type") != "application/x-www-form-urlencoded; charset=utf-8":
                    self.send_error(415)
                    return
                fields = parse_qsl(wire_body.decode("utf-8"), keep_blank_values=True)
                valid = [name for name, _ in fields] == ["q", "scope", "scope"] and [value for _, value in fields[1:]] == [
                    "public",
                    "active",
                ]
            else:
                content_type = self.headers.get("Content-Type", "")
                if not content_type.startswith("multipart/form-data; boundary="):
                    self.send_error(415)
                    return
                message = BytesParser(policy=default).parsebytes(
                    b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + wire_body
                )
                parts = list(message.iter_parts())
                fields = [(part.get_param("name", header="content-disposition"), part.get_payload(decode=True)) for part in parts]
                valid = (
                    len(fields) == 3
                    and [name for name, _ in fields] == ["q", "attachment", "scope"]
                    and fields[1][1] == b"public-search-fixture"
                    and fields[2][1] == b"public"
                    and parts[1].get_filename() == "query.txt"
                    and parts[1].get_content_type() == "text/plain"
                )
                if valid:
                    fields = [("q", fields[0][1].decode("utf-8")), ("attachment", ""), ("scope", "public")]
        except (KeyError, ValueError, UnicodeError):
            self.send_error(400)
            return
        if not valid:
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
