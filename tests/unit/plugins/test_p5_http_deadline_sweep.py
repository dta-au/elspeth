"""Every audited HTTP caller is bounded by one absolute per-request deadline.

httpx timeouts bound each connect, read or write separately, so an upstream
that trickles bytes just under the per-read timeout keeps a request alive
without limit. The shared client binds one absolute deadline, derived from
the configured timeout, around every request (redirect hops included), so the
configured ``timeout`` is the request's total wall time. Each caller family
is driven against a slow-drip local server and must be cut off at that total.
"""

from __future__ import annotations

import importlib.util
import inspect
import ipaddress
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest

from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients import deadline as deadline_module
from elspeth.plugins.infrastructure.clients.dataverse import DataverseClient, DataverseClientError
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.clients.test_http import _CallRecorder, _ExecutionRepositoryFake

# The configured total, the drip interval (well under the per-read timeout,
# so httpx's per-phase timeouts never fire) and how long an unbounded drip
# would run.
TOTAL_SECONDS = 1.0
DRIP_INTERVAL_SECONDS = 0.2
DRIP_BYTES = 20  # 4 seconds when nothing cuts it off
SLACK_SECONDS = 0.6

_LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)


@pytest.fixture(autouse=True)
def _fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-only-http-deadline-key")


def _drip_body(conn: socket.socket) -> None:
    conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {DRIP_BYTES}\r\n\r\n".encode())
    for _ in range(DRIP_BYTES):
        time.sleep(DRIP_INTERVAL_SECONDS)
        conn.sendall(b"x")


def _drip_headers(conn: socket.socket) -> None:
    head = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"y" * DRIP_BYTES + b"\r\nContent-Length: 2\r\n\r\nok"
    for index in range(len(head)):
        if index >= len(b"HTTP/1.1 200 OK\r\nX-Slow: "):
            time.sleep(DRIP_INTERVAL_SECONDS)
        conn.sendall(head[index : index + 1])


