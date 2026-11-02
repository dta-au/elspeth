"""Authenticate unissued attachment preparation DATA before fresh preparation.

Historical preparation never grants a read, current creator or live timer.
The caller retains its current SQL claim/CAS and the read effect owner still
refuses every existing read effect before a physical inspection can start.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import (
    RestoredPreparedBatchData,
    decode_coupled_batch_chunk,
    decode_prepared_batch_data,
    encode_restored_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_dispatch_audit import _binding
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, read_chunk
from elspeth.web.sessions.continuation_attachment_result import _chunk, _input, _selected
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


class AttachmentPreparationSource(ContinuationValue):
    schema_version: Literal["composer.attachment-preparation.v1"] = "composer.attachment-preparation.v1"
    previous_checkpoint_id: str = Field(min_length=1)
    cursor: int = Field(gt=0)
    sequence: int = Field(gt=0)
    generation: int = Field(gt=0)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(gt=0)
    response: PrivateChunkRef
    prepared: PrivateChunkRef
    tool_index: int = Field(ge=0)


class _PreparationEvent(ContinuationValue):
    owner: Literal["composer_tool_phase"]
    transition: Literal["attachment_prepared", "attachment_reprepared"]
    preparation: AttachmentPreparationSource


def require_fresh_attachment_preparation(
    original: RestoredPreparedBatchData, candidate: RestoredPreparedBatchData, *, names: frozenset[str]
) -> None:
    """Compare the complete graph, allowing only fresh audit/creator fields."""
    # Decode an independent expected graph; never mutate either live owner.
    expected = decode_prepared_batch_data(encode_restored_prepared_batch_data(original, env_ref_names=names), env_ref_names=names)
    old, new = expected.batch.current_call, candidate.batch.current_call
    if (
        old is None
        or new is None
        or _binding(original.audit) != _binding(candidate.audit)
        or candidate.audit.started_at < original.audit.started_at
    ):
        raise AuditIntegrityError("fresh attachment preparation changed its original audit binding")
    old.original_creator_id, old.original_creator_epoch = new.original_creator_id, new.original_creator_epoch
    expected = RestoredPreparedBatchData(expected.batch, expected.accumulator, expected.calls, candidate.audit, expected.dialect)
    if encode_restored_prepared_batch_data(expected, env_ref_names=names) != encode_restored_prepared_batch_data(
        candidate, env_ref_names=names
    ):
        raise AuditIntegrityError("fresh attachment preparation changed its complete original prepared graph")


def _preparation_source(
    conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot, *, names: frozenset[str]
) -> RestoredPreparedBatchData:
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
    from elspeth.web.sessions.continuation_semantic_advisor import _historical_snapshot

    lineage = semantic_recovery_source(conn, sid, operation, selected)
    source = lineage.snapshot
    root = decode_continuation_snapshot(source.payload_json)
    current = SelectedContinuationState(
        source.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )
    _compose, frame, prepared, _key, _digest = _input(current, names)
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == source.cursor)
    ).one_or_none()
    if event is None or event.tag != "checkpoint" or event.payload_chunk_key != f"event:{source.cursor}":
        raise AuditIntegrityError("attachment preparation lost its original checkpoint event")
    codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key == event.payload_chunk_key
        )
    ).scalar_one_or_none()
    checkpoint_kind = conn.execute(
        select(checkpoints.c.kind).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.checkpoint_id == source.checkpoint_id
        )
    ).scalar_one_or_none()
    if codec != "event.v2" or checkpoint_kind != "transition":
        raise AuditIntegrityError("attachment preparation lost its exact publication codec or checkpoint kind")
    encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=event.payload_chunk_key).decode("utf-8", errors="strict")
    try:
        publication = _PreparationEvent.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("attachment preparation lost its closed original publication") from error
    identity = publication.preparation
    call = prepared.batch.current_call
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == identity.sequence,
            attempts.c.generation == identity.generation,
        )
    ).one_or_none()
    if (
        canonical_json(publication.model_dump(mode="json")) != encoded
        or identity.cursor != source.cursor
        or identity.response != frame.response
        or identity.prepared != frame.accumulator
        or identity.tool_index != frame.next_tool_index
        or identity.sequence != _compose.slice_sequence
        or frame.phase != "tool_ready"
        or call is None
        or creator is None
        or (identity.creator_operation_id, identity.creator_operation_epoch)
        != (creator.session_operation_id, creator.session_operation_epoch)
        or (call.original_creator_id, call.original_creator_epoch) != (identity.creator_operation_id, identity.creator_operation_epoch)
        or (lineage.sequence is not None and (lineage.sequence, lineage.previous_generation) != (identity.sequence, identity.generation))
    ):
        raise AuditIntegrityError("attachment preparation changed its actual creator or retained source")
    before = _historical_snapshot(conn, sid, operation, checkpoint_id=identity.previous_checkpoint_id)
    previous_root = decode_continuation_snapshot(before.payload_json)
    previous = SelectedContinuationState(
        before.checkpoint_id,
        previous_root,
        load_private_chunks(conn, session_id=sid, operation_id=operation, stack=previous_root),
    )
    prior = previous_root.frames[-1]
    if (
        before.cursor + 1 != source.cursor
        or type(prior) is not ToolBatchFrame
        or prior.phase != "tool_ready"
        or prior.response != frame.response
        or prior.next_tool_index != frame.next_tool_index
        or prior.settled_effect_ids != frame.settled_effect_ids
        or previous_root.logical != root.logical
        or previous_root.admission_policy_json != root.admission_policy_json
        or previous_root.policy_hash != root.policy_hash
        or previous_root.mutation_ceiling != root.mutation_ceiling
        or previous_root.provider_cost_policy != root.provider_cost_policy
        or previous_root.effect_frontier != root.effect_frontier
        or root.pending_owner_results
        or root.pending_settlement_ids
    ):
        raise AuditIntegrityError("attachment preparation changed its actual unissued predecessor")
    if publication.transition == "attachment_prepared":
        original = decode_coupled_batch_chunk(_chunk(previous, prior.accumulator), env_ref_names=names)
        if (
            original.calls != prepared.calls
            or original.batch.phase != "call_ready"
            or original.batch.current_call is not None
            or original.batch.next_tool_index != call.index
            or original.batch.tool_outcomes != prepared.batch.tool_outcomes
            or canonical_json(original.batch.state.to_dict()) != canonical_json(prepared.batch.state.to_dict())
        ):
            raise AuditIntegrityError("attachment preparation changed its original paid call/prefix")
    else:
        original_preparation = _preparation_source(conn, sid, operation, before, names=names)
        require_fresh_attachment_preparation(original_preparation, prepared, names=names)
    read_selected_turn_evidence_graph_data_on_connection(conn, session_id=UUID(sid), operation_id=operation, selected=current, names=names)
    return prepared


def read_unissued_attachment_preparation_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> RestoredPreparedBatchData:
    current, names = _selected(conn, running, selected, inventory)
    _compose, frame, prepared, key, _digest = _input(selected, names)
    if frame.phase != "tool_ready" or any(entry.disposition not in ("committed", "refused") for entry in selected.stack.effect_frontier):
        raise AuditIntegrityError("attachment preparation is no longer unissued")
    if (
        conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == str(running.claim.session_id),
                effects.c.operation_id == running.claim.operation_id,
                effects.c.effect_key == key,
            )
        ).one_or_none()
        is not None
    ):
        raise AuditIntegrityError("attachment preparation cannot repeat an existing read effect")
    original = _preparation_source(conn, str(running.claim.session_id), running.claim.operation_id, current, names=names)
    lineage = semantic_recovery_source(conn, str(running.claim.session_id), running.claim.operation_id, current)
    call = original.batch.current_call
    if (
        call is None
        or (
            lineage.sequence is not None
            and (lineage.sequence, lineage.generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        )
        or (
            lineage.sequence is None
            and (call.original_creator_id, call.original_creator_epoch)
            != (running.session_operation_context.fence.operation_id, running.session_operation_context.fence.operation_epoch)
        )
    ):
        raise AuditIntegrityError("attachment preparation lost its complete current recovery lineage")
    if encode_restored_prepared_batch_data(original, env_ref_names=names) != encode_restored_prepared_batch_data(
        prepared, env_ref_names=names
    ):
        raise AuditIntegrityError("attachment preparation changed its selected complete historical graph")
    response = read_committed_primary_response_on_connection(conn, running, selected=selected, reference=frame.response)
    if decode_continuation_response(response).tool_batch.calls != prepared.calls:
        raise AuditIntegrityError("attachment preparation changed its original paid provider calls")
    return prepared
