"""Private independent watchdog executable. No application or provider imports."""

from __future__ import annotations

import os
import selectors
import signal
import socket
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from elspeth.web.process_watchdog_codec import GRACE_NS, FrameKind, FrameReader, WatchdogFrame, WatchdogProtocolError, encode_frame
else:
    from process_watchdog_codec import GRACE_NS, FrameKind, FrameReader, WatchdogFrame, WatchdogProtocolError, encode_frame


def _verify_pidfd(pidfd: int, reflected_pid: int) -> None:
    signal.pidfd_send_signal(pidfd, 0)
    with open(f"/proc/self/fdinfo/{pidfd}", "rb") as source:
        raw = source.read(4097)
    if len(raw) > 4096:
        raise WatchdogProtocolError("Target descriptor metadata exceeded")
    values = [line[4:].strip() for line in raw.splitlines() if line.startswith(b"Pid:")]
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) != reflected_pid or reflected_pid < 1:
        raise WatchdogProtocolError("Inherited target descriptor mismatch")


def supervise(control_fd: int, pidfd: int) -> int:
    control = socket.socket(fileno=control_fd)
    control.setblocking(False)
    signal_read, signal_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    previous_wakeup = signal.set_wakeup_fd(signal_write)
    signal.signal(signal.SIGTERM, lambda signum, frame: None)
    signal.signal(signal.SIGINT, lambda signum, frame: None)
    reader = FrameReader()
    nonce: str | None = None
    target_pid: int | None = None
    armed: int | None = None
    deadline: int | None = None
    handshake_deadline = time.monotonic_ns() + 2_000_000_000
    pairs = 0
    outgoing = bytearray()
    killed = False
    channel_open = True
    completing = False
    with selectors.DefaultSelector() as selector:
        selector.register(pidfd, selectors.EVENT_READ, "target")
        selector.register(signal_read, selectors.EVENT_READ, "signal")
        selector.register(control, selectors.EVENT_READ, "control")

        def arm() -> None:
            nonlocal armed, deadline
            if armed is None:
                armed = time.monotonic_ns()
                deadline = armed + GRACE_NS

        try:
            while True:
                now = time.monotonic_ns()
                if nonce is None and now >= handshake_deadline:
                    return 2
                if deadline is not None and now >= deadline and not killed:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                    killed = True
                timeout = (
                    None
                    if killed
                    else max(
                        0.0,
                        ((deadline if deadline is not None else handshake_deadline if nonce is None else now + 1_000_000_000) - now) / 1e9,
                    )
                )
                for key, events in selector.select(timeout):
                    if key.data == "target":
                        return 0
                    if key.data == "signal":
                        os.read(signal_read, 4096)
                        if nonce is not None:
                            arm()
                        continue
                    try:
                        if events & selectors.EVENT_WRITE and outgoing:
                            sent = control.send(outgoing)
                            del outgoing[:sent]
                            selector.modify(control, selectors.EVENT_READ | (selectors.EVENT_WRITE if outgoing else 0), "control")
                            if not outgoing and nonce is not None and pairs == -1:
                                return 0
                        if events & selectors.EVENT_READ:
                            data = control.recv(reader.read_capacity)
                            if not data:
                                raise WatchdogProtocolError("Private watchdog channel closed")
                            for frame in reader.feed(data):
                                if completing:
                                    raise WatchdogProtocolError("Frame after watchdog completion")
                                if nonce is None:
                                    if frame.kind is not FrameKind.HELLO:
                                        raise WatchdogProtocolError("Expected private watchdog hello")
                                    _verify_pidfd(pidfd, frame.target_pid)
                                    nonce, target_pid = frame.nonce, frame.target_pid
                                    outgoing.extend(encode_frame(WatchdogFrame(FrameKind.WATCHING_ACK, nonce, target_pid)))
                                else:
                                    assert target_pid is not None
                                    frame.validate_target(nonce=nonce, target_pid=target_pid)
                                    if frame.kind is FrameKind.BEGIN_RECOVERY:
                                        pairs += 1
                                        if pairs > 4:
                                            raise WatchdogProtocolError("Too many recovery triggers")
                                        arm()
                                        outgoing.extend(
                                            encode_frame(
                                                WatchdogFrame(
                                                    FrameKind.GRACE_ACK, nonce, target_pid, armed_at_ns=armed, deadline_ns=deadline
                                                )
                                            )
                                        )
                                    elif frame.kind is FrameKind.COMPLETE:
                                        completing = True
                                        pairs = -1
                                        outgoing.extend(encode_frame(WatchdogFrame(FrameKind.DISARM_ACK, nonce, target_pid)))
                                    else:
                                        raise WatchdogProtocolError("Unexpected private watchdog frame")
                                if len(outgoing) > 1028:
                                    raise WatchdogProtocolError("Watchdog outgoing buffer exceeded")
                                selector.modify(control, selectors.EVENT_READ | selectors.EVENT_WRITE, "control")
                    except (OSError, WatchdogProtocolError):
                        if nonce is None:
                            return 2
                        arm()
                        if channel_open:
                            selector.unregister(control)
                            control.close()
                            channel_open = False
        finally:
            signal.set_wakeup_fd(previous_wakeup)
            os.close(signal_read)
            os.close(signal_write)
            if channel_open:
                control.close()
            os.close(pidfd)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(2)
    raise SystemExit(supervise(int(sys.argv[1]), int(sys.argv[2])))
