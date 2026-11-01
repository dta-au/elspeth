"""Current-fence retention of a control's original paid source and result.

This checkpoint is historical DATA, not a dispatch or successor capability.
It preserves the paid response cut; actual result capture and yield consumption
have separate live owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.tool_calls import PROVIDER_TOOL_CALL_ID_MAX_LENGTH
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_controls import (
    CONTINUATION_CONTROL_RESULT,
    ContinuationControlRefused,
    parse_continuation_control_batch,
)
from elspeth.web.composer.continuation_env_inventory import (
    ContinuationEnvSelection,
    CurrentContinuationEnvInventory,
    require_current_env_inventory_on_connection,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.continuation_primary import RetainedPrimaryPhaseData, read_retained_primary_phase_on_connection
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

if TYPE_CHECKING:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


class ContinuationControlIntentSource(ContinuationValue):
    schema_version: Literal["composer.control-intent.v1"] = "composer.control-intent.v1"
    previous_checkpoint_id: str = Field(min_length=1)
    previous_cursor: int = Field(ge=0)
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)
    provider_effect_key: str = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    provider_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_call_id: str = Field(min_length=1)
    response: PrivateChunkRef
    tool_call_id: str = Field(min_length=1, max_length=PROVIDER_TOOL_CALL_ID_MAX_LENGTH)
    arguments_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _ControlIntentPayload(ContinuationValue):
    owner: Literal["composer_control_intent"] = "composer_control_intent"
    source: ContinuationControlIntentSource
    result: str


@dataclass(frozen=True, slots=True)
class StoredContinuationControlIntent:
    """The original accepting checkpoint, never the reader's current head."""

    snapshot: StoredContinuationSnapshot
    source: ContinuationControlIntentSource
    result: str


def _current_paid_source(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    accepting: bool,
) -> tuple[StoredContinuationSnapshot, ComposeFrame, RetainedPrimaryPhaseData]:
    if type(running) is not ComposerOperationRunning or type(selected) is not SelectedContinuationState:
        raise TypeError("control intent requires its actual running and selected owners")
    _require_current_continuation_running(conn, running, allow_cancel=not accepting, allow_expired_segment=not accepting)
    selected.__post_init__()
    encoded = encode_continuation_snapshot(selected.stack)
    if (
        type(inventory) is not CurrentContinuationEnvInventory
        or inventory.running is not running
        or type(inventory.selection) is not ContinuationEnvSelection
        or inventory.selection != ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encoded)
        or type(inventory.names) is not frozenset
        or any(type(name) is not str for name in inventory.names)
    ):
        raise AuditIntegrityError("control intent changed its complete current inventory")
    if accepting:
        require_current_env_inventory_on_connection(
            conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
        )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    current = load_snapshot(conn, session_id=sid, operation_id=operation)
    if current.checkpoint_id != selected.checkpoint_id or current.payload_json != encoded:
        raise ContinuationCASConflict("control intent changed its complete selected predecessor")
    if load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation, selected=current) != selected.chunks:
        raise AuditIntegrityError("control intent changed its complete selected private source")
    frame = selected.stack.frames[-1]
    if (
        len(selected.stack.frames) != 1
        or type(frame) is not ComposeFrame
        or frame.phase != "response_retained"
        or frame.pending_response is None
        or frame.provider_effect_key is None
        or frame.provider_input_sha256 is None
    ):
        raise AuditIntegrityError("control intent requires its original committed PRIMARY response cut")
    paid = read_retained_primary_phase_on_connection(
        conn, running, selected=selected, reference=frame.pending_response, env_ref_names=inventory.names
    )
    return current, frame, paid


def _historical_snapshot(
    conn: Connection, *, session_id: str, operation_id: str, checkpoint_id: str
) -> tuple[StoredContinuationSnapshot, ContinuationFrameStack]:
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("control intent lost its accepting checkpoint")
    encoded = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=row.snapshot_chunk_key).decode(
        "utf-8", errors="strict"
    )
    stack = decode_continuation_snapshot(encoded)
    if (stack.logical.session_id, stack.logical.operation_id, stack.cursor) != (session_id, operation_id, row.cursor):
        raise AuditIntegrityError("control checkpoint changed its complete historical identity")
    return StoredContinuationSnapshot(encoded, row.cursor, row.checkpoint_id), stack


def _verify_receipt(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    frame: ComposeFrame,
    paid: RetainedPrimaryPhaseData,
    snapshot: StoredContinuationSnapshot,
    payload: _ControlIntentPayload,
) -> StoredContinuationControlIntent:
    source = payload.source
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    checkpoint_kind = conn.execute(
        select(checkpoints.c.kind).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == snapshot.checkpoint_id,
        )
    ).scalar_one_or_none()
    parent, parent_stack = _historical_snapshot(conn, session_id=sid, operation_id=operation, checkpoint_id=source.previous_checkpoint_id)
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == source.sequence,
            attempts.c.generation == source.generation,
        )
    ).one_or_none()
    try:
        intent = parse_continuation_control_batch(paid.outcome.completion.tool_batch)
    except ContinuationControlRefused as error:
        raise AuditIntegrityError("retained control intent no longer has its original accepted call") from error
    audit = paid.outcome.audit_call
    if (
        intent is None
        or audit is None
        or creator is None
        or checkpoint_kind != "transition"
        or source.sequence != running.claim.segment_sequence
        or source.generation > running.claim.ownership_generation
        or (source.generation < running.claim.ownership_generation and creator.revoked_at is None)
        or (source.creator_operation_id, source.creator_operation_epoch) != (creator.session_operation_id, creator.session_operation_epoch)
        or source.previous_cursor != parent.cursor
        or snapshot.cursor != parent.cursor + 1
        or parent_stack.model_copy(update={"cursor": selected.stack.cursor}) != selected.stack
        or source.provider_effect_key != frame.provider_effect_key
        or source.provider_input_sha256 != frame.provider_input_sha256
        or source.provider_call_id != audit.call_id
        or source.response != paid.response.reference
        or source.tool_call_id != intent.tool_call_id
        or source.arguments_sha256 != sha256(paid.outcome.completion.tool_batch.calls[0].function.arguments.encode("utf-8")).hexdigest()
        or payload.result != CONTINUATION_CONTROL_RESULT
    ):
        raise AuditIntegrityError("control intent changed its original accepting or paid source")
    return StoredContinuationControlIntent(snapshot, source, payload.result)


