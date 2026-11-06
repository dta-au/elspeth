"""Historical semantic DATA across exact same-sequence recovery transitions.

This reader grants no current claim, fence, dispatch or writer authority. Its
callers must separately prove current ownership/CAS and the original semantic
producer. Only actual consecutive recovered events can precede that producer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame, ContinuationValue
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, read_chunk
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


class _RecoveredSemanticEvent(ContinuationValue):
    version: Literal[2]
    same_sequence: int = Field(gt=0)
    previous_generation: int = Field(gt=0)
    generation: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class SemanticRecoverySource:
    """A proved historical source and its optional contiguous generation span."""

    snapshot: StoredContinuationSnapshot
    sequence: int | None
    previous_generation: int | None
    generation: int | None


def semantic_recovery_source(conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot) -> SemanticRecoverySource:
    # Local import avoids a module cycle; this is the existing strict immutable
    # checkpoint/codec/digest reader, not the current ownership admission path.
    from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot

    if type(selected) is not StoredContinuationSnapshot:
        raise TypeError("semantic recovery requires its nominal selected DATA")
    actual = _historical_snapshot(conn, sid, operation, checkpoint_id=selected.checkpoint_id)
    if actual != selected:
        raise AuditIntegrityError("semantic recovery substituted its complete selected checkpoint")
    current = selected
    sequence: int | None = None
    first_generation: int | None = None
    last_generation: int | None = None
    while True:
        event = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == current.cursor)
        ).one_or_none()
        if event is None:
            raise AuditIntegrityError("semantic recovery lost its exact current event")
        if event.tag != "recovered":
            return SemanticRecoverySource(current, sequence, first_generation, last_generation)
        codec = conn.execute(
            select(chunks.c.codec).where(
                chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == event.payload_chunk_key
            )
        ).scalar_one_or_none()
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
        try:
            recovered = _RecoveredSemanticEvent.model_validate_json(text, strict=True)
        except ValidationError as error:
            raise AuditIntegrityError("semantic recovery lost its closed generation event") from error
        root = decode_continuation_snapshot(current.payload_json)
        compose = root.frames[0]
        if (
            codec != "event.v2"
            or event.payload_chunk_key != f"event:{current.cursor}"
            or text != canonical_json(recovered.model_dump(mode="json"))
            or recovered.generation != recovered.previous_generation + 1
            or type(compose) is not ComposeFrame
            or compose.slice_sequence != recovered.same_sequence
            or (sequence is not None and sequence != recovered.same_sequence)
            or (first_generation is not None and first_generation != recovered.generation)
        ):
            raise AuditIntegrityError("semantic recovery changed its exact sequence/generation chain")
        checkpoint = conn.execute(
            select(checkpoints.c.kind).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.checkpoint_id == current.checkpoint_id,
            )
        ).scalar_one_or_none()
        segment = conn.execute(
            select(segments.c.generation).where(
                segments.c.session_id == sid,
                segments.c.operation_id == operation,
                segments.c.sequence == recovered.same_sequence,
            )
        ).scalar_one_or_none()
        predecessor = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == recovered.same_sequence,
                attempts.c.generation == recovered.previous_generation,
            )
        ).one_or_none()
        successor = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == recovered.same_sequence,
                attempts.c.generation == recovered.generation,
            )
        ).one_or_none()
        if (
            checkpoint != "transition"
            or segment is None
            or segment < recovered.generation
            or predecessor is None
            or successor is None
            or predecessor.revoked_at is None
            or predecessor.outcome != "interrupted"
            or successor.claim_attempt <= predecessor.claim_attempt
            or successor.session_operation_epoch <= predecessor.session_operation_epoch
            or successor.session_operation_id == predecessor.session_operation_id
            or successor.session_operation_lease_token == predecessor.session_operation_lease_token
        ):
            raise AuditIntegrityError("semantic recovery lost its actual interrupted/reclaimed attempt pair")
        before = _historical_snapshot(conn, sid, operation, cursor=current.cursor - 1)
        before_root = decode_continuation_snapshot(before.payload_json)
        if current.payload_json != encode_continuation_snapshot(before_root.model_copy(update={"cursor": current.cursor})):
            raise AuditIntegrityError("semantic recovery changed its original root beyond the journal cursor")
        sequence = recovered.same_sequence
        first_generation = recovered.previous_generation
        if last_generation is None:
            last_generation = recovered.generation
        current = before