@contextmanager
def _server(handlers: list[Callable[[socket.socket], None]]) -> Iterator[int]:
    """Serve one connection per handler, in order, on a loopback port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(len(handlers))
    srv.settimeout(10)
    port = srv.getsockname()[1]

    def serve() -> None:
        for handler in handlers:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(65536)
                    handler(conn)
                except OSError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        srv.close()


def _timed(call: Callable[[], Any]) -> tuple[float, BaseException | None]:
    started = time.monotonic()
    error: BaseException | None = None
    try:
        call()
    except Exception as exc:
        error = exc
    return time.monotonic() - started, error


def _assert_cut_off(elapsed: float, error: BaseException | None, expected: type[BaseException]) -> None:
    assert error is not None, f"slow-drip request completed after {elapsed:.2f}s with a {TOTAL_SECONDS}s timeout"
    assert isinstance(error, expected), f"expected {expected.__name__}, got {type(error).__name__}: {error}"
    assert elapsed < TOTAL_SECONDS + SLACK_SECONDS, f"request ran {elapsed:.2f}s against a {TOTAL_SECONDS}s total deadline"


def _client(*, cap: int | None = None) -> AuditedHTTPClient:
    return AuditedHTTPClient(
        **mock_audit_authority(),
        execution=_ExecutionRepositoryFake(),
        state_id="state",
        run_id="run",
        telemetry_emit=_CallRecorder(),
        timeout=TOTAL_SECONDS,
        max_response_body_bytes=cap,
    )


def _pinned(port: int, path: str = "/") -> SSRFSafeRequest:
    return SSRFSafeRequest(
        original_url=f"http://127.0.0.1:{port}{path}",
        resolved_ip="127.0.0.1",
        host_header=f"127.0.0.1:{port}",
        port=port,
        path=path,
        scheme="http",
        bare_hostname="127.0.0.1",
    )


def test_shared_deadline_mechanism_is_public() -> None:
    assert importlib.util.find_spec("elspeth.plugins.infrastructure.clients.deadline") is not None
    names = vars(deadline_module)
    assert "http_deadline_scope" in names, "no shared scope binds an absolute deadline to every HTTP request"
    assert "deadline_bound_client" in names, "no shared factory builds an httpx client that honours the scoped deadline"


@pytest.mark.parametrize("drip", [_drip_body, _drip_headers], ids=["body", "headers"])
@pytest.mark.parametrize("cap", [None, 1024], ids=["uncapped", "capped"])
def test_get_and_post_are_cut_off_at_the_configured_total(drip: Callable[[socket.socket], None], cap: int | None) -> None:
    """OpenRouter, Gateway, Azure safety and AI Search call get()/post()."""
    for method in ("GET", "POST"):
        with _server([drip]) as port:
            client = _client(cap=cap)
            url = f"http://127.0.0.1:{port}/"
            call = (lambda: client.get(url)) if method == "GET" else (lambda: client.post(url, json={"q": 1}))
            elapsed, error = _timed(call)
            client.close()
        _assert_cut_off(elapsed, error, httpx.TimeoutException)


@pytest.mark.parametrize("drip", [_drip_body, _drip_headers], ids=["body", "headers"])
def test_ssrf_safe_request_is_cut_off_at_the_configured_total(drip: Callable[[socket.socket], None]) -> None:
    """web_scrape, blob_fetch and Azure DI/CU use the IP-pinned path."""
    with _server([drip]) as port:
        client = _client(cap=1024)
        elapsed, error = _timed(lambda: client.request_ssrf_safe("GET", _pinned(port), allowed_ranges=_LOOPBACK))
        client.close()
    _assert_cut_off(elapsed, error, httpx.TimeoutException)


def test_redirect_hops_share_one_budget() -> None:
    """A slow first hop leaves only the remainder of the budget for the next."""

    with _server([_drip_body]) as target_port:

        def slow_redirect(conn: socket.socket) -> None:
            time.sleep(TOTAL_SECONDS * 0.7)
            conn.sendall(f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{target_port}/next\r\nContent-Length: 0\r\n\r\n".encode())

        with _server([slow_redirect]) as port:
            client = _client(cap=1024)
            elapsed, error = _timed(lambda: client.request_ssrf_safe("GET", _pinned(port), follow_redirects=True, allowed_ranges=_LOOPBACK))
            client.close()
    _assert_cut_off(elapsed, error, httpx.TimeoutException)


class _FakeToken:
    token = "fake-token"


class _FakeCredential:
    def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken()


def test_dataverse_requests_are_cut_off_at_the_configured_total() -> None:
    params = inspect.signature(DataverseClient.__init__).parameters
    assert "request_timeout_seconds" in params, "Dataverse has a hard-coded per-phase timeout and no total deadline"
    with _server([_drip_body]) as port:
        client = DataverseClient(
            environment_url="https://org.crm.dynamics.com",
            credential=cast(Any, _FakeCredential()),
            request_timeout_seconds=TOTAL_SECONDS,
        )
        elapsed, error = _timed(lambda: client._execute_request("GET", f"http://127.0.0.1:{port}/", ssrf_safe=_pinned(port)))
        client.close()
    _assert_cut_off(elapsed, error, DataverseClientError)
    assert isinstance(error, DataverseClientError)
    assert isinstance(error.__cause__, httpx.TimeoutException)


def _timeout_fields() -> list[tuple[str, str | None]]:
    from elspeth.plugins.sources.llm.config import GatewayLLMSourceConfig, OpenRouterLLMSourceConfig
    from elspeth.plugins.transforms.azure.ai_search import AzureAISearchConfig
    from elspeth.plugins.transforms.azure.analyze_lro import AzureAnalyzeConfig
    from elspeth.plugins.transforms.blob_fetch import BlobFetchHTTPConfig
    from elspeth.plugins.transforms.llm.providers.gateway import GatewayConfig
    from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterConfig
    from elspeth.plugins.transforms.web_scrape import WebScrapeHTTPConfig

    return [
        ("web_scrape.http.timeout", WebScrapeHTTPConfig.model_fields["timeout"].description),
        ("blob_fetch.http.timeout", BlobFetchHTTPConfig.model_fields["timeout"].description),
        ("openrouter.timeout_seconds", OpenRouterConfig.model_fields["timeout_seconds"].description),
        ("gateway.timeout_seconds", GatewayConfig.model_fields["timeout_seconds"].description),
        ("llm_source.openrouter.timeout_seconds", OpenRouterLLMSourceConfig.model_fields["timeout_seconds"].description),
        ("llm_source.gateway.timeout_seconds", GatewayLLMSourceConfig.model_fields["timeout_seconds"].description),
        ("azure_ai_search.request_timeout", AzureAISearchConfig.model_fields["request_timeout"].description),
        ("azure_analyze.request_timeout_seconds", AzureAnalyzeConfig.model_fields["request_timeout_seconds"].description),
    ]


def test_timeout_descriptions_state_the_total_bound() -> None:
    vague = [(name, description) for name, description in _timeout_fields() if description is None or "total" not in description.lower()]
    assert vague == [], f"timeout descriptions do not state the total per-request bound: {vague}"
