"""Single typed SQL authority for durable composer operations.

Admission is immutable; a running job is never returned to the queue. Poll
reads omit request bodies and every cross-session discovery has a hard limit.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast, final
from uuid import UUID, uuid4, uuid5

from pydantic import JsonValue, ValidationError
from sqlalchemy import Connection, Engine, Row, and_, false, func, insert, or_, select, update
from sqlalchemy.engine import RootTransaction
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
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.continuation_retention_memo import (
    RetentionStageMemo,
    borrow_v4_preparation_memo_scope,
    current_retention_stage_memo,
    retention_stage_memo_scope,
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
    composer_operation_admission_request_hash,
    composer_operation_result_hash,
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
from elspeth.web.sessions.locking import continuation_connection_deadline_scope, locked_session_transaction, session_data_read_transaction
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_graph_parks_table,
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
    from elspeth.web.composer.continuation_control_transitions import StoredContinuationControlIntent
    from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit
    from elspeth.web.composer.continuation_env_inventory import ContinuationEnvSelection, CurrentContinuationEnvInventory
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.composer.continuation_owed_evidence import OwedP4EvidenceData
    from elspeth.web.composer.continuation_prepared_p4 import PreparedP4Value
    from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer, PrimaryNoSdkRefusalPermit
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.composer.continuation_tool_failure import TerminalServiceCompletion
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.sessions.continuation import ContinuationOwnerCommit, PreparedSegmentSettlement
    from elspeth.web.sessions.continuation_attachment_application import StoredAttachmentApplication
    from elspeth.web.sessions.continuation_attachment_result import AttachmentEffectPreparation, AttachmentResultCheckpoint
    from elspeth.web.sessions.continuation_composition_initialization import CurrentCompositionInitializationSource
    from elspeth.web.sessions.continuation_graph_recovery_projection import GraphRecoveryProjectionInput
    from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result
    from elspeth.web.sessions.continuation_p4_application_store import (
        StoredP4Application,
        StoredTerminalP4Application,
        StoredTerminalP4Preparation,
    )
    from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt, PrimaryProviderResult, RetainedPrimaryPhaseData
    from elspeth.web.sessions.continuation_publication_projection import PublicationProjectionInput
    from elspeth.web.sessions.continuation_recovery_candidates import ContinuationInactiveCandidate, ContinuationRecoveryCandidate
    from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource
    from elspeth.web.sessions.continuation_tool_failure import RetainedToolFailureSource
    from elspeth.web.sessions.graph_recovery import GraphRecoverySettlement
    from elspeth.web.sessions.protocol import ChatMessageRecord, CompositionStateRecord
    from elspeth.web.sessions.service import SessionServiceImpl


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
            created_at=restore_utc(row.created_at),
            updated_at=restore_utc(row.updated_at),
            started_at=restore_utc(row.started_at) if row.started_at is not None else None,
            settled_at=restore_utc(row.settled_at) if row.settled_at is not None else None,
            cancel_requested_at=restore_utc(row.cancel_requested_at) if row.cancel_requested_at is not None else None,
            claim_owner_instance_id=row.claim_owner_instance_id,
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
            phase=row.phase,
            status_revision=row.status_revision,
            current_segment_sequence=row.current_segment_sequence,
            ownership_generation=row.ownership_generation,
            checkpoint_id=row.checkpoint_id,
            journal_cursor=row.journal_cursor,
            ingress_phase=row.ingress_phase,
            reserved_user_message_id=UUID(row.reserved_user_message_id),
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

    An operation writes under one fence per segment attempt, so its saves are
    folded across every attempt fence and keyed under its last fence, which is
    the key the caller looks up (design §2.7, protocol R8).
    """
    owner_of: dict[str, str] = {}
    for record in records:
        if record.session_operation_id is None:
            continue
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


def _segment_claim_predicates(claim: ComposerOperationClaim, owner_instance_id: str) -> tuple[ColumnElement[bool], ...]:
    """The exact pre-start claim: its segment row, generation, token and owner."""
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
    """Mark Stop on a nonterminal parent; never settle it here.

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


def _request_cancel_on_connection(conn: Connection, row: Row[Any]) -> ComposerOperationRecord:
    """Mutate only under the caller's session lock, after its authorization."""
    if row.status in ("completed", "failed"):
        return _record_from_row(row)
    return _request_continuation_cancel_on_connection(conn, row, now=database_now(conn))


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
        "failure_code": outcome.failure_code if isinstance(outcome, ComposerOperationError) else None,
        "settled_by": settled_by,
        "result_schema": COMPOSER_OPERATION_RESULT_SCHEMA_ERROR if failed else COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS,
        "result_json": encoded,
        "result_sha256": composer_operation_result_hash(encoded),
        "settled_at": now,
        "updated_at": now,
    }


