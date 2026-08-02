"""Shared telemetry helpers for plugin lifecycle defaults."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts.events import ResourceCleanupFailed, TelemetryEvent


class WarningLogger(Protocol):
    def warning(self, message: str, **kwargs: Any) -> None: ...


_logger = structlog.get_logger(__name__)


def warn_telemetry_before_start(event: Any, *, logger: WarningLogger = _logger) -> None:
    """Default telemetry callback before on_start() warns instead of silently dropping."""
    logger.warning(
        "telemetry_emit called before on_start() — event dropped",
        event_type=type(event).__name__,
    )


def make_warn_telemetry_before_start(logger: WarningLogger) -> Callable[[Any], None]:
    """Bind the shared pre-start telemetry warning to a module-specific logger."""
    return partial(warn_telemetry_before_start, logger=logger)


def emit_resource_cleanup_failed(
    telemetry_emit: Callable[[TelemetryEvent], None],
    *,
    run_id: str,
    component: str,
    resource: str,
    error: BaseException,
    suppressed: bool,
    state_id: str | None,
    operation_id: str | None,
    token_id: str | None,
    logger: WarningLogger = _logger,
) -> None:
    """Emit bounded cleanup health telemetry; log ordinary delivery failures.

    Tier-1 invariant failures and process-control exceptions remain
    unsuppressible; callers must never turn those into best-effort warnings.
    """
    try:
        telemetry_emit(
            ResourceCleanupFailed(
                timestamp=datetime.now(UTC),
                run_id=run_id,
                component=component,
                resource=resource,
                error_type=type(error).__name__,
                suppressed=suppressed,
                state_id=state_id,
                operation_id=operation_id,
                token_id=token_id,
            )
        )
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as telemetry_error:
        logger.warning(
            "resource_cleanup_telemetry_failed",
            component=component,
            resource=resource,
            error_type=type(telemetry_error).__name__,
        )
