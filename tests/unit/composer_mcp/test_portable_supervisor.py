"""Private portable owner controls; destructive faults stay in owned children."""

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from elspeth.web.process_watchdog_codec import FrameReader, encode_frame

ROOT = Path(__file__).resolve().parents[3]
PROBE = ROOT / "tests/helpers/portable_supervisor_probe.py"


def read_proc_stat(pid):
    """Fields of /proc/<pid>/stat, or None once the entry is gone.

    A zombie can be reaped between any existence check and the read that
    follows it, so existence is never tested separately: the read itself
    reports it (FileNotFoundError, or ProcessLookupError on a vanishing task).
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    head, tail = text.rsplit(")", 1)  # comm may hold spaces; keep proc(5) field numbering
    return [head.split()[0], head.split("(", 1)[1], *tail.split()]


def test_read_proc_stat_reports_a_vanished_entry_as_none_not_an_error(monkeypatch):
    # Both ways a reaped zombie surfaces on a read that followed a successful
    # existence check: the file is gone, or the task vanished mid-read.
    assert read_proc_stat(2**22 + 1) is None
    original = Path.read_text

    def vanishing(self, *args, **kwargs):
        if self.name == "stat" and self.parent.name == str(os.getpid()):
            raise ProcessLookupError(3, "No such process")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", vanishing)
    assert read_proc_stat(os.getpid()) is None
    monkeypatch.undo()
    fields = read_proc_stat(os.getpid())
    assert fields[0] == str(os.getpid()) and fields[2] in {"R", "S"} and len(fields) > 51


def probe_evidence(directory):
    evidence = []
    for name in ("timeline.jsonl", "diagnostics.txt"):
        try:
            content = (directory / name).read_text(errors="replace")
        except OSError:
            content = "unreadable"
        evidence.append(f"{name}:\n{content}")
    return "\n".join(evidence)


def launch(directory, mode, *, speed=1, ignored_chld=False):
    command = [sys.executable, str(PROBE), "supervisor", str(directory), mode, str(speed)]
    if ignored_chld:
        command.append("ignored-chld")
    return subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy(), start_new_session=os.name == "posix"
    )


def finish(process, directory, *, timeout=15):
    # The probe's shorter outer alarm unwinds the real production owner, which
    # kills/reaps its own Popen child before this outer process timeout expires.
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        from tests.helpers.process_diagnostics import linux_process_snapshot

        proc = linux_process_snapshot(process.pid)
        if process.poll() is None:
            if sys.platform == "linux":
                # The real owner launches its runner in another session. The
                # kernel's direct-child list belongs to this retained Popen;
                # event-file PIDs must never become signal authority.
                try:
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children").read_text().split()
                except OSError as read_error:
                    proc += f"\nowned child lookup unreadable: {read_error}"
                    children = []
                for child in children:
                    child_pid = int(child)
                    try:
                        status = Path(f"/proc/{child_pid}/status").read_text().splitlines()
                        if f"PPid:\t{process.pid}" not in status:
                            continue  # The owner exited or custody changed.
                        proc += f"\nrunner pid={child_pid} {linux_process_snapshot(child_pid)}"
                        if os.getpgid(child_pid) == child_pid:
                            os.killpg(child_pid, signal.SIGKILL)
                        else:
                            os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The owner reaped its runner during observation.
                    except OSError as child_error:
                        proc += f"\nowned child cleanup failed: {child_error}"
            try:
                if os.name == "posix" and os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass  # It exited between poll and kill.
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as cleanup_error:
            out, err = cleanup_error.output or b"", cleanup_error.stderr or b""
        (directory / "stdout").write_bytes(out)
        (directory / "stderr").write_bytes(err)
        raise AssertionError(
            f"Portable supervisor timed out after {timeout}s; returncode={process.poll()}\n{proc}\n"
            + probe_evidence(directory)
            + f"\nstderr:\n{err.decode(errors='replace')}"
        ) from error
    (directory / "stdout").write_bytes(out)
    (directory / "stderr").write_bytes(err)
    (directory / "exit").write_text(str(process.returncode))
    assert not err, f"{probe_evidence(directory)}\nstderr:\n{err.decode(errors='replace')}"
    return out, json.loads((directory / "supervisor.json").read_text())


def wait_event(process, directory, name):
    deadline = time.monotonic() + 5
    while not (directory / name).exists():
        assert process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_finish_timeout_retains_timeline_stderr_and_reaps_process(tmp_path):
    (tmp_path / "timeline.jsonl").write_text("controlled child-started timeline\n")
    (tmp_path / "diagnostics.txt").write_text("controlled alarm diagnostics\n")
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; print('controlled stderr', file=sys.stderr, flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        with pytest.raises(AssertionError) as failure:
            finish(process, tmp_path, timeout=0.5)
        message = str(failure.value)
        assert "controlled child-started timeline" in message
        assert "controlled alarm diagnostics" in message
        assert "controlled stderr" in message
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="detached runner custody control reads Linux proc")
def test_finish_timeout_also_stops_runner_in_separate_session(tmp_path):
    command = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True); "
        "print(child.pid, flush=True); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", command], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    runner_pid = None
    try:
        assert process.stdout is not None
        runner_pid = int(process.stdout.readline())
        with pytest.raises(AssertionError, match="Portable supervisor timed out"):
            finish(process, tmp_path, timeout=0.5)
        assert process.poll() is not None
        deadline = time.monotonic() + 5
        while (fields := read_proc_stat(runner_pid)) is not None and fields[2] != "Z":
            assert time.monotonic() < deadline, "detached runner survived probe timeout cleanup"
            time.sleep(0.01)
    finally:
        if runner_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(runner_pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_probe_alarm_records_owner_proc_and_stacks(tmp_path, monkeypatch):
    import runpy

    from tests.helpers import portable_supervisor_probe as probe

    child_pids = []

    class AlarmOwner:
        def __init__(self, command, *, _clock_ns):
            self.child = None
            self.armed_at = None
            self.deadline = None
            self.stop_requested = False
            self.failed = False
            self.completed = False

        def run(self):
            self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            child_pids.append(self.child.pid)
            try:
                signal.raise_signal(signal.SIGALRM)
            finally:
                self.child.kill()
                self.child.wait(timeout=5)

    monkeypatch.setattr(runpy, "run_path", lambda path: {"_Supervisor": AlarmOwner})
    previous = signal.getsignal(signal.SIGALRM)
    try:
        with pytest.raises(TimeoutError, match="Outer owned-process probe expired"):
            probe.supervise(tmp_path, "early-stop-abort", 20, False)
    finally:
        signal.signal(signal.SIGALRM, previous)
    diagnostics = (tmp_path / "diagnostics.txt").read_text()
    assert "elapsed=" in diagnostics and "stop_requested=True" in diagnostics
    assert f"child_pid={child_pids[0]}" in diagnostics and "proc:" in diagnostics
    assert f"threads=[{child_pids[0]}:" in diagnostics
    assert "test_probe_alarm_records_owner_proc_and_stacks" in diagnostics


def test_probe_records_child_import_and_connection_timeline(tmp_path):
    process = launch(tmp_path, "bootstrap")
    finish(process, tmp_path)
    timeline = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
    assert [entry["event"] for entry in timeline] == ["child-started", "imports-done", "connected", "bootstrap-completed"]
    assert all(entry["pid"] > 0 for entry in timeline)
    assert [entry["monotonic_ns"] for entry in timeline] == sorted(entry["monotonic_ns"] for entry in timeline)


def test_child_start_is_recorded_before_application_import_failure(tmp_path, monkeypatch):
    import builtins

    from tests.helpers import portable_supervisor_probe as probe

    original_import = builtins.__import__

    def fail_bootstrap_import(name, *args, **kwargs):
        if name == "elspeth.composer_mcp.portable_bootstrap":
            assert (tmp_path / "child-started").exists()
            raise ModuleNotFoundError("controlled first application import failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_bootstrap_import)
    with pytest.raises(ModuleNotFoundError, match="controlled first application import failure"):
        probe.child(tmp_path, "bootstrap")
    timeline = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
    assert [entry["event"] for entry in timeline] == ["child-started"]
    assert timeline[0]["pid"] == int((tmp_path / "child-started").read_text())


def test_child_timeline_with_controlled_bootstrap_transport(tmp_path, monkeypatch):
    from elspeth.composer_mcp import portable_bootstrap, portable_control
    from elspeth.contracts.process_watchdog_wire import ProcessWatchdogTarget
    from tests.helpers import portable_supervisor_probe as probe

    connection = object()
    target = ProcessWatchdogTarget(os.getpid(), "a" * 64)
    completed = []

    class BootstrapControl:
        def __init__(self, received, draining):
            assert received is connection
            self.target = target

        def complete_bootstrap(self, witness):
            completed.append(witness.target)

    monkeypatch.setattr(portable_bootstrap, "connect_supervisor", lambda: connection)
    monkeypatch.setattr(portable_control, "PortableProcessWatchdog", BootstrapControl)
    assert probe.child(tmp_path, "bootstrap") == 0
    assert completed == [target]
    timeline = [json.loads(line) for line in (tmp_path / "timeline.jsonl").read_text().splitlines()]
    assert [entry["event"] for entry in timeline] == ["child-started", "imports-done", "connected", "bootstrap-completed"]
    assert all(entry["pid"] == os.getpid() for entry in timeline)
    assert [entry["monotonic_ns"] for entry in timeline] == sorted(entry["monotonic_ns"] for entry in timeline)


def test_finish_alarm_failure_includes_saved_evidence(tmp_path):
    (tmp_path / "timeline.jsonl").write_text("controlled alarm timeline\n")
    (tmp_path / "diagnostics.txt").write_text("controlled alarm stacks\n")
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('controlled alarm stderr', file=sys.stderr); raise SystemExit(1)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    with pytest.raises(AssertionError) as failure:
        finish(process, tmp_path)
    assert "controlled alarm timeline" in str(failure.value)
    assert "controlled alarm stacks" in str(failure.value)
    assert "controlled alarm stderr" in str(failure.value)


def test_shutdown_intent_has_closed_roundtrip():
    # Positive HELLO prevents vacuous rejection by a broken parser.
    from elspeth.web.process_watchdog_codec import FrameKind, WatchdogFrame

    hello = WatchdogFrame(FrameKind.HELLO, "a" * 64, 1)
    assert FrameReader().feed(encode_frame(hello)) == (hello,)
    payload = json.dumps({"v": 1, "kind": "SHUTDOWN_REQUEST", "nonce": "a" * 64, "target_pid": 1}).encode()
    (frame,) = FrameReader().feed(len(payload).to_bytes(4, "big") + payload)
    assert frame.kind.value == "SHUTDOWN_REQUEST"
    assert encode_frame(frame) == len(payload.replace(b" ", b"")).to_bytes(4, "big") + payload.replace(b" ", b"")


@pytest.mark.parametrize("extra", [{"completion_seq": 1}, {"reason": "NORMAL_SHUTDOWN"}, {"grace_ns": 30_000_000_000}])
def test_shutdown_intent_refuses_non_schema_fields(extra):
    from elspeth.web.process_watchdog_codec import WatchdogProtocolError

    payload = json.dumps({"v": 1, "kind": "SHUTDOWN_REQUEST", "nonce": "a" * 64, "target_pid": 1, **extra}).encode()
    with pytest.raises(WatchdogProtocolError):
        FrameReader().feed(len(payload).to_bytes(4, "big") + payload)


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "bootstrap",
        "no-root",
        "wrong-witness",
        "repeated-cancel",
        "cancel-completion",
        "extra-peer",
        "raw-normal",
        "stdio",
        "exit-seven",
    ],
)
def test_real_owned_completion_and_status(tmp_path, mode):
    process = launch(tmp_path, mode)
    out, receipt = finish(process, tmp_path)
    expected = 7 if mode == "exit-seven" else 0
    assert process.returncode == receipt["result"] == expected
    assert receipt["returncode"] == expected
    assert receipt["completed"] and receipt["reaped"] and not receipt["failed"]
    assert out == (b"MCP-PROBE\n" if mode == "stdio" else b"")
    if mode == "wrong-witness":
        assert (tmp_path / "witnesses-refused").exists()
    if mode == "repeated-cancel":
        assert (tmp_path / "retained").exists()
    if mode == "no-root":
        assert (tmp_path / "unbound-refused").exists()


@pytest.mark.parametrize(
    "mode",
    ["wrong-nonce", "wrong-pid", "partial-hello", "no-handshake", pytest.param("peer-flood", marks=pytest.mark.timing_sensitive)],
)
def test_authentication_and_partial_startup_fail_closed(tmp_path, mode):
    process = launch(tmp_path, mode)
    out, receipt = finish(process, tmp_path)
    assert process.returncode != 0 and out == b""
    assert receipt["returncode"] == -signal.SIGKILL
    assert receipt["failed"] and receipt["reaped"] and not receipt["completed"]
    assert not (tmp_path / "authenticated").exists()


@pytest.mark.parametrize(
    "mode",
    [
        "wrong-direction",
        "reflected-pid",
        "frame-flood",
        "replay",
        "partial-flood",
        "channel-loss",
        "recovery-block-loop",
        "required-worker",
    ],
)
@pytest.mark.timing_sensitive
def test_deadline_survives_real_protocol_and_child_faults(tmp_path, mode):
    started = time.monotonic()
    process = launch(tmp_path, mode, speed=50)
    out, receipt = finish(process, tmp_path)
    assert process.returncode != 0 and out == b""
    assert receipt["returncode"] == -signal.SIGKILL
    assert receipt["failed"] and receipt["reaped"] and not receipt["completed"]
    assert time.monotonic() - started < 5
    if mode == "replay":
        assert (tmp_path / "immutable").exists()
    if mode == "required-worker":
        assert (tmp_path / "required-observed").exists()


@pytest.mark.timing_sensitive
def test_production_grace_independently_kills_stuck_loop(tmp_path):
    process = launch(tmp_path, "production-grace", speed=1)
    wait_event(process, tmp_path, "armed")
    armed = time.monotonic()
    out, receipt = finish(process, tmp_path, timeout=40)
    elapsed = time.monotonic() - armed
    assert 28 <= elapsed <= 35
    assert out == b"" and process.returncode != 0
    assert receipt["returncode"] == -signal.SIGKILL and receipt["reaped"]


@pytest.mark.parametrize("mode", ["wait-stop", "unresponsive-stop"])
def test_catchable_parent_stop_is_channel_intent_then_exact_kill(tmp_path, mode):
    process = launch(tmp_path, mode, speed=50 if mode == "unresponsive-stop" else 1)
    wait_event(process, tmp_path, "ready")
    process.send_signal(signal.SIGTERM)
    process.send_signal(signal.SIGINT)
    out, receipt = finish(process, tmp_path)
    assert out == b"" and receipt["reaped"]
    if mode == "wait-stop":
        assert process.returncode == 0
        assert (tmp_path / "stopped").exists() and (tmp_path / "completed").exists()
    else:
        assert process.returncode != 0 and receipt["returncode"] == -signal.SIGKILL


@pytest.mark.timing_sensitive
@pytest.mark.parametrize(
    "mode",
    [
        "early-stop-responsive",
        "early-stop-unresolved",
        "early-stop-bootstrap",
        "early-stop-abort",
        "early-stop-fragmented",
        "early-stop-signal",
        "early-stop-fragmented-bootstrap",
        "early-stop-fragmented-abort",
    ],
)
def test_stop_before_hello_retains_shutdown_through_handoff(tmp_path, mode):
    unresolved = mode in ("early-stop-unresolved", "early-stop-abort", "early-stop-fragmented-abort")
    process = launch(tmp_path, mode, speed=20 if unresolved else 1)
    if mode == "early-stop-signal":
        wait_event(process, tmp_path, "before-hello")
        process.send_signal(signal.SIGTERM)
        (tmp_path / "release-hello").touch()
    out, receipt = finish(process, tmp_path)
    assert out == b"" and receipt["reaped"]
    assert (tmp_path / "connected").exists()
    if unresolved:
        assert process.returncode != 0 and receipt["returncode"] == -signal.SIGKILL
        assert not receipt["completed"]
        if mode in ("early-stop-abort", "early-stop-fragmented-abort"):
            assert (tmp_path / "bootstrap-armed").exists()
    else:
        assert process.returncode == 0 and receipt["completed"] and not receipt["failed"]
        assert (tmp_path / ("bootstrap-completed" if mode.endswith("bootstrap") else "stopped")).exists()
    if mode.startswith("early-stop-fragmented"):
        assert (tmp_path / "fragmented").exists()


@pytest.mark.parametrize("kind", ["HELLO", "BEGIN_RECOVERY", "COMPLETE"])
@pytest.mark.parametrize("prefix", [0, 2, 1000], ids=["ack-only", "fragmented-stop", "complete-stop"])
def test_sync_control_stream_retains_decoder_and_shutdown_across_claim(kind, prefix):
    """Socketpair protocol seam; Popen controls above establish real custody."""
    import selectors
    import socket
    import threading

    from elspeth.composer_mcp.portable_bootstrap import _BOOTSTRAP_AUTHORITY, AuthenticatedSupervisor, _ControlStream, exchange_sync
    from elspeth.contracts.process_watchdog_wire import GRACE_NS, FrameKind, ProcessWatchdogTarget, RecoveryReason, WatchdogFrame

    target = ProcessWatchdogTarget(1, "a" * 64)
    request_kind = FrameKind(kind)
    request = WatchdogFrame(
        request_kind, target.nonce, target.pid, RecoveryReason.NORMAL_SHUTDOWN if request_kind is FrameKind.BEGIN_RECOVERY else None
    )
    reply_kind = {
        FrameKind.HELLO: FrameKind.WATCHING_ACK,
        FrameKind.BEGIN_RECOVERY: FrameKind.GRACE_ACK,
        FrameKind.COMPLETE: FrameKind.DISARM_ACK,
    }[request_kind]
    reply = WatchdogFrame(
        reply_kind,
        target.nonce,
        target.pid,
        armed_at_ns=1 if reply_kind is FrameKind.GRACE_ACK else None,
        deadline_ns=1 + GRACE_NS if reply_kind is FrameKind.GRACE_ACK else None,
    )
    shutdown = encode_frame(WatchdogFrame(FrameKind.SHUTDOWN_REQUEST, target.nonce, target.pid))
    errors = []
    channel, peer = socket.socketpair()
    channel.setblocking(False)
    peer.settimeout(1)
    stream = _ControlStream(channel, target)

    def respond():
        try:
            reader = FrameReader()
            frames = ()
            while not frames:
                frames = reader.feed(peer.recv(reader.read_capacity))
            assert frames == (request,)
            peer.sendall(encode_frame(reply) + shutdown[:prefix])
        except BaseException as error:
            errors.append(error)

    writer = threading.Thread(target=respond)
    writer.start()
    try:
        assert exchange_sync(stream, request) == reply
        connection = AuthenticatedSupervisor(stream, _BOOTSTRAP_AUTHORITY)
        claimed = connection.claim()
        assert claimed is stream
        assert claimed.shutdown_requested == (prefix >= len(shutdown))
        if prefix == 2:
            assert claimed.read_capacity == 1026
            peer.sendall(shutdown[prefix:])
            with selectors.DefaultSelector() as selector:
                selector.register(channel, selectors.EVENT_READ)
                assert selector.select(1)
            claimed.feed(channel.recv(claimed.read_capacity))
            assert claimed.shutdown_requested
        assert claimed.take_replies() == ()
        with pytest.raises(RuntimeError, match="already claimed"):
            connection.claim()
    finally:
        writer.join(timeout=2)
        channel.close()
        peer.close()
    assert not writer.is_alive() and not errors


@pytest.mark.parametrize("fault", ["nonce", "pid", "direction", "count"])
def test_retained_stream_preserves_identity_direction_and_global_frame_bound(fault):
    import socket

    from elspeth.composer_mcp.portable_bootstrap import _BOOTSTRAP_AUTHORITY, AuthenticatedSupervisor, _ControlStream
    from elspeth.contracts.process_watchdog_wire import FrameKind, ProcessWatchdogTarget, WatchdogFrame, WatchdogProtocolError

    with socket.socket() as channel:
        target = ProcessWatchdogTarget(1, "a" * 64)
        stream = _ControlStream(channel, target)
        ack = WatchdogFrame(FrameKind.WATCHING_ACK, target.nonce, target.pid)
        stream.feed(encode_frame(ack))
        assert stream.take_replies() == (ack,)
        claimed = AuthenticatedSupervisor(stream, _BOOTSTRAP_AUTHORITY).claim()
        kind = FrameKind.HELLO if fault == "direction" else FrameKind.SHUTDOWN_REQUEST
        frame = WatchdogFrame(kind, "b" * 64 if fault == "nonce" else target.nonce, 2 if fault == "pid" else target.pid)
        if fault == "count":
            for _ in range(7):
                claimed.feed(encode_frame(frame))
            assert claimed.shutdown_requested
        with pytest.raises(WatchdogProtocolError):
            claimed.feed(encode_frame(frame))
        if fault != "count":
            assert not claimed.shutdown_requested


def test_complete_does_not_strand_parent_in_atexit(tmp_path):
    process = launch(tmp_path, "atexit-hang")
    out, receipt = finish(process, tmp_path)
    assert out == b"" and process.returncode != 0
    assert (tmp_path / "completed").exists()
    assert receipt["completed"] and receipt["failed"] and receipt["returncode"] == -signal.SIGKILL


def test_inherited_sigchld_ignore_is_reset_before_actual_spawn(tmp_path):
    process = launch(tmp_path, "exit-seven", ignored_chld=True)
    out, receipt = finish(process, tmp_path)
    assert out == b"" and process.returncode == 7
    assert receipt["returncode"] == 7 and receipt["reaped"]


@pytest.mark.timing_sensitive
def test_healthy_child_fails_closed_when_supervisor_is_killed(tmp_path):
    process = launch(tmp_path, "supervisor-loss")
    wait_event(process, tmp_path, "ready")
    child_pid = int((tmp_path / "ready").read_text())
    process.kill()  # Only the actual Popen-owned supervisor, never a received PID.
    out, err = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL
    assert out == err == b""
    deadline = time.monotonic() + 5
    while (fields := read_proc_stat(child_pid)) is not None and fields[2] != "Z":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    # Its supervisor was destroyed: adoption/reaping belongs to init, so this
    # establishes actual exit, not a claim that the dead supervisor reaped it.
    assert not (tmp_path / "completed").exists()
    # Init may reap the zombie at any moment: the exit code is only observable
    # while the entry still exists, and a reaped child is an exited child.
    if (fields := read_proc_stat(child_pid)) is not None:
        assert int(fields[51]) == 70 << 8


def test_private_bootstrap_missing_authority_refuses_and_consumes_environment(tmp_path):
    code = """import os
