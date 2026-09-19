"""The approver audit view follows only live oversight edges."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, insert, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.workflow_scope_reader import (
    MAX_APPROVER_SCOPE_DEPTH,
    MAX_AUDIT_VIEW_ROWS,
    RepositoryWorkflowScopeReader,
)
from elspeth.web.sessions.models import (
    approvals_table,
    identities_table,
    identity_relationships_table,
    identity_roles_table,
    review_attestations_table,
    sessions_table,
)


def _identity(conn, identity_id: str) -> None:
    ensure_test_identity(conn, identity_id=identity_id)


def _edge(conn, from_id: str, to_id: str, *, revoked: bool = False) -> None:
    conn.execute(
        insert(identity_relationships_table).values(
            relationship_id=f"edge-{from_id}-{to_id}",
            from_identity_id=from_id,
            to_identity_id=to_id,
            relationship_type="approver",
            asserted_by_identity_id="root",
            asserted_at=datetime.now(UTC),
            revoked_at=datetime.now(UTC) if revoked else None,
        )
    )


def test_scoped_reader_filters_unrelated_history_and_cycles(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", "alice", "bob", "outsider"):
            _identity(conn, identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver", identity_id="lead", role="approver", granted_at=now, granted_by_identity_id="root"
            )
        )
        _edge(conn, "lead", "alice")
        _edge(conn, "alice", "bob")
        _edge(conn, "bob", "lead")  # seeded pre-existing cycle must terminate
        _edge(conn, "lead", "outsider", revoked=True)
        for identity_id in ("alice", "bob", "outsider"):
            conn.execute(
                insert(sessions_table).values(
                    id=f"session-{identity_id}",
                    user_id=identity_id,
                    auth_provider_type="local",
                    title="history",
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(approvals_table).values(
                    approval_id=f"approval-{identity_id}",
                    session_id=f"session-{identity_id}",
                    state_id=f"state-{identity_id}",
                    binding_json={},
                    requested_by_identity_id=identity_id,
                    approver_identity_id="lead",
                    requested_at=now,
                    decision="approved",
                )
            )
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=f"attestation-{identity_id}",
                    session_id=f"session-{identity_id}",
                    state_id=f"state-{identity_id}",
                    payload_digest="sha256:" + "ab" * 32,
                    reviewer_identity_id="lead",
                    author_identity_id=identity_id,
                    attested_at=now,
                    verdict="signed_off",
                )
            )

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="lead")

    assert scope.caller_is_approver
    assert scope.identity_ids == ("alice", "bob")
    assert not scope.truncated
    assert {row.approval_id for row in scope.approvals} == {"approval-alice", "approval-bob"}
    assert {row.attestation_id for row in scope.attestations} == {"attestation-alice", "attestation-bob"}


def test_scope_refuses_inactive_expired_or_revoked_approver(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", "alice"):
            _identity(conn, identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver",
                identity_id="lead",
                role="approver",
                granted_at=now,
                granted_by_identity_id="root",
                expires_at=now - timedelta(seconds=1),
            )
        )
        _edge(conn, "lead", "alice")
    reader = RepositoryWorkflowScopeReader(engine)
    assert not reader.read_audit_scope(caller="lead").caller_is_approver
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.role_id == "lead-approver").values(expires_at=None, revoked_at=now)
        )
    assert not reader.read_audit_scope(caller="lead").caller_is_approver


def test_depth_limit_reports_truncation_without_leaking_ninth_hop(engine: Engine) -> None:
    assert MAX_APPROVER_SCOPE_DEPTH == 8
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", *(f"person-{index}" for index in range(1, 10))):
            _identity(conn, identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver", identity_id="lead", role="approver", granted_at=now, granted_by_identity_id="root"
            )
        )
        previous = "lead"
        for index in range(1, 10):
            current = f"person-{index}"
            _edge(conn, previous, current)
            previous = current

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="lead")

    assert scope.identity_ids == tuple(f"person-{index}" for index in range(1, 9))
    assert scope.truncated


def test_malformed_service_provider_with_human_kind_has_no_audit_scope(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", "alice"):
            _identity(conn, identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver", identity_id="lead", role="approver", granted_at=now, granted_by_identity_id="root"
            )
        )
        _edge(conn, "lead", "alice")
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "lead").values(provider="service", kind="human"))
    assert not RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="lead").caller_is_approver


def test_audit_history_is_bounded_to_latest_rows(engine: Engine) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("root", "lead", "alice"):
            _identity(conn, identity_id)
        conn.execute(
            insert(identity_roles_table).values(
                role_id="lead-approver", identity_id="lead", role="approver", granted_at=now, granted_by_identity_id="root"
            )
        )
        _edge(conn, "lead", "alice")
        conn.execute(
            insert(sessions_table).values(
                id="session-alice", user_id="alice", auth_provider_type="local", title="history", created_at=now, updated_at=now
            )
        )
        for index in range(MAX_AUDIT_VIEW_ROWS + 1):
            conn.execute(
                insert(approvals_table).values(
                    approval_id=f"approval-{index:04d}",
                    session_id="session-alice",
                    state_id=f"state-{index:04d}",
                    binding_json={},
                    requested_by_identity_id="alice",
                    approver_identity_id="lead",
                    requested_at=now + timedelta(seconds=index),
                    decision="approved",
                )
            )

    scope = RepositoryWorkflowScopeReader(engine).read_audit_scope(caller="lead")

    assert len(scope.approvals) == MAX_AUDIT_VIEW_ROWS
    assert scope.approvals[0].approval_id == f"approval-{MAX_AUDIT_VIEW_ROWS:04d}"
    assert scope.approvals[-1].approval_id == "approval-0001"