def _existing_intent(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    frame: ComposeFrame,
    paid: RetainedPrimaryPhaseData,
) -> tuple[StoredContinuationControlIntent | None, bool]:
    """Follow only unchanged same-sequence recovery cuts to the paid prefix."""
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    checkpoint_id = selected.checkpoint_id
    while True:
        snapshot, stack = _historical_snapshot(conn, session_id=sid, operation_id=operation, checkpoint_id=checkpoint_id)
        if stack.model_copy(update={"cursor": selected.stack.cursor}) != selected.stack:
            raise AuditIntegrityError("control intent recovery corridor changed its original complete DATA")
        row = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == snapshot.cursor)
        ).one_or_none()
        if row is None:
            raise AuditIntegrityError("control intent lost its checkpoint event")
        if row.tag == "checkpoint":
            value, parse_error = parse_json_strict(
                read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8")
            )
            if parse_error is not None or type(value) is not dict or "owner" not in value:
                raise AuditIntegrityError("control checkpoint payload is not its closed JSON DATA")
            if value["owner"] != "composer_control_intent":
                return None, False
            try:
                payload = _ControlIntentPayload.model_validate(value)
            except ValidationError as error:
                raise AuditIntegrityError("control checkpoint violates its closed source schema") from error
            return _verify_receipt(conn, running, selected=selected, frame=frame, paid=paid, snapshot=snapshot, payload=payload), False
        if row.tag == "response_retained":
            return None, True
        if row.tag != "recovered":
            return None, False
        previous = conn.execute(
            select(checkpoints.c.checkpoint_id)
            .where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor < snapshot.cursor)
            .order_by(checkpoints.c.cursor.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is None:
            raise AuditIntegrityError("control recovery corridor lost its original paid checkpoint")
        checkpoint_id = previous


def read_control_intent_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> StoredContinuationControlIntent | None:
    _current, frame, paid = _current_paid_source(conn, running, selected=selected, inventory=inventory, accepting=False)
    retained, _origin = _existing_intent(conn, running, selected=selected, frame=frame, paid=paid)
    return retained


def retain_control_intent(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> StoredContinuationControlIntent:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    if type(authority) is not ComposerAsyncOperationAuthority or type(running) is not ComposerOperationRunning:
        raise TypeError("control retention requires its actual authority and running owners")
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
        current, frame, paid = _current_paid_source(conn, running, selected=selected, inventory=inventory, accepting=True)
        existing, original_paid_cut = _existing_intent(conn, running, selected=selected, frame=frame, paid=paid)
        if existing is not None:
            return existing
        if not original_paid_cut:
            raise AuditIntegrityError("control acceptance cannot overwrite an unrelated checkpoint owner")
        intent = parse_continuation_control_batch(paid.outcome.completion.tool_batch)
        audit = paid.outcome.audit_call
        if (
            intent is None
            or audit is None
            or audit.call_id is None
            or not audit.call_id
            or frame.provider_effect_key is None
            or frame.provider_input_sha256 is None
        ):
            raise AuditIntegrityError("control acceptance lost its original sole paid control")
        fence = running.session_operation_context.fence
        source = ContinuationControlIntentSource(
            previous_checkpoint_id=current.checkpoint_id,
            previous_cursor=current.cursor,
            sequence=running.claim.segment_sequence,
            generation=running.claim.ownership_generation,
            creator_operation_id=fence.operation_id,
            creator_operation_epoch=fence.operation_epoch,
            provider_effect_key=frame.provider_effect_key,
            provider_input_sha256=frame.provider_input_sha256,
            provider_call_id=audit.call_id,
            response=paid.response.reference,
            tool_call_id=intent.tool_call_id,
            arguments_sha256=sha256(paid.outcome.completion.tool_batch.calls[0].function.arguments.encode("utf-8")).hexdigest(),
        )
        payload = _ControlIntentPayload(source=source, result=CONTINUATION_CONTROL_RESULT)
        retained = append_complete_snapshot(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=current.payload_json,
            event_batch=(ContinuationEvent("checkpoint", canonical_json(payload.model_dump(mode="json"))),),
            handoff=False,
        )
        following = SelectedContinuationState(retained.checkpoint_id, decode_continuation_snapshot(retained.payload_json), selected.chunks)
        verified, _origin = _existing_intent(conn, running, selected=following, frame=frame, paid=paid)
        if verified is None or verified.snapshot != retained or verified.source != source or verified.result != CONTINUATION_CONTROL_RESULT:
            raise AuditIntegrityError("control retention lost its actual complete committed DATA")
        return verified
