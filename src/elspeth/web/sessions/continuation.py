"""Private transaction-local bytes and complete continuation snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, LargeBinary, Row, case, cast, func, insert, select, update
from sqlalchemy.engine import RootTransaction
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.continuation import (
    ContinuationFrameStack,
    InitializeFrame,
    PrivateChunkRef,
    prepare_composer_continuation_admission,
    reserved_user_message_id,
)
from elspeth.web.composer.continuation_clock import size_segment
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    ContinuationSegmentPolicy,
    decode_continuation_policy,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn,
    ComposerOperationClaim,
    ComposerOperationError,
    ComposerOperationFenceLost,
    ComposerOperationRunning,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation_events import CONTINUATION_EVENT_TAGS, ContinuationEventTag
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
)
from elspeth.web.sessions.models import (
    composer_continuation_checkpoints_table as checkpoints,
)
from elspeth.web.sessions.models import (
    composer_continuation_chunks_table as chunks,
)
from elspeth.web.sessions.models import (
    composer_continuation_effects_table as effects,
)
from elspeth.web.sessions.models import (
    composer_continuation_events_table as events,
)
from elspeth.web.sessions.models import (
    composer_continuation_provider_ordinals_table as ordinals,
)
from elspeth.web.sessions.models import (
    composer_operation_segments_table as segments,
)
from elspeth.web.sessions.models import (
    composer_segment_attempts_table as attempts,
)
from elspeth.web.sessions.models import (
    composition_states_table as states,
)
from elspeth.web.sessions.models import (
    session_operation_fences_table as fences,
)
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.state_decoder import state_record_from_row
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_handoff import ContinuationYieldReason

_OWED_P4_SCOPE_KEY = "elspeth.continuation.owed-p4"
_OWED_P4_SEAL = object()


@dataclass(frozen=True, slots=True)
class _OwedP4Scope:
    connection: Connection
    context: SessionOperationContext
    seal: object
    transaction: RootTransaction = field(init=False, repr=False)

    def __post_init__(self) -> None:
        transaction = self.connection.get_transaction()
        if transaction is None or not transaction.is_active:
            raise AuditIntegrityError("owed P4 scope requires an active SQL owner transaction")
        object.__setattr__(self, "transaction", transaction)


def _owed_continuation_p4_active(conn: Connection, context: SessionOperationContext) -> bool:
    selected = conn.info.get(_OWED_P4_SCOPE_KEY)
    if selected is None:
        return False
    if (
        type(selected) is not _OwedP4Scope
        or selected.seal is not _OWED_P4_SEAL
        or selected.connection is not conn
        or selected.context != context
        or not conn.in_transaction()
        or conn.get_transaction() is not selected.transaction
        or not selected.transaction.is_active
    ):
        raise AuditIntegrityError("owed P4 scope escaped its exact current SQL owner transaction")
    return True


def require_continuation_mutation(
    conn: Connection,
    context: SessionOperationContext,
    *,
    allow_cancel: bool = False,
    allow_expired_segment: bool = False,
) -> bool:
    if context.operation_kind is not SessionOperationKind.COMPOSE:
        return False
    fence = context.fence
    owed_p4 = _owed_continuation_p4_active(conn, context)
    allow_cancel = allow_cancel or owed_p4
    allow_expired_segment = allow_expired_segment or owed_p4
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == fence.session_id,
            attempts.c.session_operation_epoch == fence.operation_epoch,
        )
    ).one_or_none()
    if history is None:
        return False
    parent = conn.execute(
        select(jobs).where(jobs.c.session_id == history.session_id, jobs.c.operation_id == history.operation_id)
    ).one_or_none()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == history.session_id,
            segments.c.operation_id == history.operation_id,
            segments.c.sequence == history.sequence,
        )
    ).one_or_none()
    now = database_now(conn)
    live_fence = conn.execute(
        select(fences.c.session_id).where(
            fences.c.session_id == fence.session_id,
            fences.c.operation_id == fence.operation_id,
            fences.c.operation_epoch == fence.operation_epoch,
            fences.c.lease_token == fence.lease_token,
            fences.c.operation_kind == SessionOperationKind.COMPOSE.value,
            fences.c.released_at.is_(None),
            fences.c.lease_expires_at > now,
        )
    ).one_or_none()
    principal = conn.execute(
        select(sessions_table.c.user_id, sessions_table.c.archived_at).where(sessions_table.c.id == fence.session_id)
    ).one_or_none()
    live = (
        parent is not None
        and segment is not None
        and live_fence is not None
        and parent.lifecycle_version == 2
        and parent.status == "running"
        and principal is not None
        and principal.archived_at is None
        and principal.user_id == parent.actor_user_id
        and parent.current_segment_sequence == history.sequence
        and parent.ownership_generation == history.generation
        and parent.attempt == history.claim_attempt
        and segment.generation == history.generation
        and segment.state == "running"
        and history.revoked_at is None
        and history.session_operation_id == fence.operation_id
        and history.session_operation_lease_token == fence.lease_token
        and segment.session_operation_id == fence.operation_id
        and segment.session_operation_epoch == fence.operation_epoch
        and segment.session_operation_lease_token == fence.lease_token
        and segment.deadline_at is not None
        and (allow_expired_segment or restore_utc(segment.deadline_at) > now)
    )
    if parent is None or segment is None or not live:
        raise ComposerOperationFenceLost(session_id=UUID(history.session_id), operation_id=history.operation_id)
    if parent.cancel_requested_at is not None and not allow_cancel:
        raise ComposerOperationCancelledDuringTurn(
            ComposerOperationClaim(
                session_id=UUID(history.session_id),
                operation_id=history.operation_id,
                claim_token=segment.claim_token,
                attempt=parent.attempt,
                segment_sequence=history.sequence,
                ownership_generation=history.generation,
            )
        )
    return True


@dataclass(frozen=True, slots=True)
class StoredContinuationSnapshot:
    payload_json: str
    cursor: int
    checkpoint_id: str


def initialize_continuation(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    checkpoint_id: str,
    snapshot_json: str,
    action_json: str,
    policy_sha256: str,
) -> None:
    now = database_now(conn)
    snapshot_key = f"snapshot:{checkpoint_id}"
    put_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=snapshot_key, payload=snapshot_json.encode("utf-8"), codec="snapshot.v2"
    )
    conn.execute(
        insert(checkpoints).values(
            session_id=session_id,
            operation_id=operation_id,
            checkpoint_id=checkpoint_id,
            kind="transition",
            cursor=0,
            snapshot_chunk_key=snapshot_key,
            frame_stack_version=1,
            created_at=now,
        )
    )
    conn.execute(
        insert(segments).values(
            session_id=session_id,
            operation_id=operation_id,
            sequence=1,
            predecessor_sequence=None,
            state="queued",
            generation=1,
            enqueued_at=now,
            next_eligible_at=now,
            checkpoint_id=checkpoint_id,
            next_action_json=action_json,
            policy_sha256=policy_sha256,
        )
    )


@dataclass(frozen=True, slots=True)
class ContinuationEvent:
    tag: ContinuationEventTag
    payload_json: str

    def __post_init__(self) -> None:
        tag: object = self.tag
        if type(tag) is not str or tag not in CONTINUATION_EVENT_TAGS:
            raise AuditIntegrityError("continuation event has an unfamiliar owned tag")
        if type(self.payload_json) is not str:
            raise AuditIntegrityError("continuation event payload is not owned JSON text")


class ContinuationCASConflict(AuditIntegrityError):
    """The complete selected snapshot is no longer the expected head."""


def put_chunk(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    key: str,
    payload: bytes,
    codec: str,
) -> None:
    if type(payload) is not bytes or not key or not codec:
        raise ValueError("chunk requires exact bytes and owned codec/key")
    digest = sha256(payload).hexdigest()
    old = conn.execute(
        select(chunks).where(
            chunks.c.session_id == session_id,
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == key,
        )
    ).one_or_none()
    if old is not None:
        if old.payload != payload or old.sha256 != digest or old.byte_length != len(payload) or old.codec != codec:
            raise AuditIntegrityError("immutable chunk collision")
        return
    conn.execute(
        insert(chunks).values(
            session_id=session_id,
            operation_id=operation_id,
            chunk_key=key,
            payload=payload,
            sha256=digest,
            byte_length=len(payload),
            codec=codec,
        )
    )


def read_chunk(conn: Connection, *, session_id: str, operation_id: str, key: str) -> bytes:
    row = conn.execute(
        select(chunks).where(
            chunks.c.session_id == session_id,
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == key,
        )
    ).one_or_none()
    if (
        row is None
        or type(row.payload) is not bytes
        or len(row.payload) != row.byte_length
        or sha256(row.payload).hexdigest() != row.sha256
    ):
        raise AuditIntegrityError("continuation chunk digest or binding mismatch")
    return row.payload


def read_private_chunk(conn: Connection, *, session_id: str, operation_id: str, reference: PrivateChunkRef) -> bytes:
    """Return bounded private bytes through one conditional SQL projection.

    Actual database byte length and payload selection share this statement and
    connection. This does not bound database work, roots or aggregate closure,
    nor confer any authority beyond the caller's original SQL admission.
    """
    if type(reference) is not PrivateChunkRef or not 0 <= reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        raise AuditIntegrityError("private chunk exceeds its complete document bound")
    if conn.dialect.name == "postgresql":
        actual_length = func.octet_length(chunks.c.payload)
    elif conn.dialect.name == "sqlite":
        # length(TEXT) counts characters; inspect even a forged storage class
        # as its actual UTF8 bytes instead of trusting metadata or characters.
        actual_length = func.length(cast(chunks.c.payload, LargeBinary))
    else:
        raise AuditIntegrityError("private chunk byte projection has an unsupported database")
    row = conn.execute(
        select(
            chunks.c.codec,
            chunks.c.sha256,
            chunks.c.byte_length,
            actual_length.label("actual_byte_length"),
            case(
                (actual_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, chunks.c.payload),
                else_=None,
            ).label("payload"),
        ).where(
            chunks.c.session_id == session_id,
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
        )
    ).one_or_none()
    if (
        row is None
        or type(row.actual_byte_length) is not int
        or not 0 <= row.actual_byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or type(row.payload) is not bytes
        or len(row.payload) != row.actual_byte_length
        or row.byte_length != row.actual_byte_length
        or row.actual_byte_length != reference.byte_length
        or row.codec != reference.codec
        or row.sha256 != reference.digest
        or sha256(row.payload).hexdigest() != reference.digest
    ):
        raise AuditIntegrityError("private chunk complete byte bound or SQL binding mismatch")
    return row.payload


def load_snapshot(conn: Connection, *, session_id: str, operation_id: str) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    parent = conn.execute(
        select(jobs.c.checkpoint_id, jobs.c.journal_cursor).where(
            jobs.c.session_id == session_id,
            jobs.c.operation_id == operation_id,
            jobs.c.lifecycle_version == 2,
        )
    ).one_or_none()
    if parent is None:
        raise AuditIntegrityError("continuation parent missing")
    snapshot = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == parent.checkpoint_id,
        )
    ).one_or_none()
    last = conn.execute(
        select(func.coalesce(func.max(events.c.sequence), 0)).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
        )
    ).scalar_one()
    if snapshot is None or snapshot.cursor != parent.journal_cursor or last != snapshot.cursor:
        raise AuditIntegrityError("event/snapshot cursor integrity mismatch")
    payload = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=snapshot.snapshot_chunk_key)
    encoded = payload.decode("utf-8", errors="strict")
    stack = decode_continuation_snapshot(encoded)
    if (stack.logical.session_id, stack.logical.operation_id, stack.cursor) != (session_id, operation_id, snapshot.cursor):
        raise AuditIntegrityError("complete continuation snapshot differs from its SQL identity/cursor")
    return StoredContinuationSnapshot(encoded, snapshot.cursor, snapshot.checkpoint_id)


@dataclass(frozen=True, slots=True)
class PreparedContinuationAppend:
    session_id: str
    operation_id: str
    expected_checkpoint_id: str
    expected_cursor: int
    snapshot_json: str
    events: tuple[ContinuationEvent, ...]
    checkpoint_id: str
    published_at: datetime
    handoff: bool
    predecessor_json: str
    original_values: tuple[object, ...] = field(repr=False)

    def require_original(self) -> None:
        if self.original_values != (
            self.session_id,
            self.operation_id,
            self.expected_checkpoint_id,
            self.expected_cursor,
            self.snapshot_json,
            self.events,
            self.checkpoint_id,
            self.published_at,
            self.handoff,
            self.predecessor_json,
        ):
            raise AuditIntegrityError("prepared continuation append changed its original complete values")


@dataclass(frozen=True, slots=True)
class SettlementAttemptState:
    sequence: int
    generation: int
    claim_attempt: int
    operation_id: str | None
    operation_epoch: int | None
    lease_token: str | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class SettlementDrivingState:
    status: str
    attempt: int
    status_revision: int
    sequence: int
    generation: int
    cancel_requested_at: datetime | None
    checkpoint_id: str
    cursor: int
    segment_sequence: int
    segment_generation: int
    segment_state: str
    segment_started_at: datetime | None
    segment_cooperative_at: datetime | None
    segment_deadline_at: datetime | None
    claim_token: str | None
    claim_owner_instance_id: str | None
    claim_expires_at: datetime | None
    operation_id: str | None
    operation_epoch: int | None
    lease_token: str | None
    history: SettlementAttemptState | None


@dataclass(frozen=True, slots=True)
class PreparedSegmentSettlement:
    outcome: MessageWithStateResponse | ComposerOperationError
    published_at: datetime
    append: PreparedContinuationAppend
    driving: SettlementDrivingState
    settled_by: Literal["owner_terminal", "settle_lost", "settle_lost_inactive_session"]
    attempt_outcome: Literal["settled", "interrupted"]
    outcome_json: str


def _settlement_driving(parent: Row[Any], segment: Row[Any], history: Row[Any] | None) -> SettlementDrivingState:
    return SettlementDrivingState(
        parent.status,
        parent.attempt,
        parent.status_revision,
        parent.current_segment_sequence,
        parent.ownership_generation,
        parent.cancel_requested_at,
        parent.checkpoint_id,
        parent.journal_cursor,
        segment.sequence,
        segment.generation,
        segment.state,
        None if segment.started_at is None else restore_utc(segment.started_at),
        None if segment.cooperative_at is None else restore_utc(segment.cooperative_at),
        None if segment.deadline_at is None else restore_utc(segment.deadline_at),
        segment.claim_token,
        segment.claim_owner_instance_id,
        segment.claim_expires_at,
        segment.session_operation_id,
        segment.session_operation_epoch,
        segment.session_operation_lease_token,
        None
        if history is None
        else SettlementAttemptState(
            history.sequence,
            history.generation,
            history.claim_attempt,
            history.session_operation_id,
            history.session_operation_epoch,
            history.session_operation_lease_token,
            history.revoked_at,
        ),
    )


def _settlement_transition(
    stored: StoredContinuationSnapshot,
    *,
    segment: Row[Any],
    outcome: MessageWithStateResponse | ComposerOperationError,
    published_at: datetime,
    settled_by: Literal["owner_terminal", "settle_lost", "settle_lost_inactive_session"],
) -> tuple[ContinuationFrameStack, tuple[str, ...], tuple[ContinuationEvent, ...]]:
    from elspeth.web.coordination.composer_operation_authority import _terminal_values

    stack = decode_continuation_snapshot(stored.payload_json)
    unresolved = tuple(item.effect_id for item in stack.effect_frontier if item.disposition == "dispatched")
    frontier = tuple(
        item.model_copy(update={"disposition": "unknown"}) if item.effect_id in unresolved else item for item in stack.effect_frontier
    )
    terminal = _terminal_values(outcome, now=published_at, settled_by=settled_by)
    attribution = (
        *(ContinuationEvent("effect_unknown", canonical_json({"version": 1, "effect_key": key})) for key in unresolved),
        ContinuationEvent(
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": segment.sequence,
                    "generation": segment.generation,
                    "status": terminal["status"],
                    "failure_code": terminal["failure_code"],
                }
            ),
        ),
    )
    return stack.model_copy(update={"effect_frontier": frontier}), unresolved, attribution


def prepare_segment_settlement_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    parent: Row[Any],
    segment: Row[Any],
    history: Row[Any] | None,
    outcome: MessageWithStateResponse | ComposerOperationError,
    settled_by: Literal["owner_terminal", "settle_lost", "settle_lost_inactive_session"],
    attempt_outcome: Literal["settled", "interrupted"],
) -> PreparedSegmentSettlement:
    """Freeze publication DATA under the caller's real lock, before any write."""
    stored = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    published = database_now(conn)
    stack, _unresolved, attribution = _settlement_transition(
        stored, segment=segment, outcome=outcome, published_at=published, settled_by=settled_by
    )
    encoded = encode_continuation_snapshot(stack.model_copy(update={"cursor": stored.cursor + len(attribution)}))
    checkpoint_id = str(uuid4())
    original = (
        session_id,
        operation_id,
        stored.checkpoint_id,
        stored.cursor,
        encoded,
        attribution,
        checkpoint_id,
        published,
        False,
        stored.payload_json,
    )
    append = PreparedContinuationAppend(
        session_id,
        operation_id,
        stored.checkpoint_id,
        stored.cursor,
        encoded,
        attribution,
        checkpoint_id,
        published,
        False,
        stored.payload_json,
        original,
    )
    return PreparedSegmentSettlement(
        outcome, published, append, _settlement_driving(parent, segment, history), settled_by, attempt_outcome, outcome.model_dump_json()
    )


