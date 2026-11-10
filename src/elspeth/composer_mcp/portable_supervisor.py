"""Private, dedicated Popen lifetime owner. Requires IPv4 loopback binding.

Execute this file in a fresh console interpreter to keep it stdlib-only. It
must not be embedded in a host with competing child reapers. Stdio belongs to the
runner; this owner never reads stdin or writes stdout. The public launcher
executes this file, which launches exactly one authenticated private runner.
"""

from __future__ import annotations

import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from elspeth.contracts.process_watchdog_wire import GRACE_NS, FrameKind, FrameReader, WatchdogFrame, WatchdogProtocolError, encode_frame
else:
    # The contract file itself has only stdlib dependencies. Importing the
    # contracts package would execute its eager application re-exports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))
    from process_watchdog_wire import GRACE_NS, FrameKind, FrameReader, WatchdogFrame, WatchdogProtocolError, encode_frame

STARTUP_NS = 2_000_000_000
EXIT_NS = 2_000_000_000
POLL_SECONDS = 0.02
PEER_LIMIT = 8
PORT_ENV = "_ELSPETH_COMPOSER_CONTROL_PORT"
NONCE_ENV = "_ELSPETH_COMPOSER_CONTROL_NONCE"
_CREATE_NEW_PROCESS_GROUP = 0x00000200  # Win32 CREATE_NEW_PROCESS_GROUP


def _launch_child(command: Sequence[str], environment: dict[str, str], *, windows: bool) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        env=environment,
        close_fds=True,
        creationflags=_CREATE_NEW_PROCESS_GROUP if windows else 0,
        start_new_session=not windows,
    )


class SupervisorFailure(RuntimeError):
    """The owned child could not be terminated/reaped truthfully."""


def _install_controller_death_guard(expected_parent: int, *, linux: bool = sys.platform == "linux") -> bool:
    """Preserve Linux controller-death protection without bypassing cleanup.

    The recorded parent is an observation for detecting reparenting, never a
    signal target. Other platforms retain the shared portable supervision path.
    Only the dedicated console entry may install this process-wide kernel state.
    """
    if not linux:
        return False
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, f"Composer controller-death guard failed: {os.strerror(error)}")
    return expected_parent == 1 or os.getppid() != expected_parent


