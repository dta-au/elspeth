"""Lost-owner terminal and source/accounting integrity on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.coordination.test_continuation_lost_owner_settlement import (
    test_actual_progressed_title_reproves_both_checkpoint_observations_without_writes,
    test_actual_unknown_provider_cut_settles_without_replay_or_accounting_loss,
    test_admitted_title_without_stop_recovers_with_original_physical_accounting_and_no_sdk_retry,
    test_archived_interrupted_generation_settles_without_inventing_an_attempt,
    test_archived_prestart_claim_is_not_stolen_before_expiry,
    test_archived_session_settles_without_fence_advance,
    test_archived_unclaimed_title_recovery_uses_actual_predecessor_without_attempt,
    test_coherent_paid_p4_corruption_refuses_in_live_reader_and_lost_stop,
    test_coherent_title_source_corruption_refuses_in_live_reader_and_lost_stop,
    test_coherently_changed_primary_descriptor_refuses_before_unknown_settlement,
    test_corrupt_retained_private_source_is_not_converted_to_stop_settlement,
    test_lost_stop_preserves_actual_committed_p4_and_saved_revisions,
    test_stale_candidate_writes_nothing_and_exact_original_is_positive,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_progressed_title_reproves_both_checkpoint_observations_without_writes",
    "test_actual_unknown_provider_cut_settles_without_replay_or_accounting_loss",
    "test_admitted_title_without_stop_recovers_with_original_physical_accounting_and_no_sdk_retry",
    "test_archived_interrupted_generation_settles_without_inventing_an_attempt",
    "test_archived_prestart_claim_is_not_stolen_before_expiry",
    "test_archived_session_settles_without_fence_advance",
    "test_archived_unclaimed_title_recovery_uses_actual_predecessor_without_attempt",
    "test_coherent_paid_p4_corruption_refuses_in_live_reader_and_lost_stop",
    "test_coherent_title_source_corruption_refuses_in_live_reader_and_lost_stop",
    "test_coherently_changed_primary_descriptor_refuses_before_unknown_settlement",
    "test_corrupt_retained_private_source_is_not_converted_to_stop_settlement",
    "test_lost_stop_preserves_actual_committed_p4_and_saved_revisions",
    "test_stale_candidate_writes_nothing_and_exact_original_is_positive",
]
