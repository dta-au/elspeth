"""Absolute budgets for audited HTTP requests.

httpx timeouts bound each connect, read and write separately, so an upstream
that trickles one byte just under the read timeout keeps a request alive
without limit. Every audited HTTP request therefore runs inside one
``http_deadline_scope``: an absolute expiration that every network operation
of every connection made by a ``deadline_bound_client`` shrinks its own
timeout to, including redirect hops of the same logical request. The
configured timeout is thus the request's total wall time.

The public HTTPCore backend seam lets each blocking syscall use the remaining
budget, including short TLS writes hidden beneath HTTP response buffering.
The Power Automate policy additionally pins its own literal-IP transport
(``DeadlineHTTPTransport``) to the same scoped deadline.
"""

from __future__ import annotations

import ipaddress
import math
import select
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar, Literal

import httpcore
import httpx

# httpx's own reading of the proxy environment; reusing it keeps the mount keys
# identical to the ones httpx builds, so each one is replaced (pinned httpx).
from httpx._utils import get_environment_proxies

import elspeth.contracts.errors as contract_errors

_HTTP_DNS_CAPACITY = threading.BoundedSemaphore(8)


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


# ---------------------------------------------------------------------------
# Scoped absolute deadline for every audited HTTP request
# ---------------------------------------------------------------------------

_ACTIVE_HTTP_DEADLINE: ContextVar[HTTPDeadline | None] = ContextVar("elspeth_active_http_deadline", default=None)

# Largest slice handed to one socket write, so a peer that drains our request
# body slowly is re-checked against the deadline between slices.
_WRITE_SLICE_BYTES = 16384

_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)


@contextmanager
def http_deadline_scope(total_seconds: float) -> Iterator[HTTPDeadline]:
    """Bind one absolute deadline to every network operation in this context.

    ``total_seconds`` is the request's total wall time: connect, request
    write, response headers and the full response body. A nested scope never
    extends an outer one: the earlier expiration wins, so redirect hops and
    helper calls share the caller's budget.
    """
    if not math.isfinite(total_seconds) or total_seconds <= 0:
        raise ValueError("HTTP deadline must be a finite, positive number of seconds")
    candidate = HTTPDeadline(time.monotonic() + total_seconds, clock=time.monotonic)
    outer = _ACTIVE_HTTP_DEADLINE.get()
    deadline = outer if outer is not None and outer.expires_at <= candidate.expires_at else candidate
    token = _ACTIVE_HTTP_DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _ACTIVE_HTTP_DEADLINE.reset(token)


def _scoped_timeout(phase_timeout: float | None, expired: type[httpcore.TimeoutException]) -> float:
    """Shrink one operation's timeout to the remaining scoped budget."""
    deadline = _ACTIVE_HTTP_DEADLINE.get()
    if deadline is None:
        raise contract_errors.FrameworkBugError("deadline-bound HTTP client used outside http_deadline_scope")
    remaining = deadline.expires_at - deadline.clock()
    if remaining <= 0:
        raise expired("HTTP request exceeded its total deadline")
    if phase_timeout is None:
        return remaining
    return min(phase_timeout, remaining)


class _ScopedDeadlineStream(httpcore.NetworkStream):
    """Network stream whose every blocking operation honours the scoped deadline."""

    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = self._inner.read(max_bytes, timeout=_scoped_timeout(timeout, httpcore.ReadTimeout))
        _scoped_timeout(None, httpcore.ReadTimeout)
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        # httpcore.SyncStream.write retries short sends with the same timeout
        # on every retry. Use the public socket exposed by the backend so each
        # actual send receives the *current* remaining budget.
        sock = self._inner.get_extra_info("socket")
        if not isinstance(sock, socket.socket):
            raise contract_errors.FrameworkBugError("deadline-bound stream has no socket")
        view = memoryview(buffer)
        for offset in range(0, len(view), _WRITE_SLICE_BYTES):
            chunk = view[offset : offset + _WRITE_SLICE_BYTES]
            sent_total = 0
            while sent_total < len(chunk):
                sock.settimeout(_scoped_timeout(timeout, httpcore.WriteTimeout))
                try:
                    sent = sock.send(chunk[sent_total:])
                except TimeoutError as exc:
                    raise httpcore.WriteTimeout() from exc
                except OSError as exc:
                    raise httpcore.WriteError() from exc
                if sent <= 0:
                    raise httpcore.WriteError("socket made no write progress")
                sent_total += sent
                _scoped_timeout(None, httpcore.WriteTimeout)

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        if isinstance(self._inner.get_extra_info("socket"), ssl.SSLSocket):
            # HTTPS proxy to HTTPS origin: perform the second TLS layer over
            # this already bounded outer stream. httpcore's TLS-in-TLS stream
            # otherwise loops over socket reads with one stale timeout.
            return _NestedDeadlineTLSStream(self, ssl_context, server_hostname)
        upgraded = self._inner.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=_scoped_timeout(timeout, httpcore.ConnectTimeout)
        )
        # A handshake is several reads under one socket timeout; refuse a
        # connection whose handshake alone consumed the whole budget.
        try:
            _scoped_timeout(None, httpcore.ConnectTimeout)
        except httpcore.ConnectTimeout:
            upgraded.close()
            raise
        return _ScopedDeadlineStream(upgraded)

    def get_extra_info(self, info: str) -> object:
        return self._inner.get_extra_info(info)


