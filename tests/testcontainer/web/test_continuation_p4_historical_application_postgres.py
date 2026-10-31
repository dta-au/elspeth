"""Original historical P4 projection cases against the actual serial PG store."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_p4_historical_application import (
    test_actual_application_refuses_changed_owned_input_without_partial_projection,
    test_actual_historical_projection_preserves_graph_history_channels_and_retries_once,
    test_admission_cancellation_has_no_projection_and_preserves_same_original,
    test_original_current_writer_checkpoint_witness_contract_remains_separate,
    test_retry_rechecks_real_rows_before_returning_live_cached_projection,
    test_started_read_cancellation_keeps_applied_result_and_original_cancellation,
    test_verification_failure_retains_committed_identity_and_exact_live_comparison_cause,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_application_refuses_changed_owned_input_without_partial_projection",
    "test_actual_historical_projection_preserves_graph_history_channels_and_retries_once",
    "test_admission_cancellation_has_no_projection_and_preserves_same_original",
    "test_original_current_writer_checkpoint_witness_contract_remains_separate",
    "test_retry_rechecks_real_rows_before_returning_live_cached_projection",
    "test_started_read_cancellation_keeps_applied_result_and_original_cancellation",
    "test_verification_failure_retains_committed_identity_and_exact_live_comparison_cause",
]
