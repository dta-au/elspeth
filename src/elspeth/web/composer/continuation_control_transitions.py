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
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
)
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
from elspeth.web.composer.continuation_message_family_recipes import ControlResultMessageRecipe
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
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_primary import (
    RetainedPrimaryPhaseData,
    read_committed_primary_response_data_on_connection,
    read_retained_primary_phase_on_connection,
)
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
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
        or frame.slice_sequence != running.claim.segment_sequence
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


class _ControlCompletionPayload(ContinuationValue):
    owner: Literal["composer_control_completion"] = "composer_control_completion"
    previous_checkpoint_id: str = Field(min_length=1)
    previous_cursor: int = Field(ge=0)
    intent_checkpoint_id: str | None = Field(default=None, min_length=1)
    response: PrivateChunkRef
    sequence: int = Field(ge=1)
    generation: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)


type ControlResultToolMessage = dict[str, str]


def _checkpoint_payload(
    conn: Connection,
    running: ComposerOperationRunning,
    snapshot: StoredContinuationSnapshot,
    *,
    invalid_message: str,
    expected_completion: bool = False,
) -> _ControlIntentPayload | _ControlCompletionPayload | None:
    row = conn.execute(
        select(events).where(
            events.c.session_id == str(running.claim.session_id),
            events.c.operation_id == running.claim.operation_id,
            events.c.sequence == snapshot.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("control source lost its actual checkpoint event")
    value, error = parse_json_strict(
        read_chunk(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=row.payload_chunk_key
        ).decode("utf-8")
    )
    if error is not None or type(value) is not dict or "owner" not in value:
        raise AuditIntegrityError("control checkpoint lost its complete closed payload")
    if expected_completion and value["owner"] != "composer_control_completion":
        return None
    try:
        if value["owner"] == "composer_control_intent":
            return _ControlIntentPayload.model_validate(value)
        if value["owner"] == "composer_control_completion":
            return _ControlCompletionPayload.model_validate(value)
    except ValidationError as error:
        raise AuditIntegrityError(invalid_message) from error
    return None


def control_result_message_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    recipe: ControlResultMessageRecipe,
) -> ControlResultToolMessage:
    """Historical result DATA after the caller's current SQL admission proof.

    A normal tool's issued/P4 custody never substitutes for this source.
    Original paid publication and, for acceptance, the actual intent journal
    are independently rechecked even after later provider work or a yield.
    """
    response = read_committed_primary_response_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, selected=selected, reference=recipe.response
    )
    completion = decode_continuation_response(response)
    if recipe.tool_index >= len(completion.tool_batch.calls):
        raise AuditIntegrityError("control result changed its original call position")
    try:
        intent = parse_continuation_control_batch(completion.tool_batch)
    except ContinuationControlRefused as refusal:
        if recipe.intent_checkpoint_id is not None:
            raise AuditIntegrityError("refused control acquired an accepted intent") from refusal
        content = canonical_json(refusal.to_tool_payload())
    else:
        if intent is None or recipe.intent_checkpoint_id is None or recipe.tool_index != 0:
            raise AuditIntegrityError("control result has no original accepted intent")
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        snapshot, stack = _historical_snapshot(conn, session_id=sid, operation_id=operation, checkpoint_id=recipe.intent_checkpoint_id)
        payload = _checkpoint_payload(conn, running, snapshot, invalid_message="control result changed its closed historical intent")
        if type(payload) is not _ControlIntentPayload:
            raise AuditIntegrityError("control result changed its closed historical intent")
        source = payload.source
        parent, parent_stack = _historical_snapshot(
            conn, session_id=sid, operation_id=operation, checkpoint_id=source.previous_checkpoint_id
        )
        kind = conn.execute(
            select(checkpoints.c.kind).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation,
                checkpoints.c.checkpoint_id == snapshot.checkpoint_id,
            )
        ).scalar_one_or_none()
        creator = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation,
                attempts.c.sequence == source.sequence,
                attempts.c.generation == source.generation,
            )
        ).one_or_none()
        frame = stack.frames[-1]
        audit = decode_retained_physical_audit_call(response)
        if (
            len(stack.frames) != 1
            or type(frame) is not ComposeFrame
            or frame.phase != "response_retained"
            or kind != "transition"
            or frame.pending_response != recipe.response
            or source.response != recipe.response
            or source.provider_effect_key != frame.provider_effect_key
            or source.provider_input_sha256 != frame.provider_input_sha256
            or source.provider_call_id != audit.call_id
            or source.tool_call_id != intent.tool_call_id
            or source.arguments_sha256 != sha256(completion.tool_batch.calls[0].function.arguments.encode("utf-8")).hexdigest()
            or source.sequence != frame.slice_sequence
            or source.sequence > running.claim.segment_sequence
            or source.generation > running.claim.ownership_generation
            or creator is None
            or (source.creator_operation_id, source.creator_operation_epoch)
            != (creator.session_operation_id, creator.session_operation_epoch)
            or (
                (source.sequence, source.generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
                and creator.revoked_at is None
            )
            or source.previous_cursor != parent.cursor
            or snapshot.cursor != parent.cursor + 1
            or parent_stack.model_copy(update={"cursor": stack.cursor}) != stack
            or payload.result != CONTINUATION_CONTROL_RESULT
        ):
            raise AuditIntegrityError("control result changed its original intent, paid bytes or accepting creator")
        content = payload.result
    return {"role": "tool", "tool_call_id": completion.tool_batch.calls[recipe.tool_index].id, "content": content}


def retain_control_completion(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    *,
    captured: CapturedContinuationRuntime,
    inventory: CurrentContinuationEnvInventory,
    intent: StoredContinuationControlIntent | None,
) -> StoredContinuationSnapshot:
    """Publish actual control messages before exposing a quiescent PRIMARY cut."""
    from elspeth.web.composer.continuation_message_family_builders import family_message_wire, native_assistant_message
    from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime

    if type(captured) is not CapturedContinuationRuntime:
        raise TypeError("control completion requires its actual live-owner capture")
    selected = captured.selected
    with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
        current, before, paid = _current_paid_source(conn, running, selected=selected, inventory=inventory, accepting=True)
        existing, original_paid_cut = _existing_intent(conn, running, selected=selected, frame=before, paid=paid)
        if existing != intent:
            raise AuditIntegrityError("control completion replaced its actual accepted intent")
        if intent is None and not original_paid_cut:
            raise AuditIntegrityError("control refusal cannot overwrite an unrelated checkpoint owner")
        if len(captured.stack.frames) != 1 or type(captured.stack.frames[0]) is not ComposeFrame:
            raise AuditIntegrityError("control completion cannot carry an ordinary issued batch")
        after = captured.stack.frames[0]
        retained_fields = {
            "messages": before.messages,
            "evidence": before.evidence,
            "transcript": before.transcript,
            "caches": before.caches,
            "iteration": before.iteration,
        }
        if (
            captured.stack.model_copy(update={"frames": selected.stack.frames}) != selected.stack
            or after.model_copy(update=retained_fields) != before
        ):
            raise AuditIntegrityError("control completion changed graph, paid recorder, request counters or immutable runtime")
        if before.pending_response is None:
            raise AuditIntegrityError("control completion lost its original paid response")
        # Capture the original assistant and every constant/refusal in order;
        # a checkpoint flag without their actual shelf is never sufficient.
        expected_messages = [family_message_wire(native_assistant_message(paid.outcome.completion))]
        expected_recipes: list[ControlResultMessageRecipe] = []
        for index in range(len(paid.outcome.completion.tool_batch.calls)):
            recipe = ControlResultMessageRecipe(
                response=before.pending_response,
                tool_index=index,
                intent_checkpoint_id=None if intent is None else intent.snapshot.checkpoint_id,
            )
            expected_recipes.append(recipe)
            expected_messages.append(
                family_message_wire(control_result_message_on_connection(conn, running, selected=selected, recipe=recipe))
            )
        shelves = tuple(chunk for chunk in captured.chunks if chunk.reference == after.transcript)
        originals = tuple(chunk for chunk in selected.chunks if chunk.reference == before.transcript)
        if len(shelves) != 1 or len(originals) != 1:
            raise AuditIntegrityError("control completion lost its complete actual transcript shelves")
        shelf, original_shelf = decode_message_shelf(shelves[0]), decode_message_shelf(originals[0])
        prior_messages = tuple(original_shelf.messages_json[index] for index in original_shelf.current_indexes)
        actual_messages = tuple(shelf.messages_json[index] for index in shelf.current_indexes)
        if actual_messages != (*prior_messages, *expected_messages) or not shelf.producer_recipes:
            raise AuditIntegrityError("control completion did not retain its exact original assistant and every result")
        current_recipes = tuple(shelf.producer_recipes[index] for index in shelf.current_indexes)
        if current_recipes[-len(expected_recipes) :] != tuple(expected_recipes):
            raise AuditIntegrityError("control completion lost its exact registered result producer sources")
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        following = after.model_copy(
            update={"phase": "action_ready", "pending_response": None, "provider_effect_key": None, "provider_input_sha256": None}
        )
        payload = _ControlCompletionPayload(
            previous_checkpoint_id=current.checkpoint_id,
            previous_cursor=current.cursor,
            intent_checkpoint_id=None if intent is None else intent.snapshot.checkpoint_id,
            response=before.pending_response,
            sequence=running.claim.segment_sequence,
            generation=running.claim.ownership_generation,
            creator_operation_id=running.session_operation_context.fence.operation_id,
            creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
        )
        return append_complete_snapshot(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=current.checkpoint_id,
            expected_cursor=current.cursor,
            snapshot_json=encode_continuation_snapshot(captured.stack.model_copy(update={"frames": (following,)})),
            event_batch=(ContinuationEvent("checkpoint", canonical_json(payload.model_dump(mode="json"))),),
            handoff=False,
        )


def read_pending_control_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> StoredContinuationControlIntent | None:
    """Only a current unconsumed completion can request a model yield.

    Same-sequence recovery preserves this DATA. A yielded head terminates the
    corridor: the successor cannot rediscover an old completed control and spin.
    """
    _require_current_continuation_running(conn, running)
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    if current.checkpoint_id != selected.checkpoint_id or current.payload_json != encode_continuation_snapshot(selected.stack):
        raise ContinuationCASConflict("pending control changed its current complete checkpoint")
    frame = selected.stack.frames[-1]
    if len(selected.stack.frames) != 1 or type(frame) is not ComposeFrame or frame.phase != "action_ready":
        return None
    if frame.slice_sequence != running.claim.segment_sequence:
        raise AuditIntegrityError("pending control changed its actual current segment slice")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    snapshot = current
    while True:
        event = conn.execute(
            select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == snapshot.cursor)
        ).one_or_none()
        if event is None:
            raise AuditIntegrityError("pending control lost its complete journal")
        if event.tag != "recovered":
            break
        previous = conn.execute(
            select(checkpoints.c.checkpoint_id)
            .where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor < snapshot.cursor)
            .order_by(checkpoints.c.cursor.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is None:
            raise AuditIntegrityError("pending control recovery lost its predecessor")
        snapshot, root = _historical_snapshot(conn, session_id=sid, operation_id=operation, checkpoint_id=previous)
        if root.model_copy(update={"cursor": selected.stack.cursor}) != selected.stack:
            raise AuditIntegrityError("pending control recovery changed its completed DATA")
    if event.tag != "checkpoint":
        return None
    value = _checkpoint_payload(
        conn, running, snapshot, invalid_message="pending control lost its closed completion source", expected_completion=True
    )
    if type(value) is not _ControlCompletionPayload:
        return None
    payload = value
    if payload.intent_checkpoint_id is None:
        return None
    if (
        payload.sequence != running.claim.segment_sequence
        or payload.generation > running.claim.ownership_generation
        or payload.previous_cursor + 1 != snapshot.cursor
    ):
        raise AuditIntegrityError("pending control changed its original completing segment")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == payload.sequence,
            attempts.c.generation == payload.generation,
        )
    ).one_or_none()
    previous, previous_root = _historical_snapshot(
        conn, session_id=sid, operation_id=operation, checkpoint_id=payload.previous_checkpoint_id
    )
    prior_frame = previous_root.frames[-1]
    if (
        creator is None
        or (creator.session_operation_id, creator.session_operation_epoch)
        != (payload.creator_operation_id, payload.creator_operation_epoch)
        or (payload.generation < running.claim.ownership_generation and creator.revoked_at is None)
        or previous.cursor != payload.previous_cursor
        or len(previous_root.frames) != 1
        or type(prior_frame) is not ComposeFrame
        or prior_frame.phase != "response_retained"
        or prior_frame.pending_response != payload.response
        or previous_root.model_copy(update={"cursor": selected.stack.cursor, "frames": selected.stack.frames}) != selected.stack
    ):
        raise AuditIntegrityError("pending control lost its original completion creator or paid predecessor")
    control_result_message_on_connection(
        conn,
        running,
        selected=selected,
        recipe=ControlResultMessageRecipe(response=payload.response, tool_index=0, intent_checkpoint_id=payload.intent_checkpoint_id),
    )
    retained, _root = _historical_snapshot(conn, session_id=sid, operation_id=operation, checkpoint_id=payload.intent_checkpoint_id)
    intent_payload = _checkpoint_payload(conn, running, retained, invalid_message="pending control lost its original complete intent")
    if type(intent_payload) is not _ControlIntentPayload:
        raise AuditIntegrityError("pending control lost its original complete intent")
    return StoredContinuationControlIntent(retained, intent_payload.source, intent_payload.result)
