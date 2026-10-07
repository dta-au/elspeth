"""Opt-in, bounded actual app finalizer receipt diagnostic."""

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

from tests.helpers.app_failed_receipt_terminal import (
    PREFIX,
    classify,
    expected_drain_negative,
    healthy_complete,
    parse_terminal,
)

_MODULES = {
    "returned": "tests.helpers.app_failed_physical_child",
    "partial": "tests.helpers.app_failed_partial_child",
}
_PASS_MARKERS = {
    "returned": "APP_FAILED_PHYSICAL_CONTROL_PASS",
    "partial": "ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS",
}
_CAUSES = {"healthy", "counter", "callback", "generation", "getter", "drain_error"}
_CHILD_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "PYTHONPATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTHONNOUSERSITE",
    "PYTHONDONTWRITEBYTECODE",
    "LITELLM_LOCAL_MODEL_COST_MAP",
)


def _verify_sources(root: Path, selected: dict[str, str]) -> None:
    assert len(selected) == 47
    for relative, expected in selected.items():
        target = (root / relative).resolve()
        assert target.is_relative_to(root) and target.is_file()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected, relative


def _reap_exact_child(child: subprocess.Popen[bytes]) -> list[BaseException]:
    originals: list[BaseException] = []
    try:
        child.poll()
    except BaseException as original:
        originals.append(original)
    if child.returncode is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except BaseException as original:
            originals.append(original)
        try:
            child.wait(timeout=3)
        except BaseException as original:
            originals.append(original)
    if child.returncode is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as original:
            originals.append(original)
        try:
            child.wait(timeout=3)
        except BaseException as original:
            originals.append(original)
    try:
        child.wait(timeout=0)
    except BaseException as original:
        originals.append(original)
    if child.returncode is None:
        originals.append(AssertionError("actual app diagnostic child remains unjoined"))
    return originals


def _strict_finished_capture(raw: str, scenario: str, first: dict[str, object]) -> dict[str, object]:
    if "ACTUAL_APP_TERMINAL_OBSERVER_ERROR" in raw:
        raise AssertionError("actual app terminal observer emitted an error")
    final = parse_terminal(raw)
    if final is None or final["scenario"] != scenario or final != first:
        raise AssertionError("actual app terminal changed or disappeared after child join")
    return final


