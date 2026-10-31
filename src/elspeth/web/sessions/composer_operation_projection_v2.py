"""Read-only Composer-Operation-Version 2 projections of a lifecycle-2 operation.

The parent is read through ``ComposerAsyncOperationAuthority`` (which validates
the record as Tier 1). The current segment is read here by its exact
``(sequence, generation)``. A transition that commits between the two reads
moves the parent's segment pointer or generation, so the pair is re-read
until it is consistent. This module never writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, TypedDict
from uuid import UUID

from sqlalchemy import Engine, Row, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.composer_operations import (
    ComposerOperationError,
    ComposerOperationKind,
    ComposerOperationRecord,
    ComposerOperationStatus,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.schemas import (
    ComposerActiveOperationResponse,
    ComposerOperationPhaseV2,
    ComposerOperationSegmentV2,
    ComposerOperationSnapshotV2,
    ComposerOperationStatusV2,
    MessageWithStateResponse,
)
from elspeth.web.sessions.time_normalization import restore_utc

_CONSISTENT_READ_ATTEMPTS: Final = 5


class _StatusFields(TypedDict):
    operation_id: str
    kind: ComposerOperationKind
    status: ComposerOperationStatus
    phase: ComposerOperationPhaseV2
    status_revision: int
    created_at: datetime
    observed_at: datetime
    poll_after_ms: int
    cancel_requested: bool
    segment: ComposerOperationSegmentV2 | None


class ComposerOperationProjectionUnstable(Exception):
    """The parent kept moving across every bounded attempt; the caller may retry."""


@dataclass(frozen=True, slots=True)
class ComposerOperationProjectionInputs:
    record: ComposerOperationRecord | None
    segment: ComposerOperationSegmentV2 | None
    database_now: datetime


def _read_segment_row(engine: Engine, *, session_id: UUID, operation_id: str, sequence: int, generation: int) -> Row[Any] | None:
    with engine.connect() as conn:
        return conn.execute(
            select(segments.c.state, segments.c.deadline_at).where(
                segments.c.session_id == str(session_id),
                segments.c.operation_id == operation_id,
                segments.c.sequence == sequence,
                segments.c.generation == generation,
            )
        ).one_or_none()


def _live_segment(row: Row[Any] | None, *, sequence: int, generation: int) -> ComposerOperationSegmentV2:
    if row is None:
        raise AuditIntegrityError("Live v2 parent points at a segment that does not exist")
    state: Literal["queued", "running", "interrupted"]
    match row.state:
        case "queued":
            state = "queued"
        case "running":
            state = "running"
        case "interrupted":
            state = "interrupted"
        case _:
            raise AuditIntegrityError("Live v2 parent points at a segment that is not live")
    deadline = restore_utc(row.deadline_at) if state == "running" and row.deadline_at is not None else None
    return ComposerOperationSegmentV2(sequence=sequence, generation=generation, state=state, deadline_at=deadline)


def _identity(record: ComposerOperationRecord) -> tuple[object, ...]:
    return (record.status, record.status_revision, record.current_segment_sequence, record.ownership_generation)


def read_v2_projection_inputs(
    authority: ComposerAsyncOperationAuthority, engine: Engine, *, session_id: UUID, operation_id: str
) -> ComposerOperationProjectionInputs:
    """Return a consistent parent, current segment and database time.

    The segment is read between two parent reads. Every segment state or
    deadline change commits together with a parent ``status_revision`` bump,
    so equal revisions and pointers on both sides prove the segment belongs to
    that revision. Otherwise the triple is re-read. A missing, v1 or terminal
    parent is returned with no segment.
    """
    for _ in range(_CONSISTENT_READ_ATTEMPTS):
        before, _now = authority.get_with_database_now(session_id=session_id, operation_id=operation_id)
        if before is None or before.lifecycle_version != 2 or before.status in ("completed", "failed"):
            return ComposerOperationProjectionInputs(before, None, _now)
        sequence, generation = before.current_segment_sequence, before.ownership_generation
        if sequence is None or generation is None:
            raise AuditIntegrityError("Live v2 operation has no segment pointer")
        row = _read_segment_row(engine, session_id=session_id, operation_id=operation_id, sequence=sequence, generation=generation)
        after, now = authority.get_with_database_now(session_id=session_id, operation_id=operation_id)
        if after is None or after.lifecycle_version != 2 or after.status in ("completed", "failed"):
            return ComposerOperationProjectionInputs(after, None, now)
        if _identity(after) != _identity(before):
            continue
        return ComposerOperationProjectionInputs(after, _live_segment(row, sequence=sequence, generation=generation), now)
    raise ComposerOperationProjectionUnstable


def _status_fields(inputs: ComposerOperationProjectionInputs, *, poll_after_ms: int) -> _StatusFields:
    record = inputs.record
    if record is None or record.lifecycle_version != 2 or record.phase is None:
        raise AuditIntegrityError("v2 projection requested for a non-v2 operation")
    return _StatusFields(
        operation_id=record.operation_id,
        kind=record.kind,
        status=record.status,
        phase=record.phase,
        status_revision=record.status_revision,
        created_at=restore_utc(record.created_at),
        observed_at=restore_utc(inputs.database_now),
        poll_after_ms=poll_after_ms,
        cancel_requested=record.cancel_requested_at is not None and record.status != "completed",
        segment=inputs.segment,
    )


def project_v2_status(inputs: ComposerOperationProjectionInputs, *, poll_after_ms: int) -> ComposerOperationStatusV2:
    return ComposerOperationStatusV2(**_status_fields(inputs, poll_after_ms=poll_after_ms))


def project_v2_snapshot(inputs: ComposerOperationProjectionInputs, *, poll_after_ms: int) -> ComposerOperationSnapshotV2:
    fields = _status_fields(inputs, poll_after_ms=poll_after_ms)
    record = inputs.record
    if record is None:
        raise AuditIntegrityError("v2 projection lost its parent")
    result: MessageWithStateResponse | None = None
    error: ComposerOperationError | None = None
    if record.status in ("completed", "failed"):
        if record.result_json is None:
            raise AuditIntegrityError("Terminal operation has no sealed result")
        if record.status == "completed":
            result = MessageWithStateResponse.model_validate_json(record.result_json, strict=True)
        else:
            error = ComposerOperationError.model_validate_json(record.result_json, strict=True)
    return ComposerOperationSnapshotV2(**fields, result=result, error=error)


def read_active_operation(engine: Engine, *, session_id: UUID, actor_user_id: str) -> ComposerActiveOperationResponse | None:
    """The actor's live operation in the session, in any lifecycle and phase, or None.

    The parent row answers in every phase, including between v2 segments when
    no worker publishes progress.
    """
    with engine.connect() as conn:
        row = conn.execute(
            select(jobs.c.operation_id, jobs.c.kind, jobs.c.lifecycle_version, jobs.c.status)
            .where(
                jobs.c.session_id == str(session_id),
                jobs.c.actor_user_id == actor_user_id,
                jobs.c.status.in_(("queued", "running")),
            )
            .order_by(jobs.c.created_at)
            .limit(1)
        ).one_or_none()
    if row is None:
        return None
    return ComposerActiveOperationResponse(
        operation_id=row.operation_id, kind=row.kind, lifecycle_version=row.lifecycle_version, status=row.status
    )
