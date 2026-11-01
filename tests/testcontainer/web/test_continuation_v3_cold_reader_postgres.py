"""V3 cold chunk transfer bounds against actual PostgreSQL octet lengths."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import insert
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
)
from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_store as operation_store,
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
    test_v3_writer_history_reads_conditionally_project_corrupt_oversize as test_v3_writer_history_reads_conditionally_project_corrupt_oversize,
)

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import composer_async_operations_table as jobs

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def continuation_parent(request: pytest.FixtureRequest):
    engine, _repository, _authority, _service, session_id = request.getfixturevalue("operation_store")
    sid, operation_id = str(session_id), str(uuid4())
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(
            insert(jobs).values(
                session_id=sid,
                operation_id=operation_id,
                kind="compose_message",
                status="queued",
                phase="queued",
                current_segment_sequence=1,
                ownership_generation=1,
                checkpoint_id="initial-checkpoint",
                ingress_phase="pending",
                reserved_user_message_id="original",
                request_hash="a" * 64,
                continuation_policy_version="composer.continuation.policy.v1",
                actor_user_id="alice",
                request_json="{}",
                created_at=now,
                updated_at=now,
            )
        )
    return engine, sid, operation_id
