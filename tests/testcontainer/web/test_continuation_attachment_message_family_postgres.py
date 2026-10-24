"""The same actual current attachment-message source joins on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_attachment_message_family import (
    test_attachment_recipe_refuses_one_changed_original_producer_fact_after_positive,
    test_current_attachment_family_refuses_another_call_locator_after_real_positive,
    test_current_attachment_family_refuses_unselected_wire_after_real_positive,
    test_current_attachment_family_restores_exact_original_wire_and_guard_without_new_read,
)

pytestmark = pytest.mark.testcontainer
__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_attachment_recipe_refuses_one_changed_original_producer_fact_after_positive",
    "test_current_attachment_family_refuses_another_call_locator_after_real_positive",
    "test_current_attachment_family_refuses_unselected_wire_after_real_positive",
    "test_current_attachment_family_restores_exact_original_wire_and_guard_without_new_read",
]
