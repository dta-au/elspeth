"""Single typed SQL authority for durable composer operations.

Admission is immutable; a running job is never returned to the queue. Poll
reads omit request bodies and every cross-session discovery has a hard limit.
"""

from __future__ import annotations

import math
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast, final
from uuid import UUID, uuid4

from pydantic import JsonValue, ValidationError
from sqlalchemy import Connection, Engine, Row, and_, false, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.continuation import (
    ContinuationAdmissionSeed,
    prepare_composer_continuation_admission,
    reserved_user_message_id,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    decode_continuation_policy,
)
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationKind
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from elspeth.web.sessions.composer_operation_preconditions import (
    ComposerOperationPreconditions,
    check_composer_request_preconditions_on_connection,
)
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_PUBLIC_FAILURE_TYPES,
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
    ComposerOperationFailureCursor,
    ComposerOperationFailureOutcome,
    ComposerOperationFenceLost,
    ComposerOperationKind,
    ComposerOperationPreconditionRefused,
    ComposerOperationPublicFailureType,
    ComposerOperationRecord,
    ComposerOperationRunning,
    ComposerOperationSavedRevisions,
    ComposerOperationSettledBy,
    ComposerTurnDeadlineExpired,
    composer_operation_request_hash,
    composer_operation_result_hash,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation import (
    ContinuationProviderBinding,
    HistoricalEffectLocator,
    PreparedContinuationProvider,
    ProviderActionObservation,
    StoredContinuationSnapshot,
    ValidatedHistoricalEffectReference,
    _require_current_continuation_running,
    _require_reserved_provider_frame,
    initialize_continuation,
    load_snapshot,
    prepare_provider_effect_on_connection,
    require_continuation_mutation,
    require_snapshot_cas,
    validate_historical_effect_reference,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.locking import continuation_connection_deadline_scope, locked_session_transaction
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    sessions_table,
    web_instances_table,
)
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as segment_attempts
from elspeth.web.sessions.models import (
    session_operation_fences_table as fences,
)
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerOperationBinding
from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.state_decoder import state_record_from_row
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
    from elspeth.web.composer.continuation import PrivateChunkRef
    from elspeth.web.composer.continuation_attachment_finisher import CompletedAttachmentProjection
    from elspeth.web.composer.continuation_attachment_read import CurrentAttachmentRead
    from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
    from elspeth.web.composer.continuation_clock import ContinuationTimingObservation
    from elspeth.web.composer.continuation_composition_initialization_data import CapturedInitialComposition, InitialCompositionOwners
    from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
    from elspeth.web.composer.continuation_env_inventory import ContinuationEnvSelection, CurrentContinuationEnvInventory
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.composer.continuation_owed_evidence import OwedP4EvidenceData
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.sessions.continuation import ContinuationOwnerCommit, PreparedSegmentSettlement
    from elspeth.web.sessions.continuation_attachment_application import StoredAttachmentApplication
    from elspeth.web.sessions.continuation_attachment_result import AttachmentEffectPreparation, AttachmentResultCheckpoint
    from elspeth.web.sessions.continuation_composition_initialization import CurrentCompositionInitializationSource
    from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result
    from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application
    from elspeth.web.sessions.continuation_primary import PrimaryProviderResult, RetainedPrimaryPhaseData
    from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource
    from elspeth.web.sessions.continuation_tool_failure import RetainedToolFailureSource


def _continuation_claim_scope() -> tuple[ColumnElement[bool], ...]:
    return (segments.c.state.in_(("queued", "interrupted")),)


def _continuation_claim_predicate(conn: Connection, now: datetime) -> ColumnElement[bool]:
    takeover = (
        and_(
            fences.c.lease_expires_at <= now,
            select(web_instances_table.c.instance_id)
            .where(
                web_instances_table.c.instance_id == fences.c.owner_instance_id,
                web_instances_table.c.lease_expires_at <= now,
            )
            .correlate(fences)
            .exists(),
        )
        if conn.dialect.name == "postgresql"
        else fences.c.lease_expires_at <= now
        if conn.dialect.name == "sqlite"
        else false()
    )
    available_fence = (
        select(fences.c.session_id)
        .where(
            fences.c.session_id == segments.c.session_id,
            or_(fences.c.released_at.is_not(None), takeover),
        )
        .correlate(segments)
        .exists()
    )
    parent = (
        select(jobs.c.operation_id)
        .where(
            jobs.c.session_id == segments.c.session_id,
            jobs.c.operation_id == segments.c.operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.status.in_(("queued", "running")),
            jobs.c.current_segment_sequence == segments.c.sequence,
            jobs.c.ownership_generation == segments.c.generation,
            jobs.c.checkpoint_id == segments.c.checkpoint_id,
            select(sessions_table.c.id)
            .where(
                sessions_table.c.id == jobs.c.session_id,
                sessions_table.c.user_id == jobs.c.actor_user_id,
                sessions_table.c.archived_at.is_(None),
            )
            .correlate(jobs)
            .exists(),
        )
        .correlate(segments)
        .exists()
    )
    return and_(
        *_continuation_claim_scope(),
        parent,
        available_fence,
        segments.c.next_eligible_at <= now,
        or_(segments.c.claim_token.is_(None), segments.c.claim_expires_at <= now),
    )


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
            deadline_at=restore_utc(row.deadline_at) if row.deadline_at is not None else None,
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
            lifecycle_version=row.lifecycle_version,
            phase=row.phase,
            status_revision=row.status_revision,
            current_segment_sequence=row.current_segment_sequence,
            ownership_generation=row.ownership_generation,
            checkpoint_id=row.checkpoint_id,
            journal_cursor=row.journal_cursor,
            ingress_phase=row.ingress_phase,
            reserved_user_message_id=UUID(row.reserved_user_message_id) if row.reserved_user_message_id is not None else None,
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