def append_complete_snapshot(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    snapshot_json: str,
    event_batch: tuple[ContinuationEvent, ...],
    handoff: bool,
    prepared_append: PreparedContinuationAppend | None = None,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot

    current = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if current.checkpoint_id != expected_checkpoint_id or current.cursor != expected_cursor:
        raise ContinuationCASConflict("continuation snapshot compare-and-swap failed")
    if not event_batch:
        raise ValueError("a durable transition requires its attribution event")
    previous_stack = decode_continuation_snapshot(current.payload_json)
    frame_stack = decode_continuation_snapshot(snapshot_json)
    if (
        frame_stack.logical != previous_stack.logical
        or frame_stack.mutation_ceiling != previous_stack.mutation_ceiling
        or frame_stack.policy_hash != previous_stack.policy_hash
        or frame_stack.admission_policy_json != previous_stack.admission_policy_json
        or frame_stack.cursor not in (expected_cursor, expected_cursor + len(event_batch))
    ):
        raise ContinuationCASConflict("continuation transition changed immutable request/policy or cursor")
    if prepared_append is None:
        now = database_now(conn)
        checkpoint_id = str(uuid4())
    else:
        prepared_append.require_original()
        final_json = encode_continuation_snapshot(frame_stack.model_copy(update={"cursor": expected_cursor + len(event_batch)}))
        if (
            prepared_append.session_id != session_id
            or prepared_append.operation_id != operation_id
            or prepared_append.expected_checkpoint_id != expected_checkpoint_id
            or prepared_append.expected_cursor != expected_cursor
            or prepared_append.snapshot_json != final_json
            or prepared_append.predecessor_json != current.payload_json
            or prepared_append.events != event_batch
            or prepared_append.handoff is not handoff
        ):
            raise AuditIntegrityError("prepared append differs from its actual current transition")
        now, checkpoint_id = prepared_append.published_at, prepared_append.checkpoint_id
    cursor = expected_cursor
    for item in event_batch:
        cursor += 1
        event_key = f"event:{cursor}"
        put_chunk(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            key=event_key,
            payload=item.payload_json.encode("utf-8"),
            codec="event.v2",
        )
        conn.execute(
            insert(events).values(
                session_id=session_id,
                operation_id=operation_id,
                sequence=cursor,
                tag=item.tag,
                payload_chunk_key=event_key,
                created_at=now,
            )
        )
    snapshot_json = (
        encode_continuation_snapshot(frame_stack.model_copy(update={"cursor": cursor}))
        if prepared_append is None
        else prepared_append.snapshot_json
    )
    snapshot_key = f"snapshot:{checkpoint_id}"
    put_chunk(
        conn, session_id=session_id, operation_id=operation_id, key=snapshot_key, payload=snapshot_json.encode("utf-8"), codec="snapshot.v2"
    )
    conn.execute(
        insert(checkpoints).values(
            session_id=session_id,
            operation_id=operation_id,
            checkpoint_id=checkpoint_id,
            kind="handoff" if handoff else "transition",
            cursor=cursor,
            snapshot_chunk_key=snapshot_key,
            frame_stack_version=1,
            created_at=now,
        )
    )
    changed = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == session_id,
            jobs.c.operation_id == operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.checkpoint_id == expected_checkpoint_id,
            jobs.c.journal_cursor == expected_cursor,
        )
        .values(checkpoint_id=checkpoint_id, journal_cursor=cursor, status_revision=jobs.c.status_revision + 1, updated_at=now)
    )
    if changed.rowcount != 1:
        raise ContinuationCASConflict("continuation snapshot compare-and-swap failed")
    return StoredContinuationSnapshot(snapshot_json, cursor, checkpoint_id)


