"""Executable exception-field obligations at real non-message consumers.

Each public exercise is callable without pytest fixtures so the diagnostic
inventory and standalone integration tests run the same assertions. The
shutdown seam starts with the control-flow exception, and the pool seam is
the provider callback: neither claims to run an entire pipeline.
"""

from __future__ import annotations

import time
from typing import Any

from elspeth.contracts import TransformResult
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.errors import CapacityError, GracefulShutdownError, PluginRetryableError
from elspeth.contracts.events import RunCompletionStatus, RunSummary
from elspeth.core.events import EventBus
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.plugins.infrastructure.pooling.config import PoolConfig
from elspeth.plugins.infrastructure.pooling.executor import PooledExecutor, RowContext
from elspeth.testing import make_pipeline_row
from tests.fixtures.exception_audit_consumer import exercise_audit_failed_turn as exercise_audit_failed_turn
from tests.fixtures.exception_coalesce_consumer import exercise_coalesce_metadata as exercise_coalesce_metadata
from tests.fixtures.exception_coalesce_consumer import make_diagnostic_recorder


def exercise_graceful_shutdown_summary() -> None:
    """Forward every counter and destination through ceremony and EventBus.

    Routing and quarantine counts remain subsets of succeeded/failed. An
    interrupted run can include a processed row without a terminal outcome.
    The separate ceremony run_id argument is event identity coverage; this
    exercise does not claim that ceremony consumes the exception's run_id.
    """
    cases = (
        (20, 12, 8, 2, 3, 2, {"archive": 3, "review": 2}),
        (21, 12, 8, 2, 3, 2, {"archive": 3, "review": 2}),
        (20, 11, 8, 2, 3, 2, {"archive": 3, "review": 2}),
        (20, 12, 7, 2, 3, 2, {"archive": 3, "review": 2}),
        (20, 12, 8, 3, 3, 2, {"archive": 3, "review": 2}),
        (20, 12, 8, 2, 4, 2, {"archive": 4, "review": 2}),
        (20, 12, 8, 2, 3, 3, {"archive": 3, "review": 3}),
        (20, 12, 8, 2, 3, 2, {"archive": 2, "review": 3}),
    )
    for index, (processed, succeeded, failed, quarantined, routed_success, routed_failure, destinations) in enumerate(cases):
        run_id = f"diagnostic-shutdown-{index}"
        setup = make_diagnostic_recorder(run_id=run_id)
        try:
            events = EventBus()
            summaries: list[RunSummary] = []
            events.subscribe(RunSummary, summaries.append)
            shutdown = GracefulShutdownError(
                processed,
                run_id,
                rows_succeeded=succeeded,
                rows_failed=failed,
                rows_quarantined=quarantined,
                rows_routed_success=routed_success,
                rows_routed_failure=routed_failure,
                routed_destinations=destinations,
            )
            RunCeremony(events=events, telemetry=None).emit_interrupted_ceremony(
                run_id,
                setup.factory,
                shutdown,
                time.perf_counter(),
                coordination_token=setup.coordination_token,
            )
            assert len(summaries) == 1
            summary = summaries[0]
            assert summary.run_id == run_id
            assert summary.status == RunCompletionStatus.INTERRUPTED
            assert summary.total_rows == processed
            assert summary.succeeded == succeeded
            assert summary.failed == failed
            assert summary.quarantined == quarantined
            assert summary.routed_success == routed_success
            assert summary.routed_failure == routed_failure
            assert dict(summary.routed_destinations) == destinations
            persisted_run = setup.run_lifecycle.get_run(run_id)
            assert persisted_run is not None
            assert persisted_run.status == RunStatus.INTERRUPTED
        finally:
            setup.db.close()


def _pool_config() -> PoolConfig:
    return PoolConfig(pool_size=1, max_capacity_retry_seconds=1, min_dispatch_delay_ms=0, max_dispatch_delay_ms=10)


def exercise_plugin_retryable_decision() -> None:
    """Only the retryable field changes; observe actual callback invocations."""
    for retryable in (False, True):
        attempts = 0

        def process(row: dict[str, Any], state_id: str, *, retryable: bool = retryable) -> TransformResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PluginRetryableError("same admitted failure", retryable=retryable, status_code=503)
            return TransformResult.success(make_pipeline_row({"result": "ok"}), success_reason={"action": "diagnostic-control"})

        executor = PooledExecutor(_pool_config())
        try:
            results = executor.execute_batch([RowContext(row={"id": 1}, state_id="diagnostic-row", row_index=0)], process)
            assert len(results) == 1
            result = results[0].result
            if retryable:
                assert attempts == 2
                assert result.status == "success"
            else:
                assert attempts == 1
                assert result.status == "error"
                assert result.reason is not None
                assert result.reason["reason"] == "permanent_error"
        finally:
            executor.shutdown(wait=True)


def _exercise_timeout(error: PluginRetryableError | CapacityError, expected_code: int | None) -> None:
    attempts = 0

    def process(row: dict[str, Any], state_id: str) -> TransformResult:
        nonlocal attempts
        attempts += 1
        raise error

    executor = PooledExecutor(_pool_config())
    try:
        results = executor.execute_batch([RowContext(row={"id": 1}, state_id="diagnostic-row", row_index=0)], process)
        assert attempts >= 1
        assert len(results) == 1
        result = results[0].result
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "retry_timeout"
        assert result.reason["error_type"] == type(error).__name__
        if expected_code is None:
            assert "status_code" not in result.reason
        else:
            assert result.reason["status_code"] == expected_code
    finally:
        executor.shutdown(wait=True)


def exercise_plugin_status_code() -> None:
    """Same timeout message, distinct HTTP codes and explicit absent code."""
    for status_code in (503, 529, None):
        _exercise_timeout(PluginRetryableError("same admitted failure", retryable=True, status_code=status_code), status_code)


def exercise_capacity_status_code() -> None:
    """Capacity errors admit integer codes only; both survive real retries."""
    for status_code in (429, 503):
        _exercise_timeout(CapacityError(status_code=status_code, message="same admitted failure"), status_code)
