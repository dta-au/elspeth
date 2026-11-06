"""The same actual committed-P4 recovery/new-start checks on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.coordination.test_continuation_p4_recovery_start import (
    test_recovery_preserves_refusal_for_foreign_prepared_uncommitted_business_sql,
    test_recovery_refuses_live_owner_and_wrong_generation_without_writes,
    test_released_committed_p4_cut_restarts_through_real_recovery_claim_and_fence,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_recovery_preserves_refusal_for_foreign_prepared_uncommitted_business_sql",
    "test_recovery_refuses_live_owner_and_wrong_generation_without_writes",
    "test_released_committed_p4_cut_restarts_through_real_recovery_claim_and_fence",
]
