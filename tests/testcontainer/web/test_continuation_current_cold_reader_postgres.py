"""Current cold byte bounds and immutable policy tuples on PostgreSQL."""

from uuid import uuid4

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_current_cold_reader import (
    test_cold_event_chunk_uses_original_allowance as test_cold_event_chunk_uses_original_allowance,
)
from tests.unit.web.sessions.test_continuation_current_cold_reader import (
    test_cold_root_refuses_actual_oversize_before_decode as test_cold_root_refuses_actual_oversize_before_decode,
)
from tests.unit.web.sessions.test_continuation_current_cold_reader import (
    test_parent_policy_tuple_is_immutable as test_parent_policy_tuple_is_immutable,
)
from tests.unit.web.sessions.test_continuation_current_cold_reader import (
    test_writer_history_conditionally_projects_corrupt_oversize as test_writer_history_conditionally_projects_corrupt_oversize,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def continuation_parent(request: pytest.FixtureRequest):
    engine, _repository, _authority, _service, session_id = request.getfixturevalue("operation_store")
    return engine, str(session_id), str(uuid4())