@dataclass(frozen=True, slots=True)
class _SegmentStartSelection:
    snapshot: StoredContinuationSnapshot
    policy: ContinuationAdmissionPolicy
    action: ContinuationActionPolicy
    action_json: str


def _initial_segment_action(
    conn: Connection,
    *,
    parent: Row[Any],
    segment: Row[Any],
    snapshot: StoredContinuationSnapshot,
    stack: ContinuationFrameStack,
    policy: ContinuationAdmissionPolicy,
) -> ContinuationActionPolicy:
    del conn
    if (
        segment.sequence != 1
        or segment.generation != 1
        or segment.state != "queued"
        or parent.status != "queued"
        or parent.started_at is not None
        or snapshot.cursor != 0
    ):
        raise AuditIntegrityError("later continuation starts require the reviewed C3.4b producer binding")
    expected = prepare_composer_continuation_admission(
        session_id=UUID(parent.session_id),
        operation_id=parent.operation_id,
        kind=stack.logical.kind,
        request_json=parent.request_json,
        request_hash=parent.request_hash,
        base_state_id=UUID(parent.base_state_id) if parent.base_state_id is not None else None,
        policy_hash=stack.policy_hash,
        admission_policy_json=stack.admission_policy_json,
        initial_trust_mode=stack.mutation_ceiling.initial_trust_mode,
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        original_user_message_id=parent.reserved_user_message_id if parent.kind == "compose_recompose" else None,
    )
    if decode_continuation_snapshot(expected.snapshot_json) != stack:
        raise AuditIntegrityError("first segment is not its exact admitted complete initializer")
    return policy.actions.session_sql


def _selected_segment_action(
    conn: Connection,
    *,
    parent: Row[Any],
    segment: Row[Any],
    snapshot: StoredContinuationSnapshot,
    stack: ContinuationFrameStack,
    policy: ContinuationAdmissionPolicy,
) -> ContinuationActionPolicy:
    if segment.sequence == 1 and segment.generation == 1:
        return _initial_segment_action(conn, parent=parent, segment=segment, snapshot=snapshot, stack=stack, policy=policy)
    from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    if (
        parent.current_segment_sequence != segment.sequence
        or parent.ownership_generation != segment.generation
        or parent.checkpoint_id != snapshot.checkpoint_id
        or segment.checkpoint_id != snapshot.checkpoint_id
    ):
        raise AuditIntegrityError("selected segment differs from its current SQL snapshot")
    private = load_snapshot_private_data_on_connection(
        conn, session_id=parent.session_id, operation_id=parent.operation_id, selected=snapshot
    )
    return next_continuation_action_policy(selected=SelectedContinuationState(snapshot.checkpoint_id, stack, private), policy=policy)


def _retained_successor_action(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    snapshot: StoredContinuationSnapshot,
    next_action_json: str | None,
    policy_sha256: str | None,
) -> tuple[str, str]:
    from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    actual = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if actual != snapshot:
        raise ContinuationCASConflict("producer selected another retained continuation head")
    stack = decode_continuation_snapshot(actual.payload_json)
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    private = load_snapshot_private_data_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=actual)
    expected = next_continuation_action_policy(selected=SelectedContinuationState(actual.checkpoint_id, stack, private), policy=policy)
    if policy_sha256 is not None and policy_sha256 != stack.policy_hash:
        raise AuditIntegrityError("successor substituted another retained complete policy")
    if next_action_json is not None:
        try:
            supplied = ContinuationActionPolicy.model_validate_json(next_action_json, strict=True)
        except ValueError as error:
            raise AuditIntegrityError("successor allowance is not its owned policy") from error
        if supplied != expected:
            raise AuditIntegrityError("successor allowance differs from the actual selected retained action")
    return expected.model_dump_json(), stack.policy_hash


@dataclass(frozen=True, slots=True)
class ComposerSegmentRecoveryReceipt:
    """One same-sequence generation advance: peer recovery or a live handover."""

    session_id: UUID
    operation_id: str
    sequence: int
    previous_generation: int
    generation: int
    checkpoint_id: str
    cursor: int
    next_eligible_at: datetime
    # Observed by a live handover under its fence (design §2.5, F11); a peer
    # recovery holds no lease. Not part of the transition's identity.
    lease_validity_remaining: timedelta | None = field(default=None, compare=False)
    # Set only by the live handover transaction after releasing this exact
    # fence. Peer recovery has no current lease to release.
    released_context: SessionOperationContext | None = field(default=None, compare=False)


@dataclass(slots=True)
class ComposerSegmentStartObservation:
    """The actual start producer's context and commit/readback observations.

    A context observed before commit is not authority. Reconciliation proves
    the exact claim/fence/attempt in SQL before it may be adopted after error.
    """

    claim: ComposerOperationClaim
    context: SessionOperationContext | None = None
    committed: bool = False
    reconciled: bool = False
    policy: ContinuationAdmissionPolicy | None = None
    hard_expires_at: float | None = None


def recover_segment_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    parent: Row[Any],
    segment: Row[Any],
    history: Row[Any],
    now: datetime,
    next_eligible_at: datetime,
) -> ComposerSegmentRecoveryReceipt:
    """Advance one owner's segment to the next generation at the same sequence.

    The caller has already proved the owner may be replaced: a dead owner by
    lease and membership expiry, or a live owner by its own exact fence. This
    body is shared by both, so every phase reader that accepts a recovery
    accepts a handover: the event bytes, the unchanged root and the
    interrupted attempt pair are identical (design §2.2, review F3).

    The retained cut is classified and sized before any write. A cut holding
    unresolved work raises ``ContinuationCutNotResumable`` with nothing written.
    """
    expected_generation = segment.generation
    stored = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    action_json, policy_hash = _retained_successor_action(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        snapshot=stored,
        next_action_json=None,
        policy_sha256=None,
    )
    retained = append_complete_snapshot(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=stored.payload_json,
        event_batch=(
            ContinuationEvent(
                "recovered",
                canonical_json(
                    {
                        "version": 2,
                        "same_sequence": segment.sequence,
                        "previous_generation": expected_generation,
                        "generation": expected_generation + 1,
                    }
                ),
            ),
        ),
        handoff=False,
    )
    previous = conn.execute(
        update(attempts)
        .where(
            attempts.c.session_id == session_id,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == segment.sequence,
            attempts.c.generation == expected_generation,
            attempts.c.claim_attempt == history.claim_attempt,
            attempts.c.session_operation_id == history.session_operation_id,
            attempts.c.session_operation_epoch == history.session_operation_epoch,
            attempts.c.session_operation_lease_token == history.session_operation_lease_token,
            attempts.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, outcome="interrupted")
    )
    recovered = conn.execute(
        update(segments)
        .where(
            segments.c.session_id == session_id,
            segments.c.operation_id == operation_id,
            segments.c.sequence == segment.sequence,
            segments.c.generation == expected_generation,
            segments.c.state == "running",
            segments.c.claim_token == segment.claim_token,
        )
        .values(
            state="interrupted",
            generation=expected_generation + 1,
            checkpoint_id=retained.checkpoint_id,
            claim_token=None,
            claim_owner_instance_id=None,
            claim_expires_at=None,
            enqueued_at=now,
            next_eligible_at=next_eligible_at,
            next_action_json=action_json,
            policy_sha256=policy_hash,
        )
    )
    advanced = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == session_id,
            jobs.c.operation_id == operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.status == "running",
            jobs.c.current_segment_sequence == segment.sequence,
            jobs.c.ownership_generation == expected_generation,
            jobs.c.checkpoint_id == retained.checkpoint_id,
            jobs.c.attempt == parent.attempt,
        )
        .values(
            ownership_generation=expected_generation + 1,
            phase="reconciling" if next_eligible_at <= now else "retry_waiting",
            updated_at=now,
            status_revision=jobs.c.status_revision + 1,
        )
    )
    if previous.rowcount != 1 or recovered.rowcount != 1 or advanced.rowcount != 1:
        raise AuditIntegrityError("continuation recovery lost its exact generation transition")
    return ComposerSegmentRecoveryReceipt(
        UUID(session_id),
        operation_id,
        segment.sequence,
        expected_generation,
        expected_generation + 1,
        retained.checkpoint_id,
        retained.cursor,
        next_eligible_at,
    )