def _saved_failure_revisions(
    conn: Connection, session_id: UUID, records: tuple[ComposerOperationRecord, ...]
) -> dict[str, ComposerOperationSavedRevisions]:
    """Aggregate only the returned operations' own fences, never later session edits.

    A V2 operation writes under one fence per segment attempt, so its saves are
    folded across every attempt fence and keyed under its last fence, which is
    the key the caller looks up (design §2.7, protocol R8).
    """
    owner_of: dict[str, str] = {}
    for record in records:
        if record.session_operation_id is None:
            continue
        if record.lifecycle_version == 2:
            for fence_id in conn.execute(
                select(segment_attempts.c.session_operation_id).where(
                    segment_attempts.c.session_id == str(session_id),
                    segment_attempts.c.operation_id == record.operation_id,
                )
            ).scalars():
                owner_of[fence_id] = record.session_operation_id
        owner_of[record.session_operation_id] = record.session_operation_id
    operation_fences = tuple(owner_of)
    if not operation_fences:
        return {}
    states = composition_states_table
    progress = (
        select(
            states.c.creator_operation_id.label("fence_id"),
            func.count().label("saved_count"),
            func.max(states.c.version).label("latest_version"),
        )
        .where(states.c.session_id == str(session_id), states.c.creator_operation_id.in_(operation_fences))
        .group_by(states.c.creator_operation_id)
        .subquery()
    )
    rows = conn.execute(
        select(progress.c.fence_id, progress.c.saved_count, progress.c.latest_version, states.c.id)
        .join(
            states,
            and_(states.c.creator_operation_id == progress.c.fence_id, states.c.version == progress.c.latest_version),
        )
        .where(states.c.session_id == str(session_id))
    ).all()
    by_owner: dict[str, list[Row[Any]]] = {owner: [] for owner in owner_of.values()}
    for row in rows:
        by_owner[owner_of[row.fence_id]].append(row)
    saved: dict[str, ComposerOperationSavedRevisions] = {}
    try:
        for key, group in by_owner.items():
            if not group:
                continue
            latest = max(group, key=lambda row: row.latest_version)
            saved[key] = ComposerOperationSavedRevisions(
                attribution="fenced",
                count=sum(row.saved_count for row in group),
                latest_state_id=UUID(latest.id),
                latest_version=latest.latest_version,
            )
    except ValueError as exc:
        raise AuditIntegrityError("Invalid operation-owned saved revision projection") from exc
    return saved


def _failure_outcome(record: ComposerOperationRecord, saved: ComposerOperationSavedRevisions) -> ComposerOperationFailureOutcome:
    if record.status != "failed" or record.settled_at is None or record.result_json is None:
        raise AuditIntegrityError("Failure projection requires a sealed failed operation")
    error = ComposerOperationError.model_validate_json(record.result_json, strict=True)
    category = (
        cast(ComposerOperationPublicFailureType, error.error_type) if error.error_type in COMPOSER_OPERATION_PUBLIC_FAILURE_TYPES else None
    )
    return ComposerOperationFailureOutcome(
        session_id=record.session_id,
        operation_id=record.operation_id,
        kind=record.kind,
        user_message_id=record.user_message_id,
        settled_at=record.settled_at,
        failure_code=error.failure_code,
        error_type=category,
        diagnostic_id=error.diagnostic_id,
        request_id=record.request_id,
        saved_revisions=saved,
    )


