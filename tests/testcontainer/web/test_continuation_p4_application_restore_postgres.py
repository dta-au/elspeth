"""Actual cold application finish-once checks over PostgreSQL transactions."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_p4_application_restore import (
    test_actual_cold_application_finishes_both_tickets_and_restores_the_shared_graph,
    test_cancel_after_actual_commit_drains_projection_and_retains_every_original_error,
    test_cold_application_admission_cancellation_keeps_original_and_submits_nothing,
    test_missing_projection_reader_leaves_one_applied_root_recoverable_with_actual_service,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_cold_application_finishes_both_tickets_and_restores_the_shared_graph",
    "test_cancel_after_actual_commit_drains_projection_and_retains_every_original_error",
    "test_cold_application_admission_cancellation_keeps_original_and_submits_nothing",
    "test_missing_projection_reader_leaves_one_applied_root_recoverable_with_actual_service",
]
