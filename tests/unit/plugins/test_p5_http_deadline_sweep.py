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
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpcore
import httpx
import pytest

from elspeth.contracts import CallStatus
from elspeth.core.rate_limit import RateLimiter
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
DRIP_BYTES = 30  # 6 seconds when nothing cuts it off
SLACK_SECONDS = 1.5

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


def test_httpx_proxy_environment_reader_is_available() -> None:
    """The pinned httpx private seam must fail loudly if an upgrade removes it."""
    assert callable(deadline_module.get_environment_proxies)


def test_short_socket_sends_recheck_the_same_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer making one byte of progress per send cannot reset the budget."""
    clock = [0.0]

    class FakeSocket:
        timeouts: list[float]

        def __init__(self) -> None:
            self.timeouts = []

        def settimeout(self, timeout: float) -> None:
            self.timeouts.append(timeout)

        def send(self, _buffer: memoryview) -> int:
            clock[0] += 0.1
            return 1

    class FakeStream:
        def __init__(self, sock: FakeSocket) -> None:
            self.sock = sock

        def get_extra_info(self, info: str) -> object:
            return self.sock if info == "socket" else None

    sock = FakeSocket()
    monkeypatch.setattr(deadline_module, "socket", SimpleNamespace(socket=FakeSocket))
    deadline = deadline_module.HTTPDeadline(expires_at=0.35, clock=lambda: clock[0])
    token = deadline_module._ACTIVE_HTTP_DEADLINE.set(deadline)
    try:
        with pytest.raises(httpcore.WriteTimeout, match="HTTP request exceeded its total deadline"):
            deadline_module._ScopedDeadlineStream(cast(Any, FakeStream(sock))).write(b"abcdefghij")
    finally:
        deadline_module._ACTIVE_HTTP_DEADLINE.reset(token)
    assert len(sock.timeouts) == 4
    assert sock.timeouts == pytest.approx([0.35, 0.25, 0.15, 0.05])


def test_nested_tls_handshake_checks_budget_between_reads() -> None:
    """An HTTPS proxy handshake cannot renew its timeout on each TLS read."""
    clock = [0.0]

    class FakeOuter:
        closed = False
        reads = 0

        def read(self, _size: int) -> bytes:
            self.reads += 1
            clock[0] += 0.2
            return b"x"

        def write(self, _data: bytes) -> None:
            return

        def close(self) -> None:
            self.closed = True

    class FakeTLS:
        def do_handshake(self) -> None:
            raise ssl.SSLWantReadError()

    class FakeContext:
        def wrap_bio(self, _incoming: ssl.MemoryBIO, _outgoing: ssl.MemoryBIO, *, server_hostname: str | None) -> FakeTLS:
            assert server_hostname == "example.com"
            return FakeTLS()

    outer = FakeOuter()
    deadline = deadline_module.HTTPDeadline(expires_at=0.35, clock=lambda: clock[0])
    token = deadline_module._ACTIVE_HTTP_DEADLINE.set(deadline)
    try:
        with pytest.raises(httpcore.ConnectTimeout, match="HTTP request exceeded its total deadline"):
            deadline_module._NestedDeadlineTLSStream(cast(Any, outer), cast(Any, FakeContext()), "example.com")
    finally:
        deadline_module._ACTIVE_HTTP_DEADLINE.reset(token)
    assert outer.reads == 2
    assert outer.closed


def test_dns_overrun_never_starts_a_socket_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocking resolver can overrun once, but cannot then start a connect."""
    clock = [0.0]
    backend = deadline_module._ScopedDeadlineBackend()

    class FakeBackend:
        def connect_tcp(self, *_args: object, **_kwargs: object) -> None:
            pytest.fail("socket connect started after DNS consumed the deadline")

    def resolve(
        _host: str, _port: int, _family: int = 0, type: int = socket.SOCK_STREAM, _protocol: int = 0
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == socket.SOCK_STREAM
        clock[0] = 2.0
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(backend, "_inner", FakeBackend())
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    deadline = deadline_module.HTTPDeadline(expires_at=1.0, clock=lambda: clock[0])
    token = deadline_module._ACTIVE_HTTP_DEADLINE.set(deadline)
    try:
        with pytest.raises(httpcore.ConnectTimeout, match="HTTP request exceeded its total deadline"):
            backend.connect_tcp("example.com", 443)
    finally:
        deadline_module._ACTIVE_HTTP_DEADLINE.reset(token)


@pytest.mark.timing_sensitive
def test_slow_dns_resolver_is_bounded_by_remaining_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = deadline_module._ScopedDeadlineBackend()
    release = threading.Event()
    started = threading.Event()

    def resolve(
        _host: str, _port: int, _family: int = 0, type: int = socket.SOCK_STREAM, _protocol: int = 0
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert type == socket.SOCK_STREAM
        started.set()
        release.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    connects: list[object] = []

    class FakeSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def settimeout(self, _timeout: float | None) -> None:
            pass

        def connect(self, address: object) -> None:
            connects.append(address)
            raise OSError("simulated connect refusal")

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    began = time.monotonic()
    try:
        with deadline_module.http_deadline_scope(0.05), suppress(httpcore.ConnectError, httpcore.ConnectTimeout):
            backend.connect_tcp("example.com", 443)
    finally:
        release.set()
    assert started.is_set()
    assert connects == [], "expired DNS lookup started a socket connect"
    assert time.monotonic() - began < 0.5


@pytest.mark.timing_sensitive
def test_dns_capacity_refuses_new_worker_after_timed_out_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(deadline_module, "_HTTP_DNS_CAPACITY", threading.BoundedSemaphore(1))

    def resolve(*_args: object, **_kwargs: object) -> list[object]:
        started.set()
        # Hold the only slot until the test releases it; a short cap let a slow host free it
        # before the second call and turned "capacity exhausted" into a DNS deadline.
        release.wait(30)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    backend = deadline_module._ScopedDeadlineBackend()
    try:
        with deadline_module.http_deadline_scope(0.02), pytest.raises(httpcore.ConnectTimeout, match="DNS lookup"):
            backend.connect_tcp("example.com", 443)
        assert started.is_set()
        with deadline_module.http_deadline_scope(0.1), pytest.raises(httpcore.ConnectTimeout, match="capacity exhausted"):
            backend.connect_tcp("example.com", 443)
    finally:
        release.set()


@pytest.mark.parametrize("outcome", ["success", "error", "base_exception"])
@pytest.mark.timing_sensitive
def test_dns_capacity_recovers_after_resolver_finishes(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    release = threading.Event()
    completed = threading.Event()
    calls = [0]
    monkeypatch.setattr(deadline_module, "_HTTP_DNS_CAPACITY", threading.BoundedSemaphore(1))

    def resolve(*_args: object, **_kwargs: object) -> list[object]:
        calls[0] += 1
        if calls[0] == 1:
            release.wait(1)
            completed.set()
            if outcome == "error":
                raise OSError("DNS failed")
            if outcome == "base_exception":
                raise SystemExit("resolver stopped")
        return []

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    backend = deadline_module._ScopedDeadlineBackend()
    with deadline_module.http_deadline_scope(0.02), pytest.raises(httpcore.ConnectTimeout, match="DNS lookup"):
        backend.connect_tcp("example.com", 443)
    release.set()
    assert completed.wait(1)
    # The worker signals completion just before its finally releases the
    # permit. Wait for that release without consuming a permit ourselves.
    for _ in range(100):
        if deadline_module._HTTP_DNS_CAPACITY.acquire(blocking=False):
            deadline_module._HTTP_DNS_CAPACITY.release()
            break
        time.sleep(0.001)
    with deadline_module.http_deadline_scope(0.2), pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("example.com", 443)
    assert calls[0] == 2


def test_dns_worker_base_exception_is_rethrown_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = SystemExit("resolver stopped")

    def resolve(*_args: object, **_kwargs: object) -> list[object]:
        raise failure

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with deadline_module.http_deadline_scope(1), pytest.raises(SystemExit) as caught:
        deadline_module._ScopedDeadlineBackend().connect_tcp("example.com", 443)
    assert caught.value is failure


def test_initial_limiter_timeout_is_audited_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    execution = _ExecutionRepositoryFake()
    client = AuditedHTTPClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state",
        run_id="run",
        telemetry_emit=_CallRecorder(),
        timeout=1.0,
    )
    waits: list[float | None] = []

    def refuse(*, timeout: float | None = None) -> None:
        waits.append(timeout)
        raise TimeoutError("no permit")

    monkeypatch.setattr(client, "_acquire_rate_limit", refuse)
    with pytest.raises(Exception) as caught:
        client.get("https://example.com")
    assert isinstance(caught.value, httpx.ConnectTimeout), type(caught.value).__name__
    assert "no request was sent" in str(caught.value)
    assert waits == [1.0]
    assert execution.record_call.call_count == 1
    assert execution.record_call.call_args is not None
    recorded = execution.record_call.call_args.kwargs
    assert recorded["status"].value == "error"
    assert recorded["error"].type == "HTTPLimiterRefusal"
    assert recorded["error"].to_dict()["wait_budget_seconds"] == 1.0
    assert recorded["error"].to_dict()["request_sent"] is False


@pytest.mark.timing_sensitive
def test_sqlite_limiter_contention_refuses_before_http_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "http-limiter.db"
    sends: list[str] = []
    with RateLimiter("http_sqlite", requests_per_minute=60, persistence_path=str(db_path)) as limiter:
        client = AuditedHTTPClient(
            **mock_audit_authority(),
            execution=_ExecutionRepositoryFake(),
            state_id="state",
            run_id="run",
            telemetry_emit=_CallRecorder(),
            limiter=limiter,
            timeout=0.05,
        )

        def send(*_args: object, **_kwargs: object) -> None:
            sends.append("sent")

        monkeypatch.setattr(client, "_request_with_optional_body_cap", send)
        with sqlite3.connect(db_path) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            started = time.monotonic()
            with pytest.raises(Exception) as caught:
                client.get("https://example.com")
            assert isinstance(caught.value, httpx.ConnectTimeout), type(caught.value).__name__
            assert time.monotonic() - started < 0.15
        assert sends == []


def test_limiter_refusal_replay_reconstructs_structured_no_send_outcome() -> None:
    from elspeth.plugins.infrastructure.clients.http import HTTPLimiterRefusal, _recorded_transport_failure

    evidence = SimpleNamespace(
        status=CallStatus.ERROR,
        response_data=None,
        error_data={
            "type": "HTTPLimiterRefusal",
            "message": "Rate-limiter wait exceeded 0.5s; no request was sent",
            "refusal_category": "rate_limiter",
            "wait_budget_seconds": 0.5,
            "request_sent": False,
        },
    )
    restored = _recorded_transport_failure(evidence, request=httpx.Request("GET", "https://example.com"))
    assert restored is not None
    error, payload = restored
    assert isinstance(error, HTTPLimiterRefusal)
    assert error.wait_budget_seconds == 0.5
    assert error.request_sent is False
    assert payload.to_dict() == evidence.error_data


def test_limiter_wait_does_not_consume_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [10.0]
    monkeypatch.setattr(deadline_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = _client()

    def acquire(*, timeout: float | None = None) -> None:
        assert timeout == TOTAL_SECONDS
        clock[0] += 0.8 * TOTAL_SECONDS

    def send(*_args: object, **_kwargs: object) -> httpx.Response:
        clock[0] += 0.8 * TOTAL_SECONDS
        assert deadline_module._scoped_timeout(None, httpcore.ReadTimeout) == pytest.approx(0.2 * TOTAL_SECONDS)
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"), content=b"ok")

    monkeypatch.setattr(client, "_acquire_rate_limit", acquire)
    monkeypatch.setattr(client, "_request_with_optional_body_cap", send)
    response = client.get("https://example.com")
    assert response.status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("drip", [_drip_body, _drip_headers], ids=["body", "headers"])
@pytest.mark.parametrize("cap", [None, 1024], ids=["uncapped", "capped"])
@pytest.mark.timing_sensitive
def test_get_and_post_are_cut_off_at_the_configured_total(method: str, drip: Callable[[socket.socket], None], cap: int | None) -> None:
    """OpenRouter, Gateway, Azure safety and AI Search call get()/post()."""
    with _server([drip]) as port:
        client = _client(cap=cap)
        url = f"http://127.0.0.1:{port}/"
        call = (lambda: client.get(url)) if method == "GET" else (lambda: client.post(url, json={"q": 1}))
        elapsed, error = _timed(call)
        client.close()
    _assert_cut_off(elapsed, error, httpx.TimeoutException)


@pytest.mark.parametrize("drip", [_drip_body, _drip_headers], ids=["body", "headers"])
@pytest.mark.timing_sensitive
def test_ssrf_safe_request_is_cut_off_at_the_configured_total(drip: Callable[[socket.socket], None]) -> None:
    """web_scrape, blob_fetch and Azure DI/CU use the IP-pinned path."""
    with _server([drip]) as port:
        client = _client(cap=1024)
        elapsed, error = _timed(lambda: client.request_ssrf_safe("GET", _pinned(port), allowed_ranges=_LOOPBACK))
        client.close()
    _assert_cut_off(elapsed, error, httpx.TimeoutException)


@pytest.mark.timing_sensitive
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


@pytest.mark.timing_sensitive
def test_outer_scope_bounds_a_longer_client_timeout() -> None:
    """A caller budget (web_scrape pagination) cuts off a request whose own timeout is longer."""
    with _server([_drip_body]) as port:
        client = AuditedHTTPClient(
            **mock_audit_authority(),
            execution=_ExecutionRepositoryFake(),
            state_id="state",
            run_id="run",
            telemetry_emit=_CallRecorder(),
            timeout=TOTAL_SECONDS * 30,
            max_response_body_bytes=1024,
        )

        def fetch() -> None:
            with deadline_module.http_deadline_scope(TOTAL_SECONDS):
                client.request_ssrf_safe("GET", _pinned(port), allowed_ranges=_LOOPBACK)

        elapsed, error = _timed(fetch)
        client.close()
    _assert_cut_off(elapsed, error, httpx.TimeoutException)


@pytest.mark.timing_sensitive
def test_deadline_bound_client_refuses_requests_outside_a_scope() -> None:
    from elspeth.contracts.errors import FrameworkBugError

    with (
        _server([_drip_body]) as port,
        deadline_module.deadline_bound_client(timeout=TOTAL_SECONDS) as raw,
        pytest.raises(FrameworkBugError, match="outside http_deadline_scope"),
    ):
        raw.get(f"http://127.0.0.1:{port}/")


class _FakeToken:
    token = "fake-token"


class _FakeCredential:
    def get_token(self, *scopes: str) -> _FakeToken:
        return _FakeToken()


@pytest.mark.timing_sensitive
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
