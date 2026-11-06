"""The same genuine terminal-service P4 controls on PostgreSQL, serially."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_terminal_p4 import (
    test_terminal_p4_actual_earlier_effect_is_a_full_publication_prerequisite,
    test_terminal_p4_actual_unavailable_publication_precedes_cold_projection,
    test_terminal_p4_cold_projection_retains_actual_registered_inventory,
    test_terminal_p4_cold_unavailable_settlement_preserves_integrity_priority,
    test_terminal_p4_healthy_ordinary_neighbor_retains_portable_application,
    test_terminal_p4_live_and_fresh_sql_readers_recognize_mode_before_ordinary_restore,
    test_terminal_p4_lost_owner_recognition_precedes_ordinary_recovery,
    test_terminal_p4_prior_sql_effect_tamper_refuses_before_readback_or_recovery,
    test_terminal_p4_readback_refuses_changed_sql_source_and_restores_positive,
    test_terminal_p4_real_service_publication_binds_complete_input_and_actual_receipt,
    test_terminal_p4_released_issued_fence_cannot_publish_with_live_original,
    test_terminal_p4_settled_readback_requires_exact_original_transition,
    test_terminal_p4_stop_after_real_issue_finishes_original_service_prefix,
    test_terminal_p4_transaction_boundaries_keep_real_original_and_one_physical_writer,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_terminal_p4_actual_earlier_effect_is_a_full_publication_prerequisite",
    "test_terminal_p4_actual_unavailable_publication_precedes_cold_projection",
    "test_terminal_p4_cold_projection_retains_actual_registered_inventory",
    "test_terminal_p4_cold_unavailable_settlement_preserves_integrity_priority",
    "test_terminal_p4_healthy_ordinary_neighbor_retains_portable_application",
    "test_terminal_p4_live_and_fresh_sql_readers_recognize_mode_before_ordinary_restore",
    "test_terminal_p4_lost_owner_recognition_precedes_ordinary_recovery",
    "test_terminal_p4_prior_sql_effect_tamper_refuses_before_readback_or_recovery",
    "test_terminal_p4_readback_refuses_changed_sql_source_and_restores_positive",
    "test_terminal_p4_real_service_publication_binds_complete_input_and_actual_receipt",
    "test_terminal_p4_released_issued_fence_cannot_publish_with_live_original",
    "test_terminal_p4_settled_readback_requires_exact_original_transition",
    "test_terminal_p4_stop_after_real_issue_finishes_original_service_prefix",
    "test_terminal_p4_transaction_boundaries_keep_real_original_and_one_physical_writer",
]