def verify_recovery_on_connection(conn: Connection, receipt: ComposerSegmentRecoveryReceipt) -> None:
    """Verify this committed generation transition, allowing later peer progress.

    The recovered checkpoint/event and revoked predecessor attempt are the
    immutable evidence. The current segment is allowed to have started,
    recovered again or settled; requiring it to remain interrupted races the
    peer that the committing handover deliberately made immediately eligible.
    """
    if type(receipt) is not ComposerSegmentRecoveryReceipt:
        raise TypeError("recovery verification requires its nominal receipt")
    sid, operation = str(receipt.session_id), receipt.operation_id
    if receipt.generation != receipt.previous_generation + 1:
        raise AuditIntegrityError("recovery receipt skipped its predecessor generation")
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == receipt.checkpoint_id,
        )
    ).one_or_none()
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == receipt.cursor)
    ).one_or_none()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == receipt.sequence,
            attempts.c.generation == receipt.previous_generation,
        )
    ).one_or_none()
    current = conn.execute(
        select(segments).where(segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == receipt.sequence)
    ).one_or_none()
    if (
        checkpoint is None
        or event is None
        or history is None
        or current is None
        or checkpoint.cursor != receipt.cursor
        or checkpoint.kind != "transition"
        or event.tag != "recovered"
        or history.revoked_at is None
        or history.outcome != "interrupted"
        or current.generation < receipt.generation
    ):
        raise AuditIntegrityError("durable recovery readback mismatch")
    expected = canonical_json(
        {
            "version": 2,
            "same_sequence": receipt.sequence,
            "previous_generation": receipt.previous_generation,
            "generation": receipt.generation,
        }
    ).encode("utf-8")
    if read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key) != expected:
        raise AuditIntegrityError("recovery event names another generation transition")
    recovered_stack = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation, key=checkpoint.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    if (
        recovered_stack.cursor != receipt.cursor
        or recovered_stack.logical.session_id != sid
        or recovered_stack.logical.operation_id != operation
    ):
        raise AuditIntegrityError("recovery checkpoint names another logical request or cursor")
    for generation in range(receipt.generation, current.generation):
        later = conn.execute(
            select(attempts.c.outcome, attempts.c.revoked_at).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == receipt.sequence,
                attempts.c.generation == generation,
            )
        ).one_or_none()
        if later is None or later.outcome != "interrupted" or later.revoked_at is None:
            raise AuditIntegrityError("recovery advanced without its predecessor attempt history")
    context = receipt.released_context
    if context is not None:
        fence = context.fence
        if (
            context.operation_kind is not SessionOperationKind.COMPOSE
            or fence.session_id != sid
            or history.session_operation_id != fence.operation_id
            or history.session_operation_epoch != fence.operation_epoch
            or history.session_operation_lease_token != fence.lease_token
        ):
            raise AuditIntegrityError("handover release differs from its exact predecessor fence")
        owner = conn.execute(select(fences).where(fences.c.session_id == sid)).one_or_none()
        if owner is None or owner.operation_epoch < fence.operation_epoch:
            raise AuditIntegrityError("handover lost its physical fence history")
        if owner.operation_epoch == fence.operation_epoch and (
            owner.operation_id != fence.operation_id or owner.lease_token != fence.lease_token or owner.released_at is None
        ):
            raise AuditIntegrityError("handover did not release its physical fence")


def settle_segment_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    parent: Row[Any],
    segment: Row[Any],
    history: Row[Any] | None,
    outcome: MessageWithStateResponse | ComposerOperationError,
    settled_by: Literal["owner_terminal", "settle_lost", "settle_lost_inactive_session"],
    attempt_outcome: Literal["settled", "interrupted"],
    now: datetime,
    prepared_settlement: PreparedSegmentSettlement | None = None,
) -> None:
    """Write one V2 terminal: settled event, attempt, segment and parent together.

    Shared by the live owner's terminal (``owner_terminal``, attempt
    ``settled``) and the settlement of a lost owner (``settle_lost*``, attempt
    ``interrupted``). Every ``dispatched`` effect becomes ``unknown`` with one
    ``effect_unknown`` event: its outcome is not durably known, and its real
    cost stays in the quota ledger (design §5.3). A never-started segment
    stays ``queued``; moving it to ``settled`` would violate its time bundle.
    """
    from elspeth.web.coordination.composer_operation_authority import _terminal_values

    stored = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if prepared_settlement is not None:
        candidate = prepared_settlement
        candidate.append.require_original()
        if (
            candidate.driving != _settlement_driving(parent, segment, history)
            or candidate.outcome_json != outcome.model_dump_json()
            or candidate.outcome.model_dump_json() != candidate.outcome_json
            or candidate.settled_by != settled_by
            or candidate.attempt_outcome != attempt_outcome
            or candidate.published_at != candidate.append.published_at
        ):
            raise AuditIntegrityError("prepared terminal settlement changed its actual driving state or outcome")
        now = candidate.published_at
    stack, unresolved, event_batch = _settlement_transition(
        stored, segment=segment, outcome=outcome, published_at=now, settled_by=settled_by
    )
    terminal_values = _terminal_values(outcome, now=now, settled_by=settled_by)
    retained = append_complete_snapshot(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=encode_continuation_snapshot(stack),
        event_batch=event_batch,
        handoff=False,
        prepared_append=None if prepared_settlement is None else prepared_settlement.append,
    )
    if unresolved:
        marked = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == session_id,
                effects.c.operation_id == operation_id,
                effects.c.effect_key.in_(unresolved),
                effects.c.disposition == "dispatched",
            )
            .values(disposition="unknown")
        )
        if marked.rowcount != len(unresolved):
            raise AuditIntegrityError("continuation settlement lost an unresolved effect row")
    if history is not None:
        revoked = conn.execute(
            update(attempts)
            .where(
                attempts.c.session_id == session_id,
                attempts.c.operation_id == operation_id,
                attempts.c.sequence == history.sequence,
                attempts.c.generation == history.generation,
                attempts.c.session_operation_id == history.session_operation_id,
                attempts.c.revoked_at.is_(None),
            )
            .values(revoked_at=now, outcome=attempt_outcome)
        )
        if revoked.rowcount != 1:
            raise AuditIntegrityError("continuation settlement lost its exact attempt")
    if segment.state != "queued":
        closed = conn.execute(
            update(segments)
            .where(
                segments.c.session_id == session_id,
                segments.c.operation_id == operation_id,
                segments.c.sequence == segment.sequence,
                segments.c.generation == segment.generation,
                segments.c.state == segment.state,
            )
            .values(state="settled", claim_token=None, claim_owner_instance_id=None, claim_expires_at=None)
        )
        if closed.rowcount != 1:
            raise AuditIntegrityError("continuation settlement lost its exact segment")
    terminal = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == session_id,
            jobs.c.operation_id == operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.status == parent.status,
            jobs.c.current_segment_sequence == segment.sequence,
            jobs.c.ownership_generation == segment.generation,
            jobs.c.checkpoint_id == retained.checkpoint_id,
        )
        .values(**terminal_values, phase="settled")
    )
    if terminal.rowcount != 1:
        raise AuditIntegrityError("continuation settlement lost its exact parent")


def settle_continuation_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    outcome: MessageWithStateResponse | ComposerOperationError,
    prepared_settlement: PreparedSegmentSettlement | None = None,
) -> None:
    """The live owner's V2 terminal under its exact current fence and attempt.

    A ``request_cancelled`` outcome is the one terminal a pending Stop allows.
    The fence is left for the lease close, as for V1, so the worker verifies
    the terminal before it releases (design §2.7).
    """
    claim, context = running.claim, running.session_operation_context
    if claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("continuation settlement requires its segment claim")
    cancelling = isinstance(outcome, ComposerOperationError) and outcome.failure_code == "request_cancelled"
    # False means no live continuation attempt holds this fence: not this owner.
    if not require_continuation_mutation(conn, context, allow_cancel=cancelling, allow_expired_segment=True):
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    sid = str(claim.session_id)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == claim.operation_id)).one()
    if cancelling and parent.cancel_requested_at is None:
        raise AuditIntegrityError("cancel outcome requires a durable Stop marker")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid,
            segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == claim.operation_id,
            attempts.c.sequence == claim.segment_sequence,
            attempts.c.generation == claim.ownership_generation,
        )
    ).one()
    if segment.claim_token != claim.claim_token or parent.attempt != claim.attempt or history.claim_attempt != claim.attempt:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    settle_segment_on_connection(
        conn,
        session_id=sid,
        operation_id=claim.operation_id,
        parent=parent,
        segment=segment,
        history=history,
        outcome=outcome,
        settled_by="owner_terminal",
        attempt_outcome="settled",
        now=database_now(conn),
        prepared_settlement=prepared_settlement,
    )


