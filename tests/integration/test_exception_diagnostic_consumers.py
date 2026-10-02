"""Run the same real-consumer obligations used by the diagnostic inventory."""

from tests.fixtures.exception_diagnostic_consumers import (
    exercise_audit_failed_turn,
    exercise_capacity_status_code,
    exercise_coalesce_metadata,
    exercise_graceful_shutdown_summary,
    exercise_plugin_retryable_decision,
    exercise_plugin_status_code,
)


def test_audit_failed_turn() -> None:
    exercise_audit_failed_turn()


def test_coalesce_metadata() -> None:
    exercise_coalesce_metadata()


def test_graceful_shutdown_summary() -> None:
    exercise_graceful_shutdown_summary()


def test_plugin_retryable_decision() -> None:
    exercise_plugin_retryable_decision()


def test_plugin_status_code() -> None:
    exercise_plugin_status_code()


def test_capacity_status_code() -> None:
    exercise_capacity_status_code()
