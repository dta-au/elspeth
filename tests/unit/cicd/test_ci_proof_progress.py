"""Controlled process tests for the inactive Linux proof supervisor."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "ci_proof_progress.py"
NODEID = "tests/example.py::test_proof"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group/subreaper contract")


def _command(report: Path, code: str, *, timeout: float = 5) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--nodeid",
        NODEID,
        "--timeout-seconds",
        str(timeout),
        "--heartbeat-seconds",
        "0.05",
        "--grace-seconds",
        "0.15",
        "--report",
        str(report),
        "--",
        sys.executable,
        "-c",
        code,
    ]


def _run(report: Path, code: str, *, timeout: float = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(_command(report, code, timeout=timeout), cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)


@pytest.fixture
def family_pidfile(tmp_path: Path) -> Iterator[Path]:
    pidfile = tmp_path / "pids"
    try:
        yield pidfile
    finally:
        # Run after assertions so fallback cleanup cannot mask a regression.
        if pidfile.exists():
            pgid = int(pidfile.read_text().split()[0])
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)


@pytest.mark.parametrize("exit_status", [0, 1, 7])
def test_preserves_child_status_and_records_truthful_phase_and_cpu(tmp_path: Path, exit_status: int) -> None:
    report = tmp_path / "result.json"
    result = _run(report, f"import time, sys; time.sleep(0.12); sys.exit({exit_status})")
    assert result.returncode == exit_status, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["child_returncode"] == data["exit_status"] == exit_status
    assert data["outcome"] == ("success" if exit_status == 0 else "child-failed")
    assert data["phase"] == "child-process-including-collection"
    assert data["elapsed_seconds"] >= 0.12
    assert data["user_cpu_seconds"] > 0
    assert data["system_cpu_seconds"] >= 0
    assert data["cpu_scope"] == "wait4-reaped-processes"
    assert data["cleanup_complete"] is True
    heartbeats = [line for line in result.stdout.splitlines() if line.startswith("proof nodeid=")]
    assert len(heartbeats) >= 2
    assert all(NODEID in line and "elapsed_seconds=" in line and "including-collection" in line for line in heartbeats)


def test_child_argv_is_literal_and_output_is_inherited(tmp_path: Path) -> None:
    report = tmp_path / "result.json"
    literal = "spaces ; $(this-is-not-a-shell)"
    command = [*_command(report, "import sys; print(sys.argv[1], flush=True)"), literal]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert literal in result.stdout


def test_timeout_remains_failure_even_when_child_exits_zero_on_term(tmp_path: Path) -> None:
    report = tmp_path / "result.json"
    result = _run(
        report,
        "import signal, sys, time; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(60)",
        timeout=0.25,
    )
    assert result.returncode == 124, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["outcome"] == "timeout"
    assert data["child_returncode"] == 0
    assert data["cleanup_complete"] is True


def _family_code(pidfile: Path, *, parent_exits: bool = False) -> str:
    return (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "descendant = os.fork()\n"
        "if descendant == 0:\n"
        "    time.sleep(60)\n"
        "else:\n"
        f"    open({str(pidfile)!r}, 'w').write(str(os.getpid()) + ' ' + str(descendant))\n"
        + ("    os._exit(0)\n" if parent_exits else "    time.sleep(60)\n")
    )


def _assert_gone(pidfile: Path) -> None:
    pids = [int(value) for value in pidfile.read_text().split()]
    assert len(pids) == 2
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def _cleanup_supervisor(child: subprocess.Popen[str], pgid: int | None) -> None:
    # Kill the controlled family before draining its inherited output pipes.
    # A failed assertion must not leave children alive or mask group cleanup.
    if pgid is not None:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
    try:
        child.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate(timeout=5)


def test_timeout_kills_and_reaps_termination_ignoring_family(tmp_path: Path, family_pidfile: Path) -> None:
    report = tmp_path / "result.json"
    result = _run(report, _family_code(family_pidfile), timeout=0.3)
    assert result.returncode == 124, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["forced_kill"] is True
    assert data["child_returncode"] == -signal.SIGKILL
    assert data["cleanup_complete"] is True
    _assert_gone(family_pidfile)


def test_successful_parent_cannot_abandon_live_descendants(tmp_path: Path, family_pidfile: Path) -> None:
    report = tmp_path / "result.json"
    result = _run(report, _family_code(family_pidfile, parent_exits=True))
    assert result.returncode == 125, result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["outcome"] == "abandoned-descendants"
    assert data["child_returncode"] == 0
    assert data["forced_kill"] is True
    assert data["cleanup_complete"] is True
    _assert_gone(family_pidfile)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_cancellation_forwards_signal_and_reaps_stubborn_descendants(tmp_path: Path, signum: int) -> None:
    report = tmp_path / "result.json"
    pidfile = tmp_path / "pids"
    child = subprocess.Popen(
        _command(report, _family_code(pidfile)), cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists(), "controlled child never became ready"
        child.send_signal(signum)
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 128 + signum, stdout + stderr
        data = json.loads(report.read_text())
        assert data["outcome"] == "cancelled"
        assert data["exit_status"] == 128 + signum
        assert data["cancellation_signal"] == signum
        assert data["forced_kill"] is True
        assert data["cleanup_complete"] is True
        _assert_gone(pidfile)
    finally:
        _cleanup_supervisor(child, int(pidfile.read_text().split()[0]) if pidfile.exists() else None)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_cancellation_reaches_child_and_cannot_become_success(tmp_path: Path, signum: int) -> None:
    report = tmp_path / "result.json"
    ready = tmp_path / "ready"
    received = tmp_path / "received"
    code = (
        "import os, signal, sys, time\n"
        "def stop(signum, frame):\n"
        f"    open({str(received)!r}, 'w').write(str(signum))\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        f"open({str(ready)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen(_command(report, code), cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child.send_signal(signum)
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 128 + signum, stdout + stderr
        assert received.read_text() == str(signum)
        data = json.loads(report.read_text())
        assert data["child_returncode"] == 0
        assert data["outcome"] == "cancelled"
        assert data["cleanup_complete"] is True
    finally:
        _cleanup_supervisor(child, int(ready.read_text()) if ready.exists() else None)


def test_failed_launch_removes_stale_success_report(tmp_path: Path) -> None:
    report = tmp_path / "result.json"
    report.write_text('{"outcome": "success", "exit_status": 0}')
    command = _command(report, "")
    command[-3:] = [str(tmp_path / "nonexistent-executable")]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode != 0
    assert not report.exists()


@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf"])
def test_invalid_timeout_rejected_before_launch(tmp_path: Path, invalid: str) -> None:
    report = tmp_path / "result.json"
    command = _command(report, "raise AssertionError('must not launch')")
    command[command.index("--timeout-seconds") + 1] = invalid
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2
    assert not report.exists()


@pytest.mark.parametrize("mode", ["success", "final-print-fails", "restore-fails", "cancel-after-replace", "cancel-before-publication"])
def test_terminal_evidence_matches_finalization_exit(tmp_path: Path, mode: str) -> None:
    report = tmp_path / "result.json"
    report.write_text('{"outcome": "success", "exit_status": 0}')
    # Each wrapper runs the actual supervisor and a real successful tiny child.
    # Inject only the named finalization boundary around successful publication.
    wrapper = (
        "import builtins, errno, os, signal, sys\n"
        "from scripts import ci_proof_progress as proof\n"
        "real_print = builtins.print\n"
        "real_replace = proof.os.replace\n"
        "real_cdll = proof.ctypes.CDLL\n"
        f"mode = {mode!r}\n"
        "def final_print(*values, **kwargs):\n"
        "    if values and str(values[0]).startswith('proof result='):\n"
        "        raise BrokenPipeError('controlled final log failure')\n"
        "    real_print(*values, **kwargs)\n"
        "def cancelled_replace(source, target):\n"
        "    real_replace(source, target)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "def cancel_before_publication(frame, event, arg):\n"
        "    if (event == 'line' and frame.f_code.co_name == '_supervise'\n"
        "        and frame.f_locals.get('outcome') == 'success'\n"
        "        and not frame.f_locals.get('publishing_result')):\n"
        "        sys.settrace(None)\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "    return cancel_before_publication\n"
        "class RestoreFailure:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.libc = real_cdll(*args, **kwargs)\n"
        "        self.set_calls = 0\n"
        "    def prctl(self, option, *values):\n"
        "        if option == proof.PR_SET_CHILD_SUBREAPER:\n"
        "            self.set_calls += 1\n"
        "            if self.set_calls == 2:\n"
        "                proof.ctypes.set_errno(errno.EPERM)\n"
        "                return -1\n"
        "        return self.libc.prctl(option, *values)\n"
        "if mode == 'final-print-fails':\n"
        "    proof.print = final_print\n"
        "elif mode == 'restore-fails':\n"
        "    proof.ctypes.CDLL = RestoreFailure\n"
        "elif mode == 'cancel-after-replace':\n"
        "    proof.os.replace = cancelled_replace\n"
        "elif mode == 'cancel-before-publication':\n"
        "    sys.settrace(cancel_before_publication)\n"
        "raise SystemExit(proof.main())\n"
    )
    command = [sys.executable, "-c", wrapper, *_command(report, "pass")[2:]]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=False)
    if mode == "success":
        assert result.returncode == 0, result.stdout + result.stderr
        data = json.loads(report.read_text())
        assert data["outcome"] == "success"
        assert data["child_returncode"] == data["exit_status"] == 0
        assert data["cleanup_complete"] is True
        assert "proof result=" in result.stdout
        assert list(tmp_path.iterdir()) == [report]
    elif mode == "cancel-before-publication":
        assert result.returncode == 143, result.stdout + result.stderr
        data = json.loads(report.read_text())
        assert data["outcome"] == "cancelled"
        assert data["exit_status"] == 143
        assert data["cancellation_signal"] == signal.SIGTERM
        assert data["cleanup_complete"] is True
    else:
        assert result.returncode == (143 if mode == "cancel-after-replace" else 1), result.stdout + result.stderr
        assert not report.exists()
        assert list(tmp_path.iterdir()) == []
        if mode == "final-print-fails":
            assert "BrokenPipeError: controlled final log failure" in result.stderr
        elif mode == "restore-fails":
            assert "cannot restore subreaper state" in result.stderr
