"""The actual private byte projection on the supported PostgreSQL driver."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_private_document import (
    test_private_sql_consumers_keep_strict_utf8_error_and_original_cause,
    test_private_sql_does_not_return_excess_payload_with_false_small_reference,
    test_private_sql_refuses_one_binding_fault_after_actual_positive,
    test_private_sql_supported_actual_bytes,
    test_private_sql_suppresses_actual_excess_with_deliberately_corrupt_small_metadata,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_private_sql_consumers_keep_strict_utf8_error_and_original_cause",
    "test_private_sql_does_not_return_excess_payload_with_false_small_reference",
    "test_private_sql_refuses_one_binding_fault_after_actual_positive",
    "test_private_sql_supported_actual_bytes",
    "test_private_sql_suppresses_actual_excess_with_deliberately_corrupt_small_metadata",
]
