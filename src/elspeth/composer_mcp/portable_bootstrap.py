"""Consume private bootstrap authority before importing application runtime."""

from __future__ import annotations

import os
import selectors
import socket
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from elspeth.contracts.process_watchdog_wire import ProcessWatchdogTarget, WatchdogFrame

PORT_ENV = "_ELSPETH_COMPOSER_CONTROL_PORT"
NONCE_ENV = "_ELSPETH_COMPOSER_CONTROL_NONCE"
_BOOTSTRAP_AUTHORITY = object()


class _ControlStream:
    """One bounded decoder for the whole child connection, including bootstrap."""

    def __init__(self, channel: socket.socket, target: ProcessWatchdogTarget) -> None:
        from elspeth.contracts.process_watchdog_wire import FrameReader

        self.channel = channel
        self.target = target
        self._reader = FrameReader()
        self._replies: list[WatchdogFrame] = []
        self.shutdown_requested = False

    @property
    def read_capacity(self) -> int:
        return self._reader.read_capacity

    def feed(self, data: bytes) -> None:
        from elspeth.contracts.process_watchdog_wire import FRAME_COUNT_LIMIT, FrameKind, WatchdogProtocolError

        for frame in self._reader.feed(data):
            frame.validate_target(nonce=self.target.nonce, target_pid=self.target.pid)
            if frame.kind is FrameKind.SHUTDOWN_REQUEST:
                # Intent is idempotent and survives every synchronous exchange.
                self.shutdown_requested = True
            elif frame.kind in (FrameKind.WATCHING_ACK, FrameKind.GRACE_ACK, FrameKind.DISARM_ACK):
                self._replies.append(frame)
                if len(self._replies) > FRAME_COUNT_LIMIT:
                    raise WatchdogProtocolError("Composer pending reply limit exceeded")
            else:
                raise WatchdogProtocolError("Wrong Composer control direction")

    def take_replies(self) -> tuple[WatchdogFrame, ...]:
        replies = tuple(self._replies)
        self._replies.clear()
        return replies


class AuthenticatedSupervisor:
    """Single-use connection receipt issued only after WATCHING_ACK."""

    def __init__(self, stream: _ControlStream, authority: object) -> None:
        if authority is not _BOOTSTRAP_AUTHORITY:
            raise RuntimeError("Composer connection receipt requires authenticated bootstrap")
        self._stream = stream
        self._claimed = False

    def claim(self) -> _ControlStream:
        if self._claimed:
            raise RuntimeError("Composer connection receipt already claimed")
        self._claimed = True
        return self._stream


def exchange_sync(stream: _ControlStream, frame: WatchdogFrame) -> WatchdogFrame:
    from elspeth.contracts.process_watchdog_wire import WatchdogProtocolError, encode_frame

    channel = stream.channel
    if stream.take_replies():
        raise WatchdogProtocolError("Unsolicited Composer bootstrap reply")
    outgoing = memoryview(encode_frame(frame))
    deadline = time.monotonic() + 1.0
    with selectors.DefaultSelector() as selector:
        selector.register(channel, selectors.EVENT_READ | selectors.EVENT_WRITE)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WatchdogProtocolError("Private Composer exchange timed out")
            for _, events in selector.select(remaining):
                if events & selectors.EVENT_WRITE and outgoing:
                    try:
                        count = channel.send(outgoing)
                    except BlockingIOError:
                        count = 0
                    outgoing = outgoing[count:]
                    if not outgoing:
                        selector.modify(channel, selectors.EVENT_READ)
                if events & selectors.EVENT_READ:
                    try:
                        data = channel.recv(stream.read_capacity)
                    except BlockingIOError:
                        continue
                    if not data:
                        raise WatchdogProtocolError("Private Composer channel closed")
                    stream.feed(data)
                    frames = stream.take_replies()
                    if frames:
                        if len(frames) != 1 or outgoing:
                            raise WatchdogProtocolError("Unexpected Composer bootstrap frame")
                        reply = frames[0]
                        reply.validate_target(nonce=frame.nonce, target_pid=frame.target_pid)
                        return reply


def connect_supervisor() -> AuthenticatedSupervisor:
    # Pop both even on malformed/missing bootstrap. No runtime/plugin imports
    # or nested subprocess may inherit this launch's private nonce.
    port_text = os.environ.pop(PORT_ENV, None)
    nonce = os.environ.pop(NONCE_ENV, None)
    from elspeth.contracts.process_watchdog_wire import FrameKind, ProcessWatchdogTarget, WatchdogFrame, WatchdogProtocolError

    if port_text is None or nonce is None or len(port_text) > 5 or not port_text.isascii() or not port_text.isdecimal():
        raise WatchdogProtocolError("Private Composer bootstrap is required")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise WatchdogProtocolError("Private Composer bootstrap port refused")
    target = ProcessWatchdogTarget(os.getpid(), nonce)
    hello = WatchdogFrame(FrameKind.HELLO, nonce, target.pid)
    channel = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    channel.setblocking(False)
    deadline = time.monotonic() + 1.0
    try:
        # Nonblocking connect is observed through the socket selector only.
        error = channel.connect_ex(("127.0.0.1", port))
        if error:
            with selectors.DefaultSelector() as selector:
                selector.register(channel, selectors.EVENT_WRITE)
                if not selector.select(max(0.0, deadline - time.monotonic())) or channel.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                    raise WatchdogProtocolError("Private Composer supervisor connection failed")
        stream = _ControlStream(channel, target)
        reply = exchange_sync(stream, hello)
        if reply.kind is not FrameKind.WATCHING_ACK:
            raise WatchdogProtocolError("Private Composer ownership acknowledgement refused")
        return AuthenticatedSupervisor(stream, _BOOTSTRAP_AUTHORITY)
    except BaseException:
        channel.close()
        raise
