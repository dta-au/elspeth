"""Same real recovery/expiry/Stop/current-history cases on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.coordination.test_continuation_p4_recovery_boundaries import (
    test_expired_committed_owner_requires_backend_liveness_then_real_new_fence,
    test_reclaimed_real_segment_claim_and_old_checkpoint_cannot_start_or_restore,
    test_recovery_refuses_exact_owned_corruption_without_partial_transition,
    test_restart_stop_allows_only_exact_committed_pending_application,
    test_two_real_recovery_callers_commit_one_generation_and_one_root_cas,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_expired_committed_owner_requires_backend_liveness_then_real_new_fence",
    "test_reclaimed_real_segment_claim_and_old_checkpoint_cannot_start_or_restore",
    "test_recovery_refuses_exact_owned_corruption_without_partial_transition",
    "test_restart_stop_allows_only_exact_committed_pending_application",
    "test_two_real_recovery_callers_commit_one_generation_and_one_root_cas",
]
