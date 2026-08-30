"""Tests for shared plugin telemetry helpers."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.plugins.infrastructure.telemetry import emit_resource_cleanup_failed, warn_telemetry_before_start


class _RecordingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **kwargs: Any) -> None:
        self.calls.append((message, kwargs))


def test_warn_telemetry_before_start_records_event_type() -> None:
    logger = _RecordingLogger()

    warn_telemetry_before_start(object(), logger=logger)

    assert logger.calls == [
        (
            "telemetry_emit called before on_start() — event dropped",
            {"event_type": "object"},
        )
    ]


@pytest.mark.parametrize("failure", [FrameworkBugError("telemetry invariant failed"), KeyboardInterrupt(), SystemExit(17)])
def test_cleanup_telemetry_does_not_suppress_unsuppressible_failures(failure: BaseException) -> None:
    def fail_emit(_event: object) -> None:
        raise failure

    with pytest.raises(type(failure)):
        emit_resource_cleanup_failed(
            fail_emit,
            run_id="run-1",
            component="component",
            resource="provider",
            error=RuntimeError("cleanup failed"),
            suppressed=True,
            state_id=None,
            operation_id="operation-1",
            token_id=None,
        )


@pytest.mark.parametrize("bug", [TypeError("bad signature"), AttributeError("missing attr"), KeyError("field"), NameError("symbol")])
def test_cleanup_telemetry_reraises_programming_errors(bug: Exception) -> None:
    """First-party bugs in the telemetry callback crash; only delivery failures are contained."""

    def fail_emit(_event: object) -> None:
        raise bug

    with pytest.raises(type(bug)):
        emit_resource_cleanup_failed(
            fail_emit,
            run_id="run-1",
            component="component",
            resource="provider",
            error=RuntimeError("cleanup failed"),
            suppressed=True,
            state_id=None,
            operation_id="operation-1",
            token_id=None,
        )


def test_cleanup_telemetry_logs_ordinary_delivery_failure() -> None:
    """A genuine delivery failure is recorded as a warning and contained."""
    logger = _RecordingLogger()

    def fail_emit(_event: object) -> None:
        raise RuntimeError("transport down")

    emit_resource_cleanup_failed(
        fail_emit,
        run_id="run-1",
        component="component",
        resource="provider",
        error=RuntimeError("cleanup failed"),
        suppressed=True,
        state_id=None,
        operation_id="operation-1",
        token_id=None,
        logger=logger,
    )

    assert logger.calls == [
        (
            "resource_cleanup_telemetry_failed",
            {"component": "component", "resource": "provider", "error_type": "RuntimeError"},
        )
    ]
