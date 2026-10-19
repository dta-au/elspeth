"""Quota SQL constraints survive direct writes outside application validation."""

import pytest
from tests.helpers.fenced_session import FencedSession
from tests.helpers.quota_schema_assertions import assert_quota_custody_constraint, assert_quota_measure_constraint

from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection


@pytest.mark.parametrize("column", ["prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"])
def test_quota_measure_constraint(fenced_session: FencedSession, column: str) -> None:
    assert_quota_measure_constraint(
        _resolve_mutation_connection(fenced_session.connection_token),
        session_id=fenced_session.session_id,
        identity_id=fenced_session.identity_id,
        column=column,
    )


@pytest.mark.parametrize("mismatch", ["identity_id", "session_id", "source", "run_id", "second_attempt"])
def test_quota_custody_constraint(fenced_session: FencedSession, mismatch: str) -> None:
    assert_quota_custody_constraint(
        _resolve_mutation_connection(fenced_session.connection_token),
        session_id=fenced_session.session_id,
        identity_id=fenced_session.identity_id,
        mismatch=mismatch,
    )