def get_composer_operation_for_start_on_connection(conn: Connection, claim: ComposerOperationClaim) -> ComposerOperationRecord:
    if type(claim) is not ComposerOperationClaim:
        raise TypeError("claim must be ComposerOperationClaim")
    row = _read(conn, claim.session_id, claim.operation_id, body=True)
    if (
        row is None
        or row.lifecycle_version != 1
        or claim.segment_sequence is not None
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


def _segment_claim_predicates(claim: ComposerOperationClaim, owner_instance_id: str) -> tuple[ColumnElement[bool], ...]:
    """The exact pre-start V2 claim: its segment row, generation, token and owner."""
    return (
        segments.c.session_id == str(claim.session_id),
        segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence,
        segments.c.generation == claim.ownership_generation,
        segments.c.state.in_(("queued", "interrupted")),
        segments.c.claim_token == claim.claim_token,
        segments.c.claim_owner_instance_id == owner_instance_id,
    )


def _request_continuation_cancel_on_connection(conn: Connection, row: Row[Any], *, now: datetime) -> ComposerOperationRecord:
    """Mark Stop on a nonterminal V2 parent; never settle it here.

    The owning worker settles ``request_cancelled`` at its next boundary, or
    the recovery sweep does when no owner remains (design §2.7). The marker
    advances ``status_revision`` so observers see it. A current segment that
    is queued or interrupted with no live claim becomes eligible now, so a
    backed-off segment does not hold the Stop behind its retry delay.
    """
    if row.cancel_requested_at is None:
        marked = conn.execute(
            update(jobs)
            .where(
                *_key(UUID(row.session_id), row.operation_id),
                jobs.c.lifecycle_version == 2,
                jobs.c.status == row.status,
                jobs.c.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=now, updated_at=now, status_revision=jobs.c.status_revision + 1)
        )
        if marked.rowcount != 1:
            raise AuditIntegrityError("continuation Stop marker lost its exact parent")
        conn.execute(
            update(segments)
            .where(
                segments.c.session_id == row.session_id,
                segments.c.operation_id == row.operation_id,
                segments.c.sequence == row.current_segment_sequence,
                segments.c.state.in_(("queued", "interrupted")),
                or_(segments.c.claim_token.is_(None), segments.c.claim_expires_at <= now),
            )
            .values(next_eligible_at=now)
        )
    updated = _read(conn, UUID(row.session_id), row.operation_id)
    if updated is None:
        raise AuditIntegrityError("cancelled operation missing")
    return _record_from_row(updated)


def _request_cancel_on_connection(
    conn: Connection, row: Row[Any], *, cancelled_failure: ComposerOperationCancelledFailure
) -> ComposerOperationRecord:
    """Mutate only under the caller's session lock, after its authorization."""
    if row.status in ("completed", "failed"):
        return _record_from_row(row)
    session_id = UUID(row.session_id)
    operation_id = row.operation_id
    now = database_now(conn)
    if row.lifecycle_version == 2:
        return _request_continuation_cancel_on_connection(conn, row, now=now)
    values: _ComposerTerminalValues = {"cancel_requested_at": row.cancel_requested_at or now, "updated_at": now}
    if row.status == "queued" and row.claim_token is None:
        values.update(_terminal_values(cancelled_failure(request_id=row.request_id), now=now, settled_by="request_cancel"))
    conn.execute(update(jobs).where(*_key(session_id, operation_id), jobs.c.status == row.status).values(**values))
    updated = _read(conn, session_id, operation_id)
    if updated is None:
        raise AuditIntegrityError("cancelled operation missing")
    return _record_from_row(updated)


def _binding(row: Row[Any], *, kind: ComposerOperationKind, request_hash: str, actor_user_id: str, base_state_id: UUID | None) -> None:
    if (
        row.kind != kind
        or row.request_hash != request_hash
        or row.actor_user_id != actor_user_id
        or row.base_state_id != (str(base_state_id) if base_state_id else None)
    ):
        raise ComposerOperationConflictError(session_id=UUID(row.session_id), operation_id=row.operation_id)


def _replay_request_hash(
    old: Row[Any], *, session_id: UUID, kind: ComposerOperationKind, request: SendMessageRequest | RecomposeRequest
) -> str:
    """The hash a replay must match: the existing row's own lifecycle hash.

    A retried POST can negotiate another lifecycle than the one that admitted
    the operation. It is the same request when its bytes hash to the stored
    value under the stored lifecycle's function; ``_binding`` refuses otherwise.
    """
    if old.lifecycle_version == 1:
        return composer_operation_request_hash(session_id=session_id, kind=kind, request=request)
    if old.lifecycle_version == 2:
        return composer_operation_v2_request_hash(session_id=session_id, kind=kind, request=request)
    raise AuditIntegrityError("stored composer operation has an unknown lifecycle version")


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
    if row.lifecycle_version == 1 and restore_utc(row.deadline_at) <= now:
        body = {
            "error_type": "composer_operation_deadline_expired",
            "detail": "The composer request timed out. Reload to review the current session state before resubmitting.",
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
    def __init__(
        self,
        engine: Engine,
        *,
        owner_instance_id: str,
        claim_lease_seconds: int,
        continuation_policy: ContinuationAdmissionPolicy | None = None,
    ) -> None:
        if engine.dialect.name not in ("sqlite", "postgresql") or not owner_instance_id.strip() or not 5 <= claim_lease_seconds <= 600:
            raise ValueError("invalid composer operation authority configuration")
        if continuation_policy is not None and type(continuation_policy) is not ContinuationAdmissionPolicy:
            raise TypeError("continuation policy must be its owned complete admission policy")
        self._engine = engine
        self._owner_instance_id = owner_instance_id
        self._claim_lease_seconds = claim_lease_seconds
        self._continuation_policy = continuation_policy

    def admit(
        self,
        *,
        quota: ComposerQuotaAdmission,
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
        lifecycle_version: Literal[1, 2] = 1,
        continuation_enabled: bool = False,
        seed: ContinuationAdmissionSeed | None = None,
    ) -> tuple[ComposerOperationRecord, bool]:
        _identity(session_id, operation_id)
        _bounded(max_nonterminal)
        selected_lifecycle: object = lifecycle_version
        selected_enabled: object = continuation_enabled
        if type(selected_lifecycle) is not int or selected_lifecycle not in (1, 2):
            raise ValueError("unknown composer lifecycle version")
        if type(selected_enabled) is not bool or (selected_lifecycle == 1 and seed is not None):
            raise ValueError("invalid continuation admission configuration")
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
            or (
                composer_operation_request_hash(session_id=session_id, kind=kind, request=request)
                if lifecycle_version == 1
                else composer_operation_v2_request_hash(session_id=session_id, kind=kind, request=request)
            )
            != request_hash
        ):
            raise ValueError("composer request does not match its immutable admission binding")
        if type(quota) is not ComposerQuotaAdmission:
            raise TypeError("Composer admission requires exact SQL quota configuration")
        # Cleanup completes before the session lock. Admission then takes the
        # session lock before its bucket lock, and commits both effects once.
        quota.prepare(self._engine)
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
            old = _read(conn, session_id, operation_id)
            if old is not None:
                _binding(
                    old,
                    kind=kind,
                    request_hash=_replay_request_hash(old, session_id=session_id, kind=kind, request=request),
                    actor_user_id=actor_user_id,
                    base_state_id=base_state_id,
                )
                return _record_from_row(old), False
            if lifecycle_version == 2 and not continuation_enabled:
                raise ComposerOperationPreconditionRefused(
                    error=ComposerOperationError(
                        http_status=409,
                        failure_code="http_error",
                        error_type="composer_continuation_disabled",
                        body={
                            "detail": {"error_type": "composer_continuation_disabled", "detail": "New continuation requests are disabled."}
                        },
                        diagnostic_id=None,
                    )
                )
            if lifecycle_version == 2:
                original_message_id = check_composer_request_preconditions_on_connection(
                    conn,
                    ComposerOperationPreconditions(session_id, actor_user_id, request_id, base_state_id, kind, request_json),
                    auth_provider_type=auth_provider_type,
                )
                if base_state_id is not None:
                    base_row = conn.execute(
                        select(composition_states_table).where(
                            composition_states_table.c.id == str(base_state_id),
                            composition_states_table.c.session_id == str(session_id),
                        )
                    ).one()
                    try:
                        state_from_record(state_record_from_row(base_row))
                    except (TypeError, ValueError, KeyError) as exc:
                        raise AuditIntegrityError("continuation base state cannot be hydrated") from exc
                initial_trust_mode = conn.execute(
                    select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(session_id))
                ).scalar_one()
                if type(initial_trust_mode) is not str or initial_trust_mode not in ("auto_commit", "explicit_approve"):
                    raise AuditIntegrityError("continuation admission requires the current owned session trust mode")
                if seed is None:
                    if self._continuation_policy is None:
                        raise ValueError("new continuation admission has no validated server policy")
                    seed = prepare_composer_continuation_admission(
                        session_id=session_id,
                        operation_id=operation_id,
                        kind=kind,
                        request_json=request_json,
                        request_hash=request_hash,
                        base_state_id=base_state_id,
                        policy_hash=self._continuation_policy.policy_hash,
                        admission_policy_json=self._continuation_policy.model_dump_json(),
                        initial_trust_mode=cast(Literal["auto_commit", "explicit_approve"], initial_trust_mode),
                        initial_action_json=self._continuation_policy.actions.session_sql.model_dump_json(),
                        original_user_message_id=str(original_message_id) if original_message_id is not None else None,
                    )
                if type(seed) is not ContinuationAdmissionSeed:
                    raise ValueError("fresh continuation admission requires its owned server seed")
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
            initial_checkpoint_id: str | None = None
            reserved_message_id: str | None = None
            if lifecycle_version == 2:
                if seed is None:
                    raise AuditIntegrityError("continuation seed disappeared")
                reserved_message_id = (
                    str(request.expected_user_message_id)
                    if isinstance(request, RecomposeRequest)
                    else reserved_user_message_id(session_id=session_id, operation_id=operation_id)
                )
                seed.validate_binding(
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=kind,
                    request_json=request_json,
                    request_hash=request_hash,
                    base_state_id=base_state_id,
                    reserved_user_message_id=reserved_message_id,
                )
                initial_stack = decode_continuation_snapshot(seed.snapshot_json)
                if initial_stack.mutation_ceiling.initial_trust_mode != initial_trust_mode:
                    raise AuditIntegrityError("fresh continuation seed substituted another initial session trust mode")
                retained_policy = decode_continuation_policy(initial_stack.admission_policy_json, initial_stack.policy_hash)
                initial_action = ContinuationActionPolicy.model_validate_json(seed.action_json, strict=True)
                if initial_action != retained_policy.actions.session_sql:
                    raise AuditIntegrityError("initial action differs from the retained complete admission policy")
                if self._continuation_policy is not None and retained_policy != self._continuation_policy:
                    raise AuditIntegrityError("fresh server seed substituted another admission policy")
                initial_checkpoint_id = str(uuid4())
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
                            deadline_at=now + timedelta(seconds=deadline_seconds) if lifecycle_version == 1 else None,
                            lifecycle_version=lifecycle_version,
                            phase="queued" if lifecycle_version == 2 else None,
                            status_revision=0,
                            current_segment_sequence=1 if lifecycle_version == 2 else None,
                            ownership_generation=1 if lifecycle_version == 2 else None,
                            checkpoint_id=initial_checkpoint_id,
                            journal_cursor=0,
                            ingress_phase=("pending" if kind == "compose_message" else "committed") if lifecycle_version == 2 else None,
                            reserved_user_message_id=reserved_message_id,
                            user_message_id=reserved_message_id if lifecycle_version == 2 and kind == "compose_recompose" else None,
                            attempt=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    if lifecycle_version == 2:
                        if seed is None or initial_checkpoint_id is None:
                            raise AuditIntegrityError("initial continuation binding disappeared")
                        initialize_continuation(
                            conn,
                            session_id=str(session_id),
                            operation_id=operation_id,
                            checkpoint_id=initial_checkpoint_id,
                            snapshot_json=seed.snapshot_json,
                            action_json=seed.action_json,
                            policy_sha256=seed.policy_hash,
                        )
            except IntegrityError:
                old = _read(conn, session_id, operation_id)
                if old is not None:
                    _binding(
                        old,
                        kind=kind,
                        request_hash=_replay_request_hash(old, session_id=session_id, kind=kind, request=request),
                        actor_user_id=actor_user_id,
                        base_state_id=base_state_id,
                    )
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
            # Outside insertion's conflict handler: quota denial or SQL failure
            # rolls back the outer transaction, including this uncommitted job.
            quota.check_on_connection(conn, actor_user_id)
            return _record_from_row(inserted), True

    def get(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None:
        return self.get_with_database_now(session_id=session_id, operation_id=operation_id)[0]

    def get_with_database_now(self, *, session_id: UUID, operation_id: str) -> tuple[ComposerOperationRecord | None, datetime]:
        _identity(session_id, operation_id)
        with self._engine.connect() as conn, continuation_connection_deadline_scope(conn, str(session_id)):
            row = _read(conn, session_id, operation_id)
            now = database_now(conn)
            return (_record_from_row(row) if row is not None else None), now

    def get_for_turn(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None:
        _identity(session_id, operation_id)
        with self._engine.connect() as conn, continuation_connection_deadline_scope(conn, str(session_id)):
            row = _read(conn, session_id, operation_id, body=True)
            return _record_from_row(row, request_json=row.request_json) if row is not None else None

    def read_segment_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord | None:
        """Read only the exact still-live pre-start segment claim.

        A successor's parent is running. There is no V1 queued-parent or
        legacy-deadline condition; start rechecks this read under its lock.
        """
        if type(claim) is not ComposerOperationClaim or claim.segment_sequence is None or claim.ownership_generation is None:
            raise TypeError("segment claim observation requires its exact V2 claim")
        with self._engine.connect() as conn:
            now = database_now(conn)
            row = conn.execute(
                select(jobs)
                .join(segments, and_(segments.c.session_id == jobs.c.session_id, segments.c.operation_id == jobs.c.operation_id))
                .where(
                    *_segment_claim_predicates(claim, self._owner_instance_id),
                    segments.c.claim_expires_at > now,
                    jobs.c.lifecycle_version == 2,
                    jobs.c.status.in_(("queued", "running")),
                    jobs.c.current_segment_sequence == claim.segment_sequence,
                    jobs.c.ownership_generation == claim.ownership_generation,
                    jobs.c.attempt == claim.attempt,
                )
            ).one_or_none()
            return _record_from_row(row, request_json=row.request_json) if row is not None else None

    def settle_continuation_owner(
        self,
        running: ComposerOperationRunning,
        *,
        outcome: MessageWithStateResponse | ComposerOperationError,
        env_ref_names: frozenset[str] = frozenset(),
        deadline: ComposerDrainDeadline | None = None,
    ) -> ComposerOperationRecord:
        """Use the existing current-fence V2 terminal body, never a V1 deadline."""
        if type(running) is not ComposerOperationRunning or running.claim.segment_sequence is None:
            raise TypeError("continuation terminal requires its actual V2 running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id), deadline=deadline) as conn:
            from elspeth.web.composer.continuation_next_action import validate_continuation_structure_on_connection
            from elspeth.web.composer.continuation_selection import SelectedContinuationState
            from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            selected = SelectedContinuationState(
                stored.checkpoint_id,
                decode_continuation_snapshot(stored.payload_json),
                load_selected_private_chunks_on_connection(conn, running, stored),
            )
            ambiguous = validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=env_ref_names)
            if ambiguous:
                from elspeth.web.sessions.composer_operation_errors import request_cancelled_error, worker_lost_error

                if type(outcome) is MessageWithStateResponse:
                    raise AuditIntegrityError("continuation success cannot conceal an unresolved actual effect")
                parent = _read(conn, running.claim.session_id, running.claim.operation_id)
                if parent is None:
                    raise AuditIntegrityError("continuation disposition lost its logical request")
                outcome = (
                    request_cancelled_error(request_id=parent.request_id)
                    if parent.cancel_requested_at is not None
                    else worker_lost_error(request_id=parent.request_id)
                )
            return settle_composer_operation_on_connection(conn, running, outcome=outcome)

    def classify_continuation_owner(
        self,
        running: ComposerOperationRunning,
        *,
        env_ref_names: frozenset[str],
        deadline: ComposerDrainDeadline | None = None,
    ) -> Literal["handoff", "handover", "unresolved"]:
        from elspeth.web.composer.continuation_handoff import classify_drain_cut_on_connection
        from elspeth.web.composer.continuation_selection import SelectedContinuationState
        from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id), deadline=deadline) as conn:
            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            selected = SelectedContinuationState(
                stored.checkpoint_id,
                decode_continuation_snapshot(stored.payload_json),
                load_selected_private_chunks_on_connection(conn, running, stored),
            )
            return classify_drain_cut_on_connection(conn, running, selected, env_ref_names=env_ref_names)

    def load_continuation_snapshot(self, running: ComposerOperationRunning) -> StoredContinuationSnapshot:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            return load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)

    def read_continuation_timing(
        self, running: ComposerOperationRunning, *, deadline: ComposerDrainDeadline | None = None
    ) -> ContinuationTimingObservation:
        from elspeth.web.composer.continuation_clock import freeze_observation
        from elspeth.web.sessions.time_normalization import restore_utc

        with locked_session_transaction(self._engine, str(running.claim.session_id), deadline=deadline) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            stored = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            root = decode_continuation_snapshot(stored.payload_json)
            policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
            segment = conn.execute(
                select(segments).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                    segments.c.generation == running.claim.ownership_generation,
                )
            ).one()
            if segment.deadline_at is None or segment.cooperative_at is None:
                raise AuditIntegrityError("running continuation lost its actual segment clock")
            return freeze_observation(
                sequence=segment.sequence,
                generation=segment.generation,
                now=database_now(conn),
                deadline=restore_utc(segment.deadline_at),
                cooperative=restore_utc(segment.cooperative_at),
                watermark=policy.segment.watermark,
            )

    def load_continuation_chunks(
        self,
        running: ComposerOperationRunning,
        snapshot: StoredContinuationSnapshot,
    ) -> tuple[PrivateChunk, ...]:
        from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

        if type(running) is not ComposerOperationRunning or type(snapshot) is not StoredContinuationSnapshot:
            raise AuditIntegrityError("private selection requires nominal running and stored owners")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return load_selected_private_chunks_on_connection(conn, running, snapshot)

    def read_continuation_env_selection(
        self,
        running: ComposerOperationRunning,
        *,
        expected_checkpoint_id: str,
        expected_cursor: int,
    ) -> ContinuationEnvSelection:
        from elspeth.web.composer.continuation_env_inventory import read_env_selection_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("inventory selection requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_env_selection_on_connection(
                conn, running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor
            )

    def prepare_provider_effect(
        self,
        running: ComposerOperationRunning,
        *,
        family: int,
        input_sha256: str,
        snapshot_json: str,
        expected_checkpoint_id: str,
        expected_cursor: int,
        primary_input_json: str | None = None,
    ) -> ContinuationProviderBinding:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return prepare_provider_effect_on_connection(
                conn,
                running,
                family=family,
                input_sha256=input_sha256,
                snapshot_json=snapshot_json,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
                primary_input_json=primary_input_json,
            )

    def prepare_provider_action(
        self,
        running: ComposerOperationRunning,
        *,
        family: int,
        input_sha256: str,
        snapshot_json: str,
        expected_checkpoint_id: str,
        expected_cursor: int,
        primary_input_json: str | None = None,
    ) -> PreparedContinuationProvider:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            binding = prepare_provider_effect_on_connection(
                conn,
                running,
                family=family,
                input_sha256=input_sha256,
                snapshot_json=snapshot_json,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
                primary_input_json=primary_input_json,
            )
            snapshot = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            return PreparedContinuationProvider(binding, snapshot)

    def observe_provider_action(self, binding: ContinuationProviderBinding) -> ProviderActionObservation:
        if type(binding) is not ContinuationProviderBinding:
            raise TypeError("provider observation requires its actual nominal binding")
        running = binding.running
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        if binding.effect_key != f"provider:{binding.family}:{binding.ordinal}":
            raise AuditIntegrityError("provider observation differs from its actual prepared input")
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(self._engine, sid) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            snapshot = load_snapshot(conn, session_id=sid, operation_id=operation_id)
            selected = _require_reserved_provider_frame(decode_continuation_snapshot(snapshot.payload_json))
            if (selected.provider_effect_key, selected.provider_input_sha256) != (binding.effect_key, binding.input_sha256):
                raise AuditIntegrityError("provider observation differs from its actual selected provider locator")
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key
                )
            ).one()
            if effect.owner != "provider" or effect.input_sha256 != binding.input_sha256:
                raise AuditIntegrityError("provider observation differs from its actual prepared input")
            if (effect.creator_sequence, effect.creator_generation) != (
                running.claim.segment_sequence,
                running.claim.ownership_generation,
            ):
                raise AuditIntegrityError("initial provider observation changed its immutable preparing owner")
            if (
                effect.disposition != "prepared"
                or effect.action_json is not None
                or effect.receipt_key is not None
                or effect.result_chunk_key is not None
                or effect.provider_attempt_id is not None
                or effect.physical_sequence is not None
                or effect.physical_generation is not None
                or effect.requested_model is not None
                or effect.response_finished_at is not None
                or effect.committed_state_ids_json is not None
                or effect.graph_sha256 is not None
            ):
                raise AuditIntegrityError("storage-only provider observation requires an unadvanced prepared effect")
            return ProviderActionObservation(snapshot, binding.effect_key, "prepared", None, None, None)

    def read_composition_initialization(
        self,
        running: ComposerOperationRunning,
        *,
        composer: ComposerServiceImpl,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
    ) -> CurrentCompositionInitializationSource:
        from elspeth.web.sessions.continuation_composition_initialization import read_composition_initialization_source_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("first Compose read requires its actual nominal running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_composition_initialization_source_on_connection(conn, composer, running, selected=selected, inventory=inventory)

    def publish_composition_initialization(
        self,
        running: ComposerOperationRunning,
        *,
        composer: ComposerServiceImpl,
        owners: InitialCompositionOwners,
        captured: CapturedInitialComposition,
    ) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_composition_initialization import publish_composition_initialization_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("first Compose publication requires its actual nominal running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return publish_composition_initialization_on_connection(conn, composer, running, owners=owners, captured=captured)

    def read_continuation_runtime(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
    ) -> ContinuationRuntimeSource:
        from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("runtime read requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_tool_failure(
        self, running: ComposerOperationRunning, *, env_ref_names: frozenset[str]
    ) -> RetainedToolFailureSource | None:
        from elspeth.web.sessions.continuation_tool_failure import read_tool_failure_source_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_tool_failure_source_on_connection(conn, running, env_ref_names=env_ref_names)

    def read_continuation_owed_batch_runtime(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> ContinuationRuntimeSource:
        from elspeth.web.sessions.continuation_runtime import read_owed_batch_runtime_source_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_owed_batch_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_owed_tool_runtime(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> ContinuationRuntimeSource:
        from elspeth.web.sessions.continuation_runtime import read_owed_tool_runtime_source_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_owed_tool_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_phase_primary(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        reference: PrivateChunkRef,
        env_ref_names: frozenset[str],
    ) -> RetainedPrimaryPhaseData:
        from elspeth.web.sessions.continuation_primary import read_retained_primary_phase_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_retained_primary_phase_on_connection(
                conn, running, selected=selected, reference=reference, env_ref_names=env_ref_names
            )

    def apply_attachment_application(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        projection: CompletedAttachmentProjection,
        captured: CapturedContinuationRuntime,
    ) -> StoredAttachmentApplication:
        from elspeth.web.sessions.continuation_attachment_application import apply_attachment_application_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment application requires its actual current running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return apply_attachment_application_on_connection(
                conn, running, selected=selected, inventory=inventory, projection=projection, captured=captured
            )

    def read_applied_attachment(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        effect_key: str,
    ) -> StoredAttachmentApplication:
        from elspeth.web.sessions.continuation_attachment_application import read_applied_attachment_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment application read requires its actual current running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_applied_attachment_on_connection(conn, running, selected=selected, inventory=inventory, effect_key=effect_key)

    def prepare_attachment_result(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> AttachmentEffectPreparation:
        from elspeth.web.sessions.continuation_attachment_result import prepare_attachment_result_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment operation requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return prepare_attachment_result_on_connection(conn, running, selected=selected, inventory=inventory)

    def publish_attachment_result(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        actual: CurrentAttachmentRead,
        pre_state: CompositionState,
        read_audit: PortableDispatchAudit | None = None,
    ) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_attachment_result import publish_attachment_result_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment operation requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return publish_attachment_result_on_connection(
                conn, running, selected=selected, inventory=inventory, actual=actual, pre_state=pre_state, read_audit=read_audit
            )

    def read_attachment_result(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> AttachmentResultCheckpoint:
        from elspeth.web.sessions.continuation_attachment_result import read_attachment_result_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment operation requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_attachment_result_on_connection(conn, running, selected=selected, inventory=inventory)

    def apply_attachment_history(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_attachment_result import apply_attachment_history_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("attachment operation requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return apply_attachment_history_on_connection(conn, running, selected=selected, inventory=inventory)

    def begin_continuation_primary(self, prepared: PreparedContinuationProvider) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_primary import begin_primary_provider_on_connection

        if type(prepared) is not PreparedContinuationProvider:
            raise AuditIntegrityError("primary dispatch requires its actual prepared owner result")
        with locked_session_transaction(self._engine, str(prepared.binding.running.claim.session_id)) as conn:
            return begin_primary_provider_on_connection(conn, prepared)

    def publish_continuation_primary(
        self,
        binding: ContinuationProviderBinding,
        *,
        selected: StoredContinuationSnapshot,
        outcome: _CallModelOutcome,
        recorder: RecorderSnapshot,
        env_ref_names: frozenset[str] = frozenset(),
    ) -> PrimaryProviderResult:
        from elspeth.web.sessions.continuation_primary import publish_primary_provider_on_connection

        if type(binding) is not ContinuationProviderBinding:
            raise AuditIntegrityError("primary publication requires its actual provider binding")
        with locked_session_transaction(self._engine, str(binding.running.claim.session_id)) as conn:
            return publish_primary_provider_on_connection(
                conn,
                binding,
                selected=selected,
                outcome=outcome,
                recorder=recorder,
                env_ref_names=env_ref_names,
            )

    def read_continuation_primary(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        checkpoint_id: str,
        cursor: int,
        effect_key: str,
        input_sha256: str,
    ) -> PrimaryProviderResult:
        from elspeth.web.sessions.continuation_primary import read_primary_provider_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("primary source read requires actual current claim custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_primary_provider_on_connection(
                conn,
                running,
                selected=selected,
                checkpoint_id=checkpoint_id,
                cursor=cursor,
                effect_key=effect_key,
                input_sha256=input_sha256,
            )

    def prepare_continuation_p4(
        self,
        running: ComposerOperationRunning,
        *,
        expected_checkpoint_id: str,
        expected_cursor: int,
        pending_batch: PrivateChunk,
        env_ref_names: frozenset[str] = frozenset(),
        captured: CapturedContinuationRuntime | None = None,
    ) -> ContinuationOwnerCommit:
        from elspeth.web.sessions.continuation_p4 import prepare_p4_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return prepare_p4_on_connection(
                conn,
                running,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
                pending_batch=pending_batch,
                env_ref_names=env_ref_names,
                captured=captured,
            )

    def read_continuation_p4_result(
        self,
        running: ComposerOperationRunning,
        *,
        expected_checkpoint_id: str,
        expected_cursor: int,
        effect_key: str,
        env_ref_names: frozenset[str] = frozenset(),
    ) -> ReadmittedP4Result:
        from elspeth.web.sessions.continuation_p4 import read_p4_result_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_p4_result_on_connection(
                conn,
                running,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
                effect_key=effect_key,
                env_ref_names=env_ref_names,
            )

    def read_continuation_turn_evidence(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
    ) -> AdmittedTurnEvidence:
        from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("cold evidence requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_owed_p4_env_selection(
        self,
        running: ComposerOperationRunning,
        *,
        expected_checkpoint_id: str,
        expected_cursor: int,
        effect_key: str,
    ) -> ContinuationEnvSelection:
        from elspeth.web.composer.continuation_owed_p4 import read_owed_p4_env_selection_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("owed P4 selection requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_owed_p4_env_selection_on_connection(
                conn, running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor, effect_key=effect_key
            )

    def read_continuation_owed_p4_result(
        self,
        running: ComposerOperationRunning,
        *,
        inventory: CurrentContinuationEnvInventory,
        expected_checkpoint_id: str,
        expected_cursor: int,
        effect_key: str,
    ) -> ReadmittedP4Result:
        from elspeth.web.composer.continuation_owed_p4 import read_owed_p4_result_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("owed P4 result requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_owed_p4_result_on_connection(
                conn,
                running,
                inventory=inventory,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
                effect_key=effect_key,
            )

    def read_continuation_owed_p4_evidence(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        effect_key: str,
    ) -> OwedP4EvidenceData:
        from elspeth.web.composer.continuation_owed_evidence import read_owed_p4_evidence_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("owed evidence requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return read_owed_p4_evidence_on_connection(conn, running, selected=selected, inventory=inventory, effect_key=effect_key)

    def apply_continuation_p4_application(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        expected_evidence_chunk: PrivateChunk,
        expected_pending_batch_chunk: PrivateChunk,
    ) -> StoredP4Application:
        from elspeth.web.sessions.continuation_p4_application_store import apply_p4_application_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("P4 application requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return apply_p4_application_on_connection(
                conn,
                running,
                selected=selected,
                inventory=inventory,
                expected_evidence_chunk=expected_evidence_chunk,
                expected_pending_batch_chunk=expected_pending_batch_chunk,
            )

    def validate_historical_effect_references(
        self,
        running: ComposerOperationRunning,
        *,
        expected_checkpoint_id: str,
        expected_cursor: int,
        locators: tuple[HistoricalEffectLocator, ...],
    ) -> tuple[ValidatedHistoricalEffectReference, ...]:
        if any(type(locator) is not HistoricalEffectLocator for locator in locators):
            raise TypeError("historical validation requires nominal retained locators")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            if (
                require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
                is not True
            ):
                raise AuditIntegrityError("historical reference restore requires current continuation authority")
            require_snapshot_cas(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
            )
            parent = conn.execute(select(jobs.c.actor_user_id).where(*_key(running.claim.session_id, running.claim.operation_id))).one()
            principal = conn.execute(
                select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
                    sessions_table.c.id == str(running.claim.session_id),
                )
            ).one()
            if principal.archived_at is not None or principal.user_id != parent.actor_user_id:
                raise AuditIntegrityError("historical reference restore changed its current authenticated principal")
            return tuple(
                validate_historical_effect_reference(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    effect_key=locator.effect_key,
                    expected_input_sha256=locator.input_sha256,
                    expected_creator_operation_id=locator.creator_operation_id,
                    expected_receipt_id=locator.receipt_id,
                    expected_inserted_state_ids=locator.inserted_state_ids,
                    expected_authored_state_hash=locator.authored_state_hash,
                )
                for locator in locators
            )

    def list_failure_outcomes(
        self,
        *,
        session_id: UUID,
        actor_user_id: str,
        limit: int = 50,
        cursor: ComposerOperationFailureCursor | None = None,
        operation_id: str | None = None,
    ) -> tuple[tuple[ComposerOperationFailureOutcome, ...], ComposerOperationFailureCursor | None]:
        """Read a bounded authenticated projection; cursors never confer authority."""
        if type(session_id) is not UUID or type(actor_user_id) is not str or not actor_user_id:
            raise ValueError("Failure history requires session and actor identities")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Failure history limit must be between 1 and 100")
        if operation_id is not None:
            _identity(session_id, operation_id)
            if cursor is not None:
                raise ValueError("Exact operation lookup cannot be paginated")
        if cursor is not None and type(cursor) is not ComposerOperationFailureCursor:
            raise TypeError("Failure history cursor must be an owned cursor")
        query = select(*(column for column in jobs.c if column.name != "request_json")).where(
            jobs.c.session_id == str(session_id), jobs.c.actor_user_id == actor_user_id, jobs.c.status == "failed"
        )
        if operation_id is not None:
            query = query.where(jobs.c.operation_id == operation_id)
        if cursor is not None:
            query = query.where(
                or_(
                    jobs.c.settled_at < cursor.settled_at,
                    and_(jobs.c.settled_at == cursor.settled_at, jobs.c.operation_id < cursor.operation_id),
                )
            )
        query = query.order_by(jobs.c.settled_at.desc(), jobs.c.operation_id.desc()).limit(limit + 1)
        with self._engine.connect() as conn:
            records = tuple(_record_from_row(row) for row in conn.execute(query))
            selected = records[:limit]
            progress = _saved_failure_revisions(conn, session_id, selected)
            outcomes = []
            for record in selected:
                if record.session_operation_id is None:
                    saved = ComposerOperationSavedRevisions(attribution="unstarted", count=0, latest_state_id=None, latest_version=None)
                elif record.session_operation_id in progress:
                    saved = progress[record.session_operation_id]
                else:
                    saved = ComposerOperationSavedRevisions(attribution="fenced", count=0, latest_state_id=None, latest_version=None)
                outcomes.append(_failure_outcome(record, saved))
            next_cursor = None
            if len(records) > limit:
                last = selected[-1]
                if last.settled_at is None:
                    raise AuditIntegrityError("Failed operation has no settlement time")
                next_cursor = ComposerOperationFailureCursor(settled_at=last.settled_at, operation_id=last.operation_id)
            return tuple(outcomes), next_cursor

    def claim_segment(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]:
        _bounded(limit)
        with self._engine.connect() as conn:
            now = database_now(conn)
            candidates = conn.execute(
                select(
                    segments.c.session_id,
                    segments.c.operation_id,
                    segments.c.sequence,
                )
                .where(_continuation_claim_predicate(conn, now))
                .order_by(
                    segments.c.next_eligible_at,
                    segments.c.enqueued_at,
                    segments.c.session_id,
                    segments.c.operation_id,
                    segments.c.sequence,
                )
                .limit(limit)
            ).all()
        claimed: list[ComposerOperationClaim] = []
        for hint in candidates:
            with locked_session_transaction(self._engine, hint.session_id) as conn:
                now = database_now(conn)
                row = conn.execute(
                    select(segments, jobs.c.attempt.label("parent_attempt"))
                    .join(
                        jobs,
                        and_(jobs.c.session_id == segments.c.session_id, jobs.c.operation_id == segments.c.operation_id),
                    )
                    .where(
                        segments.c.session_id == hint.session_id,
                        segments.c.operation_id == hint.operation_id,
                        segments.c.sequence == hint.sequence,
                        _continuation_claim_predicate(conn, now),
                    )
                ).one_or_none()
                if row is None:
                    continue
                token = secrets.token_urlsafe(32)
                changed = conn.execute(
                    update(segments)
                    .where(
                        segments.c.session_id == hint.session_id,
                        segments.c.operation_id == hint.operation_id,
                        segments.c.sequence == hint.sequence,
                        segments.c.generation == row.generation,
                        _continuation_claim_predicate(conn, now),
                    )
                    .values(
                        claim_token=token,
                        claim_owner_instance_id=self._owner_instance_id,
                        claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds),
                    )
                )
                if changed.rowcount != 1:
                    continue
                parent = conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.session_id == hint.session_id,
                        jobs.c.operation_id == hint.operation_id,
                        jobs.c.lifecycle_version == 2,
                        jobs.c.status.in_(("queued", "running")),
                        jobs.c.current_segment_sequence == hint.sequence,
                        jobs.c.ownership_generation == row.generation,
                        jobs.c.checkpoint_id == row.checkpoint_id,
                        jobs.c.attempt == row.parent_attempt,
                    )
                    .values(attempt=row.parent_attempt + 1, updated_at=now, status_revision=jobs.c.status_revision + 1)
                )
                if parent.rowcount != 1:
                    raise ComposerOperationFenceLost(
                        session_id=UUID(hint.session_id), operation_id=hint.operation_id, attempt=row.parent_attempt + 1
                    )
                claimed.append(
                    ComposerOperationClaim(
                        session_id=UUID(hint.session_id),
                        operation_id=hint.operation_id,
                        claim_token=token,
                        attempt=row.parent_attempt + 1,
                        segment_sequence=hint.sequence,
                        ownership_generation=row.generation,
                    )
                )
        return tuple(claimed)

    def claim_next(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]:
        _bounded(limit)
        with self._engine.connect() as conn:
            now = database_now(conn)
            candidates = conn.execute(
                select(jobs.c.session_id, jobs.c.operation_id)
                .where(
                    jobs.c.lifecycle_version == 1,
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
                query = (
                    select(jobs)
                    .where(*_key(sid, candidate.operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued")
                    .limit(1)
                )
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
                        jobs.c.lifecycle_version == 1,
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
            if claim.segment_sequence is not None:
                # A V2 claim lives on its segment row; the parent never holds one.
                renewed = conn.execute(
                    update(segments)
                    .where(*_segment_claim_predicates(claim, self._owner_instance_id), segments.c.claim_expires_at > now)
                    .values(claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds))
                )
                if renewed.rowcount != 1:
                    raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
                row = _read(conn, claim.session_id, claim.operation_id)
                if row is None:
                    raise AuditIntegrityError("renewed operation missing")
                return _record_from_row(row)
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
            if claim.segment_sequence is not None:
                released = conn.execute(
                    update(segments)
                    .where(*_segment_claim_predicates(claim, self._owner_instance_id))
                    .values(claim_token=None, claim_owner_instance_id=None, claim_expires_at=None)
                )
                if released.rowcount != 1:
                    raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
                return
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
        """Trusted internal cancellation; HTTP callers must use request_owner_cancel."""
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            row = _read(conn, session_id, operation_id)
            if row is None:
                return None
            return _request_cancel_on_connection(conn, row, cancelled_failure=cancelled_failure)

    def request_owner_cancel(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        actor_user_id: str,
        auth_provider_type: str,
        cancelled_failure: ComposerOperationCancelledFailure,
    ) -> ComposerOperationRecord | None:
        """Authenticate the Stop actor again inside the mutation's session lock."""
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
            row = _read(conn, session_id, operation_id)
            if row is None or row.actor_user_id != actor_user_id:
                return None
            return _request_cancel_on_connection(conn, row, cancelled_failure=cancelled_failure)

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
            if row is not None and row.lifecycle_version != 1:
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
            if row is not None and row.status in ("completed", "failed"):
                return _record_from_row(row)
            now = database_now(conn)
            if row is None or row.status != "queued":
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
            predicates = [*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued"]
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
                    jobs.c.lifecycle_version == 1,
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
                    jobs.c.lifecycle_version == 1,
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
            if row is not None and row.lifecycle_version != 1:
                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
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
                .where(*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "running")
                .values(**_terminal_values(failure, now=now, settled_by=settled_by))
            )
            settled = _read(conn, session_id, operation_id)
            if settled is None:
                raise AuditIntegrityError("recovery settlement missing")
            return _record_from_row(settled)


async def admit_composer_operation(
    authority: ComposerAsyncOperationAuthority,
    *,
    quota: ComposerQuotaAdmission,
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
    lifecycle_version: Literal[1, 2] = 1,
    continuation_enabled: bool = False,
) -> tuple[ComposerOperationRecord, bool]:
    return await run_sync_in_worker(
        authority.admit,
        quota=quota,
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
        lifecycle_version=lifecycle_version,
        continuation_enabled=continuation_enabled,
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
            jobs.c.lifecycle_version == 1,
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
    from elspeth.web.sessions.continuation import require_continuation_mutation

    if require_continuation_mutation(conn, context):
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
    prepared_settlement: PreparedSegmentSettlement | None = None,
) -> ComposerOperationRecord:
    """Sealed terminal CAS. Failed results cannot authorize business mutations."""
    if running.claim.segment_sequence is not None:
        from elspeth.web.sessions.continuation import settle_continuation_on_connection

        current = _read(conn, running.claim.session_id, running.claim.operation_id)
        if current is not None and current.status in ("completed", "failed"):
            return _record_from_row(current)
        if current is not None and isinstance(outcome, ComposerOperationError):
            outcome = _select_failure_on_connection(current, outcome, now=database_now(conn), authoritative_failure=authoritative_failure)
        settle_continuation_on_connection(conn, running, outcome=outcome, prepared_settlement=prepared_settlement)
        settled_v2 = _read(conn, running.claim.session_id, running.claim.operation_id)
        if settled_v2 is None:
            raise AuditIntegrityError("continuation terminal missing")
        return _record_from_row(settled_v2)
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
