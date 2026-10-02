"""Quota policy administration over real Sessions tables."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, insert, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.conftest import _make_session

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.identity_authority import IdentityAdminActor
from elspeth.web.coordination.quota_policy_authority import (
    MAX_QUOTA_VALUE,
    QuotaDefaultMissing,
    QuotaPolicyMissing,
    QuotaSetterNotAdmin,
    QuotaTargetNotActive,
    QuotaTargetNotFound,
    QuotaTargetProviderKindMismatch,
    QuotaValueOutOfRange,
    RepositoryQuotaPolicyAuthority,
)
from elspeth.web.sessions.models import blobs_table, identities_table, identity_roles_table, quota_policies_table, token_usage_ledger_table


def _actor() -> IdentityAdminActor:
    return IdentityAdminActor(identity_id="root", on_behalf_of=None, console_request_id=None)


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        now = database_now(conn)
        for identity_id in ("root", "alice", "bob"):
            ensure_test_identity(conn, identity_id=identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="root-admin",
                identity_id="root",
                role="admin",
                granted_at=now - timedelta(days=1),
                granted_by_identity_id="root",
            )
        )
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="alice-policy",
                identity_id="alice",
                tokens_per_day=500,
                storage_bytes=2000,
                set_by_actor="system",
                set_at=now,
            )
        )
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="container-ceiling",
                identity_id=None,
                tokens_per_day=9000,
                storage_bytes=90000,
                set_by_actor="config",
                set_at=now,
            )
        )


def test_status_counts_all_live_blob_rows_and_unknown_tokens(engine: Engine) -> None:
    _seed(engine)
    with engine.begin() as conn:
        now = database_now(conn)
        _make_session(conn, session_id="a-1", user_id="alice")
        _make_session(conn, session_id="a-2", user_id="alice")
        _make_session(conn, session_id="b-1", user_id="bob")
        for blob_id, session_id, size in (("one", "a-1", 200), ("two", "a-2", 300), ("three", "b-1", 1000)):
            conn.execute(
                insert(blobs_table).values(
                    id=blob_id,
                    session_id=session_id,
                    filename=f"{blob_id}.csv",
                    mime_type="text/csv",
                    size_bytes=size,
                    content_hash="a" * 64,
                    storage_path=f"blobs/{blob_id}",
                    created_at=now,
                    created_by="user",
                    status="ready",
                )
            )
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id="unknown-tokens",
                identity_id="alice",
                source="composer",
                session_id="a-1",
                model="model",
                prompt_tokens=None,
                completion_tokens=1,
                recorded_at=now,
            )
        )
    authority = RepositoryQuotaPolicyAuthority(engine)
    alice = authority.status(identity_id="alice")
    bob = authority.status(identity_id="bob")
    assert (alice.storage_bytes_used, alice.tokens_used_today) == (500, None)
    assert (bob.storage_bytes_used, bob.tokens_used_today) == (1000, 0)
    assert alice.identity_policy is not None and alice.identity_policy.policy_id == "alice-policy"
    assert bob.identity_policy is None
    assert alice.container_policy is not None and alice.container_policy.policy_id == "container-ceiling"


def test_set_changes_one_dimension_retains_history_and_accepts_bigint(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    events = []
    result = authority.set_identity_policy(
        actor=_actor(),
        identity_id="alice",
        dimension="storage",
        value=2**40,
        default_tokens_per_day=None,
        default_storage_bytes=None,
        record=events.append,
    )
    assert len(events) == 1 and events[0] == result
    assert result.policy is not None and (result.policy.tokens_per_day, result.policy.storage_bytes) == (500, 2**40)
    assert result.previous is not None and result.previous.policy_id == "alice-policy"
    with engine.begin() as conn:
        old = conn.execute(select(quota_policies_table).where(quota_policies_table.c.policy_id == "alice-policy")).one()
        live = conn.execute(select(quota_policies_table).where(quota_policies_table.c.policy_id == result.policy.policy_id)).one()
    assert old.revoked_at is not None
    assert live.set_by_identity_id == "root" and live.set_by_actor == "identity"
    assert live.tokens_per_day == 500 and live.storage_bytes == 2**40


def test_policy_change_rolls_back_when_audit_fails(engine: Engine) -> None:
    _seed(engine)

    def audit_fails(_change: object) -> None:
        raise RuntimeError("Landscape failed")

    with pytest.raises(RuntimeError, match="Landscape failed"):
        RepositoryQuotaPolicyAuthority(engine).set_identity_policy(
            actor=_actor(),
            identity_id="alice",
            dimension="tokens",
            value=700,
            default_tokens_per_day=None,
            default_storage_bytes=None,
            record=audit_fails,
        )
    with engine.begin() as conn:
        rows = conn.execute(select(quota_policies_table).where(quota_policies_table.c.identity_id == "alice")).all()
    assert len(rows) == 1 and rows[0].policy_id == "alice-policy" and rows[0].revoked_at is None


def test_admin_role_is_reproved_and_target_must_be_active(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == "root-admin").values(revoked_at=database_now(conn))
        )
    with pytest.raises(QuotaSetterNotAdmin):
        authority.revoke_identity_policy(actor=_actor(), identity_id="alice", record=lambda _change: None)
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "root-admin").values(revoked_at=None))
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    with pytest.raises(QuotaTargetNotActive):
        authority.revoke_identity_policy(actor=_actor(), identity_id="alice", record=lambda _change: None)
    with pytest.raises(QuotaTargetNotFound):
        authority.status(identity_id="missing")


def test_legacy_service_provider_human_kind_cannot_set_or_receive_policy(engine: Engine) -> None:
    _seed(engine)
    with engine.begin() as conn:
        now = database_now(conn)
        ensure_test_identity(conn, identity_id="legacy", provider="service")
        conn.execute(
            insert(identity_roles_table).values(
                role_id="legacy-admin",
                identity_id="legacy",
                role="admin",
                granted_at=now - timedelta(days=1),
                granted_by_identity_id="root",
            )
        )
    authority = RepositoryQuotaPolicyAuthority(engine)
    with pytest.raises(QuotaSetterNotAdmin):
        authority.set_identity_policy(
            actor=IdentityAdminActor(identity_id="legacy", on_behalf_of=None, console_request_id=None),
            identity_id="alice",
            dimension="tokens",
            value=750,
            default_tokens_per_day=750,
            default_storage_bytes=1000,
            record=lambda _change: None,
        )
    with pytest.raises(QuotaTargetProviderKindMismatch):
        authority.set_identity_policy(
            actor=_actor(),
            identity_id="legacy",
            dimension="tokens",
            value=750,
            default_tokens_per_day=750,
            default_storage_bytes=1000,
            record=lambda _change: None,
        )


def test_service_admin_requires_explicit_provenance_and_human_cannot_claim_it(engine: Engine) -> None:
    _seed(engine)
    with engine.begin() as conn:
        now = database_now(conn)
        ensure_test_identity(conn, identity_id="service-admin", provider="service")
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "service-admin").values(kind="service"))
        conn.execute(
            insert(identity_roles_table).values(
                role_id="service-admin-role",
                identity_id="service-admin",
                role="admin",
                granted_at=now - timedelta(days=1),
                granted_by_identity_id="root",
            )
        )
    authority = RepositoryQuotaPolicyAuthority(engine)
    with pytest.raises(QuotaSetterNotAdmin):
        authority.revoke_identity_policy(
            actor=IdentityAdminActor(identity_id="service-admin", on_behalf_of=None, console_request_id=None),
            identity_id="alice",
            record=lambda _change: None,
        )
    with pytest.raises(QuotaSetterNotAdmin):
        authority.revoke_identity_policy(
            actor=IdentityAdminActor(identity_id="root", on_behalf_of="person", console_request_id="console-1"),
            identity_id="alice",
            record=lambda _change: None,
        )
    change = authority.revoke_identity_policy(
        actor=IdentityAdminActor(identity_id="service-admin", on_behalf_of="person", console_request_id="console-1"),
        identity_id="alice",
        record=lambda _change: None,
    )
    assert (change.actor_identity_id, change.on_behalf_of, change.console_request_id) == ("service-admin", "person", "console-1")


def test_first_policy_uses_other_default_and_revoke_is_audited(engine: Engine) -> None:
    _seed(engine)
    authority = RepositoryQuotaPolicyAuthority(engine)
    with pytest.raises(QuotaDefaultMissing):
        authority.set_identity_policy(
            actor=_actor(),
            identity_id="bob",
            dimension="tokens",
            value=750,
            default_tokens_per_day=None,
            default_storage_bytes=None,
            record=lambda _change: None,
        )
    with pytest.raises(QuotaValueOutOfRange):
        authority.set_identity_policy(
            actor=_actor(),
            identity_id="bob",
            dimension="tokens",
            value=MAX_QUOTA_VALUE + 1,
            default_tokens_per_day=1,
            default_storage_bytes=1,
            record=lambda _change: None,
        )
    set_event = authority.set_identity_policy(
        actor=_actor(),
        identity_id="bob",
        dimension="tokens",
        value=750,
        default_tokens_per_day=None,
        default_storage_bytes=1234,
        record=lambda _change: None,
    )
    assert set_event.policy is not None and set_event.policy.storage_bytes == 1234
    events = []
    revoked = authority.revoke_identity_policy(actor=_actor(), identity_id="bob", record=events.append)
    assert events == [revoked] and revoked.action == "revoke"
    assert revoked.previous == set_event.policy and revoked.policy is None
    assert authority.status(identity_id="bob").identity_policy is None
    with pytest.raises(QuotaPolicyMissing):
        authority.revoke_identity_policy(actor=_actor(), identity_id="bob", record=events.append)