@dataclass(frozen=True, slots=True)
class ComposerSegmentYieldReceipt:
    """One committed handoff: the predecessor and its one queued successor."""

    session_id: UUID
    operation_id: str
    predecessor_sequence: int
    predecessor_generation: int
    successor_sequence: int
    successor_generation: int
    checkpoint_id: str
    cursor: int
    released_epoch: int
    # Observed by the committing call only (design §2.5, F11); a read-back
    # cannot know it. Not part of the yield's identity.
    lease_validity_remaining: timedelta | None = field(default=None, compare=False)


def yield_segment_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    expected_head: UUID | None,
    expected_checkpoint_id: str,
    expected_cursor: int,
    snapshot_json: str,
    reason: ContinuationYieldReason,
    now: datetime,
) -> ComposerSegmentYieldReceipt:
    """Write a handoff checkpoint, yield the segment and queue one successor.

    The caller holds the session lock and releases the fence in this same
    transaction (``commit_checkpoint_and_successor``), so the successor is
    claimable at commit. A pending Stop refuses: Stop wins over a yield.
    """
    from elspeth.web.composer.continuation_handoff import ContinuationYieldEvent, roll_slice_for_successor

    claim, context = running.claim, running.session_operation_context
    if claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("yield requires a continuation segment claim")
    fence = context.fence
    # False means no live continuation attempt holds this fence: not this owner.
    if not require_continuation_mutation(conn, context, allow_cancel=False, allow_expired_segment=True):
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    stack = decode_continuation_snapshot(snapshot_json)
    if not stack.is_handoff_safe:
        raise AuditIntegrityError("handoff contains live or unsettled work")
    sid, operation = str(claim.session_id), claim.operation_id
    open_effect = conn.execute(
        select(effects.c.effect_key)
        .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.disposition.in_(("dispatched", "unknown")))
        .limit(1)
    ).one_or_none()
    if open_effect is not None:
        raise AuditIntegrityError("handoff contains an unresolved dispatched effect")
    head = conn.execute(
        select(states.c.id).where(states.c.session_id == sid).order_by(states.c.version.desc()).limit(1)
    ).scalar_one_or_none()
    if head != (str(expected_head) if expected_head is not None else None):
        raise ContinuationCASConflict("composition head changed before handoff")
    sequence, generation = claim.segment_sequence, claim.ownership_generation
    event = ContinuationYieldEvent(
        reason=reason,
        predecessor_sequence=sequence,
        predecessor_generation=generation,
        successor_sequence=sequence + 1,
        successor_generation=generation + 1,
    )
    retained = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
        snapshot_json=encode_continuation_snapshot(roll_slice_for_successor(stack, predecessor_sequence=sequence)),
        event_batch=(ContinuationEvent("yielded", canonical_json(event.model_dump(mode="json"))),),
        handoff=True,
    )
    action_json, policy_hash = _retained_successor_action(
        conn, session_id=sid, operation_id=operation, snapshot=retained, next_action_json=None, policy_sha256=None
    )
    revoked = conn.execute(
        update(attempts)
        .where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == sequence,
            attempts.c.generation == generation,
            attempts.c.claim_attempt == claim.attempt,
            attempts.c.session_operation_id == fence.operation_id,
            attempts.c.session_operation_epoch == fence.operation_epoch,
            attempts.c.session_operation_lease_token == fence.lease_token,
            attempts.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, outcome="yielded")
    )
    if revoked.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=operation, attempt=claim.attempt)
    yielded = conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == sequence,
            segments.c.generation == generation,
            segments.c.state == "running",
            segments.c.claim_token == claim.claim_token,
            segments.c.session_operation_id == fence.operation_id,
            segments.c.session_operation_epoch == fence.operation_epoch,
            segments.c.session_operation_lease_token == fence.lease_token,
        )
        .values(
            state="yielded",
            checkpoint_id=retained.checkpoint_id,
            claim_token=None,
            claim_owner_instance_id=None,
            claim_expires_at=None,
            rollover_reason=reason,
        )
    )
    if yielded.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=operation, attempt=claim.attempt)
    try:
        with conn.begin_nested():
            conn.execute(
                insert(segments).values(
                    session_id=sid,
                    operation_id=operation,
                    sequence=sequence + 1,
                    predecessor_sequence=sequence,
                    state="queued",
                    generation=generation + 1,
                    enqueued_at=now,
                    next_eligible_at=now,
                    checkpoint_id=retained.checkpoint_id,
                    next_action_json=action_json,
                    policy_sha256=policy_hash,
                )
            )
    except IntegrityError as error:
        raise AuditIntegrityError("a yielded segment already has its successor") from error
    advanced = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == sid,
            jobs.c.operation_id == operation,
            jobs.c.lifecycle_version == 2,
            jobs.c.status == "running",
            jobs.c.current_segment_sequence == sequence,
            jobs.c.ownership_generation == generation,
            jobs.c.attempt == claim.attempt,
            jobs.c.checkpoint_id == retained.checkpoint_id,
            jobs.c.cancel_requested_at.is_(None),
            jobs.c.session_operation_id == fence.operation_id,
            jobs.c.session_operation_lease_token == fence.lease_token,
            jobs.c.session_operation_epoch == fence.operation_epoch,
        )
        .values(
            current_segment_sequence=sequence + 1,
            ownership_generation=generation + 1,
            phase="continuation_queued",
            status_revision=jobs.c.status_revision + 1,
            updated_at=now,
        )
    )
    if advanced.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=operation, attempt=claim.attempt)
    return ComposerSegmentYieldReceipt(
        claim.session_id,
        operation,
        sequence,
        generation,
        sequence + 1,
        generation + 1,
        retained.checkpoint_id,
        retained.cursor,
        fence.operation_epoch,
    )


def verify_yield_on_connection(conn: Connection, receipt: ComposerSegmentYieldReceipt) -> None:
    """Read back a committed yield, tolerating any later progress of its successor.

    The fence was released at commit, so a peer may already have started,
    recovered or even settled the successor. Only the yield's own immutable
    rows are compared: the predecessor, its attempt, the handoff checkpoint and
    event, and the successor's lineage (design §2.3, review F7).
    """
    from elspeth.web.composer.continuation_handoff import ContinuationYieldEvent

    sid, operation = str(receipt.session_id), receipt.operation_id
    predecessor = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == receipt.predecessor_sequence
        )
    ).one_or_none()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == receipt.predecessor_sequence,
            attempts.c.generation == receipt.predecessor_generation,
        )
    ).one_or_none()
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.checkpoint_id == receipt.checkpoint_id
        )
    ).one_or_none()
    event_row = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == receipt.cursor)
    ).one_or_none()
    successor = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == receipt.successor_sequence
        )
    ).one_or_none()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one_or_none()
    if (
        predecessor is None
        or history is None
        or checkpoint is None
        or event_row is None
        or successor is None
        or parent is None
        or predecessor.state != "yielded"
        or predecessor.generation != receipt.predecessor_generation
        or predecessor.checkpoint_id != receipt.checkpoint_id
        or history.revoked_at is None
        or history.outcome != "yielded"
        or checkpoint.kind != "handoff"
        or checkpoint.cursor != receipt.cursor
        or event_row.tag != "yielded"
        or successor.predecessor_sequence != receipt.predecessor_sequence
        or successor.generation < receipt.successor_generation
    ):
        raise AuditIntegrityError("durable yield readback mismatch")
    try:
        payload = read_chunk(conn, session_id=sid, operation_id=operation, key=event_row.payload_chunk_key).decode("utf-8", errors="strict")
        event = ContinuationYieldEvent.model_validate_json(payload, strict=True)
    except ValueError as error:  # includes UnicodeDecodeError
        raise AuditIntegrityError("yield event violates its closed payload") from error
    if (event.predecessor_sequence, event.predecessor_generation, event.successor_sequence, event.successor_generation) != (
        receipt.predecessor_sequence,
        receipt.predecessor_generation,
        receipt.successor_sequence,
        receipt.successor_generation,
    ):
        raise AuditIntegrityError("yield event names another predecessor or successor")
    # Any generation above the one the yield queued must be a recovery step.
    for later in range(receipt.successor_generation, successor.generation):
        step = conn.execute(
            select(attempts.c.outcome, attempts.c.revoked_at).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == receipt.successor_sequence,
                attempts.c.generation == later,
            )
        ).one_or_none()
        if step is None or step.revoked_at is None or step.outcome != "interrupted":
            raise AuditIntegrityError("yield successor advanced without its recovery history")
    if parent.status in ("queued", "running") and (
        parent.current_segment_sequence < receipt.successor_sequence
        or (parent.current_segment_sequence == receipt.successor_sequence and parent.ownership_generation < receipt.successor_generation)
    ):
        raise AuditIntegrityError("parent pointer is behind its committed yield")


