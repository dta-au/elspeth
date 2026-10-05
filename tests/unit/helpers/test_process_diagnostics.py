"""Deterministic controls for asynchronously written child stack dumps."""

from __future__ import annotations

import os
from collections.abc import Callable
from multiprocessing.process import BaseProcess
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.helpers import process_diagnostics


class _DiagnosticClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep: Callable[[], None] = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.on_sleep()


@pytest.mark.skipif(os.name != "posix", reason="native stack requests use a POSIX signal")
@pytest.mark.parametrize(
    "finish_at, complete",
    [(0.01, True), (0.2, True), (0.3, False)],
    ids=["during-grace", "at-deadline", "unfinished-at-deadline"],
)
def test_snapshot_retains_later_dump_chunks_after_partial_growth(monkeypatch: pytest.MonkeyPatch, finish_at: float, complete: bool) -> None:
    clock = _DiagnosticClock()
    monkeypatch.setattr(process_diagnostics, "time", clock)
    diagnostics = process_diagnostics.ProcessDiagnostics()
    trace = Path(diagnostics.path)
    trace.write_text("stack signal ready\nphase=running action\n", encoding="utf-8")
    process = Mock(spec=BaseProcess)
    process.is_alive.return_value = True
    process.pid = 12345
    process.exitcode = None
    finished = False

    def write_prefix(_pid: int, _signum: int) -> None:
        with trace.open("a", encoding="utf-8") as writer:
            writer.write('Current thread:\n  File "controlled-child.py"')

    def finish_dump() -> None:
        nonlocal finished
        if clock.now >= finish_at and not finished:
            with trace.open("a", encoding="utf-8") as writer:
                writer.write(", line 1 in _block_before_process_seam\n")
            finished = True

    clock.on_sleep = finish_dump
    request = Mock(spec=os.kill, side_effect=write_prefix)
    monkeypatch.setattr(process_diagnostics.os, "kill", request)
    try:
        snapshot = diagnostics.snapshot(process)
        assert ("_block_before_process_seam" in snapshot) is complete
        assert finished is complete
        assert clock.now == pytest.approx(0.2)
        request.assert_called_once_with(12345, process_diagnostics.signal.SIGUSR1)
    finally:
        diagnostics.close()


@pytest.mark.parametrize("alive, handler_ready", [(False, True), (True, False)], ids=["dead-child", "still-importing"])
def test_snapshot_does_not_signal_dead_or_unregistered_child(monkeypatch: pytest.MonkeyPatch, alive: bool, handler_ready: bool) -> None:
    clock = _DiagnosticClock()
    monkeypatch.setattr(process_diagnostics, "time", clock)
    diagnostics = process_diagnostics.ProcessDiagnostics()
    if handler_ready:
        Path(diagnostics.path).write_text("stack signal ready\nphase=target exited\n", encoding="utf-8")
    process = Mock(spec=BaseProcess)
    process.is_alive.return_value = alive
    process.pid = 12345
    process.exitcode = None if alive else 1
    request = Mock(spec=os.kill)
    monkeypatch.setattr(process_diagnostics.os, "kill", request)
    try:
        snapshot = diagnostics.snapshot(process)
        assert f"alive={alive}" in snapshot
        assert f"exitcode={process.exitcode}" in snapshot
        request.assert_not_called()
        assert clock.now == 0.0
    finally:
        diagnostics.close()
