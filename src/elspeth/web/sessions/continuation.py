"""Private transaction-local bytes and complete continuation snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Row, func, insert, select, update
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.continuation import (
    ContinuationFrameStack,
    InitializeFrame,
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
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest
from elspeth.web.sessions.state_decoder import state_record_from_row
from elspeth.web.sessions.time_normalization import restore_utc

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
    now = database_now(conn)
    checkpoint_id = str(uuid4())
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
    snapshot_json = encode_continuation_snapshot(frame_stack.model_copy(update={"cursor": cursor}))
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
