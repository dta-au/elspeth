"""OpenTelemetry helpers for first-run tutorial signals."""

from __future__ import annotations

from typing import Literal

import structlog
from opentelemetry import metrics

from elspeth.contracts import errors as contract_errors

_log = structlog.get_logger(__name__)

_CompletionPath = Literal["first_time", "skip", "retake", "repeat", "exit"]
_COMPLETION_PATHS: frozenset[str] = frozenset({"first_time", "skip", "retake", "repeat", "exit"})

_meter = metrics.get_meter(__name__)
_TUTORIAL_COMPLETED_COUNTER = _meter.create_counter(
    "composer.tutorial.completed_total",
    description=(
        "First-run tutorial completion preference writes. Attributes: completion_path in {first_time, skip, retake, repeat, exit}."
    ),
)
_TUTORIAL_ABANDON_COUNTER = _meter.create_counter(
    "composer.tutorial.abandon_total",
    description="Best-effort tutorial abandon beacons sent during page unload/navigation away.",
)


def _log_telemetry_failure(*, operation: str, error_type: str) -> None:
    """Acknowledge exporter failure through the last available channel."""
    try:
        _log.error("tutorial_telemetry_failed", operation=operation, error_type=error_type)
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception:
        # Ordinary failure of the last-resort logger cannot replace a
        # committed preference write or an acknowledged abandon beacon.
        return


def record_tutorial_completed_path(completion_path: _CompletionPath) -> None:
    """Increment the tutorial completion counter with a server-derived path."""
    if completion_path not in _COMPLETION_PATHS:
        raise ValueError(f"completion_path must be one of {sorted(_COMPLETION_PATHS)!r}; got {completion_path!r}")
    try:
        _TUTORIAL_COMPLETED_COUNTER.add(1, attributes={"completion_path": completion_path})
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        # Operational telemetry is best-effort. The preference write that
        # established this outcome has already committed.
        _log_telemetry_failure(operation="completed", error_type=type(exc).__name__)
        return None
    return None


def record_tutorial_abandoned() -> None:
    """Increment the best-effort tutorial abandon counter."""
    try:
        _TUTORIAL_ABANDON_COUNTER.add(1, attributes={})
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as exc:
        # A page-unload beacon must not become an application failure because
        # an optional telemetry exporter is unavailable.
        _log_telemetry_failure(operation="abandoned", error_type=type(exc).__name__)
        return None
    return None
