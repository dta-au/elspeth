"""The same current native/failure producer source joins on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_message_families import (
    test_actual_selected_failed_result_rebuilds_current_policy_schema_without_new_rows,
    test_actual_selected_native_response_audit_and_shelf_restore_without_new_rows,
    test_failed_result_without_actual_private_plugin_identity_cannot_mint_reference_after_positive,
    test_native_current_owner_refuses_unselected_argument_wire_after_actual_positive,
    test_native_current_source_refuses_one_coherent_changed_fact_after_real_positive,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_selected_failed_result_rebuilds_current_policy_schema_without_new_rows",
    "test_actual_selected_native_response_audit_and_shelf_restore_without_new_rows",
    "test_failed_result_without_actual_private_plugin_identity_cannot_mint_reference_after_positive",
    "test_native_current_owner_refuses_unselected_argument_wire_after_actual_positive",
    "test_native_current_source_refuses_one_coherent_changed_fact_after_real_positive",
]