class _Supervisor:
    """Single-threaded owner. The clock seam is private subprocess-test support."""

    def __init__(self, command: Sequence[str], *, _clock_ns: Callable[[], int] = time.monotonic_ns, dedicated_cli: bool = False) -> None:
        self.command = command
        self.clock = _clock_ns
        self.child: subprocess.Popen[bytes] | None = None
        self.stop_requested = False
        self.armed_at: int | None = None
        self.deadline: int | None = None
        self.failed = False
        self.completed = False
        self.dedicated_cli = dedicated_cli
        self._controller_parent = os.getppid()

    def _stop(self, signum: int, frame: object) -> None:
        # No socket, signal or wait operations in a signal handler.
        self.stop_requested = True

    def _arm(self) -> None:
        if self.armed_at is None:
            self.armed_at = self.clock()
            recovery_deadline = self.armed_at + GRACE_NS
            self.deadline = min(self.deadline, recovery_deadline) if self.deadline is not None else recovery_deadline

    def _kill_and_reap(self) -> int:
        assert self.child is not None
        # This loop is the exclusive poll/signal/reap authority. Never signal
        # after observed exit; on Windows Popen.kill uses the retained handle.
        status = self.child.poll()
        if status is not None:
            return status
        try:
            self.child.kill()
            return self.child.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SupervisorFailure("Owned Composer child termination/reap failed") from error

    def run(self) -> int:
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        previous_chld = None
        if os.name != "nt":
            previous_chld = signal.getsignal(signal.SIGCHLD)
            # Inherited SIG_IGN can discard wait status and lose PID custody.
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for sig in previous:
            signal.signal(sig, self._stop)
        try:
            if self.dedicated_cli and _install_controller_death_guard(self._controller_parent):
                self.stop_requested = True
            return self._run_owned()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            if os.name != "nt":
                signal.signal(signal.SIGCHLD, previous_chld)

    def _run_owned(self) -> int:
        nonce = secrets.token_hex(32)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener, selectors.DefaultSelector() as selector:
            listener.bind(("127.0.0.1", 0))
            listener.listen(PEER_LIMIT)
            listener.setblocking(False)
            selector.register(listener, selectors.EVENT_READ)
            environment = os.environ.copy()
            environment[PORT_ENV] = str(listener.getsockname()[1])
            environment[NONCE_ENV] = nonce
            # No received identity ever influences this process reference.
            self.child = _launch_child(self.command, environment, windows=os.name == "nt")
            environment.pop(NONCE_ENV)
            peers: dict[socket.socket, FrameReader] = {}
            control: socket.socket | None = None
            outgoing = bytearray()
            attempts = 0
            startup_deadline = self.clock() + STARTUP_NS
            shutdown_sent = False
            listener_open = True

            def close_peer(peer: socket.socket) -> None:
                selector.unregister(peer)
                peer.close()
                del peers[peer]

            def fail_channel() -> None:
                nonlocal control
                self.failed = True
                self._arm()
                if control is not None:
                    close_peer(control)
                    control = None
                outgoing.clear()

            def queue(frame: WatchdogFrame) -> None:
                assert control is not None
                outgoing.extend(encode_frame(frame))
                if len(outgoing) > 1028:
                    raise WatchdogProtocolError("Supervisor outgoing buffer exceeded")
                selector.modify(control, selectors.EVENT_READ | selectors.EVENT_WRITE)

            try:
                if self.dedicated_cli:
                    # Popen already inherited the original handles. The owner
                    # must not independently keep client input/output alive.
                    # Flush/close wrappers before releasing the descriptors:
                    # CPython's original standard streams use closefd=False.
                    # os.close also releases their CRT-owned handles on Windows.
                    # Stderr remains available for custody diagnostics.
                    sys.stdin.close()
                    sys.stdout.close()
                    os.close(0)
                    os.close(1)
                while True:
                    status = self.child.poll()
                    if status is not None:
                        return status if status != 0 else 0 if self.completed and not self.failed else 2
                    now = self.clock()
                    if control is None and not self.completed and listener_open and now >= startup_deadline:
                        self.failed = True
                        return self._kill_and_reap() or 2
                    if self.deadline is not None and now >= self.deadline:
                        self.failed = True
                        return self._kill_and_reap() or 2
                    if self.stop_requested:
                        self._arm()
                        if control is not None and not shutdown_sent and not self.completed:
                            try:
                                queue(WatchdogFrame(FrameKind.SHUTDOWN_REQUEST, nonce, self.child.pid))
                                shutdown_sent = True
                            except (OSError, WatchdogProtocolError):
                                fail_channel()
                    nearest = self.deadline
                    if listener_open:
                        nearest = min(nearest, startup_deadline) if nearest is not None else startup_deadline
                    timeout = POLL_SECONDS if nearest is None else min(POLL_SECONDS, max(0.0, (nearest - now) / 1e9))
                    for key, events in selector.select(timeout):
                        if key.fileobj is listener:
                            try:
                                accepted, _ = listener.accept()
                            except BlockingIOError:
                                continue
                            attempts += 1
                            if attempts > PEER_LIMIT:
                                accepted.close()
                                continue
                            accepted.setblocking(False)
                            peers[accepted] = FrameReader()
                            selector.register(accepted, selectors.EVENT_READ)
                            continue
                        peer = key.fileobj
                        assert isinstance(peer, socket.socket)
                        if peer not in peers:
                            continue
                        try:
                            if events & selectors.EVENT_WRITE and outgoing and peer is control:
                                try:
                                    sent = peer.send(outgoing)
                                except BlockingIOError:
                                    sent = 0
                                del outgoing[:sent]
                                if not outgoing:
                                    selector.modify(peer, selectors.EVENT_READ)
                            if events & selectors.EVENT_READ:
                                try:
                                    raw = peer.recv(peers[peer].read_capacity)
                                except BlockingIOError:
                                    continue
                                if not raw:
                                    if peer is control and self.completed:
                                        close_peer(peer)
                                        control = None
                                        continue
                                    raise WatchdogProtocolError("Supervisor channel closed")
                                for frame in peers[peer].feed(raw):
                                    frame.validate_target(nonce=nonce, target_pid=self.child.pid)
                                    if control is None and listener_open:
                                        if frame.kind is not FrameKind.HELLO:
                                            raise WatchdogProtocolError("Supervisor requires HELLO")
                                        control = peer
                                        selector.unregister(listener)
                                        listener.close()
                                        listener_open = False
                                        for other in tuple(peers):
                                            if other is not peer:
                                                close_peer(other)
                                        queue(WatchdogFrame(FrameKind.WATCHING_ACK, nonce, self.child.pid))
                                    elif peer is not control or self.completed:
                                        raise WatchdogProtocolError("Unexpected supervisor frame")
                                    elif frame.kind is FrameKind.BEGIN_RECOVERY:
                                        self._arm()
                                        queue(
                                            WatchdogFrame(
                                                FrameKind.GRACE_ACK,
                                                nonce,
                                                self.child.pid,
                                                armed_at_ns=self.armed_at,
                                                deadline_ns=self.deadline,
                                            )
                                        )
                                    elif frame.kind is FrameKind.COMPLETE:
                                        self.completed = True
                                        exit_deadline = self.clock() + EXIT_NS
                                        self.deadline = min(self.deadline, exit_deadline) if self.deadline is not None else exit_deadline
                                        queue(WatchdogFrame(FrameKind.DISARM_ACK, nonce, self.child.pid))
                                    else:
                                        raise WatchdogProtocolError("Wrong supervisor frame direction")
                        except (OSError, WatchdogProtocolError):
                            if peer is control:
                                fail_channel()
                            elif peer in peers:
                                close_peer(peer)
            except BaseException:
                self._kill_and_reap()
                raise
            finally:
                for peer in tuple(peers):
                    close_peer(peer)


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("Private Composer supervisor requires an owned runner command\n")
        return 2
    try:
        return _Supervisor(sys.argv[1:], dedicated_cli=True).run()
    except (OSError, SupervisorFailure):
        sys.stderr.write("Private Composer supervisor startup or process custody failed\n")
        return 2


if __name__ == "__main__":
    result = main()
    if result < 0 and os.name != "nt":
        # Custody ended in main: the child has already been reaped. Reflect its
        # actual signal status to stdio clients observing this launcher Popen.
        terminated_by = -result
        if terminated_by != signal.SIGKILL:
            signal.signal(terminated_by, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {terminated_by})
        os.kill(os.getpid(), terminated_by)
    raise SystemExit(128 - result if result < 0 else result)
