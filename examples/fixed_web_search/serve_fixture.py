"""Small deterministic government-style search fixture for the example pipeline."""

from __future__ import annotations

import hashlib
import html
import json
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlsplit

_ENTITY_MATCHES = {
    "Riverdale Council": (("Riverdale Council", "RC-001"),),
    "North Coast Trading": (
        ("North Coast Trading Pty Ltd", "NCT-101"),
        ("North Coast Trading Services", "NCT-102"),
    ),
}
_ENTITY_DETAILS = {
    "RC-001": ("Riverdale Council", "active"),
    "NCT-101": ("North Coast Trading Pty Ltd", "active"),
    "NCT-102": ("North Coast Trading Services", "inactive"),
}


class SearchHandler(BaseHTTPRequestHandler):
    access_log: Path
    wire_log: Path

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

    @staticmethod
    def _entity_search_html(query: str) -> bytes:
        entries = "".join(
            f'<article class="candidate"><a href="/entity/{entity_id}">{html.escape(name)}</a></article>'
            for name, entity_id in _ENTITY_MATCHES.get(query, ())
        )
        return f'<main class="entity-results"><h1>{html.escape(query)}</h1>{entries}</main>'.encode()

    @classmethod
    def _entity_detail_html(cls, entity_id: str) -> bytes:
        name, status = _ENTITY_DETAILS[entity_id]
        return (
            '<main class="entity-detail">'
            f'<h1 class="legal-name">{html.escape(name)}</h1>'
            f'<p class="registry-id">{html.escape(entity_id)}</p>'
            f'<p class="record-status">{html.escape(status)}</p>'
            "</main>"
        ).encode()

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
        elif parsed.path == "/entity-search":
            query = parse_qs(parsed.query).get("name", [""])[0]
            body = self._entity_search_html(query)
            self._record_query(f"search:{query}")
        elif parsed.path.startswith("/entity/") and parsed.path.removeprefix("/entity/") in _ENTITY_DETAILS:
            entity_id = parsed.path.removeprefix("/entity/")
            body = self._entity_detail_html(entity_id)
            self._record_query(f"detail:{entity_id}")
        else:
            self.send_error(404)
            return
        # Keep response headers stable so HTTP verify compares like with like.
        self._send_html(body)

    def do_POST(self) -> None:
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
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
        if path == "/multipart-search":
            fixture_ids = parse_qs(parsed_url.query).get("fixture_id", [])
            if len(fixture_ids) != 1 or not fixture_ids[0].isdigit():
                self.send_error(400)
                return
            with self.wire_log.open("a", encoding="ascii") as log:
                log.write(json.dumps({"fixture_id": fixture_ids[0], "sha256": hashlib.sha256(wire_body).hexdigest()}) + "\n")
        self._record_query(query)
        self._send_html(self._directory_html(query))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--access-log", type=Path, required=True)
    parser.add_argument("--wire-log", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8213)
    args = parser.parse_args()
    SearchHandler.access_log = args.access_log
    SearchHandler.wire_log = args.wire_log
    HTTPServer(("127.0.0.1", args.port), SearchHandler).serve_forever()
