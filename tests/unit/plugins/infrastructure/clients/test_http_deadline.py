"""Absolute HTTP budgets cover socket operations and streamed responses."""

from __future__ import annotations

import socket
import ssl
import time
from collections.abc import Iterator
from socket import socket as Socket

import httpcore
import httpx
import pytest
import respx

from elspeth.plugins.infrastructure.clients import deadline as deadline_module
from elspeth.plugins.infrastructure.clients.deadline import (
    DeadlineHTTPTransport,
    DeadlineNetworkBackend,
    DeadlineNetworkStream,
    DeadlineResponseStream,
    HTTPDeadline,
    HTTPDeadlineExceeded,
    HTTPDeadlineTransportError,
)


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class SocketOperations:
    clock: Clock
    timeouts: list[float | None]
    writes: list[bytes]
    step: float
    sent: int
    received: list[bytes]
    closed: bool
    connected: tuple[str, int] | None
    handshake_timeout: float | None
    failure: OSError | None

    def initialize(self, clock: Clock) -> None:
        self.clock = clock
        self.timeouts = []
        self.writes = []
        self.step = 0.0
        self.sent = 2
        self.received = [b"x"]
        self.closed = False
        self.connected = None
        self.handshake_timeout = None
        self.failure = None

    def settimeout(self, value: float | None) -> None:
        self.timeouts.append(value)

    def send(self, data: bytes | memoryview, flags: int = 0) -> int:
        self.writes.append(bytes(data))
        self.clock.now += self.step
        if self.failure is not None:
            raise self.failure
        return min(self.sent, len(data))

    def recv(self, size: int, flags: int = 0) -> bytes:
        self.clock.now += self.step
        if self.failure is not None:
            raise self.failure
        return self.received.pop(0)

    def connect(self, address: tuple[str, int]) -> None:
        self.connected = address
        self.clock.now += self.step
        if self.failure is not None:
            raise self.failure

    def do_handshake(self, block: bool = False) -> None:
        self.handshake_timeout = self.timeouts[-1]
        self.clock.now += self.step
        if self.failure is not None:
            raise self.failure


class PlainSocket(SocketOperations, socket.socket):
    def __init__(self, clock: Clock) -> None:
        Socket.__init__(self)
        self.initialize(clock)

    def close(self) -> None:
        self.closed = True
        Socket.close(self)


class TLSSocket(SocketOperations, ssl.SSLSocket):
    def __init__(self, clock: Clock) -> None:
        Socket.__init__(self)
        self.initialize(clock)

    def close(self) -> None:
        self.closed = True
        Socket.close(self)


