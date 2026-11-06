"""PostgreSQL checks for the same actual owed evidence input owner."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_owed_evidence import (
    test_actual_owed_evidence_restores_before_result_without_repeating_writer,
    test_owed_evidence_admission_cancellation_keeps_original_without_submission,
    test_owed_evidence_rechecks_actual_current_root_lease_and_result,
    test_owed_evidence_refuses_an_extra_pending_cohort_after_real_positive,
    test_owed_restore_rebuilds_required_reader_and_refuses_missing_service,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_owed_evidence_restores_before_result_without_repeating_writer",
    "test_owed_evidence_admission_cancellation_keeps_original_without_submission",
    "test_owed_evidence_rechecks_actual_current_root_lease_and_result",
    "test_owed_evidence_refuses_an_extra_pending_cohort_after_real_positive",
    "test_owed_restore_rebuilds_required_reader_and_refuses_missing_service",
]
