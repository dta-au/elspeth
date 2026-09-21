"""Approval authority regressions for role eligibility and terminal history."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select, update
from sqlalchemy.dialects import postgresql
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.approval_authority import (
    BOUND_EVIDENCE_FIELDS,
    EXCLUDED_EVIDENCE_FIELDS,
    ApprovalAlreadyDecided,
    ApprovalAuthorIsApprover,
    ApprovalBinding,
    ApprovalNoteRequired,
    ApprovalNotFound,
    ApprovalTransactionAuthority,
    ApproverRoleRequired,
    RepositoryApprovalAuthority,
    supersede_open_approvals,
)
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    approval_decisions_table,
    approvals_table,
    composition_states_table,
    identities_table,
    identity_roles_table,
    sessions_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)
BINDING = ApprovalBinding(
    config_hash="config",
    canonical_version="canonical",
    runtime_val_manifest_sha256="manifest",
    openrouter_catalog_sha256="catalog",
    binding_generation_fingerprint="generation",
    policy_hash="policy",
)


def test_transaction_locks_session_before_participant_identity() -> None:
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="author")
        conn.execute(insert(sessions_table).values(id="session", user_id="author", title="approval", created_at=NOW, updated_at=NOW))
    statements: list[str] = []

    def capture(_conn, statement, _multiparams, _params, _execution_options) -> None:
        statements.append(str(statement.compile(dialect=postgresql.dialect())))

    def mutation(token: str) -> None:
        conn = _resolve_mutation_connection(token)
        conn.execute(select(identities_table).where(identities_table.c.identity_id == "author").with_for_update()).one()

    event.listen(engine, "before_execute", capture)
    try:
        ApprovalTransactionAuthority(engine).run("session", mutation)
    finally:
        event.remove(engine, "before_execute", capture)
        engine.dispose()

    assert len(statements) == 2
    assert "FROM sessions" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "FROM identities" in statements[1]
    assert "FOR UPDATE" in statements[1]


def test_evidence_field_decision_is_closed_and_keeps_generation_but_excludes_snapshot() -> None:
    evidence_names = {field.name for field in fields(WebPluginPolicyEvidence)}
    assert evidence_names == BOUND_EVIDENCE_FIELDS | EXCLUDED_EVIDENCE_FIELDS
    assert frozenset() == BOUND_EVIDENCE_FIELDS & EXCLUDED_EVIDENCE_FIELDS
    assert "binding_generation_fingerprint" in BOUND_EVIDENCE_FIELDS
    assert "snapshot_hash" in EXCLUDED_EVIDENCE_FIELDS


def _seed(fenced_session) -> None:
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    for identity_id in ("addressed", "cover", "outsider"):
        ensure_test_identity(conn, identity_id=identity_id)
    for identity_id in ("addressed", "cover"):
        conn.execute(
            insert(identity_roles_table).values(
                role_id=f"approver-{identity_id}",
                identity_id=identity_id,
                role="approver",
                granted_by_identity_id="alice",
                granted_at=NOW,
            )
        )
    conn.execute(
        insert(composition_states_table).values(
            id="state-1",
            session_id=fenced_session.session_id,
            version=1,
            is_valid=True,
            provenance="session_seed",
            created_at=NOW,
        )
    )


def _request(fenced_session, events: list, *, state_id: str = "state-1"):
    return RepositoryApprovalAuthority.request(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        state_id=state_id,
        binding=BINDING,
        requested_by="alice",
        approver="addressed",
        note="please review",
        record=events.append,
    )


def test_any_active_non_author_approver_can_decide_and_addressed_is_first(fenced_session) -> None:
    _seed(fenced_session)
    events: list = []
    request = _request(fenced_session, events)
    with pytest.raises(ApproverRoleRequired):
        RepositoryApprovalAuthority.decide(
            fenced_session.connection_token,
            approval_id=request.approval_id,
            decided_by="outsider",
            decision="approved",
            note=None,
            record=events.append,
        )
    with pytest.raises(ApprovalAuthorIsApprover):
        RepositoryApprovalAuthority.decide(
            fenced_session.connection_token,
            approval_id=request.approval_id,
            decided_by="alice",
            decision="approved",
            note=None,
            record=events.append,
        )
    decided = RepositoryApprovalAuthority.decide(
        fenced_session.connection_token,
        approval_id=request.approval_id,
        decided_by="cover",
        decision="approved",
        note=None,
        record=events.append,
    )
    assert decided.decision == "approved"
    assert decided.decided_by_identity_id == "cover"
    assert [event.decision for event in events] == [None, "approved"]
    with pytest.raises(ApprovalAlreadyDecided):
        RepositoryApprovalAuthority.decide(
            fenced_session.connection_token,
            approval_id=request.approval_id,
            decided_by="addressed",
            decision="approved",
            note=None,
            record=events.append,
        )


def test_historical_service_provider_cannot_decide_with_human_kind_and_approver_role(fenced_session) -> None:
    _seed(fenced_session)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    conn.execute(update(identities_table).where(identities_table.c.identity_id == "cover").values(provider="service"))
    events: list = []
    request = _request(fenced_session, events)
    with pytest.raises(ApproverRoleRequired):
        RepositoryApprovalAuthority.decide(
            fenced_session.connection_token,
            approval_id=request.approval_id,
            decided_by="cover",
            decision="approved",
            note=None,
            record=events.append,
        )
    assert conn.execute(select(identities_table.c.kind).where(identities_table.c.identity_id == "cover")).scalar_one() == "human"
    assert len(events) == 1


def test_later_rejection_retires_earlier_approval_and_audits_it(fenced_session) -> None:
    _seed(fenced_session)
    events: list = []
    first = _request(fenced_session, events)
    RepositoryApprovalAuthority.decide(
        fenced_session.connection_token,
        approval_id=first.approval_id,
        decided_by="addressed",
        decision="approved",
        note=None,
        record=events.append,
    )
    second = _request(fenced_session, events)
    with pytest.raises(ApprovalNoteRequired):
        RepositoryApprovalAuthority.decide(
            fenced_session.connection_token,
            approval_id=second.approval_id,
            decided_by="cover",
            decision="rejected",
            note="  ",
            record=events.append,
        )
    retired: list = []

    def record_bundle(decided, supersessions) -> None:
        events.append(decided)
        retired.extend(supersessions)

    RepositoryApprovalAuthority.decide(
        fenced_session.connection_token,
        approval_id=second.approval_id,
        decided_by="cover",
        decision="rejected",
        note="needs changes",
        record=events.append,
        record_rejection_bundle=record_bundle,
    )
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    rows = conn.execute(select(approvals_table).order_by(approvals_table.c.requested_at, approvals_table.c.approval_id)).all()
    assert {row.approval_id: row.decision for row in rows} == {
        first.approval_id: "superseded",
        second.approval_id: "rejected",
    }
    assert (
        RepositoryApprovalAuthority.approved_bindings(
            fenced_session.connection_token, session_id=fenced_session.session_id, state_id="state-1"
        )
        == ()
    )
    history = conn.execute(select(approval_decisions_table)).all()
    assert {(row.approval_id, row.decision) for row in history} == {
        (first.approval_id, "approved"),
        (second.approval_id, "rejected"),
    }
    assert [(item.approval.approval_id, item.cause, item.trigger_approval_id) for item in retired] == [
        (first.approval_id, "later_rejection", second.approval_id)
    ]


def test_failed_rejection_audit_batch_rolls_back_decision_and_retirement(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'rejection-rollback.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        for identity_id in ("alice", "addressed", "cover"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id in ("addressed", "cover"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"approver-{identity_id}",
                    identity_id=identity_id,
                    role="approver",
                    granted_by_identity_id="alice",
                    granted_at=NOW,
                )
            )
        conn.execute(insert(sessions_table).values(id="session-1", user_id="alice", title="approval", created_at=NOW, updated_at=NOW))
        conn.execute(
            insert(composition_states_table).values(
                id="state-1", session_id="session-1", version=1, is_valid=True, provenance="session_seed", created_at=NOW
            )
        )
    authority = ApprovalTransactionAuthority(engine)

    def request():
        return authority.run(
            "session-1",
            lambda token: RepositoryApprovalAuthority.request(
                token,
                session_id="session-1",
                state_id="state-1",
                binding=BINDING,
                requested_by="alice",
                approver="addressed",
                note=None,
                record=lambda _record: None,
            ),
        )

    first = request()
    authority.run(
        "session-1",
        lambda token: RepositoryApprovalAuthority.decide(
            token,
            approval_id=first.approval_id,
            decided_by="addressed",
            decision="approved",
            note=None,
            record=lambda _record: None,
        ),
    )
    second = request()

    def fail_batch(_decided, supersessions) -> None:
        assert [item.approval.approval_id for item in supersessions] == [first.approval_id]
        raise RuntimeError("injected Landscape batch failure")

    with pytest.raises(RuntimeError, match="injected Landscape batch failure"):
        authority.run(
            "session-1",
            lambda token: RepositoryApprovalAuthority.decide(
                token,
                approval_id=second.approval_id,
                decided_by="cover",
                decision="rejected",
                note="needs changes",
                record=lambda _record: None,
                record_rejection_bundle=fail_batch,
            ),
        )
    with engine.connect() as conn:
        rows = conn.execute(select(approvals_table.c.approval_id, approvals_table.c.decision)).all()
        history = conn.execute(select(approval_decisions_table.c.approval_id, approval_decisions_table.c.decision)).all()
    assert {row.approval_id: row.decision for row in rows} == {first.approval_id: "approved", second.approval_id: None}
    assert [(row.approval_id, row.decision) for row in history] == [(first.approval_id, "approved")]
    engine.dispose()


def test_new_state_supersedes_only_open_requests_and_requires_audit(fenced_session) -> None:
    _seed(fenced_session)
    events: list = []
    request = _request(fenced_session, events)
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    retired: list = []
    assert supersede_open_approvals(conn, session_id=fenced_session.session_id, now=NOW + timedelta(seconds=1), record=retired.append) == (
        request.approval_id,
    )
    assert retired[0].cause == "new_state"
    assert retired[0].approval.decision == "superseded"
    assert supersede_open_approvals(conn, session_id=fenced_session.session_id, now=NOW, record=retired.append) == ()


def test_mark_decision_seen_is_requester_only_and_first_stamp_wins(fenced_session) -> None:
    _seed(fenced_session)
    events: list = []
    request = _request(fenced_session, events)
    token = fenced_session.connection_token
    assert (
        RepositoryApprovalAuthority.mark_decision_seen(
            token, approval_id=request.approval_id, requester_identity_id="alice"
        ).decision_seen_at
        is None
    )
    with pytest.raises(ApprovalNotFound):
        RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=request.approval_id, requester_identity_id="cover")
    with pytest.raises(ApprovalNotFound):
        RepositoryApprovalAuthority.mark_decision_seen(token, approval_id="missing", requester_identity_id="alice")
    RepositoryApprovalAuthority.decide(
        token,
        approval_id=request.approval_id,
        decided_by="addressed",
        decision="approved",
        note=None,
        record=events.append,
    )
    conn = _resolve_mutation_connection(token)
    conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(provider="service"))
    with pytest.raises(ApprovalNotFound):
        RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=request.approval_id, requester_identity_id="alice")
    conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(provider="local"))
    conn.execute(update(sessions_table).where(sessions_table.c.id == fenced_session.session_id).values(user_id="cover"))
    with pytest.raises(ApprovalNotFound):
        RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=request.approval_id, requester_identity_id="alice")
    conn.execute(update(sessions_table).where(sessions_table.c.id == fenced_session.session_id).values(user_id="alice"))
    first = RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=request.approval_id, requester_identity_id="alice")
    assert first.decision_seen_at is not None
    again = RepositoryApprovalAuthority.mark_decision_seen(token, approval_id=request.approval_id, requester_identity_id="alice")
    assert again.decision_seen_at == first.decision_seen_at
    assert len(events) == 2  # The badge marker emits no auth audit event.


def test_inbox_includes_role_eligible_requests_with_addressed_first(tmp_path) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'inbox.db'}")
    initialize_session_schema(engine)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "addressed", "cover", "outsider"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id in ("addressed", "cover"):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"role-{identity_id}",
                    identity_id=identity_id,
                    role="approver",
                    granted_by_identity_id="alice",
                    granted_at=now,
                )
            )
        for session_id in ("session-a", "session-b"):
            conn.execute(
                insert(sessions_table).values(
                    id=session_id,
                    user_id="alice",
                    title=session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            conn.execute(
                insert(composition_states_table).values(
                    id=f"state-{session_id}",
                    session_id=session_id,
                    version=1,
                    is_valid=True,
                    provenance="session_seed",
                    created_at=now,
                )
            )
    authority = ApprovalTransactionAuthority(engine)

    def request(session_id: str, approver: str):
        return authority.run(
            session_id,
            lambda token: RepositoryApprovalAuthority.request(
                token,
                session_id=session_id,
                state_id=f"state-{session_id}",
                binding=BINDING,
                requested_by="alice",
                approver=approver,
                note=None,
                record=lambda _record: None,
            ),
        )

    other = request("session-a", "addressed")
    mine = request("session-b", "cover")
    assert [row.approval_id for row in authority.inbox(approver_identity_id="cover")] == [mine.approval_id, other.approval_id]
    assert authority.inbox(approver_identity_id="outsider") == ()
    assert authority.inbox(approver_identity_id="alice") == ()
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "cover").values(provider="service"))
    assert authority.inbox(approver_identity_id="cover") == ()
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "cover").values(provider="local"))
        conn.execute(
            update(identity_roles_table).where(identity_roles_table.c.identity_id == "cover").values(expires_at=now - timedelta(seconds=1))
        )
    assert authority.inbox(approver_identity_id="cover") == ()
    engine.dispose()