class TLSContext(ssl.SSLContext):
    def __new__(cls, tls_socket: TLSSocket) -> TLSContext:
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, tls_socket: TLSSocket) -> None:
        self.tls_socket = tls_socket
        self.hostname: str | None = None
        self.automatic_handshake: bool | None = None

    def wrap_socket(
        self,
        sock: socket.socket,
        server_side: bool = False,
        do_handshake_on_connect: bool = True,
        suppress_ragged_eofs: bool = True,
        server_hostname: str | None = None,
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLSocket:
        self.hostname = server_hostname
        self.automatic_handshake = do_handshake_on_connect
        sock.close()
        return self.tls_socket


def assert_safe(exc: BaseException, code: str) -> None:
    assert str(exc) == code
    assert exc.__context__ is None
    assert exc.__cause__ is None


def test_deadline_budget_is_strict_and_phase_timeout_can_only_shorten_it() -> None:
    clock = Clock()
    deadline = HTTPDeadline(12.0, clock)
    assert deadline.remaining() == 2.0
    assert deadline.remaining(0.5) == 0.5
    assert deadline.remaining(30.0) == 2.0
    clock.now = 12.0
    with pytest.raises(HTTPDeadlineExceeded) as caught:
        deadline.remaining()
    assert_safe(caught.value, "deadline_exceeded")


@pytest.mark.parametrize("expires_at", [float("nan"), float("inf"), 0.0, -1.0])
def test_deadline_rejects_invalid_expiration(expires_at: float) -> None:
    with pytest.raises(ValueError):
        HTTPDeadline(expires_at)


@pytest.mark.parametrize("socket_class", [PlainSocket, TLSSocket])
@pytest.mark.parametrize("step,expires_at,success", [(0.2, 11.0, True), (0.4, 11.0, False)])
def test_short_writes_use_shrinking_total_budget(socket_class, step: float, expires_at: float, success: bool) -> None:
    clock = Clock()
    sock = socket_class(clock)
    sock.step = step
    stream = DeadlineNetworkStream(sock, HTTPDeadline(expires_at, clock))
    try:
        if success:
            stream.write(b"abcdef", timeout=8.0)
        else:
            with pytest.raises(HTTPDeadlineExceeded):
                stream.write(b"abcdef", timeout=8.0)
        assert sock.writes == [b"abcdef", b"cdef", b"ef"]
        assert sock.timeouts == pytest.approx([1.0, 1.0 - step, 1.0 - 2 * step])
    finally:
        stream.close()


def test_zero_write_progress_fails() -> None:
    clock = Clock()
    sock = PlainSocket(clock)
    sock.sent = 0
    stream = DeadlineNetworkStream(sock, HTTPDeadline(11.0, clock))
    try:
        with pytest.raises(HTTPDeadlineTransportError) as caught:
            stream.write(b"secret")
        assert_safe(caught.value, "transport_failed")
        assert len(sock.writes) == 1
    finally:
        stream.close()


@pytest.mark.parametrize("operation", ["read", "write"])
@pytest.mark.parametrize(
    "failure,code", [(OSError("fake-private-secret"), "transport_failed"), (TimeoutError("fake-private-secret"), "deadline_exceeded")]
)
def test_socket_errors_have_no_external_chain(operation: str, failure: OSError, code: str) -> None:
    clock = Clock()
    sock = PlainSocket(clock)
    sock.failure = failure
    stream = DeadlineNetworkStream(sock, HTTPDeadline(11.0, clock))
    try:
        with pytest.raises(httpx.HTTPError) as caught:
            if operation == "read":
                stream.read(10)
            else:
                stream.write(b"x")
        assert_safe(caught.value, code)
    finally:
        stream.close()


def test_read_rejects_expiry_even_on_eof() -> None:
    clock = Clock()
    sock = PlainSocket(clock)
    sock.received = [b""]
    sock.step = 1.0
    stream = DeadlineNetworkStream(sock, HTTPDeadline(11.0, clock))
    try:
        with pytest.raises(HTTPDeadlineExceeded):
            stream.read(10)
    finally:
        stream.close()


@pytest.mark.parametrize("send_control", [False, True])
def test_blocking_read_uses_remaining_budget(send_control: bool) -> None:
    client, peer = socket.socketpair()
    stream = DeadlineNetworkStream(client, HTTPDeadline(time.monotonic() + 0.12))
    started = time.monotonic()
    try:
        if send_control:
            peer.send(b"x")
            assert stream.read(1, timeout=30.0) == b"x"
        else:
            with pytest.raises(HTTPDeadlineExceeded) as caught:
                stream.read(1, timeout=30.0)
            assert_safe(caught.value, "deadline_exceeded")
            assert 0.07 <= time.monotonic() - started < 1.5
    finally:
        stream.close()
        peer.close()


def test_connect_and_tls_share_one_budget_and_preserve_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    plain = PlainSocket(clock)
    tls = TLSSocket(clock)
    plain.step = 0.4
    tls.step = 0.3
    context = TLSContext(tls)
    monkeypatch.setattr(deadline_module.socket, "socket", lambda *args: plain)
    backend = DeadlineNetworkBackend(HTTPDeadline(11.0, clock))
    stream = backend.connect_tcp("192.0.2.1", 443, timeout=5.0)
    try:
        secured = stream.start_tls(context, "original.example", timeout=5.0)
        assert plain.connected == ("192.0.2.1", 443)
        assert plain.timeouts == pytest.approx([1.0, 0.6])
        assert tls.handshake_timeout == pytest.approx(0.6)
        assert context.hostname == "original.example"
        assert context.automatic_handshake is False
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert secured.get_extra_info("ssl_object") is tls
    finally:
        tls.close()
        plain.close()


def test_expired_connect_closes_socket_and_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    sock = PlainSocket(clock)
    sock.step = 1.0
    monkeypatch.setattr(deadline_module.socket, "socket", lambda *args: sock)
    backend = DeadlineNetworkBackend(HTTPDeadline(11.0, clock))
    with pytest.raises(HTTPDeadlineExceeded):
        backend.connect_tcp("192.0.2.1", 443)
    assert sock.closed
    assert sock.writes == []


def test_backend_refuses_dns_unix_and_local_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("DNS or socket creation occurred before literal admission")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    backend = DeadlineNetworkBackend(HTTPDeadline(11.0, Clock()))
    with pytest.raises(HTTPDeadlineTransportError) as caught:
        backend.connect_tcp("private-secret.example", 443)
    assert_safe(caught.value, "transport_failed")
    with pytest.raises(HTTPDeadlineTransportError):
        backend.connect_tcp("192.0.2.1", 443, local_address="127.0.0.1")
    with pytest.raises(HTTPDeadlineTransportError):
        backend.connect_unix_socket("/private-secret")
    with pytest.raises(HTTPDeadlineTransportError):
        backend.sleep(10.0)


def test_expiry_before_dispatch_never_calls_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(self, request):
        pytest.fail("expired request dispatched")

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", forbidden)
    clock = Clock()
    transport = DeadlineHTTPTransport(HTTPDeadline(11.0, clock))
    clock.now = 11.0
    try:
        with pytest.raises(HTTPDeadlineExceeded):
            transport.handle_request(httpx.Request("GET", "https://192.0.2.1/"))
    finally:
        transport.close()


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpcore.RemoteProtocolError("fake-private-secret"), "transport_failed"),
        (httpcore.ReadTimeout("fake-private-secret"), "deadline_exceeded"),
    ],
)
def test_core_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch, failure: Exception, code: str) -> None:
    def fail(self, request):
        raise failure

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", fail)
    transport = DeadlineHTTPTransport(HTTPDeadline(11.0, Clock()))
    try:
        with pytest.raises(httpx.HTTPError) as caught:
            transport.handle_request(httpx.Request("GET", "https://192.0.2.1/"))
        assert_safe(caught.value, code)
    finally:
        transport.close()


