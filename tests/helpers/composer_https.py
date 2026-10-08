"""Real loopback TLS transport for composer acceptance; no deployed service access."""

from __future__ import annotations

import contextlib
import signal
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

import uvicorn
from starlette.types import ASGIApp


@dataclass(frozen=True)
class LocalTLS:
    base_url: str
    verify: ssl.SSLContext
    proxy_log: Path


@dataclass(slots=True)
class _TLSOwners:
    upstream_requested: bool = False
    upstream: socket.socket | None = None
    upstream_closed: bool = False
    reservation_requested: bool = False
    reservation: socket.socket | None = None
    reservation_close_started: bool = False
    reservation_closed: bool = False
    server: uvicorn.Server | None = None
    server_thread: threading.Thread | None = None
    thread_start_declared: bool = False
    thread_joined: bool = False
    proxy_requested: bool = False
    proxy: subprocess.Popen[bytes] | None = None
    proxy_joined: bool = False
    output_requested: bool = False
    output: BinaryIO | None = None
    output_closed: bool = False
    prior_handlers: dict[signal.Signals, object] = field(default_factory=dict)
    originals: list[BaseException] = field(default_factory=list)


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
    except BaseException as primary:
        try:
            listener.close()
        except BaseException as cleanup:
            raise BaseExceptionGroup("Local listener setup and close failed", [primary, cleanup]) from None
        raise
    return listener


def _close_reservation(owners: _TLSOwners) -> None:
    if owners.reservation is None or owners.reservation_close_started:
        return
    owners.reservation_close_started = True
    try:
        owners.reservation.close()
        owners.reservation_closed = True
    except BaseException as original:
        owners.originals.append(original)


def _cleanup_tls(owners: _TLSOwners) -> None:
    """Attempt every independent actual owner; no signal or absence proves join."""
    proxy = owners.proxy
    if proxy is not None:
        try:
            proxy.terminate()
        except BaseException as original:
            owners.originals.append(original)
        try:
            returned = proxy.wait(timeout=5)
            owners.proxy_joined = type(returned) is int and proxy.returncode == returned
        except BaseException as original:
            owners.originals.append(original)
        if not owners.proxy_joined:
            try:
                proxy.kill()
            except BaseException as original:
                owners.originals.append(original)
            try:
                returned = proxy.wait(timeout=5)
                owners.proxy_joined = type(returned) is int and proxy.returncode == returned
            except BaseException as original:
                owners.originals.append(original)
    if owners.server is not None:
        try:
            owners.server.should_exit = True
        except BaseException as original:
            owners.originals.append(original)
    if owners.thread_start_declared and owners.server_thread is not None:
        try:
            owners.server_thread.join(timeout=10)
            owners.thread_joined = not owners.server_thread.is_alive()
        except BaseException as original:
            owners.originals.append(original)
    _close_reservation(owners)
    if owners.upstream is not None:
        try:
            owners.upstream.close()
            owners.upstream_closed = True
        except BaseException as original:
            owners.originals.append(original)
    if owners.output is not None:
        try:
            owners.output.close()
            owners.output_closed = True
        except BaseException as original:
            owners.originals.append(original)
    unresolved = (
        (owners.proxy_requested and not owners.proxy_joined)
        or (owners.thread_start_declared and not owners.thread_joined)
        or (owners.upstream_requested and not owners.upstream_closed)
        or (owners.reservation_requested and not owners.reservation_closed)
        or (owners.output_requested and not owners.output_closed)
    )
    if unresolved:
        owners.originals.append(RuntimeError("local TLS physical custody remains unresolved"))


def _raise_tls_originals(owners: _TLSOwners) -> None:
    if len(owners.originals) == 1:
        raise owners.originals[0]
    if owners.originals:
        raise BaseExceptionGroup("Local TLS body and cleanup originals", owners.originals)


def _install_tls_stop(owners: _TLSOwners) -> None:
    def interrupted(_number: int, _frame: object) -> None:
        owners.originals.append(KeyboardInterrupt("local TLS requested stop"))

    # Do not block the direct Caddy child's inherited signals. Non-raising
    # handlers let the returned Popen be stored before Stop is projected.
    for number in (signal.SIGINT, signal.SIGTERM):
        owners.prior_handlers[number] = signal.getsignal(number)
        signal.signal(number, interrupted)