def _select_failure_on_connection(row: Row[Any], failure: ComposerOperationError, *, authoritative_failure: bool) -> ComposerOperationError:
    if authoritative_failure:
        return failure
    if row.cancel_requested_at is not None:
        body: dict[str, JsonValue] = {"error_type": "request_cancelled", "detail": "The composer request was stopped."}
        body["request_id"] = row.request_id
        return ComposerOperationError(
            http_status=499, failure_code="request_cancelled", error_type="request_cancelled", body=body, diagnostic_id=None
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
        continuation_policy: ContinuationAdmissionPolicy
        | ContinuationAdmissionPolicyV2
        | ContinuationAdmissionPolicyV3
        | ContinuationAdmissionPolicyV4
        | None = None,
    ) -> None:
        """``continuation_policy`` is required to admit; read-only owners may omit it."""
        if engine.dialect.name not in ("sqlite", "postgresql") or not owner_instance_id.strip() or not 5 <= claim_lease_seconds <= 600:
            raise ValueError("invalid composer operation authority configuration")
        if continuation_policy is not None and type(continuation_policy) not in (
            ContinuationAdmissionPolicy,
            ContinuationAdmissionPolicyV2,
            ContinuationAdmissionPolicyV3,
            ContinuationAdmissionPolicyV4,
        ):
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
        max_nonterminal: int,
        auth_provider_type: str,
        seed: ContinuationAdmissionSeed | None = None,
    ) -> tuple[ComposerOperationRecord, bool]:
        _identity(session_id, operation_id)
        _bounded(max_nonterminal)
        if len(request_json.encode("utf-8")) > COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH:
            raise ValueError("invalid composer request encoded size")
        policy = self._continuation_policy
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
            or composer_operation_admission_request_hash(session_id=session_id, kind=kind, request=request) != request_hash
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
                _binding(old, kind=kind, request_hash=request_hash, actor_user_id=actor_user_id, base_state_id=base_state_id)
                return _record_from_row(old), False
            # Authenticated exact replay needs no new policy. Every fresh
            # admission must bind its seed to this owner's validated policy.
            if policy is None:
                raise ValueError("composer admission has no validated server policy")
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
                seed = prepare_composer_continuation_admission(
                    session_id=session_id,
                    operation_id=operation_id,
                    kind=kind,
                    request_json=request_json,
                    request_hash=request_hash,
                    base_state_id=base_state_id,
                    policy_hash=policy.policy_hash,
                    admission_policy_json=policy.model_dump_json(),
                    initial_trust_mode=cast(Literal["auto_commit", "explicit_approve"], initial_trust_mode),
                    initial_action_json=policy.actions.session_sql.model_dump_json(),
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
            if retained_policy != policy:
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
                            continuation_policy_version=retained_policy.schema_version,
                            continuation_policy_sha256=(
                                retained_policy.policy_hash
                                if isinstance(retained_policy, (ContinuationAdmissionPolicyV3, ContinuationAdmissionPolicyV4))
                                else None
                            ),
                            continuation_retained_payload_limit_bytes=(
                                retained_policy.max_retained_payload_bytes
                                if isinstance(retained_policy, (ContinuationAdmissionPolicyV3, ContinuationAdmissionPolicyV4))
                                else None
                            ),
                            actor_user_id=actor_user_id,
                            request_id=request_id,
                            base_state_id=str(base_state_id) if base_state_id else None,
                            request_json=request_json,
                            phase="queued",
                            status_revision=0,
                            current_segment_sequence=1,
                            ownership_generation=1,
                            checkpoint_id=initial_checkpoint_id,
                            journal_cursor=0,
                            ingress_phase="pending" if kind == "compose_message" else "committed",
                            reserved_user_message_id=reserved_message_id,
                            user_message_id=reserved_message_id if kind == "compose_recompose" else None,
                            attempt=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
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

        A successor's parent is running; start rechecks this read under its lock.
        """
        if type(claim) is not ComposerOperationClaim:
            raise TypeError("segment claim observation requires its exact claim")
        with self._engine.connect() as conn:
            now = database_now(conn)
            row = conn.execute(
                select(jobs)
                .join(segments, and_(segments.c.session_id == jobs.c.session_id, segments.c.operation_id == jobs.c.operation_id))
                .where(
                    *_segment_claim_predicates(claim, self._owner_instance_id),
                    segments.c.claim_expires_at > now,
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
        """Use the existing current-fence terminal body."""
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation terminal requires its actual running owner")
        with locked_session_transaction(self._engine, str(running.claim.session_id), deadline=deadline) as conn:
            from elspeth.web.composer.continuation_next_action import (
                unpaid_early_outer_on_connection,
                validate_continuation_structure_on_connection,
            )
            from elspeth.web.composer.continuation_selection import SelectedContinuationState
            from elspeth.web.sessions.continuation_p4 import read_terminal_p4_source_on_connection, terminal_service_failure
            from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application
            from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
            from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            terminal_source = read_terminal_p4_source_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, env_ref_names=env_ref_names
            )
            if terminal_source is not None:
                if not isinstance(terminal_source, StoredTerminalP4Application):
                    raise ComposerTerminalSQLCompletionUnknown("terminal P4 preparation cannot become a claimed completion")
                if type(outcome) is not ComposerOperationError:
                    raise AuditIntegrityError("terminal service completion cannot publish operation success")
                parent = _read(conn, running.claim.session_id, running.claim.operation_id)
                if parent is None:
                    raise AuditIntegrityError("terminal P4 disposition lost its actual logical request")
                terminal_failure = terminal_service_failure(terminal_source, request_id=parent.request_id)
                authoritative_failure = terminal_failure.error_type == "audit_integrity_error"
                if authoritative_failure:
                    outcome = terminal_failure
                elif parent.cancel_requested_at is not None:
                    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

                    outcome = request_cancelled_error(request_id=parent.request_id)
                return settle_composer_operation_on_connection(conn, running, outcome=outcome, authoritative_failure=authoritative_failure)

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
                if parent.cancel_requested_at is not None:
                    outcome = request_cancelled_error(request_id=parent.request_id)
                elif (
                    unpaid_early_outer_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        sequence=running.claim.segment_sequence,
                        generation=running.claim.ownership_generation,
                        selected=selected,
                        env_ref_names=env_ref_names,
                    )
                    is None
                ):
                    outcome = worker_lost_error(request_id=parent.request_id)
                # Exact unpaid EARLY keeps the actual original failure. Its
                # outer effect stays ambiguous and ordinary settlement records
                # unknown; this never invents a paid or no-SDK result.
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
        no_sdk_issuer: PrimaryNoSdkIssuer | None = None,
    ) -> PreparedContinuationProvider:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        if no_sdk_issuer is not None:
            from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer

            if type(no_sdk_issuer) is not PrimaryNoSdkIssuer or no_sdk_issuer.running is not running or family != 0:
                raise AuditIntegrityError("PRIMARY preparation changed its original no-SDK issuer")
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
            prepared = PreparedContinuationProvider(binding, snapshot)
            if no_sdk_issuer is not None:
                no_sdk_issuer.note_prepared_candidate(prepared)
            return prepared

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
        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
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
        retention_memo: RetentionStageMemo | None = None,
    ) -> ContinuationRuntimeSource:
        from elspeth.web.composer.continuation import AdvisorFrame
        from elspeth.web.composer.continuation_selection import SelectedContinuationState
        from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection

        if type(running) is not ComposerOperationRunning:
            raise AuditIntegrityError("runtime read requires its actual running owner")
        if retention_memo is not None and (
            type(retention_memo) is not RetentionStageMemo
            or not retention_memo.active
            or current_retention_stage_memo() is not retention_memo
        ):
            raise AuditIntegrityError("runtime read requires its exact active retention memo")
        memo_scope = borrow_v4_preparation_memo_scope() if retention_memo is None else nullcontext(retention_memo)
        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, memo_scope:
            if type(selected) is SelectedContinuationState and selected.stack.frames and type(selected.stack.frames[-1]) is AdvisorFrame:
                from elspeth.web.sessions.continuation_semantic_advisor import read_semantic_advisor_runtime_source_on_connection

                # Retain the actual full selected root. The semantic owner
                # reproves its original outcome/decision and refuses unresolved
                # prepared/begun work before the ordinary runtime DATA read.
                return read_semantic_advisor_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)
            return read_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_tool_failure(
        self, running: ComposerOperationRunning, *, env_ref_names: frozenset[str]
    ) -> RetainedToolFailureSource | StoredTerminalP4Preparation | StoredTerminalP4Application | None:
        from elspeth.web.sessions.continuation_tool_failure import read_tool_failure_source_on_connection

        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
            return read_tool_failure_source_on_connection(conn, running, env_ref_names=env_ref_names)

    def read_continuation_owed_batch_runtime(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> ContinuationRuntimeSource:
        from elspeth.web.sessions.continuation_runtime import read_owed_batch_runtime_source_on_connection

        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
            return read_owed_batch_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_owed_tool_runtime(
        self, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
    ) -> ContinuationRuntimeSource:
        from elspeth.web.sessions.continuation_runtime import read_owed_tool_runtime_source_on_connection

        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
            return read_owed_tool_runtime_source_on_connection(conn, running, selected=selected, inventory=inventory)

    def retain_continuation_control_intent(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
    ) -> StoredContinuationControlIntent:
        from elspeth.web.composer.continuation_control_transitions import retain_control_intent

        return retain_control_intent(self, running, selected=selected, inventory=inventory)

    def read_continuation_control_intent(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
    ) -> StoredContinuationControlIntent | None:
        from elspeth.web.composer.continuation_control_transitions import read_control_intent_on_connection

        if type(running) is not ComposerOperationRunning:
            raise TypeError("control read requires its actual running owner")
        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
            return read_control_intent_on_connection(conn, running, selected=selected, inventory=inventory)

    def read_continuation_phase_primary(
        self,
        running: ComposerOperationRunning,
        *,
        selected: SelectedContinuationState,
        reference: PrivateChunkRef,
        env_ref_names: frozenset[str],
    ) -> RetainedPrimaryPhaseData:
        from elspeth.web.sessions.continuation_primary import read_retained_primary_phase_on_connection

        with session_data_read_transaction(self._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
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

    def begin_continuation_primary(
        self, prepared: PreparedContinuationProvider, *, no_sdk_issuer: PrimaryNoSdkIssuer | None = None
    ) -> StoredContinuationSnapshot:
        from elspeth.web.sessions.continuation_primary import begin_primary_provider_on_connection

        if type(prepared) is not PreparedContinuationProvider:
            raise AuditIntegrityError("primary dispatch requires its actual prepared owner result")
        with locked_session_transaction(self._engine, str(prepared.binding.running.claim.session_id)) as conn:
            dispatched = begin_primary_provider_on_connection(conn, prepared, no_sdk_issuer=no_sdk_issuer)
            if no_sdk_issuer is not None:
                no_sdk_issuer.note_dispatched_candidate(dispatched)
            return dispatched

    def read_continuation_primary_prepare_outcome(self, issuer: PrimaryNoSdkIssuer) -> PreparedContinuationProvider | None:
        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer
        from elspeth.web.sessions.continuation_primary import (
            selected_primary_effect_data_on_connection,
            stored_primary_input_data_on_connection,
        )

        if type(issuer) is not PrimaryNoSdkIssuer:
            raise AuditIntegrityError("PRIMARY preparation readback requires its actual issuer")
        running = issuer.running
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            candidate = issuer.prepared_candidate
            if candidate is not None and current == candidate.snapshot:
                _selected, _root, frame, effect = selected_primary_effect_data_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    checkpoint_id=current.checkpoint_id,
                    cursor=current.cursor,
                    effect_key=candidate.binding.effect_key,
                    input_sha256=candidate.binding.input_sha256,
                )
                if effect.disposition != "prepared":
                    raise AuditIntegrityError("PRIMARY preparation readback found an advanced effect")
                stored_primary_input_data_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
                )
            elif (current.checkpoint_id, current.cursor) == (issuer.original_checkpoint_id, issuer.original_cursor):
                return None
            else:
                raise AuditIntegrityError("PRIMARY preparation commit outcome is not its original selected source")
        if candidate is None:
            raise AuditIntegrityError("PRIMARY committed preparation lost its actual candidate")
        issuer.bind_prepared(candidate)
        return candidate

    def read_continuation_primary_begin_outcome(self, issuer: PrimaryNoSdkIssuer) -> StoredContinuationSnapshot | None:
        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer
        from elspeth.web.sessions.continuation_primary import selected_primary_effect_data_on_connection

        if type(issuer) is not PrimaryNoSdkIssuer or issuer.prepared_candidate is None:
            raise AuditIntegrityError("PRIMARY begin readback requires its exact prepared issuer")
        running = issuer.running
        prepared = issuer.prepared_candidate
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            candidate = issuer.dispatched_candidate
            if candidate is not None and current == candidate:
                _selected, _root, _frame, effect = selected_primary_effect_data_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    checkpoint_id=current.checkpoint_id,
                    cursor=current.cursor,
                    effect_key=prepared.binding.effect_key,
                    input_sha256=prepared.binding.input_sha256,
                )
                if effect.disposition != "dispatched":
                    raise AuditIntegrityError("PRIMARY begin readback found another disposition")
            elif current == prepared.snapshot:
                return None
            else:
                raise AuditIntegrityError("PRIMARY begin commit outcome is not its original selected source")
        if candidate is None:
            raise AuditIntegrityError("PRIMARY committed begin lost its actual candidate")
        issuer.bind_dispatched(candidate)
        return candidate

    def refuse_continuation_primary(self, permit: PrimaryNoSdkRefusalPermit) -> PrimaryNoSdkRefusalReceipt:
        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit
        from elspeth.web.sessions.continuation_primary import (
            read_primary_no_sdk_refusal_on_connection,
            refuse_primary_no_sdk_on_connection,
        )

        if type(permit) is not PrimaryNoSdkRefusalPermit:
            raise AuditIntegrityError("PRIMARY refusal requires its actual live no-SDK permit")
        permit.issuer.require_refusal_permit(permit)
        running = permit.issuer.running
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            prior = read_primary_no_sdk_refusal_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=permit.prepared.binding.effect_key,
            )
            if prior is not None:
                if prior.receipt_key != permit.receipt_key:
                    raise AuditIntegrityError("PRIMARY no-SDK refusal readback selected another live receipt")
                result = prior
            else:
                result = refuse_primary_no_sdk_on_connection(conn, permit)
        permit.issuer.mark_refusal_committed(permit)
        return result

    def read_continuation_primary_no_sdk_refusal(self, permit: PrimaryNoSdkRefusalPermit) -> PrimaryNoSdkRefusalReceipt | None:
        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit
        from elspeth.web.sessions.continuation_primary import (
            read_primary_no_sdk_refusal_on_connection,
            selected_primary_effect_data_on_connection,
        )

        if type(permit) is not PrimaryNoSdkRefusalPermit:
            raise AuditIntegrityError("PRIMARY readback requires its actual live no-SDK permit")
        permit.issuer.require_refusal_permit(permit)
        running = permit.issuer.running
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            receipt = read_primary_no_sdk_refusal_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=permit.prepared.binding.effect_key,
            )
            if receipt is not None:
                if receipt.receipt_key != permit.receipt_key:
                    raise AuditIntegrityError("PRIMARY no-SDK readback selected another refusal")
            else:
                source = permit.dispatched if permit.dispatched is not None else permit.prepared.snapshot
                selected_primary_effect_data_on_connection(
                    conn,
                    session_id=running.claim.session_id,
                    operation_id=running.claim.operation_id,
                    checkpoint_id=source.checkpoint_id,
                    cursor=source.cursor,
                    effect_key=permit.prepared.binding.effect_key,
                    input_sha256=permit.prepared.binding.input_sha256,
                )
        if receipt is not None:
            permit.issuer.mark_refusal_committed(permit)
        return receipt

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

    def prepare_terminal_continuation_p4(
        self,
        running: ComposerOperationRunning,
        *,
        completion: TerminalServiceCompletion,
        prepared: PreparedP4Value,
        env_ref_names: frozenset[str],
    ) -> ContinuationOwnerCommit:
        from elspeth.web.sessions.continuation_p4 import prepare_terminal_p4_on_connection

        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return prepare_terminal_p4_on_connection(conn, running, completion=completion, prepared=prepared, env_ref_names=env_ref_names)

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

    def claim_segment(
        self, *, limit: int, excluded_operations: frozenset[tuple[UUID, str]] = frozenset()
    ) -> tuple[ComposerOperationClaim, ...]:
        _bounded(limit)
        if type(excluded_operations) is not frozenset:
            raise TypeError("local claim exclusions must be an immutable operation-key snapshot")
        for key in excluded_operations:
            if type(key) is not tuple or len(key) != 2:
                raise TypeError("local claim exclusion requires a complete operation key")
            _identity(*key)
        blocked = (
            or_(
                *(
                    and_(segments.c.session_id == str(session_id), segments.c.operation_id == operation_id)
                    for session_id, operation_id in sorted(excluded_operations)
                )
            )
            if excluded_operations
            else false()
        )
        with self._engine.connect() as conn:
            now = database_now(conn)
            candidates = conn.execute(
                select(
                    segments.c.session_id,
                    segments.c.operation_id,
                    segments.c.sequence,
                )
                # A locally finishing predecessor must not consume LIMIT and
                # starve unrelated work. This is only this caller's scheduling
                # hint; peer/global SQL eligibility and claim CAS stay intact.
                .where(_continuation_claim_predicate(conn, now), ~blocked)
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
                from elspeth.web.sessions.continuation import require_v3_unpaid_terminal_reserve_on_connection

                # Validate the V3 terminal reserve under the final claimed
                # parent. Refusal must roll back its token and attempt too.
                require_v3_unpaid_terminal_reserve_on_connection(conn, session_id=str(hint.session_id), operation_id=hint.operation_id)
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

    def renew_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord:
        with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
            now = database_now(conn)
            # A claim lives on its segment row; the parent never holds one.
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

    def release_claim(self, claim: ComposerOperationClaim) -> None:
        with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
            released = conn.execute(
                update(segments)
                .where(*_segment_claim_predicates(claim, self._owner_instance_id))
                .values(claim_token=None, claim_owner_instance_id=None, claim_expires_at=None)
            )
            if released.rowcount != 1:
                raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)

    def request_cancel(self, *, session_id: UUID, operation_id: str) -> ComposerOperationRecord | None:
        """Trusted internal cancellation; HTTP callers must use request_owner_cancel."""
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            row = _read(conn, session_id, operation_id)
            if row is None:
                return None
            return _request_cancel_on_connection(conn, row)

    def request_owner_cancel(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        actor_user_id: str,
        auth_provider_type: str,
    ) -> ComposerOperationRecord | None:
        """Authenticate the Stop actor again inside the mutation's session lock."""
        with locked_session_transaction(self._engine, str(session_id)) as conn:
            _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
            row = _read(conn, session_id, operation_id)
            if row is None or row.actor_user_id != actor_user_id:
                return None
            return _request_cancel_on_connection(conn, row)

    def list_recoverable_segments(self, *, limit: int) -> tuple[ContinuationRecoveryCandidate, ...]:
        from elspeth.web.sessions.continuation_recovery_candidates import list_recoverable_segments

        return list_recoverable_segments(self._engine, limit=limit)

    def list_inactive_continuations(self, *, limit: int) -> tuple[ContinuationInactiveCandidate, ...]:
        from elspeth.web.sessions.continuation_recovery_candidates import list_inactive_continuations

        return list_inactive_continuations(self._engine, limit=limit)

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
    max_nonterminal: int,
    auth_provider_type: str,
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
        max_nonterminal=max_nonterminal,
        auth_provider_type=auth_provider_type,
    )