@pytest.mark.parametrize("scenario", ("returned", "partial"))
def test_actual_app_receipt_terminal_after_physical_join(tmp_path: Path, scenario: str) -> None:
    raw_case = os.environ.get("ELSPETH_APP_RECEIPT_EXPECT_CAUSE")
    raw_map = os.environ.get("ELSPETH_APP_RECEIPT_SOURCE_HASHES")
    if raw_case is None and raw_map is None:
        pytest.skip("bounded app receipt diagnostic requires an exact source map")
    assert raw_case in _CAUSES and raw_map is not None
    selected = json.loads(raw_map)
    assert type(selected) is dict and all(
        type(name) is str and type(digest) is str and len(digest) == 64 for name, digest in selected.items()
    )
    root = Path(__file__).resolve().parents[3]
    _verify_sources(root, selected)
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"
    assert all(
        name not in os.environ
        for name in ("DATABASE_URL", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    )
    child_env = {name: os.environ[name] for name in _CHILD_ENV if name in os.environ}
    child_env["ELSPETH_APP_RECEIPT_DIAGNOSTIC"] = scenario
    output_path = tmp_path / f"app-receipt-{scenario}.log"
    child: subprocess.Popen[bytes] | None = None
    output = None
    primary: BaseException | None = None
    cleanup_originals: list[BaseException] = []
    first: dict[str, object] | None = None
    final: dict[str, object] | None = None
    allocation_attempted = False
    deferred: list[int] = []
    old_handlers: dict[signal.Signals, object] = {}
    mask_before: set[signal.Signals] | None = None
    mask_restored = True
    interruption_original: InterruptedError | None = None

    def defer_interrupt(signum, frame) -> None:
        deferred.append(signum)

    def stop_if_deferred() -> None:
        nonlocal interruption_original
        if deferred:
            if interruption_original is None:
                interruption_original = InterruptedError("app diagnostic received deferred interruption")
            raise interruption_original

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, defer_interrupt)
        output = output_path.open("wb")
        try:
            mask_before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
            mask_restored = False
            # The blocked interval closes the delivery-to-allocation gap.
            stop_if_deferred()
            allocation_attempted = True
            # Reset blocked INT/TERM before the child imports project modules.
            entry = (
                "import runpy,signal;"
                "signal.pthread_sigmask(signal.SIG_UNBLOCK,(signal.SIGINT,signal.SIGTERM));"
                f"runpy.run_module({_MODULES[scenario]!r},run_name='__main__')"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", entry],
                cwd=root,
                env=child_env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            if mask_before is not None and not mask_restored:
                try:
                    signal.pthread_sigmask(signal.SIG_SETMASK, mask_before)
                    mask_restored = True
                except BaseException as original:
                    cleanup_originals.append(original)
            if output is not None:
                try:
                    output.close()
                except BaseException as original:
                    cleanup_originals.append(original)
        if child is None:
            raise AssertionError("app diagnostic allocation returned no child")
        if not cleanup_originals and not deferred:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                stop_if_deferred()
                observed = output_path.read_text(encoding="utf-8", errors="replace")
                if "ACTUAL_APP_TERMINAL_OBSERVER_ERROR" in observed:
                    raise AssertionError("actual app terminal observer emitted an error")
                incomplete = any(line.startswith(PREFIX) and not line.endswith("\n") for line in observed.splitlines(keepends=True))
                first = None if incomplete else parse_terminal(observed)
                if first is not None:
                    break
                if child.poll() is not None:
                    raise AssertionError("app diagnostic child exited before actual terminal")
                time.sleep(0.02)
            else:
                raise AssertionError("app diagnostic terminal absent; elapsed deadline is inconclusive")
            assert first is not None and first["scenario"] == scenario
            if raw_case == "healthy":
                natural_deadline = time.monotonic() + 35
                while child.poll() is None and time.monotonic() < natural_deadline:
                    stop_if_deferred()
                    time.sleep(0.02)
                stop_if_deferred()
                if child.poll() is None:
                    raise AssertionError("healthy app diagnostic child did not reach a waitable exit")
    except BaseException as original:
        primary = original
    finally:
        if child is not None:
            try:
                cleanup_originals.extend(_reap_exact_child(child))
            except BaseException as original:
                cleanup_originals.append(original)
        elif allocation_attempted:
            cleanup_originals.append(AssertionError("app diagnostic allocation outcome Unknown; no next child may be issued"))
        if mask_before is not None and not mask_restored:
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask_before)
                mask_restored = True
            except BaseException as original:
                cleanup_originals.append(original)
        if child is not None and child.returncode is not None and first is not None:
            try:
                observed = output_path.read_text(encoding="utf-8", errors="replace")
                final = _strict_finished_capture(observed, scenario, first)
            except BaseException as original:
                cleanup_originals.append(original)
        for signum, previous in old_handlers.items():
            try:
                signal.signal(signum, previous)
            except BaseException as original:
                cleanup_originals.append(original)
    if deferred:
        if interruption_original is None:
            interruption_original = InterruptedError("app diagnostic received deferred interruption")
        if primary is not interruption_original:
            cleanup_originals.append(interruption_original)
    if primary is not None or cleanup_originals:
        originals = [*([primary] if primary is not None else []), *cleanup_originals]
        raise BaseExceptionGroup("actual app receipt diagnostic and child cleanup originals", originals)
    assert child is not None and child.returncode is not None and final is not None
    if raw_case == "healthy":
        assert healthy_complete(final), "healthy app terminal lacks a full physical and original outcome"
        assert child.returncode == 0
        assert observed.splitlines().count(_PASS_MARKERS[scenario]) == 1
    elif raw_case == "drain_error":
        assert expected_drain_negative(final), "drain-error negative lacks its physical/identity prerequisites"
    else:
        assert classify(final) == raw_case
