"""Configured container caps are durable without inventing a storage limit."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, insert, select, text
from sqlalchemy.exc import IntegrityError
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.quota_policy_authority import QuotaPolicyRefusal, RepositoryQuotaPolicyAuthority
from elspeth.web.sessions.models import quota_policies_table

NOW = datetime(2026, 10, 15, tzinfo=UTC)


def _seed(engine: Engine, *, actor: str = "config", storage: int | None = 800) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="existing", identity_id=None, tokens_per_day=500, storage_bytes=storage, set_by_actor=actor, set_at=NOW
            )
        )


def test_configured_cap_is_created_once_and_matching_replicas_retain_it(engine: Engine) -> None:
    authority = RepositoryQuotaPolicyAuthority(engine)
    recorded = []
    first = authority.ensure_container_token_policy(tokens_per_day=500, record=recorded.append)
    second = authority.ensure_container_token_policy(tokens_per_day=500, record=recorded.append)
    assert first == second and first is not None
    assert (first.tokens_per_day, first.storage_bytes) == (500, None)
    assert recorded == [first]
    with engine.connect() as conn:
        (row,) = conn.execute(select(quota_policies_table)).all()
    assert (row.policy_id, row.set_by_actor, row.set_by_identity_id, row.revoked_at) == (first.policy_id, "config", None, None)


@pytest.mark.parametrize("actor", ["config", "operator", "system"])
def test_existing_numeric_storage_and_explicit_policy_are_preserved(engine: Engine, actor: str) -> None:
    _seed(engine, actor=actor)
    recorded = []
    policy = RepositoryQuotaPolicyAuthority(engine).ensure_container_token_policy(tokens_per_day=500, record=recorded.append)
    assert policy is not None and (policy.policy_id, policy.storage_bytes) == ("existing", 800)
    assert recorded == []


@pytest.mark.parametrize("actor", ["operator", "system"])
def test_explicit_policy_retains_precedence_over_configuration(engine: Engine, actor: str) -> None:
    _seed(engine, actor=actor)
    policy = RepositoryQuotaPolicyAuthority(engine).ensure_container_token_policy(tokens_per_day=999, record=lambda _policy: None)
    assert policy is not None and (policy.tokens_per_day, policy.storage_bytes) == (500, 800)


def test_configuration_drift_refuses_without_replacing_or_revoking(engine: Engine) -> None:
    _seed(engine)
    with engine.connect() as conn:
        before = conn.execute(select(quota_policies_table)).all()
    recorded = []
    with pytest.raises(QuotaPolicyRefusal, match="conflict"):
        RepositoryQuotaPolicyAuthority(engine).ensure_container_token_policy(tokens_per_day=999, record=recorded.append)
    with engine.connect() as conn:
        assert conn.execute(select(quota_policies_table)).all() == before
    assert recorded == []


def test_absent_configuration_does_not_create_or_revoke(engine: Engine) -> None:
    authority = RepositoryQuotaPolicyAuthority(engine)
    assert authority.ensure_container_token_policy(tokens_per_day=None, record=lambda _policy: None) is None
    _seed(engine)
    policy = authority.ensure_container_token_policy(tokens_per_day=None, record=lambda _policy: None)
    assert policy is not None and policy.policy_id == "existing"


def test_configuration_audit_failure_rolls_back_new_policy(engine: Engine) -> None:
    def fail(_policy: object) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        RepositoryQuotaPolicyAuthority(engine).ensure_container_token_policy(tokens_per_day=500, record=fail)
    with engine.connect() as conn:
        assert conn.execute(select(quota_policies_table)).all() == []


def test_container_singleton_is_enforced_across_actors(engine: Engine) -> None:
    _seed(engine)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="second", identity_id=None, tokens_per_day=600, storage_bytes=900, set_by_actor="operator", set_at=NOW
            )
        )


def test_ambiguous_legacy_population_refuses_without_writes(engine: Engine) -> None:
    # A damaged or pre-conversion store must not select an arbitrary winner.
    _seed(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_quota_policies_active_container_ceiling"))
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="second", identity_id=None, tokens_per_day=600, storage_bytes=900, set_by_actor="operator", set_at=NOW
            )
        )
    recorded = []
    with pytest.raises(QuotaPolicyRefusal, match="ambiguous"):
        RepositoryQuotaPolicyAuthority(engine).ensure_container_token_policy(tokens_per_day=500, record=recorded.append)
    assert recorded == []
    with engine.connect() as conn:
        assert len(conn.execute(select(quota_policies_table)).all()) == 2


def test_identity_storage_remains_mandatory(engine: Engine) -> None:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="identity", identity_id="alice", tokens_per_day=500, storage_bytes=None, set_by_actor="system", set_at=NOW
            )
        )
