"""Same actual post-read timing/publication/history joins on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_attachment_timing import (
    test_actual_post_read_observation_retains_original_start_and_completed_elapsed,
    test_completed_read_timing_refuses_changed_original_binding_after_positive,
    test_legacy_history_only_result_does_not_invent_completed_read_timing,
)

pytestmark = pytest.mark.testcontainer
__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_actual_post_read_observation_retains_original_start_and_completed_elapsed",
    "test_completed_read_timing_refuses_changed_original_binding_after_positive",
    "test_legacy_history_only_result_does_not_invent_completed_read_timing",
]