def _running_predicates(running: ComposerOperationRunning) -> tuple[Any, ...]:
    claim, fence = running.claim, running.session_operation_context.fence
    if running.session_operation_context.operation_kind is not SessionOperationKind.COMPOSE or fence.session_id != str(claim.session_id):
        raise AuditIntegrityError("composer running context is foreign")
    return (
        *_key(claim.session_id, claim.operation_id),
        jobs.c.status == "running",
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
    bound = conn.execute(
        select(jobs.c.operation_id, jobs.c.attempt)
        .where(jobs.c.session_id == f.session_id, jobs.c.session_operation_epoch == f.operation_epoch)
        .limit(1)
    ).one_or_none()
    if bound is not None:
        # A durable job carries this epoch but no segment attempt grants it.
        raise ComposerOperationFenceLost(session_id=UUID(f.session_id), operation_id=bound.operation_id, attempt=bound.attempt)


def _proposal_claim_token(conn: Connection, row: Row[Any], context: SessionOperationContext) -> str | None:
    """The live claim token that owns a proposal-writing composer operation.

    The parent never holds a claim: its owner is the current segment's
    claim, bound to this exact fence by an unrevoked attempt at the parent's
    attempt. A revoked or superseded segment owner has no claim here.
    """
    fence = context.fence
    segment = conn.execute(
        select(segments.c.claim_token, segments.c.state).where(
            segments.c.session_id == fence.session_id,
            segments.c.operation_id == row.operation_id,
            segments.c.sequence == row.current_segment_sequence,
            segments.c.generation == row.ownership_generation,
        )
    ).one_or_none()
    attempt = conn.execute(
        select(segment_attempts.c.claim_attempt).where(
            segment_attempts.c.session_id == fence.session_id,
            segment_attempts.c.operation_id == row.operation_id,
            segment_attempts.c.sequence == row.current_segment_sequence,
            segment_attempts.c.generation == row.ownership_generation,
            segment_attempts.c.session_operation_id == fence.operation_id,
            segment_attempts.c.session_operation_epoch == fence.operation_epoch,
            segment_attempts.c.revoked_at.is_(None),
        )
    ).one_or_none()
    if segment is None or segment.state != "running" or attempt is None or attempt.claim_attempt != row.attempt:
        return None
    return _owned_claim_token(segment.claim_token)


def _owned_claim_token(value: object) -> str | None:
    if value is not None and type(value) is not str:
        raise AuditIntegrityError("composer claim token is not owned text")
    return value


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
        or (claim_token := _proposal_claim_token(conn, row, context)) is None
        or row.attempt < 1
    ):
        raise AuditIntegrityError("proposal composer job/fence authority mismatch")
    if running is not None and (
        running.session_operation_context != context
        or running.claim.session_id != UUID(fence.session_id)
        or running.claim.operation_id != row.operation_id
        or running.claim.claim_token != claim_token
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


@dataclass(frozen=True, slots=True)
class _GraphRecoveryPriorRead:
    transaction: RootTransaction
    running: ComposerOperationRunning
    running_snapshot: ComposerOperationRunning
    state: CompositionStateRecord
    state_snapshot: CompositionStateRecord


_GRAPH_RECOVERY_PRIOR_READ = "elspeth.graph_recovery_prior_read"


@contextmanager
def graph_recovery_prior_read_scope(conn: Connection) -> Iterator[None]:
    """Keep a prior-row observation within one terminal attempt only."""
    conn.info.pop(_GRAPH_RECOVERY_PRIOR_READ, None)
    try:
        yield
    finally:
        conn.info.pop(_GRAPH_RECOVERY_PRIOR_READ, None)


def _require_graph_recovery_prior_read(
    conn: Connection,
    running: ComposerOperationRunning,
    prior: CompositionStateRecord | None,
) -> None:
    """Consume only this terminal transaction's actual SQL observation."""
    prior_read = conn.info.pop(_GRAPH_RECOVERY_PRIOR_READ, None)
    if (
        type(prior_read) is not _GraphRecoveryPriorRead
        or prior_read.transaction is not conn.get_transaction()
        or prior_read.running is not running
        or prior_read.running_snapshot != running
        or prior_read.state is not prior
        or prior_read.state_snapshot != prior
    ):
        raise AuditIntegrityError("graph recovery park lost its actual pre-DML transaction read")


def read_graph_recovery_prior_state_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    service: SessionServiceImpl,
    recovery: GraphRecoverySettlement,
) -> CompositionStateRecord | None:
    """Read retained park state on the terminal connection before any writes."""
    from elspeth.web.sessions.content_publication import read_content_publication
    from elspeth.web.sessions.graph_recovery import GraphRecoverySettlement
    from elspeth.web.sessions.service import SessionServiceImpl

    conn.info.pop(_GRAPH_RECOVERY_PRIOR_READ, None)
    if type(service) is not SessionServiceImpl or type(recovery) is not GraphRecoverySettlement:
        raise AuditIntegrityError("graph recovery prior state requires its actual terminal source")
    if recovery.intent.action != "park" or recovery.state_id is None or recovery.state_id != recovery.expected_state_id:
        return None
    if recovery.session_id != running.claim.session_id or recovery.operation_id != running.session_operation_context.fence.operation_id:
        raise AuditIntegrityError("graph recovery prior state changed its original operation")
    if recovery.receipt_id is None or recovery.park_id is None:
        raise AuditIntegrityError("graph recovery prior state lost its planned receipt or bookmark")
    if (
        conn.execute(
            select(chat_messages_table.c.id).where(
                chat_messages_table.c.id.in_((str(recovery.receipt_id), str(uuid5(recovery.receipt_id, "audit"))))
            )
        ).first()
        is not None
        or conn.execute(
            select(composition_graph_parks_table.c.id).where(composition_graph_parks_table.c.id == str(recovery.park_id))
        ).first()
        is not None
    ):
        raise AuditIntegrityError("graph recovery prior state was read after its terminal writes")
    row = conn.execute(
        select(composition_states_table)
        .where(composition_states_table.c.session_id == str(running.claim.session_id))
        .order_by(composition_states_table.c.version.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("graph recovery park lost its original state before settlement")
    prior = service._row_to_state_record(row)
    if prior.id != recovery.expected_state_id or prior.version != recovery.expected_version:
        raise AuditIntegrityError("graph recovery park changed its original head before settlement")
    read_content_publication(prior)
    transaction = conn.get_transaction()
    if transaction is None:
        raise AuditIntegrityError("graph recovery prior state lost its actual transaction")
    running_snapshot = replace(
        running,
        claim=replace(running.claim),
        session_operation_context=replace(running.session_operation_context, fence=replace(running.session_operation_context.fence)),
    )
    conn.info[_GRAPH_RECOVERY_PRIOR_READ] = _GraphRecoveryPriorRead(
        transaction, running, running_snapshot, prior, state_record_from_row(row)
    )
    return prior


def settle_composer_operation_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    outcome: MessageWithStateResponse | ComposerOperationError,
    authoritative_failure: bool = False,
    prepared_settlement: PreparedSegmentSettlement | None = None,
    terminal_recorded_at: datetime | None = None,
    publication_result: ComposerResult | None = None,
    publication_projection: PublicationProjectionInput | GraphRecoveryProjectionInput | None = None,
    publication_assistant: ChatMessageRecord | None = None,
    publication_service: SessionServiceImpl | None = None,
    publication_actor: str | None = None,
    recovery_prior_state: CompositionStateRecord | None = None,
) -> ComposerOperationRecord:
    """Sealed terminal CAS. Failed results cannot authorize business mutations."""
    from elspeth.web.sessions.continuation import settle_continuation_on_connection

    current = _read(conn, running.claim.session_id, running.claim.operation_id)
    if current is not None and current.status in ("completed", "failed"):
        return _record_from_row(current)
    if current is not None and isinstance(outcome, ComposerOperationError):
        outcome = _select_failure_on_connection(current, outcome, authoritative_failure=authoritative_failure)
    if isinstance(outcome, MessageWithStateResponse):
        from elspeth.web.composer.protocol import ComposerResult
        from elspeth.web.sessions.continuation_graph_recovery_projection import (
            GraphRecoveryProjectionInput,
            require_graph_recovery_postconditions_on_connection,
        )
        from elspeth.web.sessions.continuation_publication_projection import (
            PublicationProjectionInput,
            require_publication_projection_on_connection,
        )
        from elspeth.web.sessions.continuation_semantic_actions import require_semantic_publication_on_connection
        from elspeth.web.sessions.continuation_semantic_advisor import (
            require_semantic_iteration_publication_on_connection,
            require_semantic_ordinary_recovery_publication_on_connection,
            require_semantic_owned_iteration_publication_on_connection,
        )
        from elspeth.web.sessions.protocol import ChatMessageRecord
        from elspeth.web.sessions.routes._helpers import _message_response, composer_turn_end_assistant_row
        from elspeth.web.sessions.service import SessionServiceImpl

        selected = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        owned_proof = require_semantic_owned_iteration_publication_on_connection(conn, running, selected=selected)
        iteration_proof = (
            require_semantic_iteration_publication_on_connection(conn, running, selected=selected) if owned_proof is None else None
        )
        recovery_proof = (
            require_semantic_ordinary_recovery_publication_on_connection(conn, running, selected=selected)
            if owned_proof is None and iteration_proof is None
            else None
        )
        review_proof = (
            require_semantic_publication_on_connection(conn, running, selected=selected)
            if owned_proof is None and iteration_proof is None and recovery_proof is None
            else None
        )
        if (
            publication_projection is not None
            and owned_proof is None
            and iteration_proof is None
            and recovery_proof is None
            and review_proof is None
        ):
            raise AuditIntegrityError("publication projection lacks the original paid whole-request Stop")
        if owned_proof is not None or iteration_proof is not None or recovery_proof is not None or review_proof is not None:
            if type(publication_result) is not ComposerResult:
                raise AuditIntegrityError("semantic publication lost its actual shared finalizer result")
            if owned_proof is not None:
                original = owned_proof.returned.result
                audit_call = owned_proof.admission.paid.audit_call
            elif iteration_proof is not None:
                original = iteration_proof.returned.result
                audit_call = iteration_proof.admission.paid.audit_call
            elif recovery_proof is not None:
                original = recovery_proof.result
                audit_call = recovery_proof.admission.paid.audit_call
            else:
                if review_proof is None:
                    raise AuditIntegrityError("semantic publication lost both original paid Stop sources")
                original = review_proof.review_result.result
                audit_call = review_proof.admission.paid.audit_call
            expected = replace(original, llm_calls=(*original.llm_calls, audit_call))
            if expected.graph_recovery_intent is not None and type(publication_projection) is not GraphRecoveryProjectionInput:
                raise AuditIntegrityError("semantic graph recovery requires its original SQL projection")
            if publication_projection is None:
                if publication_result != expected:
                    raise AuditIntegrityError("semantic publication replaced its original reviewed result or paid Stop audit")
                if outcome.message.content != composer_turn_end_assistant_row(expected).content:
                    raise AuditIntegrityError("semantic publication changed its committed assistant result")
            else:
                if (
                    type(publication_projection) not in (PublicationProjectionInput, GraphRecoveryProjectionInput)
                    or type(publication_assistant) is not ChatMessageRecord
                    or type(publication_service) is not SessionServiceImpl
                    or type(publication_actor) is not str
                    or not publication_actor
                    or publication_projection.original_result != expected
                ):
                    raise AuditIntegrityError("publication projection lost its actual SQL, assistant or actor owner")
                require_composer_settlement_actor_on_connection(conn, running, actor=publication_actor)
                assistant_row = conn.execute(
                    select(chat_messages_table).where(
                        chat_messages_table.c.id == str(publication_assistant.id),
                        chat_messages_table.c.session_id == str(running.claim.session_id),
                    )
                ).one_or_none()
                if assistant_row is None or publication_service._row_to_chat_message_record(assistant_row) != publication_assistant:
                    raise AuditIntegrityError("publication projection substituted its actual assistant or public response")
                if type(publication_projection) is GraphRecoveryProjectionInput:
                    classifier_recovery = (
                        owned_proof is not None
                        and owned_proof.outcome_kind == "owned_recovery"
                        and owned_proof.source_kind == "graph_recovery"
                        and owned_proof.recovery_context == publication_projection.original_context
                    )
                    ordinary_recovery = (
                        recovery_proof is not None
                        and recovery_proof.recovery_context == publication_projection.original_context
                        and recovery_proof.result.graph_recovery_intent == publication_projection.recovery.intent
                    )
                    if not classifier_recovery and not ordinary_recovery:
                        raise AuditIntegrityError("graph recovery projection lacks its original owned recovery source")
                    recovery = publication_projection.recovery
                    retained_head = (
                        recovery.intent.action == "park"
                        and recovery.state_id is not None
                        and recovery.state_id == recovery.expected_state_id
                    )
                    if retained_head:
                        _require_graph_recovery_prior_read(conn, running, recovery_prior_state)
                    elif recovery_prior_state is not None:
                        raise AuditIntegrityError("graph recovery invented a retained prior state")
                    require_graph_recovery_postconditions_on_connection(
                        conn,
                        running,
                        publication_service,
                        original_result=expected,
                        publication_result=publication_result,
                        assistant=publication_assistant,
                        outcome=outcome,
                        projection=publication_projection,
                        recovery_prior_state=recovery_prior_state,
                    )
                elif type(publication_projection) is PublicationProjectionInput:
                    require_publication_projection_on_connection(
                        conn,
                        running,
                        publication_service,
                        original_result=expected,
                        publication_result=publication_result,
                        assistant=publication_assistant,
                        actor=publication_actor,
                        settlement=publication_projection.settlement,
                        revocation=publication_projection.revocation,
                    )
                    if outcome.message != _message_response(publication_assistant):
                        raise AuditIntegrityError("publication projection substituted its actual public response")
                else:
                    raise AuditIntegrityError("publication projection changed its nominal source kind")
        elif publication_assistant is not None or publication_service is not None or publication_actor is not None:
            raise AuditIntegrityError("publication proof inputs were supplied without an original whole-request Stop")
    settle_continuation_on_connection(
        conn,
        running,
        outcome=outcome,
        authoritative_failure=authoritative_failure,
        prepared_settlement=prepared_settlement,
        terminal_recorded_at=terminal_recorded_at,
    )
    settled = _read(conn, running.claim.session_id, running.claim.operation_id)
    if settled is None:
        raise AuditIntegrityError("continuation terminal missing")
    return _record_from_row(settled)
