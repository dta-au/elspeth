"""Exact one-transaction V4 TITLE complete-result capacity custody."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_title_values import CapturedTitleProviderResult
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    _effect_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    admit_retained_payload_chunks,
)
from elspeth.web.sessions.continuation_title_provider import (
    PreparedTitleProvider,
    TitleTransition,
    V4TitleProviderAdmission,
    _event,
    _frame,
    require_title_creator,
)

_SCOPE_KEY = "elspeth.v4_title_complete_tail"


@dataclass(frozen=True, slots=True)
class V4TitleCompleteBatch:
    source: ProviderTerminalHoldSource
    admitted: StoredContinuationSnapshot
    result_chunk: ProspectiveContinuationChunk
    received: PreparedContinuationAppend
    settled: PreparedContinuationAppend
    prospective: tuple[ProspectiveContinuationChunk, ...]

    @property
    def final(self) -> StoredContinuationSnapshot:
        return StoredContinuationSnapshot(self.settled.snapshot_json, self.settled.expected_cursor + 1, self.settled.checkpoint_id)


def _title_event(
    prepared: PreparedTitleProvider, selected: StoredContinuationSnapshot, stage: Literal["received", "settled"]
) -> ContinuationEvent:
    sequence, generation = require_title_creator(prepared.running)
    return ContinuationEvent(
        "checkpoint",
        _event(
            TitleTransition(
                stage=stage,
                predecessor_checkpoint_id=selected.checkpoint_id,
                predecessor_cursor=selected.cursor,
                input=prepared.input.reference,
                creator_sequence=sequence,
                creator_generation=generation,
            )
        ),
    )


def build_v4_title_complete_batch(
    *,
    prepared: PreparedTitleProvider,
    admission: V4TitleProviderAdmission,
    captured: CapturedTitleProviderResult,
    published_at: datetime,
) -> V4TitleCompleteBatch:
    """Prebuild both existing normal TITLE transitions before the first write."""
    if (
        type(prepared) is not PreparedTitleProvider
        or type(admission) is not V4TitleProviderAdmission
        or type(captured) is not CapturedTitleProviderResult
        or type(published_at) is not datetime
        or published_at.tzinfo is None
    ):
        raise TypeError("V4 TITLE complete batch requires original nominal inputs and aware database time")
    source, admitted = admission.hold, admission.selected
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if (
        source.session_id != sid
        or source.operation_id != operation
        or source.effect_key != "provider:3:0"
        or admission.tail.identity.attempt_id != admission.attempt.attempt_id
        or admitted != admission.tail.admitted
        or captured.audit_call.call_id != admission.attempt.attempt_id
        or captured.snapshot.effect_key != source.effect_key
        or captured.snapshot.input_sha256 != source.source_input_sha256
    ):
        raise AuditIntegrityError("V4 TITLE complete batch changed its original admitted producer")
    root = decode_continuation_snapshot(admitted.payload_json)
    frame = _frame(admitted)
    if frame.title_attempt_id != admission.attempt.attempt_id or frame.title_response is not None:
        raise AuditIntegrityError("V4 TITLE complete batch lost its unresolved admitted frame")
    receipt = f"title:{captured.chunk.reference.digest}"
    following = root.model_copy(update={"frames": (frame.model_copy(update={"title_response": captured.chunk.reference}),)})
    received_json = _effect_snapshot(
        encode_continuation_snapshot(following), effect_key=source.effect_key, disposition="committed", receipt_id=receipt
    )
    received = prepare_snapshot_append(
        admitted, snapshot_json=received_json, events=(_title_event(prepared, admitted, "received"),), published_at=published_at
    )
    received_selected = StoredContinuationSnapshot(received.snapshot_json, admitted.cursor + 1, received.checkpoint_id)
    settled_json = _effect_snapshot(
        received.snapshot_json,
        effect_key=source.effect_key,
        disposition="committed",
        receipt_id=receipt,
        remove_attempt_id=admission.attempt.attempt_id,
    )
    settled = prepare_snapshot_append(
        received_selected,
        snapshot_json=settled_json,
        events=(_title_event(prepared, received_selected, "settled"),),
        published_at=published_at,
    )
    result = ProspectiveContinuationChunk(
        private_chunk_key(captured.chunk), captured.chunk.canonical.encode("utf-8"), captured.chunk.reference.codec
    )
    prospective = (
        result,
        ProspectiveContinuationChunk(f"event:{admitted.cursor + 1}", received.events[0].payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{received.checkpoint_id}", received.snapshot_json.encode("utf-8"), "snapshot.v2"),
        ProspectiveContinuationChunk(f"event:{admitted.cursor + 2}", settled.events[0].payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{settled.checkpoint_id}", settled.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    return V4TitleCompleteBatch(source, admitted, result, received, settled, prospective)


def require_v4_title_complete_batch_matches_capture(
    *,
    prepared: PreparedTitleProvider,
    admission: V4TitleProviderAdmission,
    captured: CapturedTitleProviderResult,
    batch: V4TitleCompleteBatch,
) -> None:
    """Bind a candidate's exact bytes to the normal two TITLE transitions.

    The checkpoint IDs are the candidate's generated IDs; every other field
    is rederived from the original admission and complete paid result.
    """
    if type(batch) is not V4TitleCompleteBatch:
        raise TypeError("V4 TITLE complete candidate requires its owned batch")
    for checkpoint_id in (batch.received.checkpoint_id, batch.settled.checkpoint_id):
        try:
            valid_uuid = str(UUID(checkpoint_id)) == checkpoint_id and UUID(checkpoint_id).version == 4
        except (TypeError, ValueError, AttributeError):
            valid_uuid = False
        if not valid_uuid:
            raise AuditIntegrityError("V4 TITLE complete candidate changed its generated checkpoint identity")
    if batch.received.checkpoint_id == batch.settled.checkpoint_id:
        raise AuditIntegrityError("V4 TITLE complete candidate reused its checkpoint identity")
    batch.received.require_original()
    batch.settled.require_original()
    expected = build_v4_title_complete_batch(
        prepared=prepared, admission=admission, captured=captured, published_at=batch.received.published_at
    )
    received = replace(expected.received, checkpoint_id=batch.received.checkpoint_id, original_values=batch.received.original_values)
    if (
        batch.source != expected.source
        or batch.admitted != expected.admitted
        or batch.result_chunk != expected.result_chunk
        or batch.received != received
    ):
        raise AuditIntegrityError("V4 TITLE complete candidate changed its received result or original transition")
    selected = StoredContinuationSnapshot(received.snapshot_json, received.expected_cursor + 1, received.checkpoint_id)
    settled = prepare_snapshot_append(
        selected,
        snapshot_json=_effect_snapshot(
            received.snapshot_json,
            effect_key=batch.source.effect_key,
            disposition="committed",
            receipt_id=f"title:{captured.chunk.reference.digest}",
            remove_attempt_id=admission.attempt.attempt_id,
        ),
        events=(_title_event(prepared, selected, "settled"),),
        published_at=received.published_at,
    )
    settled = replace(settled, checkpoint_id=batch.settled.checkpoint_id, original_values=batch.settled.original_values)
    prospective = (
        batch.result_chunk,
        ProspectiveContinuationChunk(f"event:{batch.admitted.cursor + 1}", received.events[0].payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{received.checkpoint_id}", received.snapshot_json.encode("utf-8"), "snapshot.v2"),
        ProspectiveContinuationChunk(f"event:{batch.admitted.cursor + 2}", settled.events[0].payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{settled.checkpoint_id}", settled.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    if batch.settled != settled or batch.prospective != prospective:
        raise AuditIntegrityError("V4 TITLE complete candidate changed its settled transition or retained bytes")


@dataclass(slots=True)
class _CompleteScope:
    connection: Connection
    transaction: RootTransaction
    batch: V4TitleCompleteBatch
    stage: int = 0


def _current(conn: Connection, session_id: str, operation_id: str) -> _CompleteScope | None:
    scope = conn.info.get(_SCOPE_KEY)
    if scope is None:
        return None
    if (
        type(scope) is not _CompleteScope
        or scope.connection is not conn
        or scope.transaction is not conn.get_transaction()
        or not scope.transaction.is_active
        or (scope.batch.source.session_id, scope.batch.source.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("V4 complete TITLE tail crossed its original transaction or operation")
    return scope


def complete_tail_credit_for_chunk(
    conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    if scope.stage != 0 or candidate != scope.batch.result_chunk:
        raise AuditIntegrityError("V4 complete TITLE chunk differs from its exact admitted result")
    return scope.batch.source


def record_complete_tail_chunk(conn: Connection, *, session_id: str, operation_id: str, candidate: ProspectiveContinuationChunk) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        complete_tail_credit_for_chunk(conn, session_id=session_id, operation_id=operation_id, candidate=candidate)
        scope.stage = 1


def complete_tail_credit_for_append(
    conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None
) -> ProviderTerminalHoldSource | None:
    scope = _current(conn, session_id, operation_id)
    if scope is None:
        return None
    expected = scope.batch.received if scope.stage == 1 else scope.batch.settled if scope.stage == 2 else None
    if append is None or append != expected:
        raise AuditIntegrityError("V4 complete TITLE append differs from its exact ordered batch")
    append.require_original()
    return scope.batch.source


def record_complete_tail_append(conn: Connection, *, session_id: str, operation_id: str, append: PreparedContinuationAppend | None) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is not None:
        complete_tail_credit_for_append(conn, session_id=session_id, operation_id=operation_id, append=append)
        scope.stage += 1


def record_complete_tail_hold_consumed(conn: Connection, *, session_id: str, operation_id: str, source: ProviderTerminalHoldSource) -> None:
    scope = _current(conn, session_id, operation_id)
    if scope is None or scope.stage != 3 or source != scope.batch.source:
        raise AuditIntegrityError("V4 complete TITLE hold was not consumed by its exact batch")
    scope.stage = 4


def preflight_v4_title_complete_batch_on_connection(conn: Connection, *, batch: V4TitleCompleteBatch) -> None:
    """Admit the whole result with original H credit before any success DML."""
    if type(batch) is not V4TitleCompleteBatch:
        raise TypeError("V4 TITLE complete preflight requires its owned batch")
    source = batch.source
    if load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != batch.admitted:
        raise AuditIntegrityError("V4 complete TITLE tail changed its original admitted head")
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if hold.state != "active" or hold.provider_attempt_id is None:
        raise AuditIntegrityError("V4 complete TITLE tail lost its active original paid hold")
    batch.received.require_original()
    batch.settled.require_original()
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )


@contextmanager
def admit_v4_title_complete_tail_on_connection(conn: Connection, *, batch: V4TitleCompleteBatch) -> Iterator[None]:
    """Replace one active H with the exact complete result and two normal roots."""
    if type(batch) is not V4TitleCompleteBatch or _SCOPE_KEY in conn.info:
        raise AuditIntegrityError("V4 complete TITLE tail requires its sole nominal batch")
    transaction = conn.get_transaction()
    if type(transaction) is not RootTransaction or not transaction.is_active:
        raise AuditIntegrityError("V4 complete TITLE tail requires its original root transaction")
    source = batch.source
    if load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != batch.admitted:
        raise AuditIntegrityError("V4 complete TITLE tail changed its original admitted head")
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if hold.state != "active" or hold.provider_attempt_id is None:
        raise AuditIntegrityError("V4 complete TITLE tail lost its active original paid hold")
    batch.received.require_original()
    batch.settled.require_original()
    if batch.prospective != (
        batch.result_chunk,
        ProspectiveContinuationChunk(
            f"event:{batch.admitted.cursor + 1}", batch.received.events[0].payload_json.encode("utf-8"), "event.v2"
        ),
        ProspectiveContinuationChunk(
            f"snapshot:{batch.received.checkpoint_id}", batch.received.snapshot_json.encode("utf-8"), "snapshot.v2"
        ),
        ProspectiveContinuationChunk(
            f"event:{batch.admitted.cursor + 2}", batch.settled.events[0].payload_json.encode("utf-8"), "event.v2"
        ),
        ProspectiveContinuationChunk(f"snapshot:{batch.settled.checkpoint_id}", batch.settled.snapshot_json.encode("utf-8"), "snapshot.v2"),
    ):
        raise AuditIntegrityError("V4 complete TITLE tail changed an immutable key or byte")
    admit_retained_payload_chunks(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        prospective=batch.prospective,
        retained_payload_limit_bytes=source.allowance_bytes,
        consuming_hold=source,
    )
    scope = _CompleteScope(conn, transaction, batch)
    conn.info[_SCOPE_KEY] = scope
    try:
        yield
        final_hold = _require_hold_fields(conn, source=source, contract=contract)
        if (
            scope.stage != 4
            or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != batch.final
            or final_hold.state != "consumed"
            or final_hold.provider_attempt_id != hold.provider_attempt_id
        ):
            raise AuditIntegrityError("V4 complete TITLE tail did not consume its full result, roots and hold")
    except BaseException:
        transaction.rollback()
        raise
    finally:
        if conn.info.pop(_SCOPE_KEY, None) is not scope:
            raise AuditIntegrityError("V4 complete TITLE tail lost its original transaction scope")
