"""Quota nonnegative measures and relational custody on PostgreSQL."""

import pytest
from tests.helpers.fenced_session import FencedSession
from tests.helpers.quota_schema_assertions import assert_quota_custody_constraint, assert_quota_measure_constraint
from tests.testcontainer.web import test_quota_authority_postgres

from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection

pytestmark = pytest.mark.testcontainer
pg_fenced = test_quota_authority_postgres.pg_fenced


@pytest.mark.parametrize("column", ["prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"])
def test_quota_measure_constraint(pg_fenced: FencedSession, column: str) -> None:
    assert_quota_measure_constraint(
        _resolve_mutation_connection(pg_fenced.connection_token),
        session_id=pg_fenced.session_id,
        identity_id=pg_fenced.identity_id,
        column=column,
    )


@pytest.mark.parametrize("mismatch", ["identity_id", "session_id", "source", "run_id", "second_attempt"])
def test_quota_custody_constraint(pg_fenced: FencedSession, mismatch: str) -> None:
    assert_quota_custody_constraint(
        _resolve_mutation_connection(pg_fenced.connection_token),
        session_id=pg_fenced.session_id,
        identity_id=pg_fenced.identity_id,
        mismatch=mismatch,
    )