def read_yield_outcome_on_connection(conn: Connection, running: ComposerOperationRunning) -> ComposerSegmentYieldReceipt | None:
    """After an ambiguous commit: ``None`` if the yield did not commit, else its receipt.

    ``None`` means the caller's attempt is still live and unrevoked under its own
    fence, so the yield may be retried while that fence lasts. Any other
    revocation means another writer moved the operation: an integrity stop.
    """
    from elspeth.web.composer.continuation_handoff import ContinuationYieldEvent

    claim, fence = running.claim, running.session_operation_context.fence
    if claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("yield outcome requires a continuation segment claim")
    sid, operation = str(claim.session_id), claim.operation_id
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == claim.segment_sequence,
            attempts.c.generation == claim.ownership_generation,
        )
    ).one_or_none()
    if (
        history is None
        or history.session_operation_id != fence.operation_id
        or history.session_operation_epoch != fence.operation_epoch
        or history.session_operation_lease_token != fence.lease_token
        or history.claim_attempt != claim.attempt
    ):
        raise AuditIntegrityError("yield outcome lost the caller's exact attempt")
    if history.revoked_at is None:
        return None
    if history.outcome != "yielded":
        raise AuditIntegrityError("the caller's attempt was revoked by another writer")
    predecessor = conn.execute(
        select(segments.c.checkpoint_id).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == claim.segment_sequence
        )
    ).scalar_one()
    checkpoint = conn.execute(
        select(checkpoints.c.cursor).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.checkpoint_id == predecessor
        )
    ).scalar_one()
    event_row = conn.execute(
        select(events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == checkpoint
        )
    ).scalar_one()
    try:
        event = ContinuationYieldEvent.model_validate_json(
            read_chunk(conn, session_id=sid, operation_id=operation, key=event_row).decode("utf-8", errors="strict"), strict=True
        )
    except ValueError as error:
        raise AuditIntegrityError("yield event violates its closed payload") from error
    receipt = ComposerSegmentYieldReceipt(
        claim.session_id,
        operation,
        event.predecessor_sequence,
        event.predecessor_generation,
        event.successor_sequence,
        event.successor_generation,
        predecessor,
        checkpoint,
        history.session_operation_epoch,
    )
    verify_yield_on_connection(conn, receipt)
    return receipt


def _select_segment_start_on_connection(
    conn: Connection,
    claim: ComposerOperationClaim,
    *,
    owner_instance_id: str,
    segment_policy: ContinuationSegmentPolicy | None = None,
) -> _SegmentStartSelection:
    if type(claim) is not ComposerOperationClaim or claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("continuation start requires its nominal segment claim")
    if segment_policy is not None and type(segment_policy) is not ContinuationSegmentPolicy:
        raise TypeError("segment policy must be its actual owned type")
    now = database_now(conn)
    parent = conn.execute(
        select(jobs).where(
            jobs.c.session_id == str(claim.session_id),
            jobs.c.operation_id == claim.operation_id,
        )
    ).one_or_none()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == str(claim.session_id),
            segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
        )
    ).one_or_none()
    if (
        parent is None
        or segment is None
        or parent.lifecycle_version != 2
        or parent.status not in ("queued", "running")
        or parent.attempt != claim.attempt
        or parent.current_segment_sequence != claim.segment_sequence
        or parent.ownership_generation != claim.ownership_generation
        or segment.generation != claim.ownership_generation
        or segment.state not in ("queued", "interrupted")
        or segment.claim_token != claim.claim_token
        or segment.claim_owner_instance_id != owner_instance_id
        or segment.claim_expires_at is None
        or restore_utc(segment.claim_expires_at) <= now
        or restore_utc(segment.next_eligible_at) > now
    ):
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    principal = conn.execute(
        select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
            sessions_table.c.id == str(claim.session_id),
        )
    ).one_or_none()
    if principal is None or principal.archived_at is not None or principal.user_id != parent.actor_user_id:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    snapshot = load_snapshot(conn, session_id=str(claim.session_id), operation_id=claim.operation_id)
    stack = decode_continuation_snapshot(snapshot.payload_json)
    if snapshot.checkpoint_id != parent.checkpoint_id or segment.checkpoint_id != snapshot.checkpoint_id:
        raise AuditIntegrityError("segment and parent selected different complete checkpoints")
    request: SendMessageRequest | RecomposeRequest
    try:
        if parent.kind == "compose_message":
            request = SendMessageRequest.model_validate_json(parent.request_json)
            original = reserved_user_message_id(claim.session_id, claim.operation_id)
        elif parent.kind == "compose_recompose":
            request = RecomposeRequest.model_validate_json(parent.request_json)
            original = str(request.expected_user_message_id)
        else:
            raise AuditIntegrityError("continuation parent kind is invalid")
    except ValueError as exc:
        raise AuditIntegrityError("SQL continuation request violates its admitted owned shape") from exc
    logical = stack.logical
    if (
        request.operation_id != claim.operation_id
        or (str(request.state_id) if request.state_id is not None else None) != parent.base_state_id
        or composer_operation_v2_request_hash(session_id=claim.session_id, kind=parent.kind, request=request) != parent.request_hash
        or (
            logical.session_id,
            logical.operation_id,
            logical.kind,
            logical.request_hash,
            logical.base_state_id,
            logical.original_user_message_id,
        )
        != (
            str(claim.session_id),
            claim.operation_id,
            parent.kind,
            parent.request_hash,
            parent.base_state_id,
            parent.reserved_user_message_id,
        )
        or original != parent.reserved_user_message_id
        or (parent.ingress_phase == "committed" and parent.user_message_id != original)
        or (parent.ingress_phase == "pending" and parent.user_message_id is not None)
    ):
        raise AuditIntegrityError("complete snapshot differs from its actual SQL logical request")
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if segment_policy is not None and segment_policy != policy.segment:
        raise AuditIntegrityError("start substituted current defaults for retained segment policy")
    if segment.policy_sha256 != stack.policy_hash:
        raise AuditIntegrityError("segment policy pointer differs from the retained complete policy")
    expected = _selected_segment_action(conn, parent=parent, segment=segment, snapshot=snapshot, stack=stack, policy=policy)
    try:
        action = ContinuationActionPolicy.model_validate_json(segment.next_action_json, strict=True)
    except ValueError as exc:
        raise AuditIntegrityError("segment action violates its retained owned contract") from exc
    if action != expected:
        raise AuditIntegrityError("segment allowance differs from its selected retained action")
    return _SegmentStartSelection(snapshot, policy, expected, segment.next_action_json)


def start_segment_on_connection(
    conn: Connection,
    claim: ComposerOperationClaim,
    *,
    context: SessionOperationContext,
    owner_instance_id: str,
    segment_policy: ContinuationSegmentPolicy | None = None,
) -> ComposerOperationRunning:
    if type(claim) is not ComposerOperationClaim:
        raise TypeError("continuation start requires its nominal claim")
    if (
        type(context) is not SessionOperationContext
        or context.operation_kind is not SessionOperationKind.COMPOSE
        or context.fence.session_id != str(claim.session_id)
    ):
        raise AuditIntegrityError("continuation start requires its exact same-session COMPOSE context")
    selected = _select_segment_start_on_connection(conn, claim, owner_instance_id=owner_instance_id, segment_policy=segment_policy)
    now = database_now(conn)
    f = context.fence
    owner = conn.execute(
        select(fences.c.lease_expires_at).where(
            fences.c.session_id == f.session_id,
            fences.c.operation_id == f.operation_id,
            fences.c.operation_epoch == f.operation_epoch,
            fences.c.lease_token == f.lease_token,
            fences.c.operation_kind == SessionOperationKind.COMPOSE.value,
            fences.c.owner_instance_id == owner_instance_id,
            fences.c.released_at.is_(None),
            fences.c.lease_expires_at > now,
        )
    ).one_or_none()
    if owner is None:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    duration = size_segment(selected.action, selected.policy.segment)
    deadline = now + timedelta(seconds=duration)
    cooperative = now + timedelta(seconds=min(selected.policy.segment.quantum, duration - selected.policy.segment.watermark))
    changed = conn.execute(
        update(segments)
        .where(
            segments.c.session_id == str(claim.session_id),
            segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
            segments.c.generation == claim.ownership_generation,
            segments.c.state.in_(("queued", "interrupted")),
            segments.c.claim_token == claim.claim_token,
            segments.c.claim_owner_instance_id == owner_instance_id,
            segments.c.claim_expires_at > now,
            segments.c.next_eligible_at <= now,
            segments.c.checkpoint_id == selected.snapshot.checkpoint_id,
            segments.c.policy_sha256 == selected.policy.policy_hash,
            segments.c.next_action_json == selected.action_json,
        )
        .values(
            state="running",
            started_at=now,
            cooperative_at=cooperative,
            deadline_at=deadline,
            session_operation_id=f.operation_id,
            session_operation_lease_token=f.lease_token,
            session_operation_epoch=f.operation_epoch,
        )
    )
    if changed.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    conn.execute(
        insert(attempts).values(
            session_id=str(claim.session_id),
            operation_id=claim.operation_id,
            sequence=claim.segment_sequence,
            generation=claim.ownership_generation,
            claim_attempt=claim.attempt,
            session_operation_id=f.operation_id,
            session_operation_epoch=f.operation_epoch,
            session_operation_lease_token=f.lease_token,
            owner_instance_id=owner_instance_id,
            started_at=now,
            claim_expires_at=restore_utc(owner.lease_expires_at),
        )
    )
    parent = conn.execute(
        update(jobs)
        .where(
            jobs.c.session_id == str(claim.session_id),
            jobs.c.operation_id == claim.operation_id,
            jobs.c.lifecycle_version == 2,
            jobs.c.status.in_(("queued", "running")),
            jobs.c.attempt == claim.attempt,
            jobs.c.current_segment_sequence == claim.segment_sequence,
            jobs.c.ownership_generation == claim.ownership_generation,
            jobs.c.checkpoint_id == selected.snapshot.checkpoint_id,
            jobs.c.journal_cursor == selected.snapshot.cursor,
        )
        .values(
            status="running",
            phase="executing",
            started_at=func.coalesce(jobs.c.started_at, now),
            session_operation_id=f.operation_id,
            session_operation_lease_token=f.lease_token,
            session_operation_epoch=f.operation_epoch,
            claim_owner_instance_id=owner_instance_id,
            status_revision=jobs.c.status_revision + 1,
            updated_at=now,
        )
    )
    if parent.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    return ComposerOperationRunning(claim=claim, session_operation_context=context)