class _NestedDeadlineTLSStream(httpcore.NetworkStream):
    """TLS inside a TLS proxy, with every encrypted I/O through the outer budget."""

    def __init__(self, outer: _ScopedDeadlineStream, context: ssl.SSLContext, hostname: str | None) -> None:
        self._outer = outer
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_hostname=hostname)
        try:
            self._perform(self._tls.do_handshake, httpcore.ConnectTimeout)
        except BaseException:
            self.close()
            raise

    def _perform[T](self, operation: Callable[[], T], expired: type[httpcore.TimeoutException]) -> T:
        while True:
            _scoped_timeout(None, expired)
            want_read = False
            try:
                result = operation()
            except ssl.SSLWantReadError:
                want_read = True
            except ssl.SSLWantWriteError:
                pass
            else:
                encrypted = self._outgoing.read()
                if encrypted:
                    self._outer.write(encrypted)
                _scoped_timeout(None, expired)
                return result
            encrypted = self._outgoing.read()
            if encrypted:
                self._outer.write(encrypted)
            if want_read:
                incoming = self._outer.read(_WRITE_SLICE_BYTES)
                if incoming:
                    self._incoming.write(incoming)
                else:
                    self._incoming.write_eof()

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        _scoped_timeout(timeout, httpcore.ReadTimeout)
        return self._perform(partial(self._tls.read, max_bytes), httpcore.ReadTimeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        view = memoryview(buffer)
        offset = 0
        while offset < len(view):
            _scoped_timeout(timeout, httpcore.WriteTimeout)
            sent = self._perform(partial(self._tls.write, bytes(view[offset : offset + _WRITE_SLICE_BYTES])), httpcore.WriteTimeout)
            if sent <= 0:
                raise httpcore.WriteError("nested TLS made no write progress")
            offset += sent

    def close(self) -> None:
        self._outer.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("a third TLS layer is unsupported")

    def get_extra_info(self, info: str) -> object:
        if info == "ssl_object":
            return self._tls
        return self._outer.get_extra_info(info)


class _ScopedDeadlineBackend(httpcore.NetworkBackend):
    """Default sync backend with every connect bounded by the scoped deadline."""

    def __init__(self) -> None:
        self._inner = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        _scoped_timeout(timeout, httpcore.ConnectTimeout)
        addresses: list[
            tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]]
        ] = []
        errors: list[BaseException] = []
        resolved = threading.Event()
        if not _HTTP_DNS_CAPACITY.acquire(blocking=False):
            raise httpcore.ConnectTimeout("HTTP DNS resolver capacity exhausted")

        def resolve() -> None:
            try:
                addresses.extend(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except BaseException as exc:
                errors.append(exc)
            finally:
                _HTTP_DNS_CAPACITY.release()
                resolved.set()

        # The system resolver cannot be cancelled. A daemon worker lets the
        # caller stop at the deadline without waiting for that resolver.
        try:
            threading.Thread(target=resolve, name="elspeth-http-dns", daemon=True).start()
        except BaseException:
            _HTTP_DNS_CAPACITY.release()
            raise
        if not resolved.wait(_scoped_timeout(timeout, httpcore.ConnectTimeout)):
            raise httpcore.ConnectTimeout("HTTP request exceeded its total deadline during DNS lookup")
        if errors:
            if isinstance(errors[0], OSError):
                raise httpcore.ConnectError() from errors[0]
            raise errors[0]
        _scoped_timeout(None, httpcore.ConnectTimeout)
        last_error: httpcore.ConnectError | None = None
        for _family, _kind, _protocol, _name, sockaddr in addresses:
            try:
                stream = self._inner.connect_tcp(
                    str(sockaddr[0]),
                    port,
                    timeout=_scoped_timeout(timeout, httpcore.ConnectTimeout),
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
                continue
            try:
                _scoped_timeout(None, httpcore.ConnectTimeout)
            except httpcore.ConnectTimeout:
                stream.close()
                raise
            return _ScopedDeadlineStream(stream)
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("DNS returned no TCP addresses")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        stream = self._inner.connect_unix_socket(
            path, timeout=_scoped_timeout(timeout, httpcore.ConnectTimeout), socket_options=socket_options
        )
        try:
            _scoped_timeout(None, httpcore.ConnectTimeout)
        except httpcore.ConnectTimeout:
            stream.close()
            raise
        return _ScopedDeadlineStream(stream)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(min(seconds, _scoped_timeout(None, httpcore.ConnectTimeout)))
        _scoped_timeout(None, httpcore.ConnectTimeout)


class DeadlineBoundTransport(httpx.HTTPTransport):
    """httpx's HTTP transport over the scoped-deadline network backend.

    Construction mirrors ``httpx.HTTPTransport`` (direct, HTTP proxy and SOCKS
    proxy pools) through httpcore's public ``network_backend`` seam; request
    handling and httpcore-to-httpx exception mapping are inherited unchanged,
    so a deadline expiry surfaces as an ordinary ``httpx.TimeoutException``.
    """

    def __init__(
        self,
        *,
        verify: ssl.SSLContext | str | bool = True,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = _DEFAULT_LIMITS,
        proxy: httpx.Proxy | None = None,
    ) -> None:
        ssl_context = httpx.create_ssl_context(verify=verify, trust_env=trust_env)
        backend = _ScopedDeadlineBackend()
        if proxy is None:
            self._pool = httpcore.ConnectionPool(
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
                network_backend=backend,
            )
            return
        proxy_url = httpcore.URL(
            scheme=proxy.url.raw_scheme,
            host=proxy.url.raw_host,
            port=proxy.url.port,
            target=proxy.url.raw_path,
        )
        if proxy.url.scheme in ("http", "https"):
            self._pool = httpcore.HTTPProxy(
                proxy_url=proxy_url,
                proxy_auth=proxy.raw_auth,
                proxy_headers=proxy.headers.raw,
                ssl_context=ssl_context,
                proxy_ssl_context=proxy.ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
                network_backend=backend,
            )
        elif proxy.url.scheme in ("socks5", "socks5h"):
            self._pool = httpcore.SOCKSProxy(
                proxy_url=proxy_url,
                proxy_auth=proxy.raw_auth,
                ssl_context=ssl_context,
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                http1=http1,
                http2=http2,
                network_backend=backend,
            )
        else:
            raise ValueError(f"unsupported proxy scheme {proxy.url.scheme!r}")


def deadline_bound_client(
    *,
    timeout: float,
    trust_env: bool = True,
    event_hooks: Mapping[str, list[Callable[..., Any]]] | None = None,
) -> httpx.Client:
    """The one httpx client factory for audited HTTP callers.

    Every request made with the returned client must run inside
    ``http_deadline_scope``; a network operation outside one is a framework
    bug and fails closed. ``timeout`` stays the per-phase ceiling; the scope
    bounds the request's total wall time. Redirects are never followed by
    httpx (callers validate every hop for SSRF themselves).

    With ``trust_env`` httpx still selects proxies from ``HTTP(S)_PROXY`` /
    ``ALL_PROXY`` / ``NO_PROXY``; every proxy mount it would build is replaced,
    key for key from httpx's own environment reading, by a deadline-bound
    transport, so no environment route escapes the deadline. ``event_hooks``
    are passed to httpx unchanged (the SSRF clients use one to admit each raw
    redirect Location before httpx builds the next request).
    """
    mounts: dict[str, httpx.BaseTransport | None] = {}
    if trust_env:
        for pattern, proxy_url in get_environment_proxies().items():
            mounts[pattern] = None if proxy_url is None else DeadlineBoundTransport(trust_env=True, proxy=httpx.Proxy(url=proxy_url))
    return httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=trust_env,
        transport=DeadlineBoundTransport(trust_env=trust_env),
        mounts=mounts,
        event_hooks=None if event_hooks is None else {name: list(hooks) for name, hooks in event_hooks.items()},
    )
