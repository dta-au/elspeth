"""Bound an actual failed-physical application control in an owned process."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _reap_owned_child(child: subprocess.Popen[bytes]) -> list[BaseException]:
    cleanup_failures: list[BaseException] = []
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            # It may exit after poll. The exact child still gets waited below.
            pass
        except BaseException as original:
            cleanup_failures.append(original)
    try:
        child.wait(timeout=5)
    except BaseException as original:
        cleanup_failures.append(original)
    return cleanup_failures


def _require_owned_child_reaped(primary: BaseException | None, failures: list[BaseException], label: str) -> None:
    if failures:
        raise BaseExceptionGroup(label, [*([primary] if primary is not None else []), *failures])


def test_owned_child_reaper_waits_after_exit_during_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExitedBetweenPollAndKill:
        pid = 12345
        wait_calls = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            assert timeout == 5
            self.wait_calls += 1
            return 0

    original = ProcessLookupError("owned child exited after poll")

    def exited_during_kill(pid: int, sig: signal.Signals) -> None:
        assert pid == 12345 and sig is signal.SIGKILL
        raise original

    monkeypatch.setattr(os, "killpg", exited_during_kill)
    child = ExitedBetweenPollAndKill()
    assert _reap_owned_child(child) == []
    assert child.wait_calls == 1


def test_owned_child_reaper_retains_signal_and_wait_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnresolvedOwnedChild:
        pid = 12345
        wait_calls = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            assert timeout == 5
            self.wait_calls += 1
            raise wait_original

    signal_original = RuntimeError("unexpected signal failure")
    wait_original = subprocess.TimeoutExpired("owned child", 5)

    def failed_kill(pid: int, sig: signal.Signals) -> None:
        assert pid == 12345 and sig is signal.SIGKILL
        raise signal_original

    monkeypatch.setattr(os, "killpg", failed_kill)
    child = UnresolvedOwnedChild()
    failures = _reap_owned_child(child)
    assert child.wait_calls == 1
    assert len(failures) == 2 and failures[0] is signal_original and failures[1] is wait_original


def test_owned_child_reaper_preserves_causal_original_when_cleanup_fails() -> None:
    causal_original = AssertionError("causal membership absence")
    cleanup_original = subprocess.TimeoutExpired("owned child", 5)
    with pytest.raises(BaseExceptionGroup) as caught:
        _require_owned_child_reaped(causal_original, [cleanup_original], "owned child cleanup")
    assert caught.value.exceptions == (causal_original, cleanup_original)


def test_failed_physical_app_exit_uses_actual_producer_receipts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    output_path = tmp_path / "app-failed-physical-child.log"
    expect_causal_red = os.environ.get("ELSPETH_APP_FAILED_PHYSICAL_EXPECT_RED") == "1"
    with output_path.open("wb") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.helpers.app_failed_physical_child"],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    overall_deadline = time.monotonic() + 45
    physical_deadline: float | None = None
    physical_seen = False
    membership_seen = False
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        while time.monotonic() < overall_deadline:
            evidence = output_path.read_text(encoding="utf-8", errors="replace")
            if "ACTUAL_EXECUTOR_PHYSICAL_JOIN_OBSERVED" in evidence and not physical_seen:
                physical_seen = True
                physical_deadline = time.monotonic() + 10
            membership_seen = "ACTUAL_MEMBERSHIP_STOP_RETURNED" in evidence
            if expect_causal_red and physical_seen and "CAUSAL_MISSING_MEMBERSHIP_AFTER_PHYSICAL_JOIN" in evidence:
                assert not membership_seen
                raise AssertionError("CAUSAL_APP_MEMBERSHIP_ABSENT_AFTER_ACTUAL_PHYSICAL_JOIN")
            if (
                expect_causal_red
                and physical_seen
                and not membership_seen
                and physical_deadline is not None
                and time.monotonic() >= physical_deadline
            ):
                # This independent assertion runs after the child thread
                # measured exact private+shared physical closure. A child
                # timeout or kill without that receipt cannot satisfy it.
                assert child.poll() is None, "child exited before missing-membership causal assertion"
                raise AssertionError("CAUSAL_APP_MEMBERSHIP_ABSENT_AFTER_ACTUAL_PHYSICAL_JOIN")
            if child.poll() is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("owned child timed out without completed causal evidence; timeout is inconclusive")
    except BaseException as original:
        primary = original
    finally:
        cleanup_failures = _reap_owned_child(child)
    _require_owned_child_reaped(primary, cleanup_failures, "Owned child process cleanup failed")
    evidence = output_path.read_text(encoding="utf-8", errors="replace")
    if expect_causal_red:
        if type(primary) is not AssertionError or str(primary) != "CAUSAL_APP_MEMBERSHIP_ABSENT_AFTER_ACTUAL_PHYSICAL_JOIN":
            if primary is None:
                raise AssertionError(f"expected specific post-join failure absent; child exit={child.returncode}\n{evidence}")
            raise primary
        assert physical_seen
        assert not membership_seen and "APP_FAILED_PHYSICAL_CONTROL_PASS" not in evidence
        return
    if primary is not None:
        raise primary
    assert child.returncode == 0, f"child exit={child.returncode}\n{evidence}"
    assert physical_seen and membership_seen, evidence
    assert "ACTUAL_TELEMETRY_WITNESS" in evidence
    assert "APP_FAILED_PHYSICAL_CONTROL_PASS" in evidence


def test_failed_physical_partial_constructor_does_not_replay_claim(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    output_path = tmp_path / "app-failed-partial-child.log"
    expect_causal_red = os.environ.get("ELSPETH_APP_FAILED_PARTIAL_EXPECT_RED") == "1"
    with output_path.open("wb") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.helpers.app_failed_partial_child"],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    overall_deadline = time.monotonic() + 45
    physical_deadline: float | None = None
    physical_seen = False
    causal_red = False
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        while time.monotonic() < overall_deadline:
            evidence = output_path.read_text(encoding="utf-8", errors="replace")
            if "ACTUAL_PARTIAL_PRIVATE_AND_GENERATION_JOINED" in evidence and not physical_seen:
                physical_seen = True
                physical_deadline = time.monotonic() + 10
            if (
                expect_causal_red
                and physical_seen
                and "ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS" not in evidence
                and physical_deadline is not None
                and time.monotonic() >= physical_deadline
            ):
                assert child.poll() is None, "partial child exited before causal assertion"
                raise AssertionError("CAUSAL_PARTIAL_STARTUP_ABSENT_AFTER_ACTUAL_PHYSICAL_JOIN")
            if child.poll() is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("owned partial child timed out without causal evidence; timeout is inconclusive")
    except BaseException as original:
        primary = original
    finally:
        cleanup_failures = _reap_owned_child(child)
    _require_owned_child_reaped(primary, cleanup_failures, "Owned partial child process cleanup failed")
    evidence = output_path.read_text(encoding="utf-8", errors="replace")
    if expect_causal_red:
        if type(primary) is AssertionError and str(primary) == "CAUSAL_PARTIAL_STARTUP_ABSENT_AFTER_ACTUAL_PHYSICAL_JOIN":
            causal_red = True
        elif primary is not None:
            raise primary
        assert causal_red and physical_seen, f"partial expected post-join failure absent; child exit={child.returncode}\n{evidence}"
        assert "ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS" not in evidence
        return
    if primary is not None:
        raise primary
    assert child.returncode == 0, f"partial child exit={child.returncode}\n{evidence}"
    assert "ACTUAL_PARTIAL_CLAIMED_BEFORE_APP_JOIN" in evidence
    assert physical_seen and "ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS" in evidence
