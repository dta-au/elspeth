"""Immutable V4 preparation-error receipts; no dispatch or live authority.

These receipts prove an actual parser/preparation failure retained atomically by
the issued value-batch owner. Reading them never prepares or executes a tool.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.continuation import private_chunk_key, read_chunk
from elspeth.web.sessions.continuation_primary import read_original_committed_primary_response_data_on_connection
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


class ValueBatchPreparationRefusal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.value-batch-preparation-refusal.v1"] = "composer.value-batch-preparation-refusal.v1"
    previous_checkpoint_id: str = Field(min_length=1)
    cursor: int = Field(gt=0)
    sequence: int = Field(ge=0)
    generation: int = Field(ge=0)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(gt=0)
    response: PrivateChunkRef
    predecessor: PrivateChunkRef
    result: PrivateChunkRef
    tool_index: int = Field(ge=0)
    tool_call_id: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def read_value_batch_preparation_refusal_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    env_ref_names: frozenset[str],
) -> ValueBatchPreparationRefusal:
    """Reprove original physical creator, paid call, error and exact journal.

    This is immutable DATA only. The caller still proves its selected graph,
    actor, original PRIMARY receipt and current ownership as applicable.
    """
    sid = str(session_id)
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one_or_none()
    if effect is None or effect.owner != "tool_dispatch" or effect.disposition != "refused" or effect.action_json is None:
        raise AuditIntegrityError("value preparation refusal has no actual original refused effect")
    try:
        source = ValueBatchPreparationRefusal.model_validate_json(effect.action_json, strict=True)
    except ValueError as error:
        raise AuditIntegrityError("value preparation refusal lost its closed original source") from error
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == source.sequence,
            attempts.c.generation == source.generation,
        )
    ).one_or_none()
    if (
        canonical_json(source.model_dump(mode="json")) != effect.action_json
        or creator is None
        or (source.sequence, source.generation) != (effect.creator_sequence, effect.creator_generation)
        or (source.creator_operation_id, source.creator_operation_epoch) != (creator.session_operation_id, creator.session_operation_epoch)
        or source.input_sha256 != effect.input_sha256
        or effect_key != f"tool:{source.response.digest}:{source.tool_index}"
        or effect.receipt_key != f"value-preparation-refusal:{source.result.digest}"
        or source.predecessor.codec != "composer.coupled-batch-data.v1"
        or source.result.codec != "composer.coupled-batch-data.v1"
    ):
        raise AuditIntegrityError("value preparation refusal changed original physical identity")
    checkpoint = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == source.cursor,
        )
    ).one_or_none()
    previous = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == source.previous_checkpoint_id,
        )
    ).one_or_none()
    event = conn.execute(
        select(events).where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence == source.cursor,
        )
    ).one_or_none()
    if (
        checkpoint is None
        or previous is None
        or event is None
        or checkpoint.kind != "transition"
        or event.tag != "checkpoint"
        or previous.cursor + 1 != source.cursor
    ):
        raise AuditIntegrityError("value preparation refusal lost its exact immutable journal")
    payload = canonical_json(
        {"owner": "composer_value_batch", "transition": "preparation_refused", "source": source.model_dump(mode="json")}
    )
    if read_chunk(conn, session_id=sid, operation_id=operation_id, key=event.payload_chunk_key) != payload.encode("utf-8"):
        raise AuditIntegrityError("value preparation refusal changed its original journal source")
    before = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation_id, key=previous.snapshot_chunk_key).decode("utf-8")
    )
    after = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=operation_id, key=checkpoint.snapshot_chunk_key).decode("utf-8")
    )
    old, new = before.frames[-1], after.frames[-1]
    if (
        type(decode_continuation_policy(before.admission_policy_json, before.policy_hash)) is not ContinuationAdmissionPolicyV4
        or (before.logical.session_id, before.logical.operation_id) != (sid, operation_id)
        or after.logical != before.logical
        or after.cursor != source.cursor
        or before.cursor + 1 != after.cursor
        or type(old) is not ToolBatchFrame
        or type(new) is not ToolBatchFrame
        or len(before.frames) != 2
        or len(after.frames) != 2
        or type(before.frames[0]) is not ComposeFrame
        or type(after.frames[0]) is not ComposeFrame
        or old.phase != "tool_ready"
        or new.phase != "tool_ready"
        or old.response != source.response
        or new.response != source.response
        or old.accumulator != source.predecessor
        or new.accumulator != source.result
        or old.next_tool_index != source.tool_index
        or new.next_tool_index != source.tool_index + 1
        or new.settled_effect_ids != (*old.settled_effect_ids, effect_key)
        or after.effect_frontier
        != (*before.effect_frontier, EffectFrontierEntry(effect_id=effect_key, disposition="refused", receipt_id=effect.receipt_key))
        or after.model_copy(update={"cursor": before.cursor, "frames": before.frames, "effect_frontier": before.effect_frontier}) != before
    ):
        raise AuditIntegrityError("value preparation refusal changed original ordered checkpoint prefix")
    old_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=before)
    new_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=after)
    original = {chunk.reference: chunk for chunk in old_chunks}
    completed = {chunk.reference: chunk for chunk in new_chunks}
    prior = decode_coupled_batch_chunk(original[source.predecessor], env_ref_names=env_ref_names)
    result_chunk = completed[source.result]
    result = decode_coupled_batch_chunk(result_chunk, env_ref_names=env_ref_names)
    paid = read_original_committed_primary_response_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        source_checkpoint_id=previous.checkpoint_id,
        source_cursor=previous.cursor,
        response=source.response,
    )
    if paid != original[source.response]:
        raise AuditIntegrityError("value preparation refusal changed its original paid response")
    calls = decode_continuation_response(paid).tool_batch.calls
    index = source.tool_index
    if (
        index >= len(calls)
        or prior.calls != calls
        or result.calls != calls
        or prior.batch.phase != "call_ready"
        or prior.batch.current_call is not None
        or prior.batch.next_tool_index != index
        or len(prior.batch.tool_outcomes) != index
        or result.batch.current_call is not None
        or result.batch.phase != ("dispatch_ready" if index + 1 == len(calls) else "call_ready")
        or result.batch.next_tool_index != index + 1
        or len(result.batch.tool_outcomes) != index + 1
        or result.batch.tool_outcomes[:-1] != prior.batch.tool_outcomes
        or result.batch.tool_outcomes[index].call != calls[index]
        or source.tool_call_id != calls[index].id
        or source.input_sha256
        != stable_hash({"tool_call_id": calls[index].id, "name": calls[index].function.name, "arguments": calls[index].function.arguments})
        or effect.result_chunk_key != private_chunk_key(result_chunk)
        or result.batch.tool_outcomes[index].response is not None
        or result.batch.tool_outcomes[index].error_class is None
        or result.batch.tool_outcomes[index].error_category is None
        or result.batch.tool_outcomes[index].error_message is None
        or result.batch.tool_outcomes[index].pre_version != result.batch.tool_outcomes[index].post_version
        or result.batch.state.to_dict() != prior.batch.state.to_dict()
    ):
        raise AuditIntegrityError("value preparation refusal changed its actual paid call or error result")
    # The nominal graph constructor validates the closed source chunk closure;
    # this is DATA, never reconstruction of executable tool state.
    SelectedContinuationState(checkpoint.checkpoint_id, after, new_chunks).__post_init__()
    before_compose, after_compose = before.frames[0], after.frames[0]
    if type(before_compose) is not ComposeFrame or type(after_compose) is not ComposeFrame:
        raise AuditIntegrityError("value preparation refusal lost its original recorder owner")
    old_recorder = decode_recorder_snapshot(original[before_compose.audit_buffer])
    new_recorder = decode_recorder_snapshot(completed[after_compose.audit_buffer])
    if new_recorder.invocations[:-1] != old_recorder.invocations or len(new_recorder.invocations) != len(old_recorder.invocations) + 1:
        raise AuditIntegrityError("value preparation refusal changed its exact original recorder prefix")
    invocation, outcome = new_recorder.invocations[-1], result.batch.tool_outcomes[index]
    if (
        invocation.tool_call_id != source.tool_call_id
        or invocation.tool_name != calls[index].function.name
        or invocation.status is not ComposerToolStatus.ARG_ERROR
        or invocation.error_class != outcome.error_class
        or invocation.error_category != outcome.error_category
        or invocation.error_message != outcome.error_message
        or invocation.version_before != outcome.pre_version
        or invocation.version_after is not None
        or invocation.strict_sent != outcome.strict_sent
        or invocation.wire_conformant != outcome.wire_conformant
    ):
        raise AuditIntegrityError("value preparation refusal differs from its actual original ARG_ERROR recorder")
    from elspeth.web.composer.continuation_message_family_builders import require_feedback_invocation

    authoring = invocation.private_authoring
    if authoring is None or authoring not in new_recorder.private_authorings:
        raise AuditIntegrityError("value preparation refusal lost its original feedback authoring")
    require_feedback_invocation(call=outcome.call, invocation=invocation, authoring=authoring, source="arg_error", outcome=outcome)
    return source