def _restore_tls_stop(owners: _TLSOwners) -> None:
    for number, previous in reversed(tuple(owners.prior_handlers.items())):
        try:
            signal.signal(number, previous)
        except BaseException as original:
            owners.originals.append(original)


@contextlib.contextmanager
def local_composer_tls(
    app: ASGIApp,
    directory: Path,
    *,
    flush_interval: str = "-1",
    upstream_read_timeout: str | None = None,
    response_buffer_bytes: int | None = None,
) -> Iterator[LocalTLS]:
    """Serve an owned ASGI app behind a separate admin-disabled Caddy process.

    Certificates and process output live only in the caller's temporary directory.
    The upstream socket stays bound while uvicorn takes ownership. Caddy's port
    is checked by TLS handshake, so an occupied/misconfigured port fails loudly.
    ``flush_interval`` is explicit so controls can deliberately buffer delivery.
    """
    # Fail before acquiring physical owners when the CI prerequisite is absent.
    # A failure after the Popen attempt still retains unresolved custody.
    if not Path("/usr/bin/caddy").is_file():
        raise FileNotFoundError("local TLS requires /usr/bin/caddy")
    directory.mkdir(parents=True, exist_ok=True)
    certificate = directory / "localhost.crt"
    private_key = directory / "localhost.key"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    verification = ssl.create_default_context(cafile=str(certificate))
    owners = _TLSOwners()
    try:
        _install_tls_stop(owners)
        _raise_tls_originals(owners)
        owners.upstream_requested = True
        owners.upstream = _listener()
        upstream_port = owners.upstream.getsockname()[1]
        owners.reservation_requested = True
        owners.reservation = _listener()
        proxy_port = owners.reservation.getsockname()[1]
        _close_reservation(owners)
        _raise_tls_originals(owners)
        owners.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
        owners.server_thread = threading.Thread(target=owners.server.run, kwargs={"sockets": [owners.upstream]}, daemon=True)
        _raise_tls_originals(owners)
        owners.thread_start_declared = True
        owners.server_thread.start()
        _raise_tls_originals(owners)
        configuration = directory / "Caddyfile"
        transport = "" if upstream_read_timeout is None else f" transport http {{\n read_timeout {upstream_read_timeout}\n }}\n"
        buffering = "" if response_buffer_bytes is None else f" response_buffers {response_buffer_bytes}\n"
        configuration.write_text(
            "{\n admin off\n auto_https off\n servers {\n protocols h1 h2\n }\n}\n"
            f"https://127.0.0.1:{proxy_port} {{\n bind 127.0.0.1\n"
            f" tls {certificate} {private_key}\n"
            f" reverse_proxy 127.0.0.1:{upstream_port} {{\n flush_interval {flush_interval}\n{transport}{buffering} }}\n}}\n",
            encoding="utf-8",
        )
        proxy_log = directory / "caddy.log"
        owners.output_requested = True
        owners.output = proxy_log.open("wb")
        _raise_tls_originals(owners)
        owners.proxy_requested = True
        owners.proxy = subprocess.Popen(
            ["/usr/bin/caddy", "run", "--config", str(configuration), "--adapter", "caddyfile"],
            cwd=directory,
            stdout=owners.output,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "XDG_DATA_HOME": str(directory / "data"), "XDG_CONFIG_HOME": str(directory / "config")},
        )
        _raise_tls_originals(owners)
        assert owners.proxy is not None
        deadline = time.monotonic() + 10
        while True:
            _raise_tls_originals(owners)
            if owners.proxy.poll() is not None:
                raise RuntimeError(f"local Caddy exited {owners.proxy.returncode}: {proxy_log.read_text()}")
            try:
                with (
                    socket.create_connection(("127.0.0.1", proxy_port), timeout=0.2) as connection,
                    verification.wrap_socket(connection, server_hostname="127.0.0.1"),
                ):
                    pass
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("local Caddy TLS readiness deadline exceeded") from None
                time.sleep(0.02)
        _raise_tls_originals(owners)
        yield LocalTLS(f"https://127.0.0.1:{proxy_port}", verification, proxy_log)
    except BaseException as primary:
        if not any(original is primary for original in owners.originals):
            owners.originals.append(primary)
    finally:
        _cleanup_tls(owners)
        _restore_tls_stop(owners)
    _raise_tls_originals(owners)