def private_chunk_key(chunk: PrivateChunk) -> str:
    return f"value:{chunk.reference.codec}:{chunk.reference.digest}"


def bound_action_json(
    stack: ContinuationFrameStack,
    *,
    action_owner: Literal["primary_turn", "review", "read_tool"] | None,
    read_tool_name: str | None,
    read_tool_arguments_json: str | None,
) -> str | None:
    from elspeth.web.composer.advisor_output import PrimaryContinuationAction, ReadContinuationAction, ReviewContinuationAction
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action

    if action_owner is None:
        return None
    if stack.active_action_json is None:
        raise AuditIntegrityError("prescribed execution owner has no actual retained Advisor action")
    action = decode_prescribed_continuation_action(stack.active_action_json)
    matches = (action_owner == "primary_turn" and type(action) is PrimaryContinuationAction) or (
        action_owner == "review" and type(action) is ReviewContinuationAction
    )
    if action_owner == "read_tool" and type(action) is ReadContinuationAction:
        requested, request_error = parse_json_strict(action.arguments_json)
        actual, actual_error = parse_json_strict(read_tool_arguments_json) if read_tool_arguments_json is not None else (None, "absent")
        matches = (
            read_tool_name == action.tool_name
            and request_error is None
            and actual_error is None
            and canonical_json(requested) == canonical_json(actual)
        )
    if not matches:
        raise AuditIntegrityError("prescribed action differs from its actual execution owner")
    return stack.active_action_json


def retain_action_execution_receipt_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    effect_key: str,
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"],
    snapshot_json: str,
) -> str:
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.composer.continuation_progress import (
        ActionExecutionReceipt,
        continuation_action_key,
        decode_continuation_frontier,
        decode_prescribed_continuation_action,
        encode_continuation_frontier,
        record_action_receipt,
    )

    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == session_id,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one()
    stack = decode_continuation_snapshot(snapshot_json)
    if (
        (stack.logical.session_id, stack.logical.operation_id) != (session_id, operation_id)
        or creator.operation_id != operation_id
        or effect.disposition != disposition
    ):
        raise AuditIntegrityError("action receipt differs from actual request/effect creator")
    if effect.action_json is None:
        return snapshot_json
    action = decode_prescribed_continuation_action(effect.action_json)
    if stack.semantic_frontier is None:
        raise AuditIntegrityError("prepared action lost its retained semantic frontier")
    ref = stack.semantic_frontier
    canonical = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=f"value:{ref.codec}:{ref.digest}").decode(
        "utf-8", errors="strict"
    )
    verify_private_chunk(ref, canonical)
    frontier = decode_continuation_frontier(canonical)
    receipt = ActionExecutionReceipt(
        continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence}),
        effect.effect_key,
        disposition,
    )
    updated = record_action_receipt(frontier, receipt, actual_owner_receipt_id=effect.effect_key)
    chunk = encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated))
    put_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=private_chunk_key(chunk),
        payload=chunk.canonical.encode("utf-8"),
        codec=chunk.reference.codec,
    )
    updated_stack = stack.model_copy(update={"semantic_frontier": chunk.reference})
    if disposition in ("committed", "refused") and stack.active_action_json == effect.action_json:
        updated_stack = updated_stack.model_copy(update={"active_action_json": None, "active_action_source": None})
    return encode_continuation_snapshot(updated_stack)


def _effect_snapshot(
    encoded: str,
    *,
    effect_key: str,
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"],
    receipt_id: str | None,
    add_attempt_id: str | None = None,
    remove_attempt_id: str | None = None,
) -> str:
    from elspeth.web.composer.continuation import EffectFrontierEntry
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot

    stack = decode_continuation_snapshot(encoded)
    entry = EffectFrontierEntry(effect_id=effect_key, disposition=disposition, receipt_id=receipt_id)
    previous = tuple(item for item in stack.effect_frontier if item.effect_id != effect_key)
    pending = tuple(value for value in stack.pending_settlement_ids if value != remove_attempt_id)
    if add_attempt_id is not None and add_attempt_id not in pending:
        pending = (*pending, add_attempt_id)
    return encode_continuation_snapshot(
        stack.model_copy(
            update={
                "effect_frontier": (*previous, entry),
                "pending_settlement_ids": pending,
            }
        )
    )


def _require_current_continuation_running(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    allow_cancel: bool = False,
    allow_expired_segment: bool = False,
) -> None:
    if (
        type(running) is not ComposerOperationRunning
        or type(running.claim) is not ComposerOperationClaim
        or type(running.session_operation_context) is not SessionOperationContext
        or running.session_operation_context.fence.session_id != str(running.claim.session_id)
        or running.session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
    ):
        raise AuditIntegrityError("continuation boundary changed its actual running context")
    context = running.session_operation_context
    if (
        require_continuation_mutation(
            conn,
            context,
            allow_cancel=allow_cancel,
            allow_expired_segment=allow_expired_segment,
        )
        is not True
    ):
        raise AuditIntegrityError("continuation boundary requires current continuation authority")
    claim = running.claim
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == context.fence.session_id,
            attempts.c.session_operation_epoch == context.fence.operation_epoch,
        )
    ).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == history.session_id,
            segments.c.operation_id == history.operation_id,
            segments.c.sequence == history.sequence,
            segments.c.generation == history.generation,
        )
    ).one()
    if (
        history.session_id,
        history.operation_id,
        history.sequence,
        history.generation,
        history.claim_attempt,
        segment.claim_token,
    ) != (
        str(claim.session_id),
        claim.operation_id,
        claim.segment_sequence,
        claim.ownership_generation,
        claim.attempt,
        claim.claim_token,
    ):
        raise AuditIntegrityError("continuation boundary changed its current claim/history binding")


def _require_reserved_provider_frame(stack: ContinuationFrameStack) -> InitializeFrame:
    selected = stack.frames[-1]
    if (
        len(stack.frames) != 1
        or type(selected) is not InitializeFrame
        or selected.ingress_phase != "reserved"
        or selected.title_attempt_id is not None
        or selected.ingress_receipt_json is not None
        or selected.bootstrap is not None
        or selected.title_response is not None
        or stack.semantic_frontier is not None
        or stack.active_action_json is not None
        or stack.active_action_source is not None
        or stack.pending_owner_results
    ):
        raise AuditIntegrityError("initial provider preparation requires its reserved initializer without later action owners")
    return selected


@dataclass(frozen=True, slots=True)
class ContinuationProviderBinding:
    running: ComposerOperationRunning
    effect_key: str
    family: int
    ordinal: int
    input_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedContinuationProvider:
    binding: ContinuationProviderBinding
    snapshot: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class ProviderActionObservation:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    response: PrivateChunk | None
    provider_attempt_id: str | None
    ledger_entry_id: str | None


