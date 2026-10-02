"""Per-request workflow inspection is authorized and logged together."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, event, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
from elspeth.web.sessions.models import (
    approvals_table,
    audit_access_log_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import AuditAccessLogAuthority, AuditAccessLogWriteError, WorkflowInspectDenied


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave", "root"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id, role in (
            ("alice", "approver"),
            ("alice", "reviewer"),
            ("bob", "approver"),
            ("carol", "reviewer"),
            ("dave", "approver"),
        ):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"{identity_id}-{role}",
                    identity_id=identity_id,
                    role=role,
                    granted_at=now,
                    granted_by_identity_id="root",
                )
            )
        for session_id, state_id in (("session-1", "state-1"), ("session-2", "state-2")):
            conn.execute(
                insert(sessions_table).values(
                    id=session_id, user_id="alice", auth_provider_type="local", title="workflow", created_at=now, updated_at=now
                )
            )
            conn.execute(
                insert(composition_states_table).values(
                    id=state_id, session_id=session_id, version=1, provenance="session_seed", created_at=now
                )
            )
        conn.execute(
            insert(approvals_table).values(
                approval_id="approval-1",
                session_id="session-1",
                state_id="state-1",
                binding_json={},
                requested_by_identity_id="alice",
                approver_identity_id="bob",
                requested_at=now,
            )
        )
        conn.execute(
            insert(review_requests_table).values(
                request_id="review-1",
                session_id="session-2",
                state_id="state-2",
                requested_by_identity_id="alice",
                reviewer_identity_id=None,
                requested_at=now,
            )
        )
    return engine


def _record(engine: Engine, *, session_id: str = "session-1", state_id: str = "state-1", caller: str = "dave"):
    return RepositoryAuditAccessLogAuthority(engine).record_workflow_inspect(
        session_id=session_id,
        state_id=state_id,
        requesting_principal=caller,
        ip_address="192.0.2.1",
    )


def _count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(audit_access_log_table)).scalar_one()


def test_role_eligible_approver_other_than_addressee_can_inspect_and_writer_fixes_privacy_fields(seeded: Engine) -> None:
    signature = inspect.signature(AuditAccessLogAuthority.record_workflow_inspect)
    assert set(signature.parameters) == {"self", "session_id", "state_id", "requesting_principal", "ip_address"}

    record = _record(seeded)

    assert record.requesting_principal == "dave"
    assert record.writer_principal == "workflow_inspect"
    assert record.request_path == "/api/workflow/inspect/session-1/state-1"
    assert record.query_args == {}
    assert record.ip_address == "192.0.2.1"
    assert _count(seeded) == 1


def test_reviewer_can_inspect_unaddressed_open_request(seeded: Engine) -> None:
    record = _record(seeded, session_id="session-2", state_id="state-2", caller="carol")
    assert record.writer_principal == "workflow_inspect"
    assert _count(seeded) == 1


@pytest.mark.parametrize(
    ("session_id", "state_id", "caller"),
    [
        ("session-1", "state-1", "alice"),  # author holds both roles
        ("session-1", "state-1", "carol"),  # reviewer role cannot use approval request
        ("session-2", "state-2", "dave"),  # approver role cannot use review request
        ("session-1", "state-2", "dave"),  # state belongs to another session
        ("session-2", "state-1", "carol"),
        ("missing", "state-1", "dave"),
    ],
)
def test_denied_reads_write_no_audit_row(seeded: Engine, session_id: str, state_id: str, caller: str) -> None:
    with pytest.raises(WorkflowInspectDenied):
        _record(seeded, session_id=session_id, state_id=state_id, caller=caller)
    assert _count(seeded) == 0


@pytest.mark.parametrize("change", ["disabled", "expired", "revoked", "scoped", "closed", "archived"])
def test_access_ends_with_identity_grant_request_or_session(seeded: Engine, change: str) -> None:
    with seeded.begin() as conn:
        if change == "disabled":
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "dave").values(access_state="disabled"))
        elif change == "expired":
            conn.execute(
                update(identity_roles_table)
                .where(identity_roles_table.c.role_id == "dave-approver")
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif change == "revoked":
            conn.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "dave-approver").values(revoked_at=datetime.now(UTC))
            )
        elif change == "scoped":
            conn.execute(update(identity_roles_table).where(identity_roles_table.c.role_id == "dave-approver").values(scope="other"))
        elif change == "closed":
            conn.execute(update(approvals_table).where(approvals_table.c.approval_id == "approval-1").values(decision="approved"))
        else:
            conn.execute(update(sessions_table).where(sessions_table.c.id == "session-1").values(archived_at=datetime.now(UTC)))
    with pytest.raises(WorkflowInspectDenied):
        _record(seeded)
    assert _count(seeded) == 0


def test_attestation_closes_reviewer_inspection(seeded: Engine) -> None:
    with seeded.begin() as conn:
        conn.execute(
            insert(review_attestations_table).values(
                attestation_id="attestation-1",
                session_id="session-2",
                state_id="state-2",
                payload_digest="sha256:" + "ab" * 32,
                reviewer_identity_id="carol",
                author_identity_id="alice",
                attested_at=datetime.now(UTC) + timedelta(seconds=1),
                verdict="signed_off",
            )
        )
    with pytest.raises(WorkflowInspectDenied):
        _record(seeded, session_id="session-2", state_id="state-2", caller="carol")
    assert _count(seeded) == 0


@pytest.mark.parametrize("change", ["cancelled", "addressed_elsewhere", "role_expired", "role_revoked"])
def test_reviewer_inspection_requires_a_matching_open_request_and_live_role(seeded: Engine, change: str) -> None:
    with seeded.begin() as conn:
        if change == "cancelled":
            conn.execute(
                update(review_requests_table).where(review_requests_table.c.request_id == "review-1").values(cancelled_at=datetime.now(UTC))
            )
        elif change == "addressed_elsewhere":
            conn.execute(
                update(review_requests_table).where(review_requests_table.c.request_id == "review-1").values(reviewer_identity_id="bob")
            )
        elif change == "role_expired":
            conn.execute(
                update(identity_roles_table)
                .where(identity_roles_table.c.role_id == "carol-reviewer")
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        else:
            conn.execute(
                update(identity_roles_table).where(identity_roles_table.c.role_id == "carol-reviewer").values(revoked_at=datetime.now(UTC))
            )
    with pytest.raises(WorkflowInspectDenied):
        _record(seeded, session_id="session-2", state_id="state-2", caller="carol")
    assert _count(seeded) == 0


@pytest.mark.parametrize(("caller", "session_id", "state_id"), [("dave", "session-1", "state-1"), ("carol", "session-2", "state-2")])
def test_malformed_service_provider_with_human_kind_cannot_inspect(seeded: Engine, caller: str, session_id: str, state_id: str) -> None:
    with seeded.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == caller).values(provider="service", kind="human"))
    with pytest.raises(WorkflowInspectDenied):
        _record(seeded, session_id=session_id, state_id=state_id, caller=caller)
    assert _count(seeded) == 0


def test_audit_store_failure_fails_closed_without_an_access_row(seeded: Engine) -> None:
    def fail_audit_insert(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:
        if statement.startswith("INSERT INTO audit_access_log"):
            raise SQLAlchemyError("audit write failed")

    event.listen(seeded, "before_cursor_execute", fail_audit_insert)
    try:
        with pytest.raises(AuditAccessLogWriteError):
            _record(seeded)
    finally:
        event.remove(seeded, "before_cursor_execute", fail_audit_insert)
    assert _count(seeded) == 0
