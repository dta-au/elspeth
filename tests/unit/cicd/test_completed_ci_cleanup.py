"""Controls for the narrow wrapper around the existing host cleanup authority."""

from __future__ import annotations

import argparse
import hashlib
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from scripts.cicd import cleanup_completed_ci as cleanup


class GuardedTestHelper(cleanup.ReviewedCleanupHelper):
    protected_reason: Callable[[], None]


def test_both_candidate_and_quarantine_filters_are_narrowed() -> None:
    helper = cleanup.ReviewedCleanupHelper("helper")
    cleanup.restrict_runs(helper, ["123", "456", "123"])
    for prefix in ("ci", "bp", "mutation"):
        selected = f"{prefix}-123-1-test-3.13-2"
        match = helper.CHECKOUT.fullmatch(selected)
        assert match is not None
        assert match[1] == "123"
        quarantine = f"elspeth-nyx-4-{'a' * 32}-{selected}"
        match = helper.QUARANTINE.fullmatch(quarantine)
        assert match is not None
        assert match.groups() == ("elspeth-nyx-4", selected)
        for rejected in (f"{prefix}-789-1-test", f"{prefix}-1230-1-test", f"{prefix}-0123-1-test"):
            assert helper.CHECKOUT.fullmatch(rejected) is None
            assert helper.QUARANTINE.fullmatch(f"elspeth-nyx-4-{'a' * 32}-{rejected}") is None
    assert helper.CHECKOUT.fullmatch("source-worktree") is None
    assert helper.CHECKOUT.fullmatch("ci-123-1-test/child") is None
    assert helper.QUARANTINE.fullmatch(f"elspeth-nyx-7-{'a' * 32}-ci-123-1-test") is None


@pytest.mark.parametrize("value", ["", "0", "-1", "0123", "123|789", "123/../other", "123\n"])
def test_run_ids_cannot_expand_the_allowlist(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cleanup.run_id(value)


def test_empty_allowlist_is_refused() -> None:
    with pytest.raises(ValueError, match="At least one"):
        cleanup.restrict_runs(cleanup.ReviewedCleanupHelper("helper"), [])


def test_helper_bytes_are_verified_before_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "helper.py"
    source = b"answer = 42\n"
    path.write_bytes(source)
    monkeypatch.setattr(cleanup, "REVIEWED_SHA256", hashlib.sha256(source).hexdigest())
    assert cleanup.load_helper(path).__dict__["answer"] == 42
    path.write_bytes(source + b"raise RuntimeError('must not execute')\n")
    with pytest.raises(SystemExit, match="changed"):
        cleanup.load_helper(path)


def test_deletion_executor_has_one_worker() -> None:
    with cleanup.serial_executor(max_workers=4) as executor:
        assert executor._max_workers == 1
    with pytest.raises(ValueError, match="Unexpected"):
        cleanup.serial_executor(max_workers=8)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_interruption_uses_the_installed_helpers_unwind_status(signum: int) -> None:
    with pytest.raises(SystemExit) as error:
        cleanup.interrupted(signum, None)
    assert error.value.code == 128 + signum


@pytest.mark.parametrize("execute", [False, True])
def test_default_dry_run_and_explicit_execution_preserve_helper_guards(monkeypatch: pytest.MonkeyPatch, execute: bool) -> None:
    helper = GuardedTestHelper("helper")
    observed: list[list[str]] = []

    def original_guard() -> None:
        raise AssertionError("not called by fixture")

    def installed_main() -> None:
        observed.append(list(sys.argv))
        assert signal.getsignal(signal.SIGTERM) is cleanup.interrupted
        assert signal.getsignal(signal.SIGHUP) is cleanup.interrupted

    helper.protected_reason = original_guard
    helper.main = installed_main
    monkeypatch.setattr(cleanup, "load_helper", lambda _path: helper)
    argv = ["wrapper", "--run-id", "123"] + (["--execute"] if execute else [])
    monkeypatch.setattr(sys, "argv", argv)
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    cleanup.main()
    assert sys.argv is argv
    assert observed == [[str(cleanup.HELPER), "--min-age-hours", "1", "--json"] + ([] if execute else ["--dry-run"])]
    assert helper.protected_reason is original_guard
    assert helper.ThreadPoolExecutor is cleanup.serial_executor
    assert isinstance(helper.CHECKOUT, re.Pattern)
    assert {sig: signal.getsignal(sig) for sig in previous_handlers} == previous_handlers
