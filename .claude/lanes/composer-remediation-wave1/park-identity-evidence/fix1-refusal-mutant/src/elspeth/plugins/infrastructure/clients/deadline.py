"""Absolute budgets for a single pinned, audited HTTP dispatch.

The public HTTPCore backend seam lets each blocking syscall use the remaining
budget, including short TLS writes hidden beneath HTTP response buffering.
"""

from __future__ import annotations

import ipaddress
import math
import select
import socket
import ssl
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import ClassVar, Literal

import httpcore
import httpx


class HTTPDeadlineExceeded(httpx.HTTPError):
    """A phase timeout or the total dispatch budget expired."""

    code: ClassVar[Literal["deadline_exceeded"]] = "deadline_exceeded"

    def __init__(self) -> None:
        super().__init__(self.code)


class HTTPDeadlineTransportError(httpx.HTTPError):
    """Value-free failure at the network/HTTP protocol boundary."""

    code: ClassVar[Literal["transport_failed"]] = "transport_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True)
class HTTPDeadline:
    """One absolute expiration, shared by dispatch and body decoding."""

    expires_at: float
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at) or self.expires_at <= 0:
            raise ValueError("deadline expiration must be finite and positive")

    def remaining(self, phase_timeout: float | None = None) -> float:
        remaining = self.expires_at - self.clock()
        if not math.isfinite(remaining):
            raise ValueError("deadline clock must be finite")
        if remaining <= 0:
            raise HTTPDeadlineExceeded()
        if phase_timeout is None:
            return remaining
        if not math.isfinite(phase_timeout):
            raise ValueError("phase timeout must be finite")
        if phase_timeout <= 0:
            raise HTTPDeadlineExceeded()
        return min(remaining, phase_timeout)


def _safe_operation[T](operation: Callable[[], T]) -> T:
    """Discard external exceptions before constructing an owned failure.

    Raising after the except block also removes the external __context__,
    which `raise ... from None` alone would retain as readable object data.
    Unexpected framework and programming exceptions retain their identity.
    """
    failure: Literal["deadline_exceeded", "transport_failed"]
    try:
        return operation()
    except (TimeoutError, httpcore.TimeoutException):
        failure = "deadline_exceeded"
    except (OSError, httpcore.NetworkError, httpcore.ProtocolError, httpcore.UnsupportedProtocol, httpcore.ProxyError):
        failure = "transport_failed"
    if failure == "deadline_exceeded":
        raise HTTPDeadlineExceeded()
    raise HTTPDeadlineTransportError()


class DeadlineNetworkStream(httpcore.NetworkStream):
    """Owned socket stream with a shrinking budget for every syscall."""

    def __init__(self, sock: socket.socket, deadline: HTTPDeadline) -> None:
        self._socket = sock
        self._deadline = deadline

    def _prepare(self, timeout: float | None) -> None:
        remaining = self._deadline.remaining(timeout)
        _safe_operation(lambda: self._socket.settimeout(remaining))

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._prepare(timeout)
        data = _safe_operation(lambda: self._socket.recv(max_bytes))
        self._deadline.remaining()
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._deadline.remaining(timeout)
        view = memoryview(buffer)
        offset = 0
        while offset < len(view):
            self._prepare(timeout)
            sent = _safe_operation(partial(self._socket.send, view[offset:]))
            if sent <= 0 or sent > len(view) - offset:
                raise HTTPDeadlineTransportError()
            offset += sent
            self._deadline.remaining()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> DeadlineNetworkStream:
        self._prepare(timeout)
        if isinstance(self._socket, ssl.SSLSocket):
            raise HTTPDeadlineTransportError()
        success = False
        try:
            self._socket = _safe_operation(
                lambda: ssl_context.wrap_socket(
                    self._socket,
                    server_hostname=server_hostname,
                    do_handshake_on_connect=False,
                )
            )
            # wrap_socket transfers the fd; the new owner is closed on failure.
            self._deadline.remaining()
            self._prepare(timeout)
            tls_socket = self._socket
            assert isinstance(tls_socket, ssl.SSLSocket)
            _safe_operation(tls_socket.do_handshake)
            self._deadline.remaining()
            success = True
            return self
        finally:
            if not success:
                self.close()

    def close(self) -> None:
        _safe_operation(self._socket.close)

    def get_extra_info(self, info: str) -> object:
        if info == "ssl_object":
            return self._socket if isinstance(self._socket, ssl.SSLSocket) else None
        if info == "socket":
            return self._socket
        if info == "client_addr":
            return _safe_operation(self._socket.getsockname)
        if info == "server_addr":
            return _safe_operation(self._socket.getpeername)
        if info == "is_readable":
            return bool(_safe_operation(lambda: select.select([self._socket], [], [], 0)[0]))
        return None


class DeadlineNetworkBackend(httpcore.NetworkBackend):
    """TCP backend that connects only to an already admitted literal IP."""

    def __init__(self, deadline: HTTPDeadline) -> None:
        self._deadline = deadline

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> DeadlineNetworkStream:
        self._deadline.remaining(timeout)
        if local_address is not None:
            raise HTTPDeadlineTransportError()
        invalid_host = False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            invalid_host = True
        if invalid_host:
            raise HTTPDeadlineTransportError()
        family = socket.AF_INET if address.version == 4 else socket.AF_INET6
        sock = _safe_operation(lambda: socket.socket(family, socket.SOCK_STREAM))
        success = False
        try:
            if socket_options is not None:
                for option in socket_options:
                    _safe_operation(partial(sock.setsockopt, *option))
            _safe_operation(lambda: sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
            remaining = self._deadline.remaining(timeout)
            _safe_operation(lambda: sock.settimeout(remaining))
            _safe_operation(lambda: sock.connect((str(address), port)))
            self._deadline.remaining()
            success = True
            return DeadlineNetworkStream(sock, self._deadline)
        finally:
            if not success:
                _safe_operation(sock.close)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise HTTPDeadlineTransportError()

    def sleep(self, seconds: float) -> None:
        raise HTTPDeadlineTransportError()


class DeadlineResponseStream(httpx.SyncByteStream):
    """Keep the public core response alive until consumption or closure."""

    def __init__(self, response: httpcore.Response, deadline: HTTPDeadline) -> None:
        self._response = response
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        try:
            iterator = iter(self._response.iter_stream())
            while True:
                self._deadline.remaining()
                try:
                    chunk = _safe_operation(lambda: next(iterator))
                except StopIteration:
                    self._deadline.remaining()
                    return
                self._deadline.remaining()
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        _safe_operation(self._response.close)


class DeadlineHTTPTransport(httpx.BaseTransport):
    """Fresh HTTP/1.1 pool for one pinned dispatch; retries/proxies disabled."""

    def __init__(self, deadline: HTTPDeadline, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._deadline = deadline
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context if ssl_context is not None else httpcore.default_ssl_context(),
            network_backend=DeadlineNetworkBackend(deadline),
            proxy=None,
            retries=0,
            http1=True,
            http2=False,
            max_connections=1,
            max_keepalive_connections=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._deadline.remaining()
        assert isinstance(request.stream, httpx.SyncByteStream)
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = _safe_operation(lambda: self._pool.handle_request(core_request))
        try:
            self._deadline.remaining()
        except HTTPDeadlineExceeded:
            _safe_operation(response.close)
            raise
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=DeadlineResponseStream(response, self._deadline),
            extensions=response.extensions,
        )

    def close(self) -> None:
        _safe_operation(self._pool.close)
