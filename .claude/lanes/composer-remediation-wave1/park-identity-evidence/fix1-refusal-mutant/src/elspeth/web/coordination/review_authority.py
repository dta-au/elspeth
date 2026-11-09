"""Sessions authority for review requests and reviewer attestations.

Requests are closed by cancellation or a later attestation on the same state.
Attestations remain an append-only ledger and do not gate a run. An attestation
requires an open request for its exact state and reviewer. Each write
calls its required audit callback before its sessions transaction commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast, final, get_args

from sqlalchemy import bindparam, exists, func, insert, or_, select, update
from sqlalchemy.engine import Connection, Engine, Row

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    identity_roles_table,
    review_attestations_table,
    review_requests_table,
    sessions_table,
)

ReviewVerdict = Literal["signed_off", "changes_requested", "withdrawn"]
_VERDICTS: Final = frozenset(get_args(ReviewVerdict))
MAX_REVIEW_NOTE_BYTES: Final = 4096


class ReviewAuthorityRefusal(RuntimeError):
    """Base for refusals that a route may translate to a stable error type."""


class ReviewRequestNotFound(ReviewAuthorityRefusal):
    """The request is absent or does not belong to the caller."""


class SessionNotOwnedByRequester(ReviewAuthorityRefusal):
    """Only the owner of a live session may ask for review."""


class StateNotInSession(ReviewAuthorityRefusal):
    """The state is not part of a live session."""


class ReviewParticipantNotActive(ReviewAuthorityRefusal):
    """A participant is not an active identity."""


class ReviewerRoleRequired(ReviewAuthorityRefusal):
    """The reviewer has no live, unscoped reviewer grant."""


class ReviewerIsAuthor(ReviewAuthorityRefusal):
    """A session author may not review the same session."""


class OpenReviewRequestExists(ReviewAuthorityRefusal):
    """A state has an open review request already."""


class ReviewRequestAlreadyClosed(ReviewAuthorityRefusal):
    """The request is cancelled or closed by an attestation."""


class OpenReviewRequestRequired(ReviewAuthorityRefusal):
    """There is no open request for this state addressed to this reviewer."""


class ChangesRequestedNeedsNote(ReviewAuthorityRefusal):
    """A changes_requested verdict needs a nonblank note."""


class ReviewNoteTooLong(ReviewAuthorityRefusal):
    """A note exceeds the byte limit."""


@final
@dataclass(frozen=True, slots=True)
class ReviewRequestRecord:
    request_id: str
    session_id: str
    state_id: str
    requested_by_identity_id: str
    reviewer_identity_id: str | None
    requested_at: datetime
    cancelled_at: datetime | None
    request_note: str | None
    open: bool


@final
@dataclass(frozen=True, slots=True)
class ReviewAttestationRecord:
    attestation_id: str
    session_id: str
    state_id: str
    payload_digest: str
    reviewer_identity_id: str
    author_identity_id: str
    attested_at: datetime
    verdict: ReviewVerdict
    note: str | None
    # The request that admitted this write. This is callback metadata, not a
    # column in the append-only attestation ledger.
    authorizing_request_id: str | None = field(default=None, compare=False)


@final
@dataclass(frozen=True, slots=True)
class ReviewSentRecord:
    request: ReviewRequestRecord
    attestations: tuple[ReviewAttestationRecord, ...]


_ATTESTED_SINCE_REQUEST: Final = exists().where(
    review_attestations_table.c.session_id == review_requests_table.c.session_id,
    review_attestations_table.c.state_id == review_requests_table.c.state_id,
    review_attestations_table.c.attested_at >= review_requests_table.c.requested_at,
)
_REQUEST_ROWS: Final = select(review_requests_table, _ATTESTED_SINCE_REQUEST.label("attested"))
_REQUEST_BY_ID: Final = _REQUEST_ROWS.where(review_requests_table.c.request_id == bindparam("request_id"))
_OPEN_REQUESTS_FOR_PAIR: Final = _REQUEST_ROWS.where(
    review_requests_table.c.session_id == bindparam("session_id"),
    review_requests_table.c.state_id == bindparam("state_id"),
    review_requests_table.c.cancelled_at.is_(None),
    ~_ATTESTED_SINCE_REQUEST,
)
_OPEN_REQUESTS_FOR_PAIR_AND_REVIEWER: Final = _OPEN_REQUESTS_FOR_PAIR.where(
    or_(
        review_requests_table.c.reviewer_identity_id == bindparam("reviewer"),
        review_requests_table.c.reviewer_identity_id.is_(None),
    ),
    review_requests_table.c.requested_by_identity_id != bindparam("reviewer"),
)
_OPEN_REQUESTS_FOR_REVIEWER: Final = _REQUEST_ROWS.where(
    review_requests_table.c.cancelled_at.is_(None),
    ~_ATTESTED_SINCE_REQUEST,
    or_(review_requests_table.c.reviewer_identity_id == bindparam("reviewer"), review_requests_table.c.reviewer_identity_id.is_(None)),
    review_requests_table.c.requested_by_identity_id != bindparam("reviewer"),
).order_by(review_requests_table.c.requested_at.desc(), review_requests_table.c.request_id.desc())
_ATTESTATIONS_FOR_PAIR: Final = (
    select(review_attestations_table)
    .where(
        review_attestations_table.c.session_id == bindparam("session_id"),
        review_attestations_table.c.state_id == bindparam("state_id"),
    )
    .order_by(review_attestations_table.c.attested_at, review_attestations_table.c.attestation_id)
)
_LAST_ATTESTATION_FOR_PAIR: Final = select(func.max(review_attestations_table.c.attested_at)).where(
    review_attestations_table.c.session_id == bindparam("session_id"),
    review_attestations_table.c.state_id == bindparam("state_id"),
)
_LAST_REQUEST_FOR_PAIR: Final = select(func.max(review_requests_table.c.requested_at)).where(
    review_requests_table.c.session_id == bindparam("session_id"),
    review_requests_table.c.state_id == bindparam("state_id"),
)
_REQUESTER_PAIR: Final = review_requests_table.alias("requester_pair")
_REQUESTS_ON_REQUESTER_PAIRS: Final = _REQUEST_ROWS.where(
    exists().where(
        _REQUESTER_PAIR.c.session_id == review_requests_table.c.session_id,
        _REQUESTER_PAIR.c.state_id == review_requests_table.c.state_id,
        _REQUESTER_PAIR.c.requested_by_identity_id == bindparam("requested_by"),
    )
).order_by(review_requests_table.c.requested_at, review_requests_table.c.request_id)
_ATTESTATIONS_ON_REQUESTER_PAIRS: Final = (
    select(review_attestations_table)
    .where(
        exists().where(
            _REQUESTER_PAIR.c.session_id == review_attestations_table.c.session_id,
            _REQUESTER_PAIR.c.state_id == review_attestations_table.c.state_id,
            _REQUESTER_PAIR.c.requested_by_identity_id == bindparam("requested_by"),
        )
    )
    .order_by(review_attestations_table.c.attested_at, review_attestations_table.c.attestation_id)
)
_IDENTITY_BY_ID: Final = select(
    identities_table.c.identity_id, identities_table.c.access_state, identities_table.c.kind, identities_table.c.provider
).where(identities_table.c.identity_id == bindparam("identity_id"))
_IDENTITY_FOR_UPDATE: Final = _IDENTITY_BY_ID.with_for_update()
_REVIEWER_GRANTS: Final = select(identity_roles_table.c.role_id, identity_roles_table.c.expires_at).where(
    identity_roles_table.c.identity_id == bindparam("identity_id"),
    identity_roles_table.c.role == "reviewer",
    identity_roles_table.c.scope.is_(None),
    identity_roles_table.c.revoked_at.is_(None),
)
_REVIEWER_GRANTS_FOR_UPDATE: Final = _REVIEWER_GRANTS.with_for_update()
_SESSION_BY_ID: Final = select(sessions_table.c.id, sessions_table.c.user_id, sessions_table.c.archived_at).where(
    sessions_table.c.id == bindparam("session_id")
)
_SESSION_FOR_UPDATE: Final = _SESSION_BY_ID.with_for_update()
_STATE_IN_SESSION: Final = select(composition_states_table.c.id).where(
    composition_states_table.c.id == bindparam("state_id"),
    composition_states_table.c.session_id == bindparam("session_id"),
)


def _nonblank(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _bounded_note(value: str | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("note must be a string or None")
    stripped = value.strip()
    if len(stripped.encode("utf-8")) > MAX_REVIEW_NOTE_BYTES:
        raise ReviewNoteTooLong(f"note exceeds {MAX_REVIEW_NOTE_BYTES} bytes")
    return stripped or None


def _verdict(value: object) -> ReviewVerdict:
    if type(value) is not str or value not in _VERDICTS:
        raise ValueError("invalid review verdict")
    return cast(ReviewVerdict, value)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _request_record(row: Row[Any]) -> ReviewRequestRecord:
    cancelled_at = None if row.cancelled_at is None else _utc(row.cancelled_at)
    return ReviewRequestRecord(
        request_id=row.request_id,
        session_id=row.session_id,
        state_id=row.state_id,
        requested_by_identity_id=row.requested_by_identity_id,
        reviewer_identity_id=row.reviewer_identity_id,
        requested_at=_utc(row.requested_at),
        cancelled_at=cancelled_at,
        request_note=row.request_note,
        open=cancelled_at is None and not bool(row.attested),
    )


def _attestation_record(row: Row[Any], *, authorizing_request_id: str | None = None) -> ReviewAttestationRecord:
    verdict = row.verdict
    if type(verdict) is not str or verdict not in _VERDICTS:
        raise RuntimeError("persisted review verdict is invalid")
    return ReviewAttestationRecord(
        attestation_id=row.attestation_id,
        session_id=row.session_id,
        state_id=row.state_id,
        payload_digest=row.payload_digest,
        reviewer_identity_id=row.reviewer_identity_id,
        author_identity_id=row.author_identity_id,
        attested_at=_utc(row.attested_at),
        verdict=cast(ReviewVerdict, verdict),
        note=row.note,
        authorizing_request_id=authorizing_request_id,
    )


def _live_grant(rows: Sequence[Row[Any]], now: datetime) -> bool:
    return any(row.expires_at is None or _utc(row.expires_at) > now for row in rows)


@final
class RepositoryReviewAuthority:
    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_request(self, *, request_id: str) -> ReviewRequestRecord | None:
        _nonblank(request_id, "request_id")
        with self._engine.connect() as conn:
            row = conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one_or_none()
        return None if row is None else _request_record(row)

    def open_for(self, *, reviewer: str) -> tuple[ReviewRequestRecord, ...]:
        _nonblank(reviewer, "reviewer")
        with self._engine.connect() as conn:
            identity = conn.execute(_IDENTITY_BY_ID, {"identity_id": reviewer}).one_or_none()
            if identity is None or identity.access_state != "active":
                raise ReviewParticipantNotActive("the reviewer is not active")
            if identity.kind != "human" or identity.provider == "service":
                raise ReviewerRoleRequired("a human reviewer is required")
            grants = conn.execute(_REVIEWER_GRANTS, {"identity_id": reviewer}).all()
            now = database_now(conn)
            if not _live_grant(grants, now):
                raise ReviewerRoleRequired("an active reviewer role is required")
            rows = conn.execute(_OPEN_REQUESTS_FOR_REVIEWER, {"reviewer": reviewer}).all()
        return tuple(_request_record(row) for row in rows)

    def open_request_for(self, *, reviewer: str, session_id: str, state_id: str) -> ReviewRequestRecord | None:
        """Resolve the exact open request a live reviewer may inspect."""
        _nonblank(reviewer, "reviewer")
        _nonblank(session_id, "session_id")
        _nonblank(state_id, "state_id")
        with self._engine.connect() as conn:
            identity = conn.execute(_IDENTITY_BY_ID, {"identity_id": reviewer}).one_or_none()
            if identity is None or identity.access_state != "active":
                raise ReviewParticipantNotActive("the reviewer is not active")
            if identity.kind != "human" or identity.provider == "service":
                raise ReviewerRoleRequired("a human reviewer is required")
            grants = conn.execute(_REVIEWER_GRANTS, {"identity_id": reviewer}).all()
            now = database_now(conn)
            if not _live_grant(grants, now):
                raise ReviewerRoleRequired("an active reviewer role is required")
            row = conn.execute(
                _OPEN_REQUESTS_FOR_PAIR_AND_REVIEWER,
                {"reviewer": reviewer, "session_id": session_id, "state_id": state_id},
            ).one_or_none()
        return None if row is None else _request_record(row)

    def attestations_for(self, *, session_id: str, state_id: str) -> tuple[ReviewAttestationRecord, ...]:
        _nonblank(session_id, "session_id")
        _nonblank(state_id, "state_id")
        with self._engine.connect() as conn:
            rows = conn.execute(_ATTESTATIONS_FOR_PAIR, {"session_id": session_id, "state_id": state_id}).all()
        return tuple(_attestation_record(row) for row in rows)

    def sent_for(self, *, requested_by: str) -> tuple[ReviewSentRecord, ...]:
        _nonblank(requested_by, "requested_by")
        with self._engine.connect() as conn:
            attestation_rows = conn.execute(_ATTESTATIONS_ON_REQUESTER_PAIRS, {"requested_by": requested_by}).all()
            request_rows = conn.execute(_REQUESTS_ON_REQUESTER_PAIRS, {"requested_by": requested_by}).all()
        requests = [_request_record(row) for row in request_rows]
        by_pair: dict[tuple[str, str], list[ReviewRequestRecord]] = {}
        for request in requests:
            by_pair.setdefault((request.session_id, request.state_id), []).append(request)
        attributed: dict[str, list[ReviewAttestationRecord]] = {request.request_id: [] for request in requests}
        for row in attestation_rows:
            attestation = _attestation_record(row)
            owner: ReviewRequestRecord | None = None
            for candidate in by_pair[(attestation.session_id, attestation.state_id)]:
                if candidate.requested_at <= attestation.attested_at:
                    owner = candidate
            if owner is not None:
                attributed[owner.request_id].append(attestation)
        return tuple(
            ReviewSentRecord(request=request, attestations=tuple(attributed[request.request_id]))
            for request in reversed(requests)
            if request.requested_by_identity_id == requested_by
        )

    def request(
        self,
        *,
        session_id: str,
        state_id: str,
        requested_by: str,
        reviewer: str | None,
        note: str | None,
        record: Callable[[ReviewRequestRecord], None],
    ) -> ReviewRequestRecord:
        _nonblank(session_id, "session_id")
        _nonblank(state_id, "state_id")
        _nonblank(requested_by, "requested_by")
        if reviewer is not None:
            _nonblank(reviewer, "reviewer")
            if reviewer == requested_by:
                raise ReviewerIsAuthor("the author cannot review their own session")
        bounded_note = _bounded_note(note)
        participants = (requested_by,) if reviewer is None else (requested_by, reviewer)
        with self._engine.begin() as conn:
            session = conn.execute(_SESSION_FOR_UPDATE, {"session_id": session_id}).one_or_none()
            if session is None or session.archived_at is not None or session.user_id != requested_by:
                raise SessionNotOwnedByRequester("the requester does not own an active session")
            now, grants = self._lock_then_read_clock(conn, participants, grant_holder=reviewer)
            if reviewer is not None and not _live_grant(grants, now):
                raise ReviewerRoleRequired("an active reviewer role is required")
            if conn.execute(_STATE_IN_SESSION, {"state_id": state_id, "session_id": session_id}).first() is None:
                raise StateNotInSession("the state is not part of the session")
            pair = {"session_id": session_id, "state_id": state_id}
            if conn.execute(_OPEN_REQUESTS_FOR_PAIR, pair).first() is not None:
                raise OpenReviewRequestExists("the state already has an open review request")
            last_request = conn.execute(_LAST_REQUEST_FOR_PAIR, pair).scalar_one()
            if last_request is not None:
                now = max(now, _utc(last_request) + timedelta(microseconds=1))
            last_attestation = conn.execute(_LAST_ATTESTATION_FOR_PAIR, pair).scalar_one()
            if last_attestation is not None:
                now = max(now, _utc(last_attestation) + timedelta(microseconds=1))
            request_id = str(uuid.uuid4())
            conn.execute(
                insert(review_requests_table).values(
                    request_id=request_id,
                    session_id=session_id,
                    state_id=state_id,
                    requested_by_identity_id=requested_by,
                    reviewer_identity_id=reviewer,
                    requested_at=now,
                    cancelled_at=None,
                    request_note=bounded_note,
                )
            )
            created = _request_record(conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one())
            record(created)
            return created

    def cancel(self, *, request_id: str, requested_by: str, record: Callable[[ReviewRequestRecord], None]) -> ReviewRequestRecord:
        _nonblank(request_id, "request_id")
        _nonblank(requested_by, "requested_by")
        with self._engine.begin() as conn:
            existing = conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one_or_none()
            if existing is None or existing.requested_by_identity_id != requested_by:
                raise ReviewRequestNotFound("review request not found")
            session = conn.execute(_SESSION_FOR_UPDATE, {"session_id": existing.session_id}).one_or_none()
            if session is None:
                raise StateNotInSession("the review session no longer exists")
            now, _ = self._lock_then_read_clock(conn, (requested_by,), grant_holder=None)
            now = max(now, _utc(existing.requested_at))
            result = conn.execute(
                update(review_requests_table)
                .where(
                    review_requests_table.c.request_id == request_id,
                    review_requests_table.c.requested_by_identity_id == requested_by,
                    review_requests_table.c.cancelled_at.is_(None),
                    ~_ATTESTED_SINCE_REQUEST,
                )
                .values(cancelled_at=now)
            )
            if result.rowcount != 1:
                current = conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one_or_none()
                if current is None or current.requested_by_identity_id != requested_by:
                    raise ReviewRequestNotFound("review request not found")
                raise ReviewRequestAlreadyClosed("review request is already closed")
            cancelled = _request_record(conn.execute(_REQUEST_BY_ID, {"request_id": request_id}).one())
            record(cancelled)
            return cancelled

    def attest(
        self,
        *,
        session_id: str,
        state_id: str,
        payload_digest: str,
        reviewer: str,
        verdict: str,
        note: str | None,
        record: Callable[[ReviewAttestationRecord], None],
    ) -> ReviewAttestationRecord:
        _nonblank(session_id, "session_id")
        _nonblank(state_id, "state_id")
        _nonblank(payload_digest, "payload_digest")
        _nonblank(reviewer, "reviewer")
        checked_verdict = _verdict(verdict)
        bounded_note = _bounded_note(note)
        if checked_verdict == "changes_requested" and bounded_note is None:
            raise ChangesRequestedNeedsNote("changes_requested requires a note")
        with self._engine.begin() as conn:
            session = conn.execute(_SESSION_FOR_UPDATE, {"session_id": session_id}).one_or_none()
            if session is None or session.archived_at is not None:
                raise StateNotInSession("the session is not reviewable")
            author: str = session.user_id
            if reviewer == author:
                raise ReviewerIsAuthor("the author cannot review their own session")
            now, grants = self._lock_then_read_clock(conn, (reviewer, author), grant_holder=reviewer)
            if conn.execute(_STATE_IN_SESSION, {"state_id": state_id, "session_id": session_id}).first() is None:
                raise StateNotInSession("the state is not part of the session")
            if not _live_grant(grants, now):
                raise ReviewerRoleRequired("an active reviewer role is required")
            pair = {"session_id": session_id, "state_id": state_id}
            authorizing_request = conn.execute(_OPEN_REQUESTS_FOR_PAIR_AND_REVIEWER, {**pair, "reviewer": reviewer}).first()
            if authorizing_request is None:
                raise OpenReviewRequestRequired("an open review request for this reviewer and state is required")
            last_request = conn.execute(_LAST_REQUEST_FOR_PAIR, pair).scalar_one()
            if last_request is not None:
                now = max(now, _utc(last_request))
            attestation_id = str(uuid.uuid4())
            conn.execute(
                insert(review_attestations_table).values(
                    attestation_id=attestation_id,
                    session_id=session_id,
                    state_id=state_id,
                    payload_digest=payload_digest,
                    reviewer_identity_id=reviewer,
                    author_identity_id=author,
                    attested_at=now,
                    verdict=checked_verdict,
                    note=bounded_note,
                )
            )
            rows = conn.execute(_ATTESTATIONS_FOR_PAIR, pair).all()
            created = next(
                _attestation_record(row, authorizing_request_id=authorizing_request.request_id)
                for row in rows
                if row.attestation_id == attestation_id
            )
            record(created)
            return created

    @staticmethod
    def _lock_then_read_clock(
        conn: Connection, participants: Sequence[str], *, grant_holder: str | None
    ) -> tuple[datetime, tuple[Row[Any], ...]]:
        """Lock the admin population, identities and grant before reading time."""
        ordered = sorted(set(participants))
        # Take the same admin population lock used by identity mutations
        # before locking the reviewer identity or its grant.
        conn.execute(
            select(identity_roles_table.c.identity_id, identity_roles_table.c.expires_at, identity_roles_table.c.revoked_at)
            .select_from(identity_roles_table.join(identities_table, identity_roles_table.c.identity_id == identities_table.c.identity_id))
            .where(
                identity_roles_table.c.role == "admin",
                identity_roles_table.c.scope.is_(None),
                identities_table.c.kind == "human",
                identities_table.c.access_state == "active",
            )
            .with_for_update()
        ).all()
        for identity_id in ordered:
            row = conn.execute(_IDENTITY_FOR_UPDATE, {"identity_id": identity_id}).one_or_none()
            if row is None or row.access_state != "active":
                raise ReviewParticipantNotActive("a review participant is not active")
            if row.kind != "human" or row.provider == "service":
                if identity_id == grant_holder:
                    raise ReviewerRoleRequired("a human reviewer is required")
                raise ReviewParticipantNotActive("a human review participant is required")
        grants: tuple[Row[Any], ...] = ()
        if grant_holder is not None:
            grants = tuple(conn.execute(_REVIEWER_GRANTS_FOR_UPDATE, {"identity_id": grant_holder}).all())
        return database_now(conn), grants
