"""The battery's HTTP seam: selecting and driving the direct-to-origin transport.

`elspeth-ad5628ecda` — the public hostname sits behind an edge whose origin read
timeout cuts responses at a measured ~125s, well inside the origin's own 600s
composer budget, so slow corpus cases cannot be measured through it at all. The
driver can address the uvicorn unix socket instead. These tests drive a real
socket rather than asserting the client was constructed: a client that is built
correctly and cannot speak is exactly the failure worth catching.
"""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import drive_battery as db
import pytest


class _UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(path, handler)
        self.seen: list[dict[str, object]] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        pass

    def address_string(self) -> str:
        return "unix"  # client_address is empty for AF_UNIX; the default indexes it

    def _record_and_reply(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        assert isinstance(self.server, _UnixHTTPServer)  # a class we define; nominal, not structural
        self.server.seen.append(
            {
                "method": method,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw) if raw else None,
            }
        )
        if self.path.startswith("/redirect"):
            self.send_response(307)
            self.send_header("Location", "/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps({"path": self.path, "ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._record_and_reply("GET")

    def do_POST(self) -> None:
        self._record_and_reply("POST")


@pytest.fixture
def origin(tmp_path: Path):
    """A real HTTP origin on a real unix socket, recording what it receives."""
    path = str(tmp_path / "uvicorn.sock")
    server = _UnixHTTPServer(path, _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"unix://{path}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_unix_base_reaches_the_socket_and_carries_the_bearer_token(origin) -> None:
    server, base = origin
    client = db.build_client(base)
    client.set_token("tok-123")

    response = client.request("POST", "/api/sessions/s1/messages", json={"content": "compose"}, timeout=10)

    assert response.status_code == 200
    assert response.body == {"path": "/api/sessions/s1/messages", "ok": True}
    assert server.seen == [
        {
            "method": "POST",
            "path": "/api/sessions/s1/messages",
            "authorization": "Bearer tok-123",
            "body": {"content": "compose"},
        }
    ]


def test_the_unix_client_follows_redirects_as_the_requests_client_does(origin) -> None:
    """requests follows redirects by default and httpx does not.

    The two implementations of one seam must not disagree about it; without
    ``follow_redirects=True`` this returns the bare 307.
    """
    server, base = origin
    client = db.build_client(base)

    response = client.request("GET", "/redirect", timeout=10)

    assert response.status_code == 200
    assert response.body == {"path": "/landed", "ok": True}
    assert [entry["path"] for entry in server.seen] == ["/redirect", "/landed"]


def test_query_parameters_reach_the_origin(origin) -> None:
    server, base = origin
    client = db.build_client(base)

    client.request("GET", "/api/sessions", params={"limit": 200}, timeout=10)

    assert server.seen[0]["path"] == "/api/sessions?limit=200"


def test_build_client_selects_the_transport_the_base_url_names(origin) -> None:
    _, base = origin

    assert isinstance(db.build_client(base), db.UnixSocketClient)
    assert isinstance(db.build_client("https://elspeth.foundryside.dev"), db.RequestsClient)


def test_a_unix_base_that_is_not_a_socket_is_refused_at_construction(tmp_path: Path) -> None:
    """Fail before the round starts, not on the first request of a paid run."""
    missing = tmp_path / "absent.sock"
    with pytest.raises(ValueError, match="is not a unix socket"):
        db.build_client(f"unix://{missing}")

    regular_file = tmp_path / "not-a-socket"
    regular_file.write_text("")
    with pytest.raises(ValueError, match="is not a unix socket"):
        db.build_client(f"unix://{regular_file}")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://elspeth.foundryside.dev", "not a unix:// base URL"),
        ("unix://run/elspeth/uvicorn.sock", "three slashes"),
        ("unix://", "socket path must be absolute"),
    ],
)
def test_unix_socket_path_rejects_a_malformed_base(base: str, expected: str) -> None:
    with pytest.raises(ValueError, match=expected):
        db.unix_socket_path(base)


def test_unix_socket_path_extracts_the_absolute_path() -> None:
    assert db.unix_socket_path("unix:///run/elspeth/uvicorn.sock") == "/run/elspeth/uvicorn.sock"