from elspeth.composer_mcp.portable_bootstrap import connect_supervisor
os.environ['_ELSPETH_COMPOSER_CONTROL_NONCE']='invalid'
try:
    connect_supervisor()
except Exception:
    assert '_ELSPETH_COMPOSER_CONTROL_NONCE' not in os.environ
    assert '_ELSPETH_COMPOSER_CONTROL_PORT' not in os.environ
else:
    raise AssertionError('missing authority admitted')
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
    assert result.returncode == 0 and result.stdout == result.stderr == b""


def test_dedicated_supervisor_has_stdlib_only_startup_and_clean_stdout():
    script = str(ROOT / "src/elspeth/composer_mcp/portable_supervisor.py")
    absent = subprocess.run([sys.executable, "-S", script], capture_output=True, timeout=5)
    assert absent.returncode == 2 and absent.stdout == b""
    assert absent.stderr == b"Private Composer supervisor requires an owned runner command\n"
    exited = subprocess.run([sys.executable, "-S", script, sys.executable, "-c", "raise SystemExit(7)"], capture_output=True, timeout=5)
    assert exited.returncode == 7 and exited.stdout == exited.stderr == b""
    signaled = subprocess.run(
        [sys.executable, "-S", script, sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        capture_output=True,
        timeout=5,
    )
    assert signaled.returncode == -signal.SIGTERM and signaled.stdout == signaled.stderr == b""


@pytest.mark.parametrize("windows", [False, True])
def test_native_launch_adapter_pins_group_and_inherited_stdio(monkeypatch, windows):
    # Pure adapter evidence: this does not execute Windows or qualify its OS.
    from elspeth.composer_mcp import portable_supervisor as production

    recorded = []
    sentinel = object()

    def popen(command, **options):
        recorded.append((command, options))
        return sentinel

    monkeypatch.setattr(production.subprocess, "Popen", popen)
    command = ["private-runner"]
    environment = {"CONTROL": "private-test"}
    assert production._launch_child(command, environment, windows=windows) is sentinel
    assert recorded == [
        (
            command,
            {
                "env": environment,
                "close_fds": True,
                "creationflags": 0x200 if windows else 0,
                "start_new_session": not windows,
            },
        )
    ]


@pytest.mark.parametrize("status", [None, 0, 7, -9])
def test_exact_retained_process_adapter_never_signals_observed_exit(status):
    from elspeth.composer_mcp.portable_supervisor import _Supervisor

    calls = []

    class RetainedProcess:
        def poll(self):
            calls.append("poll")
            return status

        def kill(self):
            calls.append("kill-retained-handle")

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            return -9

    owner = _Supervisor(["unused"])
    owner.child = RetainedProcess()
    assert owner._kill_and_reap() == (-9 if status is None else status)
    assert calls == (["poll", "kill-retained-handle", ("wait", 1.0)] if status is None else ["poll"])


def test_termination_failure_is_visible_and_never_completion():
    from elspeth.composer_mcp.portable_supervisor import SupervisorFailure, _Supervisor

    class FailedProcess:
        def poll(self):
            return None

        def kill(self):
            raise OSError("controlled handle failure")

    owner = _Supervisor(["unused"])
    owner.child = FailedProcess()
    with pytest.raises(SupervisorFailure, match="termination/reap failed"):
        owner._kill_and_reap()
    assert not owner.completed


def test_first_deadline_and_completion_exit_bound_never_extend():
    from elspeth.composer_mcp.portable_supervisor import GRACE_NS, _Supervisor

    now = 1
    owner = _Supervisor(["unused"], _clock_ns=lambda: now)
    owner._arm()
    first = owner.armed_at, owner.deadline
    now = 100
    owner._arm()
    assert (owner.armed_at, owner.deadline) == first == (1, 1 + GRACE_NS)
    post_complete = _Supervisor(["unused"], _clock_ns=lambda: now)
    post_complete.deadline = 200
    post_complete._arm()
    assert post_complete.deadline == 200


def test_web_compatibility_exports_preserve_nominal_identity():
    from elspeth.contracts import process_watchdog_wire as shared
    from elspeth.web import process_watchdog as web
    from elspeth.web import process_watchdog_codec as codec

    assert web.ProcessCompletionWitness is shared.ProcessCompletionWitness
    assert web.BootstrapCompletionWitness is shared.BootstrapCompletionWitness
    assert web.ProcessWatchdogTarget is shared.ProcessWatchdogTarget
    assert web.GraceAcknowledgement is shared.GraceAcknowledgement
    assert codec.WatchdogFrame is shared.WatchdogFrame
    assert codec.FrameKind is shared.FrameKind
    assert codec.RecoveryReason is shared.RecoveryReason


def test_linux_web_helper_still_rejects_portable_shutdown_intent(tmp_path):
    import socket

    from elspeth.web.process_watchdog_codec import FrameKind, WatchdogFrame

    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pidfd = os.pidfd_open(target.pid, 0)
    parent, child = socket.socketpair()
    helper = subprocess.Popen(
        [
            sys.executable,
            "-S",
            str(ROOT / "src/elspeth/web/process_watchdog_helper.py"),
            str(child.fileno()),
            str(pidfd),
        ],
        pass_fds=(child.fileno(), pidfd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child.close()
    os.close(pidfd)
    try:
        parent.settimeout(2)
        parent.sendall(encode_frame(WatchdogFrame(FrameKind.HELLO, "a" * 64, target.pid)))
        assert FrameReader().feed(parent.recv(1028))[0].kind is FrameKind.WATCHING_ACK
        parent.sendall(encode_frame(WatchdogFrame(FrameKind.SHUTDOWN_REQUEST, "a" * 64, target.pid)))
        assert parent.recv(1028) == b""  # Unsupported direction closes channel.
        assert helper.poll() is None and target.poll() is None
    finally:
        parent.close()
        if target.poll() is None:
            target.kill()
        target.wait(timeout=3)
        out, err = helper.communicate(timeout=3)
    assert helper.returncode == 0 and out == err == b""
