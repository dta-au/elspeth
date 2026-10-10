"""Finite owned-process wrapper for actual failed physical getter controls."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TERMINAL_PREFIX = "ACTUAL_TERMINAL_SNAPSHOT "
_REGISTRY_PATH = "src/elspeth/web/execution_lease_cleanup.py"
_REGISTRY_SHA = "4e4cd915e93ae99be36b98bf81f989d97e6e33fb9e300287795d77cc2296bb42"
_CHILD_PATH = "tests/helpers/execute_failed_physical_join_child.py"
_CHILD_SHA = "cca2195062f2a5cba23ae2e5c1c5cac0e1e5de44695af8dc45e472ef32fa83a4"
_BOOLEAN_STATUS = (
    "private_join_returned",
    "second_sql_exited",
    "second_release_retired",
    "callback_entered",
    "future_done",
    "counter_returned",
    "callback_exit_receipt",
    "callback_original_retained",
    "invocation_exited",
    "generation_joined",
    "getter",
)
_CAUSES = {
    "counter": "CAUSAL_COUNTER_RECEIPT_MISSING_AFTER_ACTUAL_DRAIN_EXIT",
    "callback": "CAUSAL_CALLBACK_EXIT_RECEIPT_MISSING_AFTER_ACTUAL_DRAIN_EXIT",
    "generation": "CAUSAL_GENERATION_JOIN_RECEIPT_MISSING_AFTER_ACTUAL_DRAIN_EXIT",
    "getter": "CAUSAL_GETTER_FALSE_AFTER_ALL_ACTUAL_PHYSICAL_INPUTS",
}


def _extract_actual_snapshot(evidence: str) -> dict[str, object] | None:
    snapshots = [
        line[len(_TERMINAL_PREFIX) : -1]
        for line in evidence.splitlines(keepends=True)
        if line.startswith(_TERMINAL_PREFIX) and line.endswith("\n")
    ]
    assert len(snapshots) <= 1, "actual terminal observer published twice"
    if not snapshots:
        return None
    parsed = json.loads(snapshots[0])
    assert type(parsed) is dict and set(parsed) == {*_BOOLEAN_STATUS, "drain_error"}
    assert all(type(parsed[name]) is bool for name in _BOOLEAN_STATUS)
    assert parsed["drain_error"] is None or type(parsed["drain_error"]) is str
    return parsed


def _classify_actual_snapshot(snapshot: dict[str, object]) -> str | None:
    if snapshot.get("drain_error") is not None:
        return None
    prerequisites = (
        "private_join_returned",
        "second_sql_exited",
        "second_release_retired",
        "callback_entered",
        "future_done",
        "callback_original_retained",
        "invocation_exited",
    )
    if not all(snapshot.get(name) is True for name in prerequisites):
        return None
    if snapshot.get("counter_returned") is False:
        return "counter"
    if snapshot.get("callback_exit_receipt") is False:
        return "callback"
    if snapshot.get("generation_joined") is False:
        return "generation"
    if snapshot.get("getter") is False:
        return "getter"
    return None


def _reap_owned_child(child: subprocess.Popen[bytes]) -> list[BaseException]:
    errors: list[BaseException] = []
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as original:
            errors.append(original)
    try:
        child.wait(timeout=5)
    except BaseException as original:
        errors.append(original)
    return errors


def _require_reaped(primary: BaseException | None, errors: list[BaseException]) -> None:
    if errors:
        raise BaseExceptionGroup("Actual getter child cleanup retained originals", [*([primary] if primary is not None else []), *errors])


def _configured_source_hashes() -> dict[str, str]:
    raw = os.environ.get("ELSPETH_GETTER_EXPECT_SOURCE_HASHES")
    if raw is None:
        return {_REGISTRY_PATH: _REGISTRY_SHA, _CHILD_PATH: _CHILD_SHA}
    selected = json.loads(raw)
    assert type(selected) is dict and selected
    assert all(type(path) is str and type(digest) is str and len(digest) == 64 for path, digest in selected.items())
    assert _REGISTRY_PATH in selected
    assert selected.get(_CHILD_PATH) == _CHILD_SHA
    return selected


def _assert_source_hashes(root: Path, expected: dict[str, str]) -> None:
    for name, digest in expected.items():
        target = (root / name).resolve()
        assert target.is_relative_to(root) and target.is_file(), f"expected source absent or outside tree: {name}"
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest, f"source bytes changed: {name}"


def test_source_hash_guard_accepts_exact_bytes_and_rejects_mutation(tmp_path: Path) -> None:
    source = tmp_path / "candidate.py"
    source.write_bytes(b"exact bytes\n")
    expected = {"candidate.py": hashlib.sha256(source.read_bytes()).hexdigest()}
    _assert_source_hashes(tmp_path, expected)
    source.write_bytes(b"changed bytes\n")
    with pytest.raises(AssertionError, match="source bytes changed"):
        _assert_source_hashes(tmp_path, expected)


def test_snapshot_classifier_rejects_missing_prerequisites_and_identifies_receipts() -> None:
    healthy = {
        "private_join_returned": True,
        "second_sql_exited": True,
        "second_release_retired": True,
        "callback_entered": True,
        "future_done": True,
        "callback_original_retained": True,
        "invocation_exited": True,
        "counter_returned": True,
        "callback_exit_receipt": True,
        "generation_joined": True,
        "getter": True,
        "drain_error": None,
    }
    healthy_record = _TERMINAL_PREFIX + json.dumps(healthy) + "\n"
    assert _extract_actual_snapshot(healthy_record) == healthy
    assert _extract_actual_snapshot(healthy_record.removesuffix("\n")) is None
    with pytest.raises(AssertionError):
        _extract_actual_snapshot(_TERMINAL_PREFIX + json.dumps(dict(healthy, getter="true")) + "\n")
    with pytest.raises(json.JSONDecodeError):
        _extract_actual_snapshot(_TERMINAL_PREFIX + "{malformed\n")
    with pytest.raises(AssertionError, match="published twice"):
        _extract_actual_snapshot(healthy_record + healthy_record)
    assert _classify_actual_snapshot(healthy) is None
    for missing in (
        "private_join_returned",
        "second_sql_exited",
        "second_release_retired",
        "callback_entered",
        "future_done",
        "callback_original_retained",
        "invocation_exited",
    ):
        altered = dict(healthy, **{missing: False, "getter": False})
        assert _classify_actual_snapshot(altered) is None
    for missing, cause in (
        ("counter_returned", "counter"),
        ("callback_exit_receipt", "callback"),
        ("generation_joined", "generation"),
        ("getter", "getter"),
    ):
        assert _classify_actual_snapshot(dict(healthy, **{missing: False})) == cause


def test_generic_drain_failure_blocks_every_receipt_omission_cause() -> None:
    healthy = {
        "private_join_returned": True,
        "second_sql_exited": True,
        "second_release_retired": True,
        "callback_entered": True,
        "future_done": True,
        "counter_returned": True,
        "callback_exit_receipt": True,
        "callback_original_retained": True,
        "invocation_exited": True,
        "generation_joined": True,
        "getter": True,
        "drain_error": None,
    }
    for missing, cause in (
        ("counter_returned", "counter"),
        ("callback_exit_receipt", "callback"),
        ("generation_joined", "generation"),
        ("getter", "getter"),
    ):
        clean_omission = dict(healthy, **{missing: False})
        assert _classify_actual_snapshot(clean_omission) == cause
        generic_drain_fault = dict(clean_omission, drain_error="RuntimeError")
        assert _classify_actual_snapshot(generic_drain_fault) is None


def test_reaper_waits_after_exit_race_and_retains_wait_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        pid = 12345
        wait_calls = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            assert timeout == 5
            self.wait_calls += 1
            raise wait_original

    wait_original = subprocess.TimeoutExpired("actual getter child", 5)

    def exited_after_poll(pid: int, sig: signal.Signals) -> None:
        assert pid == 12345 and sig is signal.SIGKILL
        raise ProcessLookupError("exact child already exited")

    monkeypatch.setattr(os, "killpg", exited_after_poll)
    child = Probe()
    errors = _reap_owned_child(child)
    assert child.wait_calls == 1 and len(errors) == 1 and errors[0] is wait_original
    causal_original = AssertionError("known physical receipt missing")
    with pytest.raises(BaseExceptionGroup) as caught:
        _require_reaped(causal_original, errors)
    assert caught.value.exceptions == (causal_original, wait_original)


def test_reaper_retains_unexpected_signal_and_wait_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        pid = 12345
        wait_calls = 0

        def poll(self) -> None:
            return None

        def wait(self, *, timeout: int) -> int:
            assert timeout == 5
            self.wait_calls += 1
            raise wait_original

    signal_original = RuntimeError("actual signal failure")
    wait_original = subprocess.TimeoutExpired("actual getter child", 5)

    def failed_signal(pid: int, sig: signal.Signals) -> None:
        assert pid == 12345 and sig is signal.SIGKILL
        raise signal_original

    monkeypatch.setattr(os, "killpg", failed_signal)
    child = Probe()
    errors = _reap_owned_child(child)
    assert child.wait_calls == 1 and len(errors) == 2
    assert errors[0] is signal_original and errors[1] is wait_original


def test_failed_callback_physical_join_has_finite_causal_owner(tmp_path: Path) -> None:
    expected_cause = os.environ.get("ELSPETH_GETTER_EXPECT_CAUSE")
    assert expected_cause is None or expected_cause in _CAUSES
    root = Path(__file__).resolve().parents[3]
    source_hashes = _configured_source_hashes()
    assert expected_cause is None or os.environ.get("ELSPETH_GETTER_EXPECT_SOURCE_HASHES") is not None
    _assert_source_hashes(root, source_hashes)
    output_path = tmp_path / "actual-failed-physical-getter-child.log"
    with output_path.open("wb") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.helpers.execute_failed_physical_join_child"],
            cwd=root,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    overall_deadline = time.monotonic() + 65
    snapshot: dict[str, object] | None = None
    primary: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        while time.monotonic() < overall_deadline:
            evidence = output_path.read_text(encoding="utf-8", errors="replace")
            snapshot = _extract_actual_snapshot(evidence)
            if expected_cause is not None and snapshot is not None:
                observed_cause = _classify_actual_snapshot(snapshot)
                if observed_cause is not None:
                    assert observed_cause == expected_cause, f"different actual receipt failed: {observed_cause}"
                    raise AssertionError(_CAUSES[observed_cause])
            if child.poll() is not None:
                evidence = output_path.read_text(encoding="utf-8", errors="replace")
                snapshot = _extract_actual_snapshot(evidence)
                if expected_cause is not None and snapshot is not None:
                    observed_cause = _classify_actual_snapshot(snapshot)
                    if observed_cause is not None:
                        assert observed_cause == expected_cause, f"different actual receipt failed: {observed_cause}"
                        raise AssertionError(_CAUSES[observed_cause])
                break
            time.sleep(0.02)
        else:
            raise AssertionError("actual getter child exceeded finite owner deadline; timeout is inconclusive")
    except BaseException as original:
        primary = original
    finally:
        cleanup_errors = _reap_owned_child(child)
        try:
            _assert_source_hashes(root, source_hashes)
        except BaseException as original:
            cleanup_errors.append(original)
    _require_reaped(primary, cleanup_errors)
    evidence = output_path.read_text(encoding="utf-8", errors="replace")
    snapshot = _extract_actual_snapshot(evidence)
    if expected_cause is not None:
        expected_original = _CAUSES[expected_cause]
        if type(primary) is not AssertionError or str(primary) != expected_original:
            if primary is None:
                raise AssertionError(f"actual expected causal assertion absent; child exit={child.returncode}\n{evidence}")
            raise primary
        assert snapshot is not None and _classify_actual_snapshot(snapshot) == expected_cause
        assert "ACTUAL_FAILED_PHYSICAL_GETTER_CONTROL_PASS" not in evidence
        return
    if primary is not None:
        raise primary
    assert child.returncode == 0, f"actual getter child exit={child.returncode}\n{evidence}"
    assert snapshot is not None and snapshot.get("getter") is True and _classify_actual_snapshot(snapshot) is None
    assert all(snapshot[name] is True for name in _BOOLEAN_STATUS)
    assert snapshot["drain_error"] is None
    for phase in (
        "ACTUAL_PRIVATE_AND_SECOND_SQL_ENTERED",
        "ACTUAL_FIRST_SQL_RELEASE_RETURNED",
        "ACTUAL_FINALIZER_CALLBACK_ENTERED",
        "ACTUAL_CANCELLATION_DELIVERED_0",
        "ACTUAL_CANCELLATION_DELIVERED_1",
        "ACTUAL_CANCELLATION_DELIVERED_2",
        "ACTUAL_SECOND_SQL_RELEASE_RETURNED",
        "ACTUAL_PRIVATE_JOIN_RETURNED",
        "ACTUAL_SECOND_SQL_TRANSACTION_EXITED",
        "ACTUAL_FAILED_PHYSICAL_GETTER_CONTROL_PASS",
    ):
        assert phase in evidence, f"actual phase {phase} absent\n{evidence}"
