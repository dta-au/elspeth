"""Fenced-transaction fixtures for the identity-workflow authorities (Tasks I1, I2, I3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import FencedSession, FencedSessionWithPolicy, seed_token_policies

from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture
def fenced_session(tmp_path: Path) -> Iterator[FencedSession]:
    """An active identity ``alice``, a session she owns, and a registered token over one open transaction."""
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = SQLiteLocalSessionOperationAuthority(engine).create_session_with_initial_fence(
        user_id="alice", title="fenced", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    connection = engine.connect()
    transaction = connection.begin()
    token = _register_mutation_connection(connection)
    try:
        yield FencedSession(engine=engine, connection_token=token, identity_id="alice", session_id=str(session.id))
    finally:
        _unregister_mutation_connection(token)
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def fenced_session_with_policy(fenced_session: FencedSession) -> FencedSessionWithPolicy:
    """``fenced_session`` plus an active identity policy (1000/day) and container ceiling (5000/day)."""
    identity_policy_id, container_policy_id = seed_token_policies(
        _resolve_mutation_connection(fenced_session.connection_token), identity_id=fenced_session.identity_id
    )
    return FencedSessionWithPolicy(
        engine=fenced_session.engine,
        connection_token=fenced_session.connection_token,
        identity_id=fenced_session.identity_id,
        session_id=fenced_session.session_id,
        identity_policy_id=identity_policy_id,
        container_policy_id=container_policy_id,
    )