def prepare_effect_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    effect_key: str,
    input_sha256: str,
    owner: str,
    next_snapshot_json: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    action_owner: Literal["primary_turn", "review", "read_tool"] | None = None,
    read_tool_name: str | None = None,
    read_tool_arguments_json: str | None = None,
) -> StoredContinuationSnapshot:
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("new continuation preparation cannot use owed P4 authority")
    claim = running.claim
    stored = require_snapshot_cas(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    current = decode_continuation_snapshot(stored.payload_json)
    current_frame = _require_reserved_provider_frame(current)
    if current_frame.provider_effect_key is not None or current_frame.provider_input_sha256 is not None:
        raise ContinuationCASConflict("new provider preparation would replace an unapplied selected effect")
    if owner != "provider" or action_owner is not None or read_tool_name is not None or read_tool_arguments_json is not None:
        raise AuditIntegrityError("initial effect preparation cannot authorize later SQL, read or prescribed-action owners")
    stack = decode_continuation_snapshot(next_snapshot_json)
    if stack.cursor != expected_cursor or (stack.logical.session_id, stack.logical.operation_id) != (
        str(claim.session_id),
        claim.operation_id,
    ):
        raise ContinuationCASConflict("prepared effect carries an obsolete or foreign frame stack")
    expected_frame = current_frame.model_copy(update={"provider_effect_key": effect_key, "provider_input_sha256": input_sha256})
    expected_stack = current.model_copy(update={"frames": (expected_frame,)})
    expected_json = _effect_snapshot(
        encode_continuation_snapshot(expected_stack), effect_key=effect_key, disposition="prepared", receipt_id=None
    )
    if stack != decode_continuation_snapshot(expected_json):
        raise ContinuationCASConflict("prepared effect replaced its actual selected initializer or input")
    old = conn.execute(
        select(effects).where(
            effects.c.session_id == str(claim.session_id),
            effects.c.operation_id == claim.operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one_or_none()
    if old is not None:
        if old.input_sha256 != input_sha256 or old.owner != owner:
            raise AuditIntegrityError("prepared effect identity collision")
        raise ContinuationCASConflict("effect already prepared; restore its actual disposition")
    conn.execute(
        insert(effects).values(
            session_id=str(claim.session_id),
            operation_id=claim.operation_id,
            effect_key=effect_key,
            input_sha256=input_sha256,
            owner=owner,
            disposition="prepared",
            creator_sequence=claim.segment_sequence,
            creator_generation=claim.ownership_generation,
            action_json=bound_action_json(
                stack, action_owner=action_owner, read_tool_name=read_tool_name, read_tool_arguments_json=read_tool_arguments_json
            ),
        )
    )
    next_snapshot_json = _effect_snapshot(next_snapshot_json, effect_key=effect_key, disposition="prepared", receipt_id=None)
    retained_policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    next_action = retained_policy.actions.session_sql
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == str(claim.session_id),
            segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
            segments.c.generation == claim.ownership_generation,
        )
        .values(next_action_json=next_action.model_dump_json(), policy_sha256=stack.policy_hash)
    )
    next_snapshot_json = retain_action_execution_receipt_on_connection(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        effect_key=effect_key,
        disposition="prepared",
        snapshot_json=next_snapshot_json,
    )
    return append_complete_snapshot(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=next_snapshot_json,
        event_batch=(ContinuationEvent("tool_prepared", canonical_json({"effect_key": effect_key, "input_sha256": input_sha256})),),
        handoff=False,
    )


def prepare_provider_effect_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    family: int,
    input_sha256: str,
    snapshot_json: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    primary_input_json: str | None = None,
) -> ContinuationProviderBinding:
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("new continuation preparation cannot use owed P4 authority")
    stored = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    if snapshot_json != stored.payload_json:
        raise ContinuationCASConflict("provider preparation replaced the actual selected complete head")
    if type(family) is not int or family not in (0, 1, 2, 3):
        raise ValueError("unknown physical provider family")
    if type(input_sha256) is not str or len(input_sha256) != 64 or any(char not in "0123456789abcdef" for char in input_sha256):
        raise ValueError("provider preparation requires its actual input digest")
    prepared_stack = decode_continuation_snapshot(snapshot_json)
    from elspeth.web.composer.continuation import ComposeFrame

    if family == 0 and type(prepared_stack.frames[-1]) is ComposeFrame:
        from elspeth.web.sessions.continuation_primary import prepare_primary_provider_on_connection

        return prepare_primary_provider_on_connection(
            conn,
            running,
            input_sha256=input_sha256,
            snapshot_json=snapshot_json,
            expected_checkpoint_id=expected_checkpoint_id,
            expected_cursor=expected_cursor,
            input_json=primary_input_json,
        )
    if primary_input_json is not None:
        raise AuditIntegrityError("initial provider preparation cannot carry a later primary input descriptor")
    selected = _require_reserved_provider_frame(prepared_stack)
    if selected.provider_effect_key is not None or selected.provider_input_sha256 is not None:
        raise ContinuationCASConflict("new provider preparation would replace an unapplied selected effect")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    old = conn.execute(
        select(ordinals.c.next_ordinal).where(
            ordinals.c.session_id == sid,
            ordinals.c.operation_id == operation_id,
            ordinals.c.family == family,
        )
    ).scalar_one_or_none()
    ordinal = 0 if old is None else old
    key = f"provider:{family}:{ordinal}"
    selected = selected.model_copy(update={"provider_effect_key": key, "provider_input_sha256": input_sha256})
    prepared_stack = prepared_stack.model_copy(update={"frames": (*prepared_stack.frames[:-1], selected)})
    next_snapshot = _effect_snapshot(encode_continuation_snapshot(prepared_stack), effect_key=key, disposition="prepared", receipt_id=None)
    prepare_effect_on_connection(
        conn,
        running,
        effect_key=key,
        input_sha256=input_sha256,
        owner="provider",
        next_snapshot_json=next_snapshot,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    if old is None:
        conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation_id, family=family, next_ordinal=1))
    else:
        conn.execute(
            update(ordinals)
            .where(
                ordinals.c.session_id == sid,
                ordinals.c.operation_id == operation_id,
                ordinals.c.family == family,
            )
            .values(next_ordinal=ordinal + 1)
        )
    retained_policy = decode_continuation_policy(prepared_stack.admission_policy_json, prepared_stack.policy_hash)
    action = (
        retained_policy.actions.primary,
        retained_policy.actions.advisor,
        retained_policy.actions.primary,
        retained_policy.actions.title,
    )[family]
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation_id,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(
            next_action_json=action.model_dump_json(),
            policy_sha256=prepared_stack.policy_hash,
        )
    )
    return ContinuationProviderBinding(running, key, family, ordinal, input_sha256)


@dataclass(frozen=True, slots=True)
class ContinuationOwnerCommit:
    running: ComposerOperationRunning
    effect_key: str
    input_sha256: str
    expected_checkpoint_id: str
    expected_cursor: int


def require_owner_commit(
    conn: Connection,
    commit: ContinuationOwnerCommit,
    context: SessionOperationContext,
    *,
    allow_completed_read: bool = False,
) -> StoredContinuationSnapshot:
    if (
        type(commit) is not ContinuationOwnerCommit
        or type(commit.running) is not ComposerOperationRunning
        or type(context) is not SessionOperationContext
        or commit.running.session_operation_context != context
    ):
        raise AuditIntegrityError("SQL owner commit changed its actual execution context")
    if (
        require_continuation_mutation(
            conn,
            context,
            allow_cancel=allow_completed_read,
            allow_expired_segment=allow_completed_read,
        )
        is not True
    ):
        raise AuditIntegrityError("SQL owner commit requires current continuation authority")
    claim = commit.running.claim
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == context.fence.session_id,
            attempts.c.session_operation_epoch == context.fence.operation_epoch,
        )
    ).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == history.session_id,
            segments.c.operation_id == history.operation_id,
            segments.c.sequence == history.sequence,
            segments.c.generation == history.generation,
        )
    ).one()
    if (
        history.session_id,
        history.operation_id,
        history.sequence,
        history.generation,
        history.claim_attempt,
        segment.claim_token,
    ) != (
        str(claim.session_id),
        claim.operation_id,
        claim.segment_sequence,
        claim.ownership_generation,
        claim.attempt,
        claim.claim_token,
    ):
        raise AuditIntegrityError("SQL owner commit changed its current claim/history binding")
    stored = require_snapshot_cas(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        expected_checkpoint_id=commit.expected_checkpoint_id,
        expected_cursor=commit.expected_cursor,
    )
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == str(claim.session_id),
            effects.c.operation_id == claim.operation_id,
            effects.c.effect_key == commit.effect_key,
        )
    ).one()
    if allow_completed_read and effect.owner != "read":
        raise AuditIntegrityError("owed read-result application cannot authorize a business SQL effect")
    if effect.owner not in ("session_sql", "read") or effect.disposition != "prepared" or effect.input_sha256 != commit.input_sha256:
        raise ContinuationCASConflict("SQL owner result is already retained or its prepared authority changed")
    same_creator = (effect.creator_sequence, effect.creator_generation) == (claim.segment_sequence, claim.ownership_generation)
    if not same_creator:
        raise ContinuationCASConflict("business SQL effect changed its immutable preparing owner")
    return stored


def require_snapshot_cas(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
) -> StoredContinuationSnapshot:
    stored = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if stored.checkpoint_id != expected_checkpoint_id or stored.cursor != expected_cursor:
        raise ContinuationCASConflict("continuation snapshot changed before effect admission")
    return stored


@dataclass(frozen=True, slots=True)
class ValidatedHistoricalEffectReference:
    session_id: UUID
    operation_id: str
    effect_key: str
    input_sha256: str
    creator_operation_id: str
    creator_operation_epoch: int
    receipt_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str
    result_bytes: bytes


@dataclass(frozen=True, slots=True)
class HistoricalEffectLocator:
    effect_key: str
    input_sha256: str
    creator_operation_id: str
    receipt_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str


def _authored_row_hash(row: object) -> str:
    from elspeth.web.composer.turn_effects import authored_state_hash

    return authored_state_hash(state_from_record(state_record_from_row(row)))


def validate_historical_effect_reference(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    expected_input_sha256: str,
    expected_creator_operation_id: str,
    expected_receipt_id: str,
    expected_inserted_state_ids: tuple[UUID, ...],
    expected_authored_state_hash: str,
) -> ValidatedHistoricalEffectReference:
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == str(session_id),
            effects.c.operation_id == operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one_or_none()
    if effect is None or effect.owner != "session_sql" or effect.disposition != "committed":
        raise AuditIntegrityError("historical graph effect lacks a committed SQL owner")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(session_id),
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one_or_none()
    if creator is None or creator.session_operation_id != expected_creator_operation_id:
        raise AuditIntegrityError("historical graph effect creator mismatch")
    if effect.input_sha256 != expected_input_sha256 or effect.receipt_key != expected_receipt_id:
        raise AuditIntegrityError("historical graph effect input or receipt mismatch")
    if effect.committed_state_ids_json is None or effect.graph_sha256 != expected_authored_state_hash:
        raise AuditIntegrityError("historical graph effect lost its state frontier")
    ids, error = parse_json_strict(effect.committed_state_ids_json)
    if error is not None:
        raise AuditIntegrityError("historical graph effect state identities are not strict JSON")
    expected_ids = [str(state_id) for state_id in expected_inserted_state_ids]
    if type(ids) is not list or not ids or ids != expected_ids or len(set(ids)) != len(ids):
        raise AuditIntegrityError("historical graph effect state identities differ")
    last = None
    for state_id in ids:
        row = conn.execute(select(states).where(states.c.session_id == str(session_id), states.c.id == state_id)).one_or_none()
        if row is None or row.creator_operation_id != creator.session_operation_id:
            raise AuditIntegrityError("historical graph effect substituted another creator's state")
        last = row
    if last is None or _authored_row_hash(last) != expected_authored_state_hash:
        raise AuditIntegrityError("historical graph effect authored content mismatch")
    result = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=effect.result_chunk_key)
    return ValidatedHistoricalEffectReference(
        session_id,
        operation_id,
        effect_key,
        effect.input_sha256,
        creator.session_operation_id,
        creator.session_operation_epoch,
        effect.receipt_key,
        expected_inserted_state_ids,
        expected_authored_state_hash,
        result,
    )
