"""V3 cold chunk transfer bounds against actual PostgreSQL octet lengths."""

from __future__ import annotations

from uuid import uuid4

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_legacy_parent_policy_version_is_immutable as test_legacy_parent_policy_version_is_immutable,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v1_cold_root_keeps_legacy_read_contract as test_v1_cold_root_keeps_legacy_read_contract,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v2_cold_root_keeps_legacy_read_contract as test_v2_cold_root_keeps_legacy_read_contract,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v3_cold_event_chunk_uses_same_original_allowance as test_v3_cold_event_chunk_uses_same_original_allowance,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v3_cold_root_projects_payload_conditionally_at_original_allowance as test_v3_cold_root_projects_payload_conditionally_at_original_allowance,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v3_cold_root_refuses_actual_oversize_before_decode as test_v3_cold_root_refuses_actual_oversize_before_decode,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v3_parent_policy_tuple_is_immutable as test_v3_parent_policy_tuple_is_immutable,
)
from tests.unit.web.sessions.test_continuation_v3_cold_reader import (
    test_v3_writer_history_reads_conditionally_project_corrupt_oversize as test_v3_writer_history_reads_conditionally_project_corrupt_oversize,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def continuation_parent(request: pytest.FixtureRequest):
    engine, _repository, _authority, _service, session_id = request.getfixturevalue("operation_store")
    sid, operation_id = str(session_id), str(uuid4())
    return engine, sid, operation_id
