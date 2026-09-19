"""Review requests and reviewer attestations use the sessions authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.coordination.review_authority import (
    ChangesRequestedNeedsNote,
    OpenReviewRequestExists,
    OpenReviewRequestRequired,
    RepositoryReviewAuthority,
    ReviewerIsAuthor,
    ReviewerRoleRequired,
    ReviewNoteTooLong,
    ReviewParticipantNotActive,
    ReviewRequestAlreadyClosed,
    ReviewRequestRecord,
    SessionNotOwnedByRequester,
    StateNotInSession,
)
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)


def _noop(_value: object) -> None:
    pass


@pytest.fixture
def authority(engine: Engine) -> RepositoryReviewAuthority:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        for identity_id in ("alice", "bob", "carol", "dave", "root"):
            ensure_test_identity(conn, identity_id=identity_id)
        for identity_id, role in (("bob", "reviewer"), ("carol", "reviewer"), ("dave", "user")):
            conn.execute(
                insert(identity_roles_table).values(
                    role_id=f"role-{identity_id}",
                    identity_id=identity_id,
                    role=role,
                    granted_at=now,
                    granted_by_identity_id="root",
                )
            )
        conn.execute(
            insert(sessions_table).values(
                id="session-1", user_id="alice", auth_provider_type="local", title="review", created_at=now, updated_at=now
            )
        )
        conn.execute(
            insert(composition_states_table).values(
                id="state-1", session_id="session-1", version=1, provenance="session_seed", created_at=now
            )
        )
    return RepositoryReviewAuthority(engine)


def _request(authority: RepositoryReviewAuthority, *, reviewer: str | None = "bob"):
    return authority.request(
        session_id="session-1", state_id="state-1", requested_by="alice", reviewer=reviewer, note="  please review  ", record=_noop
    )


def _attest(authority: RepositoryReviewAuthority, *, reviewer: str = "bob", verdict: str = "signed_off", note: str | None = None):
    return authority.attest(
        session_id="session-1",
        state_id="state-1",
        payload_digest="sha256:" + "ab" * 32,
        reviewer=reviewer,
        verdict=verdict,
        note=note,
        record=_noop,
    )


def test_request_is_owned_and_audited_before_commit(engine: Engine, authority: RepositoryReviewAuthority) -> None:
    seen: list[ReviewRequestRecord] = []
    row = authority.request(
        session_id="session-1", state_id="state-1", requested_by="alice", reviewer="bob", note="  please review  ", record=seen.append
    )
    assert seen == [row]
    assert row.open and row.request_note == "please review" and row.reviewer_identity_id == "bob"
    assert authority.open_for(reviewer="bob") == (row,)
    with engine.connect() as conn:
        assert conn.execute(select(review_requests_table.c.request_id)).scalar_one() == row.request_id


def test_request_refuses_bad_owner_state_and_reviewer(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    with pytest.raises(SessionNotOwnedByRequester):
        authority.request(session_id="session-1", state_id="state-1", requested_by="carol", reviewer="bob", note=None, record=_noop)
    with pytest.raises(StateNotInSession):
        authority.request(session_id="session-1", state_id="missing", requested_by="alice", reviewer="bob", note=None, record=_noop)
    with pytest.raises(ReviewerIsAuthor):
        _request(authority, reviewer="alice")
    with pytest.raises(ReviewerRoleRequired):
        _request(authority, reviewer="dave")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(access_state="disabled"))
    with pytest.raises(ReviewParticipantNotActive):
        _request(authority)


def test_review_grant_expiry_and_revocation_refuse_request_and_attest(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(identity_roles_table)
            .where(identity_roles_table.c.identity_id == "bob")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=10))
        )
    with pytest.raises(ReviewerRoleRequired):
        _request(authority)
    with pytest.raises(ReviewerRoleRequired):
        _attest(authority)
    with pytest.raises(ReviewerRoleRequired):
        authority.open_for(reviewer="bob")
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.identity_id == "bob").values(expires_at=None))
    _request(authority)
    with engine.begin() as conn:
        conn.execute(update(identity_roles_table).where(identity_roles_table.c.identity_id == "bob").values(revoked_at=datetime.now(UTC)))
    with pytest.raises(ReviewerRoleRequired):
        _attest(authority)


def test_service_identity_cannot_review_even_with_a_planted_reviewer_grant(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(kind="service"))
    with pytest.raises(ReviewerRoleRequired):
        _request(authority)
    with pytest.raises(ReviewerRoleRequired):
        authority.open_for(reviewer="bob")
    with pytest.raises(ReviewerRoleRequired):
        _attest(authority)


def test_historical_service_provider_marked_human_cannot_review(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "bob").values(provider="service"))
    with pytest.raises(ReviewerRoleRequired):
        _request(authority)
    with pytest.raises(ReviewerRoleRequired):
        authority.open_for(reviewer="bob")
    with pytest.raises(ReviewerRoleRequired):
        authority.open_request_for(reviewer="bob", session_id="session-1", state_id="state-1")
    with pytest.raises(ReviewerRoleRequired):
        _attest(authority)


def test_service_session_owner_cannot_request_or_receive_attestation(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(kind="service"))
    with pytest.raises(ReviewParticipantNotActive):
        _request(authority)
    with pytest.raises(ReviewParticipantNotActive):
        _attest(authority)


def test_open_request_closes_by_attestation_and_can_be_requested_again(authority: RepositoryReviewAuthority) -> None:
    first = _request(authority)
    with pytest.raises(OpenReviewRequestExists):
        _request(authority, reviewer="carol")
    attested = _attest(authority)
    assert attested.author_identity_id == "alice"
    assert attested.reviewer_identity_id == "bob"
    assert attested.authorizing_request_id == first.request_id
    first_after = authority.read_request(request_id=first.request_id)
    assert first_after is not None and first_after.open is False
    assert authority.open_for(reviewer="bob") == ()
    second = _request(authority, reviewer="carol")
    assert second.open and second.request_id != first.request_id
    with pytest.raises(ReviewRequestAlreadyClosed):
        authority.cancel(request_id=first.request_id, requested_by="alice", record=_noop)


def test_attestation_requires_open_exact_state_request_and_remains_a_non_gating_ledger(
    authority: RepositoryReviewAuthority, engine: Engine
) -> None:
    with pytest.raises(ReviewerIsAuthor):
        _attest(authority, reviewer="alice")
    with pytest.raises(OpenReviewRequestRequired):
        _attest(authority)
    _request(authority)
    with pytest.raises(ChangesRequestedNeedsNote):
        _attest(authority, verdict="changes_requested")
    row = _attest(authority, verdict="changes_requested", note="  fix edge  ")
    assert row.note == "fix edge" and row.verdict == "changes_requested"
    assert authority.attestations_for(session_id="session-1", state_id="state-1") == (row,)
    with engine.connect() as conn:
        assert conn.execute(select(review_attestations_table.c.author_identity_id)).scalar_one() == "alice"


def test_audit_failure_rolls_back_each_mutation(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    def fail(_value: object) -> None:
        raise RuntimeError("audit unavailable")

    with pytest.raises(RuntimeError, match="audit unavailable"):
        authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer="bob", note=None, record=fail)
    with engine.connect() as conn:
        assert conn.execute(select(review_requests_table)).all() == []
    row = _request(authority)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        authority.cancel(request_id=row.request_id, requested_by="alice", record=fail)
    persisted = authority.read_request(request_id=row.request_id)
    assert persisted is not None and persisted.open
    with pytest.raises(RuntimeError, match="audit unavailable"):
        authority.attest(
            session_id="session-1",
            state_id="state-1",
            payload_digest="sha256:" + "ab" * 32,
            reviewer="bob",
            verdict="signed_off",
            note=None,
            record=fail,
        )
    assert authority.attestations_for(session_id="session-1", state_id="state-1") == ()


def test_cancel_and_sent_history(authority: RepositoryReviewAuthority) -> None:
    row = _request(authority, reviewer=None)
    assert authority.open_for(reviewer="bob") == (row,)
    assert authority.open_request_for(reviewer="bob", session_id="session-1", state_id="state-1") == row
    assert authority.open_request_for(reviewer="bob", session_id="session-1", state_id="absent") is None
    with pytest.raises(ReviewerRoleRequired):
        authority.open_for(reviewer="dave")
    with pytest.raises(ReviewerRoleRequired):
        authority.open_request_for(reviewer="dave", session_id="session-1", state_id="state-1")
    cancelled = authority.cancel(request_id=row.request_id, requested_by="alice", record=_noop)
    assert not cancelled.open and cancelled.cancelled_at is not None
    assert authority.open_for(reviewer="bob") == ()
    assert authority.sent_for(requested_by="alice")[0].request == cancelled
    assert authority.sent_for(requested_by="bob") == ()


def test_author_with_reviewer_role_cannot_see_their_unaddressed_request(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    _request(authority, reviewer=None)
    with engine.begin() as conn:
        conn.execute(
            insert(identity_roles_table).values(
                role_id="role-alice-reviewer",
                identity_id="alice",
                role="reviewer",
                granted_at=datetime.now(UTC),
                granted_by_identity_id="root",
            )
        )
    assert authority.open_for(reviewer="alice") == ()
    assert authority.open_request_for(reviewer="alice", session_id="session-1", state_id="state-1") is None


def test_original_requester_can_cancel_after_session_ownership_changes(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    requested = _request(authority)
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == "session-1").values(user_id="carol"))
    cancelled = authority.cancel(request_id=requested.request_id, requested_by="alice", record=_noop)
    assert cancelled.cancelled_at is not None and cancelled.open is False


def test_note_limit_is_utf8_bytes(authority: RepositoryReviewAuthority) -> None:
    with pytest.raises(ReviewNoteTooLong):
        authority.request(session_id="session-1", state_id="state-1", requested_by="alice", reviewer=None, note="€" * 1366, record=_noop)


def test_re_request_after_attestation_stays_open_on_coarse_sqlite_clock(authority: RepositoryReviewAuthority) -> None:
    first = _request(authority)
    first_attestation = _attest(authority)
    second = _request(authority, reviewer="carol")
    assert second.requested_at > first_attestation.attested_at
    first_after = authority.read_request(request_id=first.request_id)
    second_before = authority.read_request(request_id=second.request_id)
    assert first_after is not None and first_after.open is False
    assert second_before is not None and second_before.open is True
    second_attestation = _attest(authority, reviewer="carol")
    assert second_attestation.attested_at >= second.requested_at
    second_after = authority.read_request(request_id=second.request_id)
    assert second_after is not None and second_after.open is False
    newest, oldest = authority.sent_for(requested_by="alice")
    assert newest.request.request_id == second.request_id
    assert newest.attestations == (second_attestation,)
    assert oldest.request.request_id == first.request_id
    assert oldest.attestations == (first_attestation,)


def test_cancelled_re_request_owns_the_later_attestation_on_coarse_sqlite_clock(
    authority: RepositoryReviewAuthority,
) -> None:
    first = _request(authority)
    authority.cancel(request_id=first.request_id, requested_by="alice", record=_noop)
    second = _request(authority)
    assert second.requested_at > first.requested_at
    attestation = _attest(authority)
    newest, oldest = authority.sent_for(requested_by="alice")
    assert newest.request.request_id == second.request_id
    assert newest.attestations == (attestation,)
    assert oldest.request.request_id == first.request_id
    assert oldest.attestations == ()


def test_attestation_snapshots_author_even_if_session_is_reowned(authority: RepositoryReviewAuthority, engine: Engine) -> None:
    _request(authority)
    row = _attest(authority)
    assert row.author_identity_id == "alice"
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == "session-1").values(user_id="carol"))
    assert authority.attestations_for(session_id="session-1", state_id="state-1")[0].author_identity_id == "alice"


def test_addressed_request_only_admits_its_reviewer_and_cancelled_request_admits_none(
    authority: RepositoryReviewAuthority, engine: Engine
) -> None:
    request = _request(authority, reviewer="bob")
    with pytest.raises(OpenReviewRequestRequired):
        _attest(authority, reviewer="carol")
    assert authority.open_request_for(reviewer="carol", session_id="session-1", state_id="state-1") is None
    assert authority.open_request_for(reviewer="bob", session_id="session-1", state_id="state-1") == request
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id="state-2", session_id="session-1", version=2, provenance="session_seed", created_at=datetime.now(UTC)
            )
        )
    with pytest.raises(OpenReviewRequestRequired):
        authority.attest(
            session_id="session-1",
            state_id="state-2",
            payload_digest="sha256:" + "cd" * 32,
            reviewer="bob",
            verdict="signed_off",
            note=None,
            record=_noop,
        )
    authority.cancel(request_id=request.request_id, requested_by="alice", record=_noop)
    assert authority.open_request_for(reviewer="bob", session_id="session-1", state_id="state-1") is None
    with pytest.raises(OpenReviewRequestRequired):
        _attest(authority, reviewer="bob")
    unaddressed = _request(authority, reviewer=None)
    assert authority.open_request_for(reviewer="carol", session_id="session-1", state_id="state-1") == unaddressed
    attested = _attest(authority, reviewer="carol")
    assert attested.reviewer_identity_id == "carol"
    assert attested.authorizing_request_id is not None