def test_unexpected_core_bug_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self, request):
        raise AssertionError("owned invariant failed")

    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", fail)
    transport = DeadlineHTTPTransport(HTTPDeadline(11.0, Clock()))
    try:
        with pytest.raises(AssertionError, match="owned invariant failed"):
            transport.handle_request(httpx.Request("GET", "https://192.0.2.1/"))
    finally:
        transport.close()


@pytest.mark.parametrize("step,success", [(0.2, True), (0.4, False)])
def test_slow_trickle_response_obeys_total_deadline(step: float, success: bool) -> None:
    clock = Clock()
    closed: list[bool] = []

    class Body:
        def __iter__(self) -> Iterator[bytes]:
            for chunk in [b"a", b"b", b"c"]:
                clock.now += step
                yield chunk

        def close(self) -> None:
            closed.append(True)

    stream = DeadlineResponseStream(httpcore.Response(200, content=Body()), HTTPDeadline(11.0, clock))
    if success:
        assert list(stream) == [b"a", b"b", b"c"]
    else:
        iterator = iter(stream)
        assert next(iterator) == b"a"
        assert next(iterator) == b"b"
        with pytest.raises(HTTPDeadlineExceeded):
            next(iterator)
    assert closed == [True]


def test_transport_works_with_core_respx_interception() -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.post("https://192.0.2.1/callback").respond(200, content=b"exact decoded bytes")
        with httpx.Client(transport=DeadlineHTTPTransport(HTTPDeadline(time.monotonic() + 5)), trust_env=False) as client:
            response = client.post("https://192.0.2.1/callback", content=b"request")
        assert response.content == b"exact decoded bytes"
        assert route.call_count == 1


@pytest.mark.parametrize("step,success", [(0.2, True), (0.4, False)])
def test_core_buffering_cannot_hide_slow_socket_reads(monkeypatch: pytest.MonkeyPatch, step: float, success: bool) -> None:
    clock = Clock()

    class TrickleSocket(PlainSocket):
        def recv(self, size: int, flags: int = 0) -> bytes:
            self.clock.now += step
            return self.received.pop(0)

    sock = TrickleSocket(clock)
    sock.sent = 10000
    sock.received = [b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n", b"a", b"b", b"c"]
    monkeypatch.setattr(deadline_module.socket, "socket", lambda *args: sock)
    chunks: list[bytes] = []
    try:
        with httpx.Client(transport=DeadlineHTTPTransport(HTTPDeadline(11.0, clock)), trust_env=False) as client:
            if success:
                with client.stream("GET", "http://192.0.2.1/") as response:
                    chunks.extend(response.iter_raw())
                assert b"".join(chunks) == b"abc"
            else:
                with pytest.raises(HTTPDeadlineExceeded), client.stream("GET", "http://192.0.2.1/") as response:
                    for chunk in response.iter_raw():
                        chunks.append(chunk)
                assert b"".join(chunks) == b"a"
        assert sock.closed
    finally:
        sock.close()


@pytest.mark.parametrize("socket_class", [PlainSocket, TLSSocket])
def test_expiry_refuses_next_short_write(socket_class) -> None:
    clock = Clock()
    sock = socket_class(clock)
    sock.step = 0.6
    stream = DeadlineNetworkStream(sock, HTTPDeadline(11.0, clock))
    try:
        with pytest.raises(HTTPDeadlineExceeded):
            stream.write(b"abcdef", timeout=30.0)
        assert sock.writes == [b"abcdef", b"cdef"]
        with pytest.raises(HTTPDeadlineExceeded):
            stream.write(b"next")
        assert sock.writes == [b"abcdef", b"cdef"]
    finally:
        stream.close()


@pytest.mark.parametrize("failure", [None, OSError("fake-handshake-secret")])
def test_failed_or_expired_tls_handshake_closes_new_owner(failure: OSError | None) -> None:
    clock = Clock()
    plain = PlainSocket(clock)
    tls = TLSSocket(clock)
    tls.failure = failure
    tls.step = 1.0
    context = TLSContext(tls)
    stream = DeadlineNetworkStream(plain, HTTPDeadline(11.0, clock))
    expected = HTTPDeadlineExceeded if failure is None else HTTPDeadlineTransportError
    try:
        with pytest.raises(expected) as caught:
            stream.start_tls(context, "original.example")
        assert_safe(caught.value, "deadline_exceeded" if failure is None else "transport_failed")
        assert tls.closed
        assert plain.closed
    finally:
        tls.close()
        plain.close()
