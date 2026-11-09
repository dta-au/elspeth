"""Isolated probes using production custody and recovery, never a test owner."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def event(directory: Path, name: str) -> None:
    (directory / name).write_text(str(os.getpid()))


def supervise(directory: Path, mode: str, speed: int, ignored_chld: bool) -> int:
    import runpy

    namespace = runpy.run_path(str(ROOT / "src/elspeth/composer_mcp/portable_supervisor.py"))
    phase_started = None

    def clock():
        nonlocal phase_started
        now = time.monotonic_ns()
        if owner.armed_at is not None:
            return owner.armed_at + (now - owner.armed_at) * speed
        if owner.completed:
            if phase_started is None:
                phase_started = now
            return phase_started + (now - phase_started) * speed
        return now

    owner = namespace["_Supervisor"](
        [sys.executable, str(Path(__file__)), "child", str(directory), mode],
        _clock_ns=clock,
    )
    if ignored_chld:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("Outer owned-process probe expired")

    signal.signal(signal.SIGALRM, expired)
    signal.alarm(45 if speed == 1 else 12)
    try:
        result = owner.run()
        assert owner.child is not None
        (directory / "supervisor.json").write_text(
            json.dumps(
                {
                    "pid": owner.child.pid,
                    "returncode": owner.child.returncode,
                    "reaped": owner.child.poll() is not None,
                    "failed": owner.failed,
                    "completed": owner.completed,
                    "result": result,
                }
            )
        )
        return 128 - result if result < 0 else result
    finally:
        signal.alarm(0)


def raw_child(directory: Path, mode: str) -> int:
    import socket

    from elspeth.contracts.process_watchdog_wire import FrameKind, FrameReader, RecoveryReason, WatchdogFrame, encode_frame

    port = int(os.environ.pop("_ELSPETH_COMPOSER_CONTROL_PORT"))
    nonce = os.environ.pop("_ELSPETH_COMPOSER_CONTROL_NONCE")
    if mode == "peer-flood":
        held = []
        for _ in range(12):
            peer = socket.create_connection(("127.0.0.1", port), timeout=1)
            peer.sendall(b"\x00")
            held.append(peer)
        while True:
            time.sleep(1)
    if mode == "no-handshake":
        while True:
            time.sleep(1)
    channel = socket.create_connection(("127.0.0.1", port), timeout=1)
    channel.settimeout(1)
    reader = FrameReader()

    def send(kind, *, identity_nonce=nonce, pid=None):
        channel.sendall(
            encode_frame(
                WatchdogFrame(
                    kind,
                    identity_nonce,
                    os.getpid() if pid is None else pid,
                    RecoveryReason.NORMAL_SHUTDOWN if kind is FrameKind.BEGIN_RECOVERY else None,
                )
            )
        )

    def receive():
        while True:
            frames = reader.feed(channel.recv(reader.read_capacity))
            if frames:
                assert len(frames) == 1
                return frames[0]

    if mode == "wrong-nonce":
        send(FrameKind.HELLO, identity_nonce="0" * 64)
    elif mode == "wrong-pid":
        send(FrameKind.HELLO, pid=os.getppid())
    elif mode == "partial-hello":
        channel.sendall(b"\x00\x00")
    else:
        extra = None
        if mode == "extra-peer":
            extra = socket.create_connection(("127.0.0.1", port), timeout=1)
            extra.sendall(b"\x00")
        send(FrameKind.HELLO)
        assert receive().kind is FrameKind.WATCHING_ACK
        event(directory, "authenticated")
        if extra is not None:
            with suppress(ConnectionResetError):
                assert extra.recv(1) == b""
            extra.close()
        if mode == "wrong-direction":
            send(FrameKind.SHUTDOWN_REQUEST)
        elif mode == "reflected-pid":
            send(FrameKind.BEGIN_RECOVERY, pid=os.getppid())
        elif mode == "frame-flood":
            for _ in range(9):
                try:
                    send(FrameKind.BEGIN_RECOVERY)
                    receive()
                except (OSError, AssertionError):
                    break
        elif mode in ("replay", "partial-flood"):
            send(FrameKind.BEGIN_RECOVERY)
            first = receive()
            assert first.kind is FrameKind.GRACE_ACK
            event(directory, "armed")
            if mode == "replay":
                time.sleep(0.08)
                send(FrameKind.BEGIN_RECOVERY)
                assert receive() == first
                event(directory, "immutable")
            else:
                channel.sendall(b"\x00\x00\x03\xff")
                while True:
                    try:
                        channel.sendall(b" ")
                        time.sleep(0.025)
                    except OSError:
                        break
        elif mode == "channel-loss":
            channel.close()
        elif mode in ("extra-peer", "raw-normal"):
            send(FrameKind.COMPLETE)
            assert receive().kind is FrameKind.DISARM_ACK
            channel.close()
            return 0
    while True:
        time.sleep(1)


def child(directory: Path, mode: str) -> int:
    if mode in {
        "no-handshake",
        "peer-flood",
        "wrong-nonce",
        "wrong-pid",
        "partial-hello",
        "extra-peer",
        "wrong-direction",
        "reflected-pid",
        "frame-flood",
        "replay",
        "partial-flood",
        "channel-loss",
        "raw-normal",
    }:
        return raw_child(directory, mode)
    # This is deliberately the first application-facing import/call. Bootstrap
    # environment must disappear before runtime imports or worker construction.
    from elspeth.composer_mcp.portable_bootstrap import connect_supervisor

    connection = connect_supervisor()
    assert "_ELSPETH_COMPOSER_CONTROL_NONCE" not in os.environ
    assert "_ELSPETH_COMPOSER_CONTROL_PORT" not in os.environ
    import asyncio
    import atexit
    import threading

    from elspeth.composer_mcp.portable_control import PortableProcessWatchdog
    from elspeth.contracts.process_watchdog_wire import (
        BootstrapCompletionWitness,
        ProcessCompletionWitness,
        ProcessWatchdogTarget,
        RecoveryReason,
    )
    from elspeth.web.process_recovery import ProcessRecovery
    from elspeth.web.process_watchdog import ProcessWatchdogFailure

    draining = threading.Event()
    control = PortableProcessWatchdog(connection, draining)
    if mode in ("bootstrap", "no-root"):
        if mode == "no-root":
            try:
                control.assert_watching()
            except ProcessWatchdogFailure:
                event(directory, "unbound-refused")
            else:
                raise AssertionError("unbound root admitted workers")
        control.complete_bootstrap(BootstrapCompletionWitness(control.target))
        event(directory, "bootstrap-completed")
        return 0

    async def run():
        root = asyncio.current_task()
        assert root is not None
        control.bind_root_task(root)
        recovery = ProcessRecovery(watchdog=control, instance_draining=draining)
        recovery.start_monitor()
        event(directory, "ready")
        if mode == "wrong-witness":
            for witness in (
                BootstrapCompletionWitness(control.target),
                ProcessCompletionWitness(ProcessWatchdogTarget(control.target.pid, "0" * 64)),
                ProcessCompletionWitness(ProcessWatchdogTarget(control.target.pid + 1, control.target.nonce)),
            ):
                try:
                    await control.complete(witness)
                except ProcessWatchdogFailure:
                    pass
                else:
                    raise AssertionError("invalid completion witness accepted")
            try:
                control.complete_bootstrap(BootstrapCompletionWitness(control.target))
            except ProcessWatchdogFailure:
                pass
            else:
                raise AssertionError("bootstrap completed after root binding")
            event(directory, "witnesses-refused")
        if mode == "required-worker":
            from elspeth.web import async_workers

            async_workers.configure_required_executor_recovery(
                drain_seconds=0.1,
                instance_draining=draining,
                generation_unavailable=threading.Event(),
                recovery_callback=recovery.required_generation_expired,
            )
            entered = threading.Event()

            def blocked():
                entered.set()
                threading.Event().wait()

            worker = asyncio.create_task(async_workers.run_sync_in_worker(blocked))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            assert async_workers._GENERATION_CUSTODIAN is not None
            async_workers._GENERATION_CUSTODIAN.quarantine()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await recovery.join_escalation()
                assert recovery._observations
                event(directory, "required-observed")
                # The real executor join remains blocked. Withhold COMPLETE.
                await async_workers.shutdown_async_workers()
                await worker
                raise AssertionError("blocked worker unexpectedly settled") from None
        if mode in ("recovery-block-loop", "production-grace"):
            recovery.request_shutdown()
            with suppress(asyncio.CancelledError):
                await recovery.join_escalation()
            event(directory, "armed")
            while True:
                time.sleep(1)
        if mode in ("wait-stop", "unresponsive-stop", "supervisor-loss"):
            if mode == "unresponsive-stop":
                while True:
                    time.sleep(1)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                assert draining.is_set()
                cancellations = root.cancelling()
                control.request_graceful_termination()
                control.request_graceful_termination()
                assert root.cancelling() == cancellations
                event(directory, "stopped")
        if mode == "repeated-cancel":
            begin = asyncio.create_task(control.begin_recovery(RecoveryReason.NORMAL_SHUTDOWN))
            await asyncio.sleep(0)
            begin.cancel()
            begin.cancel()
            with suppress(asyncio.CancelledError):
                await begin
            first = await control.begin_recovery(RecoveryReason.REQUIRED_WORKER_LOST)
            assert await control.begin_recovery(RecoveryReason.NORMAL_SHUTDOWN) is first
            event(directory, "retained")
        recovery.begin_shutdown()
        await recovery.join_escalation()
        if mode == "cancel-completion":
            completion = asyncio.create_task(control.complete(ProcessCompletionWitness(control.target)))
            await asyncio.sleep(0)
            completion.cancel()
            completion.cancel()
            with suppress(asyncio.CancelledError):
                await completion
        await control.complete(ProcessCompletionWitness(control.target))
        await recovery.join_monitor_after_completion()
        event(directory, "completed")

    asyncio.run(run())
    if mode == "atexit-hang":
        atexit.register(lambda: threading.Event().wait())
    if mode == "stdio":
        sys.stdout.write("MCP-PROBE\n")
    return 7 if mode == "exit-seven" else 0


if __name__ == "__main__":
    role, directory_text, mode, *options = sys.argv[1:]
    directory = Path(directory_text)
    if role == "supervisor":
        raise SystemExit(supervise(directory, mode, int(options[0]), len(options) > 1))
    raise SystemExit(child(directory, mode))
