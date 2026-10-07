"""Single typed SQL authority for durable composer operations.

Admission is immutable; a running job is never returned to the queue. Poll
reads omit request bodies and every cross-session discovery has a hard limit.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal, NotRequired, TypedDict, final
from uuid import UUID

from pydantic import JsonValue, ValidationError
from sqlalchemy import Connection, Engine, Row, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH,
    COMPOSER_OPERATION_RESULT_SCHEMA_ERROR,
    COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS,
    ComposerOperationActiveError,
    ComposerOperationCancelledBeforeStart,
    ComposerOperationCancelledDuringTurn,
    ComposerOperationCancelledFailure,
    ComposerOperationCapacityError,
    ComposerOperationClaim,
    ComposerOperationConflictError,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationKind,
    ComposerOperationPreconditionRefused,
    ComposerOperationRecord,
    ComposerOperationRunning,
    ComposerOperationSettledBy,
    ComposerTurnDeadlineExpired,
    composer_operation_request_hash,
    composer_operation_result_hash,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    sessions_table,
)
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
)
from elspeth.web.sessions.models import (
    session_operation_fences_table as fences,
)
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerOperationBinding
from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.time_normalization import restore_utc


def _bounded(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be between 1 and 10000")


def _identity(session_id: UUID, operation_id: str) -> None:
    if type(session_id) is not UUID or type(operation_id) is not str or str(UUID(operation_id)) != operation_id:
        raise ValueError("session and operation identities must be canonical UUIDs")


def _key(session_id: UUID, operation_id: str) -> tuple[Any, ...]:
    return jobs.c.session_id == str(session_id), jobs.c.operation_id == operation_id


def _record_from_row(row: Row[Any], *, request_json: str | None = None) -> ComposerOperationRecord:
    try:
        record = ComposerOperationRecord(
            session_id=UUID(row.session_id),
            operation_id=row.operation_id,
            kind=row.kind,
            status=row.status,
            request_hash=row.request_hash,
            actor_user_id=row.actor_user_id,
            request_id=row.request_id,
            base_state_id=UUID(row.base_state_id) if row.base_state_id is not None else None,
            deadline_at=restore_utc(row.deadline_at),
            created_at=restore_utc(row.created_at),
            updated_at=restore_utc(row.updated_at),
            started_at=restore_utc(row.started_at) if row.started_at is not None else None,
            settled_at=restore_utc(row.settled_at) if row.settled_at is not None else None,
            cancel_requested_at=restore_utc(row.cancel_requested_at) if row.cancel_requested_at is not None else None,
            claim_token_present=row.claim_token is not None,
            claim_owner_instance_id=row.claim_owner_instance_id,
            claim_expires_at=restore_utc(row.claim_expires_at) if row.claim_expires_at is not None else None,
            attempt=row.attempt,
            session_operation_id=row.session_operation_id,
            session_operation_epoch=row.session_operation_epoch,
            user_message_id=UUID(row.user_message_id) if row.user_message_id is not None else None,
            failure_code=row.failure_code,
            settled_by=row.settled_by,
            result_schema=row.result_schema,
            result_json=row.result_json,
            result_sha256=row.result_sha256,
            request_json=request_json,
        )
    except (ValueError, TypeError, ValidationError) as exc:
        raise AuditIntegrityError("composer operation row violates its owned record contract") from exc
    if record.status in ("completed", "failed") and (
        record.result_json is None or composer_operation_result_hash(record.result_json) != record.result_sha256
    ):
        raise AuditIntegrityError("composer operation result digest mismatch")
    return record


def _read(conn: Connection, session_id: UUID, operation_id: str, *, body: bool = False) -> Row[Any] | None:
    columns = tuple(jobs.c) if body else tuple(c for c in jobs.c if c.name != "request_json")
    return conn.execute(select(*columns).where(*_key(session_id, operation_id)).limit(1)).one_or_none()


def get_composer_operation_for_start_on_connection(conn: Connection, claim: ComposerOperationClaim) -> ComposerOperationRecord:
    row = _read(conn, claim.session_id, claim.operation_id, body=True)
    if (
        row is None
        or not _claim_matches(row, claim)
        or row.claim_expires_at is None
        or restore_utc(row.claim_expires_at) <= database_now(conn)
    ):
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    if row.cancel_requested_at is not None:
        raise ComposerOperationCancelledBeforeStart(claim)
    return _record_from_row(row, request_json=row.request_json)


def _claim_matches(row: Row[Any], claim: ComposerOperationClaim) -> bool:
    return bool(row.status == "queued" and row.claim_token == claim.claim_token and row.attempt == claim.attempt)


def _binding(row: Row[Any], *, kind: ComposerOperationKind, request_hash: str, actor_user_id: str, base_state_id: UUID | None) -> None:
    if (
        row.kind != kind
        or row.request_hash != request_hash
        or row.actor_user_id != actor_user_id
        or row.base_state_id != (str(base_state_id) if base_state_id else None)
    ):
        raise ComposerOperationConflictError(session_id=UUID(row.session_id), operation_id=row.operation_id)


def _require_owner(conn: Connection, *, session_id: UUID, actor_user_id: str, auth_provider_type: str) -> None:
    row = conn.execute(
        select(sessions_table.c.user_id, sessions_table.c.auth_provider_type, sessions_table.c.archived_at)
        .where(sessions_table.c.id == str(session_id))
        .limit(1)
    ).one_or_none()
    if row is None or row.archived_at is not None or row.user_id != actor_user_id or row.auth_provider_type != auth_provider_type:
        raise ComposerOperationPreconditionRefused(
            error=ComposerOperationError(
                http_status=404, failure_code="http_error", error_type=None, body={"detail": "Session not found"}, diagnostic_id=None
            )
        )


class _ComposerTerminalValues(TypedDict, total=False):
    status: Literal["failed", "completed"]
    request_json: None
    claim_token: None
    claim_expires_at: None
    failure_code: str | None
    settled_by: ComposerOperationSettledBy
    result_schema: str
    result_json: str
    result_sha256: str
    settled_at: datetime
    updated_at: datetime
    cancel_requested_at: NotRequired[datetime]


def _terminal_values(
    outcome: MessageWithStateResponse | ComposerOperationError, *, now: datetime, settled_by: ComposerOperationSettledBy
) -> _ComposerTerminalValues:
    failed = isinstance(outcome, ComposerOperationError)
    encoded = canonical_json(outcome.model_dump(mode="json"))
    return {
        "status": "failed" if failed else "completed",
        "request_json": None,
        "claim_token": None,
        "claim_expires_at": None,
        "failure_code": outcome.failure_code if isinstance(outcome, ComposerOperationError) else None,
        "settled_by": settled_by,
        "result_schema": COMPOSER_OPERATION_RESULT_SCHEMA_ERROR if failed else COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS,
        "result_json": encoded,
        "result_sha256": composer_operation_result_hash(encoded),
        "settled_at": now,
        "updated_at": now,
    }


def _select_failure_on_connection(
    row: Row[Any], failure: ComposerOperationError, *, now: datetime, authoritative_failure: bool
) -> ComposerOperationError:
    if authoritative_failure:
        return failure
    if row.cancel_requested_at is not None:
        body: dict[str, JsonValue] = {"error_type": "request_cancelled", "detail": "The composer request was stopped."}
        body["request_id"] = row.request_id
        return ComposerOperationError(
            http_status=499, failure_code="request_cancelled", error_type="request_cancelled", body=body, diagnostic_id=None
        )
    if restore_utc(row.deadline_at) <= now:
        body = {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request waited too long to start. Please resubmit.",
            "timeout_seconds": (restore_utc(row.deadline_at) - restore_utc(row.created_at)).total_seconds(),
        }
        body["request_id"] = row.request_id
        return ComposerOperationError(
            http_status=504,
            failure_code="deadline_expired",
            error_type="composer_operation_deadline_expired",
            body=body,
            diagnostic_id=None,
        )
    return failure


@final
class ComposerAsyncOperationAuthority:
    def __init__(self, engine: Engine, *, owner_instance_id: str, claim_lease_seconds: int) -> None:
        if engine.dialect.name not in ("sqlite", "postgresql") or not owner_instance_id.strip() or not 5 <= claim_lease_seconds <= 600:
            raise ValueError("invalid composer operation authority configuration")
        self._engine = engine
        self._owner_instance_id = owner_instance_id
        self._claim_lease_seconds = claim_lease_seconds

    def admit(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: ComposerOperationKind,
        request_hash: str,
        actor_user_id: str,
        request_id: str | None,
        base_state_id: UUID | None,
        request_json: str,
        deadline_seconds: float,
        max_nonterminal: int,
        auth_provider_type: str,
    ) -> tuple[ComposerOperationRecord, bool]:
        _identity(session_id, operation_id)
        _bounded(max_nonterminal)
        if (
            not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or len(request_json.encode("utf-8")) > COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH
        ):
            raise ValueError("invalid composer request budget or encoded size")
        request: SendMessageRequest | RecomposeRequest
        if kind == "compose_message":
            request = SendMessageRequest.model_validate_json(request_json)
        elif kind == "compose_recompose":
            request = RecomposeRequest.model_validate_json(request_json)
        else:
            raise ValueError("invalid composer operation kind")
        if (
            request.operation_id != operation_id
            or request.state_id != base_state_id
            or composer_operation_request_hash(session_id=session_id, kind=kind, request=request) != request_hash
        ):
            raise ValueError("composer request does not match its immutable admission binding")
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
            old = _read(conn, session_id, operation_id)
            if old is not None:
                _binding(old, kind=kind, request_hash=request_hash, actor_user_id=actor_user_id, base_state_id=base_state_id)
                return _record_from_row(old), False
            count = conn.execute(
                select(func.count()).select_from(
                    select(jobs.c.operation_id).where(jobs.c.status.in_(("queued", "running"))).limit(max_nonterminal).subquery()
                )
            ).scalar_one()
            if count >= max_nonterminal:
                raise ComposerOperationCapacityError(retry_after_seconds=5)
            if (
                base_state_id is not None
                and conn.execute(
                    select(composition_states_table.c.id)
                    .where(composition_states_table.c.id == str(base_state_id), composition_states_table.c.session_id == str(session_id))
                    .limit(1)
                ).one_or_none()
                is None
            ):
                raise ComposerOperationPreconditionRefused(
                    error=ComposerOperationError(
                        http_status=404, failure_code="http_error", error_type=None, body={"detail": "State not found"}, diagnostic_id=None
                    )
                )
            now = database_now(conn)
            try:
                with conn.begin_nested():
                    conn.execute(
                        insert(jobs).values(
                            session_id=str(session_id),
                            operation_id=operation_id,
                            kind=kind,
                            status="queued",
                            request_hash=request_hash,
                            actor_user_id=actor_user_id,
                            request_id=request_id,
                            base_state_id=str(base_state_id) if base_state_id else None,
                            request_json=request_json,
                            deadline_at=now + timedelta(seconds=deadline_seconds),
                            attempt=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
            except IntegrityError:
                old = _read(conn, session_id, operation_id)
                if old is not None:
                    _binding(old, kind=kind, request_hash=request_hash, actor_user_id=actor_user_id, base_state_id=base_state_id)
                    return _record_from_row(old), False
                active = conn.execute(
                    select(jobs.c.operation_id, jobs.c.kind)
                    .where(jobs.c.session_id == str(session_id), jobs.c.status.in_(("queued", "running")))
                    .limit(1)
                ).one_or_none()
                if active is not None:
                    raise ComposerOperationActiveError(session_id=session_id, operation_id=active.operation_id, kind=active.kind) from None
                raise
            inserted = _read(conn, session_id, operation_id)
            if inserted is None:
                raise AuditIntegrityError("admitted composer operation vanished")
            return _record_from_row(inserted), True

    def get(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None:
        return self.get_with_database_now(session_id=session_id, operation_id=operation_id)[0]

    def get_with_database_now(self, *, session_id: UUID, operation_id: str) -> tuple[ComposerOperationRecord | None, datetime]:
        _identity(session_id, operation_id)
        with self._engine.connect() as conn:
            row = _read(conn, session_id, operation_id)
            now = database_now(conn)
            return (_record_from_row(row) if row is not None else None), now

    def get_for_turn(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None:
        _identity(session_id, operation_id)
        with self._engine.connect() as conn:
            row = _read(conn, session_id, operation_id, body=True)
            return _record_from_row(row, request_json=row.request_json) if row is not None else None

    def claim_next(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]:
        _bounded(limit)
        with self._engine.connect() as conn:
            now = database_now(conn)
            candidates = conn.execute(
                select(jobs.c.session_id, jobs.c.operation_id)
                .where(
                    jobs.c.status == "queued",
                    jobs.c.cancel_requested_at.is_(None),
                    jobs.c.deadline_at > now,
                    or_(jobs.c.claim_token.is_(None), jobs.c.claim_expires_at <= now),
                    ~select(fences.c.session_id)
                    .where(fences.c.session_id == jobs.c.session_id, fences.c.released_at.is_(None), fences.c.lease_expires_at > now)
                    .exists(),
                    select(sessions_table.c.id)
                    .where(sessions_table.c.id == jobs.c.session_id, sessions_table.c.archived_at.is_(None))
                    .exists(),
                )
                .order_by(jobs.c.created_at, jobs.c.operation_id)
                .limit(limit)
            ).all()
        claims: list[ComposerOperationClaim] = []
        for candidate in candidates:
            sid = UUID(candidate.session_id)
            with locked_session_transaction(self._engine, candidate.session_id) as conn:
                now = database_now(conn)
                query = select(jobs).where(*_key(sid, candidate.operation_id), jobs.c.status == "queued").limit(1)
                if conn.dialect.name == "postgresql":
                    query = query.with_for_update(skip_locked=True)
                row = conn.execute(query).one_or_none()
                if row is None:
                    continue
                token = secrets.token_urlsafe(32)
                result = conn.execute(
                    update(jobs)
                    .where(
                        *_key(sid, candidate.operation_id),
                        jobs.c.status == "queued",
                        jobs.c.attempt == row.attempt,
                        jobs.c.cancel_requested_at.is_(None),
                        jobs.c.deadline_at > now,
                        or_(jobs.c.claim_token.is_(None), jobs.c.claim_expires_at <= now),
                        ~select(fences.c.session_id)
                        .where(fences.c.session_id == str(sid), fences.c.released_at.is_(None), fences.c.lease_expires_at > now)
                        .exists(),
                        select(sessions_table.c.id).where(sessions_table.c.id == str(sid), sessions_table.c.archived_at.is_(None)).exists(),
                    )
                    .values(
                        claim_token=token,
                        claim_owner_instance_id=self._owner_instance_id,
                        claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds),
                        attempt=row.attempt + 1,
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    claims.append(
                        ComposerOperationClaim(session_id=sid, operation_id=row.operation_id, claim_token=token, attempt=row.attempt + 1)
                    )
        return tuple(claims)

    def renew_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord:
        with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
            now = database_now(conn)
            result = conn.execute(
                update(jobs)
                .where(
                    *_key(claim.session_id, claim.operation_id),
                    jobs.c.status == "queued",
                    jobs.c.claim_token == claim.claim_token,
                    jobs.c.attempt == claim.attempt,
                    jobs.c.claim_owner_instance_id == self._owner_instance_id,
                    jobs.c.claim_expires_at > now,
                )
                .values(claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds), updated_at=now)
            )
            if result.rowcount != 1:
                raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
            row = _read(conn, claim.session_id, claim.operation_id)
            if row is None:
                raise AuditIntegrityError("renewed operation missing")
            return _record_from_row(row)

    def release_claim(self, claim: ComposerOperationClaim) -> None:
        with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
            result = conn.execute(
                update(jobs)
                .where(
                    *_key(claim.session_id, claim.operation_id),
                    jobs.c.status == "queued",
                    jobs.c.claim_token == claim.claim_token,
                    jobs.c.attempt == claim.attempt,
                    jobs.c.claim_owner_instance_id == self._owner_instance_id,
                )
                .values(claim_token=None, claim_owner_instance_id=None, claim_expires_at=None, updated_at=database_now(conn))
            )
            if result.rowcount != 1:
                raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)

    def request_cancel(
        self, *, session_id: UUID, operation_id: str, cancelled_failure: ComposerOperationCancelledFailure
    ) -> ComposerOperationRecord | None:
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            row = _read(conn, session_id, operation_id)
            if row is None:
                return None
            if row.status in ("completed", "failed"):
                return _record_from_row(row)
            now = database_now(conn)
            values: _ComposerTerminalValues = {"cancel_requested_at": row.cancel_requested_at or now, "updated_at": now}
            if row.status == "queued" and row.claim_token is None:
                values.update(_terminal_values(cancelled_failure(request_id=row.request_id), now=now, settled_by="request_cancel"))
            conn.execute(update(jobs).where(*_key(session_id, operation_id), jobs.c.status == row.status).values(**values))
            updated = _read(conn, session_id, operation_id)
            if updated is None:
                raise AuditIntegrityError("cancelled operation missing")
            return _record_from_row(updated)

    def settle_unstarted(
        self,
        claim_or_none: ComposerOperationClaim | None,
        *,
        session_id: UUID,
        operation_id: str,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
    ) -> ComposerOperationRecord:
        _identity(session_id, operation_id)
        if type(failure) is not ComposerOperationError or type(authoritative_failure) is not bool:
            raise TypeError("queued settlement requires owned failure and nominal evidence classification")
        if claim_or_none is not None and (
            type(claim_or_none) is not ComposerOperationClaim
            or claim_or_none.session_id != session_id
            or claim_or_none.operation_id != operation_id
        ):
            raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            row = _read(conn, session_id, operation_id)
            if row is not None and row.status in ("completed", "failed"):
                return _record_from_row(row)
            now = database_now(conn)
            if row is None or row.status != "queued":
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
            predicates = [*_key(session_id, operation_id), jobs.c.status == "queued"]
            if claim_or_none is None:
                if row.claim_token is not None and row.claim_expires_at is not None and restore_utc(row.claim_expires_at) > now:
                    raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
                predicates.append(or_(jobs.c.claim_token.is_(None), jobs.c.claim_expires_at <= now))
            else:
                if (
                    not _claim_matches(row, claim_or_none)
                    or row.claim_owner_instance_id != self._owner_instance_id
                    or row.claim_expires_at is None
                    or restore_utc(row.claim_expires_at) <= now
                ):
                    raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id, attempt=claim_or_none.attempt)
                predicates.extend(
                    (
                        jobs.c.claim_token == claim_or_none.claim_token,
                        jobs.c.attempt == claim_or_none.attempt,
                        jobs.c.claim_owner_instance_id == self._owner_instance_id,
                        jobs.c.claim_expires_at > now,
                    )
                )
            selected = _select_failure_on_connection(row, failure, now=now, authoritative_failure=authoritative_failure)
            result = conn.execute(
                update(jobs).where(*predicates).values(**_terminal_values(selected, now=now, settled_by="settle_unstarted"))
            )
            if result.rowcount != 1:
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
            settled = _read(conn, session_id, operation_id)
            if settled is None:
                raise AuditIntegrityError("settled operation missing")
            return _record_from_row(settled)

    def list_expired_queued(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]:
        _bounded(limit)
        with self._engine.connect() as conn:
            now = database_now(conn)
            rows = conn.execute(
                select(*(c for c in jobs.c if c.name != "request_json"))
                .where(
                    jobs.c.status == "queued",
                    or_(jobs.c.deadline_at <= now, jobs.c.cancel_requested_at.is_not(None)),
                    or_(jobs.c.claim_token.is_(None), jobs.c.claim_expires_at <= now),
                )
                .order_by(jobs.c.created_at)
                .limit(limit)
            ).all()
            return tuple(_record_from_row(row) for row in rows)

    def list_expired_running(self, *, limit: int) -> tuple[ComposerOperationRecord, ...]:
        _bounded(limit)
        with self._engine.connect() as conn:
            now = database_now(conn)
            rows = conn.execute(
                select(*(c for c in jobs.c if c.name != "request_json"))
                .where(
                    jobs.c.status == "running",
                    ~select(fences.c.session_id)
                    .where(
                        fences.c.session_id == jobs.c.session_id,
                        fences.c.operation_id == jobs.c.session_operation_id,
                        fences.c.lease_token == jobs.c.session_operation_lease_token,
                        fences.c.operation_epoch == jobs.c.session_operation_epoch,
                        fences.c.released_at.is_(None),
                        fences.c.lease_expires_at > now,
                    )
                    .exists(),
                )
                .order_by(jobs.c.created_at)
                .limit(limit)
            ).all()
            return tuple(_record_from_row(row) for row in rows)

    def count_nonterminal(self, *, limit: int) -> int:
        _bounded(limit)
        with self._engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(
                        select(jobs.c.operation_id).where(jobs.c.status.in_(("queued", "running"))).limit(limit).subquery()
                    )
                ).scalar_one()
            )

    def settle_lost(
        self,
        *,
        session_operation_context: SessionOperationContext,
        session_id: UUID,
        operation_id: str,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
    ) -> ComposerOperationRecord:
        return self._settle_recovery(
            session_id=session_id,
            operation_id=operation_id,
            failure=failure,
            settled_by="settle_lost",
            authoritative_failure=authoritative_failure,
            recovery_context=session_operation_context,
        )

    def settle_own_lapsed(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        owner_instance_id: str,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
    ) -> ComposerOperationRecord:
        if owner_instance_id != self._owner_instance_id:
            raise ValueError("lapsed settlement must belong to this worker instance")
        return self._settle_recovery(
            session_id=session_id,
            operation_id=operation_id,
            failure=failure,
            settled_by="settle_own_lapsed",
            authoritative_failure=authoritative_failure,
            recovery_context=None,
        )

    def settle_lost_inactive_session(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        failure: ComposerOperationError,
        authoritative_failure: bool = False,
    ) -> ComposerOperationRecord:
        return self._settle_recovery(
            session_id=session_id,
            operation_id=operation_id,
            failure=failure,
            settled_by="settle_lost_inactive_session",
            authoritative_failure=authoritative_failure,
            recovery_context=None,
        )

    def _settle_recovery(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        failure: ComposerOperationError,
        settled_by: ComposerOperationSettledBy,
        recovery_context: SessionOperationContext | None,
        authoritative_failure: bool,
    ) -> ComposerOperationRecord:
        _identity(session_id, operation_id)
        if type(failure) is not ComposerOperationError or type(authoritative_failure) is not bool:
            raise TypeError("recovery settlement requires owned failure and nominal evidence classification")
        if recovery_context is not None and type(recovery_context) is not SessionOperationContext:
            raise TypeError("recovery settlement requires an exact SessionOperationContext")
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            row = _read(conn, session_id, operation_id)
            if row is not None and row.status in ("completed", "failed"):
                return _record_from_row(row)
            now = database_now(conn)
            if row is None or row.status != "running":
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
            fence = conn.execute(select(fences).where(fences.c.session_id == str(session_id)).with_for_update()).one_or_none()
            session = conn.execute(select(sessions_table.c.archived_at).where(sessions_table.c.id == str(session_id))).one_or_none()
            exact_old = (
                fence is not None
                and fence.operation_id == row.session_operation_id
                and fence.lease_token == row.session_operation_lease_token
                and fence.operation_epoch == row.session_operation_epoch
            )
            valid = False
            if settled_by == "settle_lost_inactive_session":
                valid = session is not None and session.archived_at is not None
            elif settled_by == "settle_own_lapsed":
                valid = (
                    exact_old
                    and fence is not None
                    and fence.released_at is None
                    and restore_utc(fence.lease_expires_at) <= now
                    and row.claim_owner_instance_id == self._owner_instance_id
                )
            elif recovery_context is not None and fence is not None:
                f = recovery_context.fence
                valid = (
                    recovery_context.operation_kind is SessionOperationKind.COMPOSE
                    and fence.operation_kind == SessionOperationKind.COMPOSE.value
                    and fence.owner_instance_id == self._owner_instance_id
                    and f.session_id == str(session_id)
                    and f.operation_id == fence.operation_id
                    and f.lease_token == fence.lease_token
                    and f.operation_epoch == fence.operation_epoch
                    and fence.operation_epoch > row.session_operation_epoch
                    and fence.released_at is None
                    and restore_utc(fence.lease_expires_at) > now
                )
            if not valid:
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id, attempt=row.attempt)
            failure = _select_failure_on_connection(row, failure, now=now, authoritative_failure=authoritative_failure)
            conn.execute(
                update(jobs)
                .where(*_key(session_id, operation_id), jobs.c.status == "running")
                .values(**_terminal_values(failure, now=now, settled_by=settled_by))
            )
            settled = _read(conn, session_id, operation_id)
            if settled is None:
                raise AuditIntegrityError("recovery settlement missing")
            return _record_from_row(settled)


async def admit_composer_operation(
    authority: ComposerAsyncOperationAuthority,
    *,
    rate_limit: Callable[[], Awaitable[None]],
    session_id: UUID,
    operation_id: str,
    kind: ComposerOperationKind,
    request_hash: str,
    actor_user_id: str,
    request_id: str | None,
    base_state_id: UUID | None,
    request_json: str,
    deadline_seconds: float,
    max_nonterminal: int,
    auth_provider_type: str,
) -> tuple[ComposerOperationRecord, bool]:
    previous = await run_sync_in_worker(authority.get, session_id=session_id, operation_id=operation_id)
    if previous is None:
        await rate_limit()
    return await run_sync_in_worker(
        authority.admit,
        session_id=session_id,
        operation_id=operation_id,
        kind=kind,
        request_hash=request_hash,
        actor_user_id=actor_user_id,
        request_id=request_id,
        base_state_id=base_state_id,
        request_json=request_json,
        deadline_seconds=deadline_seconds,
        max_nonterminal=max_nonterminal,
        auth_provider_type=auth_provider_type,
    )


def start_composer_operation_on_connection(
    conn: Connection, claim: ComposerOperationClaim, *, context: SessionOperationContext, user_message_id: UUID | None
) -> None:
    if context.operation_kind is not SessionOperationKind.COMPOSE or context.fence.session_id != str(claim.session_id):
        raise AuditIntegrityError("composer start context is foreign")
    job = get_composer_operation_for_start_on_connection(conn, claim)
    if (job.kind == "compose_recompose") != (user_message_id is not None):
        raise ValueError("composer start user binding must match the operation kind")
    owner = conn.execute(
        select(fences.c.owner_instance_id)
        .where(
            fences.c.session_id == context.fence.session_id,
            fences.c.operation_id == context.fence.operation_id,
            fences.c.lease_token == context.fence.lease_token,
            fences.c.operation_epoch == context.fence.operation_epoch,
        )
        .limit(1)
    ).scalar_one_or_none()
    if owner is None or job.claim_owner_instance_id != owner:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    now = database_now(conn)
    result = conn.execute(
        update(jobs)
        .where(
            *_key(claim.session_id, claim.operation_id),
            jobs.c.status == "queued",
            jobs.c.claim_token == claim.claim_token,
            jobs.c.attempt == claim.attempt,
            jobs.c.claim_owner_instance_id == owner,
            jobs.c.claim_expires_at > now,
            jobs.c.cancel_requested_at.is_(None),
            jobs.c.deadline_at > now,
        )
        .values(
            status="running",
            session_operation_id=context.fence.operation_id,
            session_operation_lease_token=context.fence.lease_token,
            session_operation_epoch=context.fence.operation_epoch,
            started_at=now,
            claim_expires_at=None,
            user_message_id=str(user_message_id) if user_message_id else None,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        row = _read(conn, claim.session_id, claim.operation_id)
        if row is not None and _claim_matches(row, claim) and row.cancel_requested_at is not None:
            raise ComposerOperationCancelledBeforeStart(claim)
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)


def _running_predicates(running: ComposerOperationRunning) -> tuple[Any, ...]:
    claim, fence = running.claim, running.session_operation_context.fence
    if running.session_operation_context.operation_kind is not SessionOperationKind.COMPOSE or fence.session_id != str(claim.session_id):
        raise AuditIntegrityError("composer running context is foreign")
    return (
        *_key(claim.session_id, claim.operation_id),
        jobs.c.status == "running",
        jobs.c.claim_token == claim.claim_token,
        jobs.c.attempt == claim.attempt,
        jobs.c.session_operation_id == fence.operation_id,
        jobs.c.session_operation_lease_token == fence.lease_token,
        jobs.c.session_operation_epoch == fence.operation_epoch,
    )


def require_composer_operation_mutation_on_connection(conn: Connection, context: SessionOperationContext) -> None:
    """Positive predicate only for a COMPOSE fence bound to a durable job.

    Ordinary COMPOSE leases (including retained synchronous paths) have no job;
    terminal rows retain their fence identity so this lookup cannot fail open.
    """
    if context.operation_kind is not SessionOperationKind.COMPOSE:
        return
    f = context.fence
    row = conn.execute(
        select(
            jobs.c.status,
            jobs.c.cancel_requested_at,
            jobs.c.deadline_at,
            jobs.c.created_at,
            jobs.c.operation_id,
            jobs.c.attempt,
            jobs.c.claim_token,
            jobs.c.session_operation_id,
            jobs.c.session_operation_lease_token,
        )
        .where(jobs.c.session_id == f.session_id, jobs.c.session_operation_epoch == f.operation_epoch)
        .limit(1)
    ).one_or_none()
    if row is None:
        return
    if row.session_operation_id != f.operation_id or row.session_operation_lease_token != f.lease_token or row.status != "running":
        raise ComposerOperationFenceLost(session_id=UUID(f.session_id), operation_id=row.operation_id, attempt=row.attempt)
    if row.cancel_requested_at is not None:
        raise ComposerOperationCancelledDuringTurn(
            ComposerOperationClaim(
                session_id=UUID(f.session_id), operation_id=row.operation_id, claim_token=row.claim_token, attempt=row.attempt
            )
        )
    now = database_now(conn)
    if restore_utc(row.deadline_at) <= now:
        raise ComposerTurnDeadlineExpired(
            session_id=UUID(f.session_id),
            operation_id=row.operation_id,
            remaining_seconds=(restore_utc(row.deadline_at) - now).total_seconds(),
            budget_seconds_at_running=(restore_utc(row.deadline_at) - restore_utc(row.created_at)).total_seconds(),
        )


def _proposal_composer_binding(
    conn: Connection,
    context: SessionOperationContext,
    *,
    user_message_id: UUID | None,
    actor: str,
    running: ComposerOperationRunning | None,
) -> ComposerOperationBinding | None:
    if context.operation_kind is not SessionOperationKind.COMPOSE:
        if running is not None:
            raise AuditIntegrityError("durable proposal binding requires COMPOSE")
        return None
    fence = context.fence
    row = conn.execute(
        select(jobs)
        .where(
            jobs.c.session_id == fence.session_id,
            jobs.c.session_operation_epoch == fence.operation_epoch,
        )
        .limit(1)
    ).one_or_none()
    if row is None:
        if running is not None:
            raise AuditIntegrityError("durable proposal binding has no job")
        return None
    lease = conn.execute(select(fences).where(fences.c.session_id == fence.session_id).limit(1)).one_or_none()
    now = database_now(conn)
    if (
        row.status != "running"
        or row.session_operation_id != fence.operation_id
        or row.session_operation_lease_token != fence.lease_token
        or lease is None
        or lease.operation_id != fence.operation_id
        or lease.lease_token != fence.lease_token
        or lease.operation_epoch != fence.operation_epoch
        or lease.operation_kind != SessionOperationKind.COMPOSE.value
        or lease.released_at is not None
        or restore_utc(lease.lease_expires_at) <= now
        or row.claim_owner_instance_id != lease.owner_instance_id
        or row.claim_token is None
        or row.attempt < 1
    ):
        raise AuditIntegrityError("proposal composer job/fence authority mismatch")
    if running is not None and (
        running.session_operation_context != context
        or running.claim.session_id != UUID(fence.session_id)
        or running.claim.operation_id != row.operation_id
        or running.claim.claim_token != row.claim_token
        or running.claim.attempt != row.attempt
    ):
        raise AuditIntegrityError("proposal composer claim authority mismatch")
    if user_message_id is None or row.user_message_id != str(user_message_id) or actor != f"composer-web:user:{row.actor_user_id}":
        raise AuditIntegrityError("proposal composer actor/user binding mismatch")
    user = conn.execute(
        select(chat_messages_table.c.role, chat_messages_table.c.writer_principal)
        .where(
            chat_messages_table.c.id == str(user_message_id),
            chat_messages_table.c.session_id == fence.session_id,
        )
        .limit(1)
    ).one_or_none()
    if user is None or user.role != "user" or user.writer_principal != "route_user_message":
        raise AuditIntegrityError("proposal composer user is not conversational ingress")
    return ComposerOperationBinding(
        operation_id=row.operation_id,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        attempt=row.attempt,
    )


def derive_proposal_composer_binding_on_connection(
    conn: Connection,
    context: SessionOperationContext,
    *,
    user_message_id: UUID | None,
    actor: str,
    running: ComposerOperationRunning | None = None,
) -> ComposerOperationBinding | None:
    require_composer_operation_mutation_on_connection(conn, context)
    return _proposal_composer_binding(conn, context, user_message_id=user_message_id, actor=actor, running=running)


def prove_revocation_composer_binding_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    user_message_id: UUID | None,
    actor: str,
) -> ComposerOperationBinding:
    if type(running) is not ComposerOperationRunning:
        raise TypeError("revocation requires exact running job")
    binding = _proposal_composer_binding(
        conn, running.session_operation_context, user_message_id=user_message_id, actor=actor, running=running
    )
    if binding is None:
        raise AuditIntegrityError("revocation requires durable job binding")
    return binding


def verify_historical_proposal_composer_binding_on_connection(
    conn: Connection,
    *,
    binding: ComposerOperationBinding,
    session_id: UUID,
    user_message_id: UUID | None,
    actor: str | None,
) -> None:
    row = conn.execute(select(jobs).where(*_key(session_id, binding.operation_id)).limit(1)).one_or_none()
    if (
        row is None
        or row.session_operation_id != binding.session_operation_id
        or row.session_operation_epoch != binding.session_operation_epoch
        or row.attempt != binding.attempt
        or user_message_id is None
        or row.user_message_id != str(user_message_id)
        or actor != f"composer-web:user:{row.actor_user_id}"
    ):
        raise AuditIntegrityError("historical proposal operation authority mismatch")


def require_composer_settlement_actor_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    actor: str,
) -> None:
    row = conn.execute(select(jobs.c.actor_user_id).where(*_running_predicates(running)).limit(1)).one_or_none()
    if row is None or actor != f"user:{row.actor_user_id}":
        raise AuditIntegrityError("composer settlement actor does not match running user")


def bind_composer_operation_user_message_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, user_message_id: UUID
) -> None:
    require_composer_operation_mutation_on_connection(conn, running.session_operation_context)
    row = conn.execute(select(jobs.c.kind, jobs.c.user_message_id).where(*_running_predicates(running)).limit(1)).one_or_none()
    if row is None:
        raise ComposerOperationFenceLost(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, attempt=running.claim.attempt
        )
    if row.kind != "compose_message":
        raise ValueError("only a compose_message operation admits a new user row")
    if row.user_message_id is not None:
        raise AuditIntegrityError("composer user message binding was not fresh")
    result = conn.execute(
        update(jobs)
        .where(
            *_running_predicates(running),
            jobs.c.user_message_id.is_(None),
            jobs.c.kind == "compose_message",
            jobs.c.cancel_requested_at.is_(None),
        )
        .values(user_message_id=str(user_message_id), updated_at=database_now(conn))
    )
    if result.rowcount != 1:
        raise AuditIntegrityError("composer user message binding was not fresh")


def settle_composer_operation_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    outcome: MessageWithStateResponse | ComposerOperationError,
    authoritative_failure: bool = False,
    settled_at: datetime | None = None,
) -> ComposerOperationRecord:
    """Sealed terminal CAS. Failed results cannot authorize business mutations."""
    row = _read(conn, running.claim.session_id, running.claim.operation_id)
    if row is not None and row.status in ("completed", "failed"):
        return _record_from_row(row)
    now = database_now(conn)
    if isinstance(outcome, MessageWithStateResponse):
        require_composer_operation_mutation_on_connection(conn, running.session_operation_context)
        if row is not None and restore_utc(row.deadline_at) <= now:
            raise ComposerOperationFenceLost(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
    elif row is not None:
        outcome = _select_failure_on_connection(row, outcome, now=now, authoritative_failure=authoritative_failure)
    if (
        isinstance(outcome, ComposerOperationError)
        and outcome.failure_code == "request_cancelled"
        and row is not None
        and row.cancel_requested_at is None
    ):
        raise AuditIntegrityError("cancel outcome requires a durable Stop marker")
    result = conn.execute(
        update(jobs)
        .where(*_running_predicates(running))
        .values(**_terminal_values(outcome, now=settled_at if settled_at is not None else now, settled_by="owner_terminal"))
    )
    if result.rowcount != 1:
        raise ComposerOperationFenceLost(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, attempt=running.claim.attempt
        )
    settled = _read(conn, running.claim.session_id, running.claim.operation_id)
    if settled is None:
        raise AuditIntegrityError("owner terminal missing")
    return _record_from_row(settled)
