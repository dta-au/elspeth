"""The same real application and transaction rollback checks on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_p4_application_store import (
    test_actual_application_rolls_back_chunks_events_and_root_on_original_error,
    test_application_refuses_wrong_nonempty_frontier_receipt_after_real_positive,
    test_current_applied_journal_refuses_a_different_actual_response_after_real_positive,
    test_current_journal_rechecks_actual_old_tool_cursor_after_real_positive,
    test_owed_application_drains_stop_and_expiry_without_another_business_write,
    test_two_actual_applications_restore_one_shared_graph_and_do_not_repeat_business_sql,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_application_rolls_back_chunks_events_and_root_on_original_error",
    "test_application_refuses_wrong_nonempty_frontier_receipt_after_real_positive",
    "test_current_applied_journal_refuses_a_different_actual_response_after_real_positive",
    "test_current_journal_rechecks_actual_old_tool_cursor_after_real_positive",
    "test_owed_application_drains_stop_and_expiry_without_another_business_write",
    "test_two_actual_applications_restore_one_shared_graph_and_do_not_repeat_business_sql",
]
