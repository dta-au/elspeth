"""Private watchdog protocol controls and signals isolated from the test process."""

import json
import os
import signal
import subprocess
import sys
import threading

import pytest

from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.process_watchdog import BootstrapCompletionWitness, ProcessCompletionWitness, ProcessWatchdogFailure
from elspeth.web.process_watchdog_codec import (
    FRAME_LIMIT,
    GRACE_NS,
    FrameKind,
    FrameReader,
    RecoveryReason,
    WatchdogFrame,
    WatchdogProtocolError,
    encode_frame,
)
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


@pytest.mark.parametrize("kind", tuple(FrameKind))
def test_closed_codec_roundtrip(kind):
    frame = WatchdogFrame(
        kind,
        "a" * 64,
        123,
        RecoveryReason.NORMAL_SHUTDOWN if kind is FrameKind.BEGIN_RECOVERY else None,
        1 if kind is FrameKind.GRACE_ACK else None,
        1 + GRACE_NS if kind is FrameKind.GRACE_ACK else None,
    )
    raw = encode_frame(frame)
    assert len(raw) - 4 <= FRAME_LIMIT
    reader = FrameReader()
    assert reader.feed(raw[:3]) == ()
    assert reader.feed(raw[3:]) == (frame,)
    with pytest.raises(WatchdogProtocolError):
        frame.validate_target(nonce="b" * 64, target_pid=123)
    with pytest.raises(WatchdogProtocolError):
        frame.validate_target(nonce="a" * 64, target_pid=124)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"v":1,"v":1}',
        b"[]",
        b'{"v":true,"kind":"HELLO"}',
        b'{"v":1.0,"kind":"HELLO"}',
        b'{"v":NaN}',
        b"\xff",
        json.dumps({"v": 1, "kind": "HELLO", "nonce": "a" * 64, "target_pid": 1, "grace_ns": GRACE_NS, "extra": 0}).encode(),
        json.dumps({"v": 1, "kind": "HELLO", "nonce": "a" * 64, "target_pid": True, "grace_ns": GRACE_NS}).encode(),
    ],
)
def test_codec_refuses_foreign_shape(payload):
    with pytest.raises(WatchdogProtocolError):
        FrameReader().feed(len(payload).to_bytes(4, "big") + payload)


def test_codec_buffer_lengths_and_frame_count_bound():
    for length in (0, FRAME_LIMIT + 1, 2**32 - 1):
        with pytest.raises(WatchdogProtocolError):
            FrameReader().feed(length.to_bytes(4, "big"))
    reader = FrameReader()
    raw = encode_frame(WatchdogFrame(FrameKind.HELLO, "a" * 64, 1))
    for _ in range(8):
        reader.feed(raw)
    with pytest.raises(WatchdogProtocolError):
        reader.feed(raw)


@pytest.mark.asyncio
async def test_recovery_ack_precedes_signal_draining_and_idempotence():
    event = threading.Event()
    watchdog = OwnedTestProcessWatchdog(event)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=event)
    recovery.start_monitor()
    recovery.request_shutdown()
    assert event.is_set()
    assert watchdog.signals == []
    await recovery.join_escalation()
    assert watchdog.signals == [signal.SIGTERM]
    recovery.request_shutdown()
    recovery.begin_shutdown()
    await recovery.join_escalation()
    assert watchdog.reasons == [RecoveryReason.REQUIRED_WORKER_LOST]
    await watchdog.complete(ProcessCompletionWitness(watchdog.target))
    await recovery.join_monitor_after_completion()


@pytest.mark.asyncio
async def test_begin_shutdown_arms_without_second_term_and_safe_helper_failure():
    event = threading.Event()
    watchdog = OwnedTestProcessWatchdog(event)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=event)
    recovery.start_monitor()
    recovery.begin_shutdown()
    await recovery.join_escalation()
    assert watchdog.signals == []
    watchdog.fail(ProcessWatchdogFailure("fixed"))
    with pytest.raises(ProcessWatchdogFailure):
        await recovery.join_monitor_after_completion()
    assert watchdog.signals == [signal.SIGKILL]


def test_sync_bootstrap_fake_never_schedules_or_signals():
    watchdog = OwnedTestProcessWatchdog(threading.Event())
    watchdog.abort_bootstrap(RecoveryReason.FAILED_STARTUP)
    assert watchdog.draining.is_set() and not watchdog.completed
    assert watchdog.signals == []
    untouched = OwnedTestProcessWatchdog(threading.Event())
    untouched.complete_bootstrap(BootstrapCompletionWitness(untouched.target))
    assert untouched.completed


def test_real_watchdog_normal_disarm_targets_only_owned_child(tmp_path):
    script = tmp_path / "owned_child.py"
    script.write_text("""import asyncio, threading
from elspeth.web.process_watchdog import create_process_watchdog, ProcessCompletionWitness
async def main():
    control=create_process_watchdog(threading.Event())
    control.assert_watching()
    print("WATCHING",flush=True)
    await control.complete(ProcessCompletionWitness(control.target))
    print("JOINED",flush=True)
asyncio.run(main())
""")
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    out, err = child.communicate(timeout=10)
    assert child.returncode == 0, err.decode()
    assert out == b"WATCHING\nJOINED\n"


def test_real_watchdog_armed_channel_loss_kills_only_owned_child(tmp_path):
    script = tmp_path / "owned_hung_child.py"
    script.write_text("""import asyncio, threading, time
from elspeth.web.process_watchdog import create_process_watchdog
from elspeth.web.process_watchdog_codec import RecoveryReason
async def main():
    control=create_process_watchdog(threading.Event())
    ack=await control.begin_recovery(RecoveryReason.REQUIRED_GENERATION_UNRESOLVED)
    print("ACK",flush=True)
    thread=threading.Thread(target=lambda:threading.Event().wait(),daemon=False)
    thread.start()
    while True: time.sleep(1)
asyncio.run(main())
""")
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    try:
        out, err = child.communicate(timeout=38)
        assert out == b"ACK\n", err.decode()
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_repeated_caller_cancel_joins_actual_watchdog_ack(fail):
    import asyncio

    class HeldWatchdog(OwnedTestProcessWatchdog):
        def __init__(self, draining):
            super().__init__(draining)
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.handshake_failure = ProcessWatchdogFailure("fixed owned handshake failure")

        async def begin_recovery(self, reason):
            self.entered.set()
            await self.release.wait()
            if fail:
                raise self.handshake_failure
            return await super().begin_recovery(reason)

    draining = threading.Event()
    watchdog = HeldWatchdog(draining)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
    recovery.request_shutdown()
    join = asyncio.create_task(recovery.join_escalation())
    await watchdog.entered.wait()
    join.cancel("first")
    await asyncio.sleep(0)
    join.cancel("second")
    await asyncio.sleep(0)
    assert not join.done()
    assert not recovery._escalation_task.done()
    watchdog.release.set()
    if fail:
        with pytest.raises(BaseExceptionGroup) as caught:
            await join
        assert caught.value.exceptions[0] is watchdog.handshake_failure
        assert isinstance(caught.value.exceptions[1], asyncio.CancelledError)
        assert watchdog.signals == [signal.SIGKILL]
    else:
        with pytest.raises(asyncio.CancelledError):
            await join
        assert watchdog.signals == [signal.SIGTERM]
    assert recovery._escalation_task.done()
