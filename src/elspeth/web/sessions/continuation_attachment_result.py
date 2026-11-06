"""Current-authorized attachment result/history checkpoints on the real SQL owner.

A history application is not tool application: its complete prepared call and
result remain selected, and the tool cursor does not advance. No retained
inspection can authorize a later filesystem read or prompt use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef, RetainedOwnerResultRef, ToolBatchFrame
from elspeth.web.composer.continuation_attachment_history import (
    ContinuationAttachmentHistory,
    RetainedAttachmentRead,
    encode_attachment_history,
    extend_attachment_history,
)
from elspeth.web.composer.continuation_attachment_read import CurrentAttachmentRead
from elspeth.web.composer.continuation_attachment_result_data import (
    AttachmentHistoryBatch,
    AttachmentPublishedResult,
    decode_attachment_history_batch,
    decode_attachment_published_result,
    encode_attachment_history_batch,
    encode_attachment_published_result,
)
from elspeth.web.composer.continuation_batch_data import RestoredPreparedBatchData, decode_prepared_batch_data
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_dispatch_audit import PortableDispatchAudit, decode_dispatch_audit, encode_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json, encode_tool_result_value
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tool_batch import ContinuationCallState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadOwnerResult,
    decode_read_owner_result,
    require_read_versions_on_connection,
)
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


@dataclass(frozen=True, slots=True)
class AttachmentEffectPreparation:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    input_sha256: str


@dataclass(frozen=True, slots=True)
class AttachmentResultCheckpoint:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    prepared: RestoredPreparedBatchData
    result: ToolResult
    retained: ContinuationReadOwnerResult
    history: ContinuationAttachmentHistory
    history_applied: bool
    read_audit: PortableDispatchAudit | None = None


def _selected(
    conn: Connection, running: ComposerOperationRunning, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> tuple[StoredContinuationSnapshot, frozenset[str]]:
    if type(selected) is not SelectedContinuationState or type(running) is not ComposerOperationRunning:
        raise AuditIntegrityError("attachment checkpoint requires actual selected/current owners")
    selected.__post_init__()
    names = require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    encoded = encode_continuation_snapshot(selected.stack)
    if inventory.selection.snapshot_json != encoded:
        raise AuditIntegrityError("attachment checkpoint changed the complete selected root")
    current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    if (current.checkpoint_id, current.cursor, current.payload_json) != (selected.checkpoint_id, selected.stack.cursor, encoded):
        raise ContinuationCASConflict("attachment checkpoint changed before current admission")
    if load_selected_private_chunks_on_connection(conn, running, current) != selected.chunks:
        raise AuditIntegrityError("attachment checkpoint substituted selected private bytes")
    return current, names


def _chunk(selected: SelectedContinuationState, reference: PrivateChunkRef) -> PrivateChunk:
    values = tuple(item for item in selected.chunks if item.reference == reference)
    if len(values) != 1:
        raise AuditIntegrityError("attachment checkpoint lost its selected chunk")
    verify_private_chunk(reference, values[0].canonical)
    return values[0]


def _input(
    selected: SelectedContinuationState, names: frozenset[str]
) -> tuple[ComposeFrame, ToolBatchFrame, RestoredPreparedBatchData, str, str]:
    root = selected.stack
    if len(root.frames) != 2:
        raise AuditIntegrityError("attachment owner requires its ordinary Compose/prepared batch cut")
    compose, frame = root.frames
    if (
        type(compose) is not ComposeFrame
        or type(frame) is not ToolBatchFrame
        or root.active_action_json is not None
        or root.active_action_source is not None
        or root.pending_settlement_ids
    ):
        raise AuditIntegrityError("attachment owner requires its ordinary Compose/prepared batch cut")
    if frame.accumulator.codec != "composer.tool-batch.v1" or frame.response.codec != "composer.response.v1":
        raise AuditIntegrityError("attachment owner changed actual prepared/response codecs")
    accumulator = _chunk(selected, frame.accumulator)
    if frame.phase == "result_retained":
        retained_batch = decode_attachment_history_batch(accumulator, env_ref_names=names)
        accumulator = encode_private_chunk(codec="composer.tool-batch.v1", canonical=retained_batch.prepared_json, env_ref_names=names)
    prepared = decode_prepared_batch_data(accumulator.canonical, env_ref_names=names)
    call = prepared.batch.current_call
    if type(call) is not ContinuationCallState or call.tool_name not in {"read_attachment", "get_blob_content"}:
        raise AuditIntegrityError("attachment owner requires an actual prepared attachment call")
    response = decode_continuation_response(_chunk(selected, frame.response))
    if (
        response.tool_batch.calls != prepared.calls
        or frame.next_tool_index != prepared.batch.next_tool_index
        or frame.next_tool_index != call.index
        or call.tool_call is not prepared.calls[frame.next_tool_index]
        or compose.pending_response != frame.response
        or _chunk(selected, compose.state).canonical != canonical_json(prepared.batch.state.to_dict())
    ):
        raise AuditIntegrityError("attachment owner changed its selected response/cursor/pre-state graph")
    key = f"attachment:{frame.response.digest}:{call.index}"
    descriptor = {
        "schema": "composer.attachment-input.v1",
        "logical": root.logical.model_dump(mode="json"),
        "response": frame.response.model_dump(mode="json"),
        "prepared": accumulator.reference.model_dump(mode="json"),
        "index": frame.next_tool_index,
        "tool_call_id": call.tool_call.id,
        "tool_name": call.tool_name,
        "arguments": call.arguments,
        "raw_arguments": call.private_raw_arguments,
    }
    return compose, frame, prepared, key, stable_hash(descriptor)


def _history(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    compose: ComposeFrame,
    names: frozenset[str],
) -> ContinuationAttachmentHistory:
    return _history_data(conn, running.claim.session_id, running.claim.operation_id, selected, compose, names)


def _history_data(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    compose: ComposeFrame,
    names: frozenset[str],
) -> ContinuationAttachmentHistory:
    from elspeth.web.sessions.continuation_attachment_application import load_attachment_history_data_on_connection

    if selected.stack.frames[0] is not compose:
        raise AuditIntegrityError("attachment history changed its actual selected Compose owner")
    return load_attachment_history_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, names=names
    ).history


def prepare_attachment_result_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> AttachmentEffectPreparation:
    current, names = _selected(conn, running, selected, inventory)
    compose, frame, prepared, key, digest = _input(selected, names)
    _history(conn, running, selected, compose, names)
    call = prepared.batch.current_call
    if type(call) is not ContinuationCallState or (call.original_creator_id, call.original_creator_epoch) != (
        running.session_operation_context.fence.operation_id,
        running.session_operation_context.fence.operation_epoch,
    ):
        raise AuditIntegrityError("attachment preparation requires its actual current prepared call creator")
    actor = conn.execute(
        select(jobs.c.actor_user_id).where(
            jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id
        )
    ).scalar_one()
    if type(actor) is not str or prepared.audit.actor != f"composer-web:user-{actor}":
        raise AuditIntegrityError("attachment preparation changed the actual request actor")
    root = selected.stack
    if (
        frame.phase != "tool_ready"
        or root.pending_owner_results
        or key in frame.settled_effect_ids
        or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("attachment preparation cannot replace unresolved or consumed work")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if (
        conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key
            )
        ).one_or_none()
        is not None
    ):
        raise ContinuationCASConflict("attachment preparation cannot repeat an existing read")
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation,
            effect_key=key,
            input_sha256=digest,
            owner="read",
            disposition="prepared",
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
            action_json=None,
        )
    )
    changed = root.model_copy(update={"frames": (compose, frame.model_copy(update={"phase": "effect_reconciling"}))})
    encoded = _effect_snapshot(encode_continuation_snapshot(changed), effect_key=key, disposition="prepared", receipt_id=None)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(next_action_json=policy.actions.session_sql.model_dump_json(), policy_sha256=root.policy_hash)
    )
    following = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("input_prepared", canonical_json({"effect_key": key, "input_sha256": digest})),),
        handoff=False,
    )
    return AttachmentEffectPreparation(following, key, digest)


def _effect(conn: Connection, running: ComposerOperationRunning, selected: SelectedContinuationState, key: str, digest: str) -> Row[Any]:
    return _effect_data(conn, running.claim.session_id, running.claim.operation_id, selected, key, digest)


def _effect_data(
    conn: Connection, session_id: UUID, operation_id: str, selected: SelectedContinuationState, key: str, digest: str
) -> Row[Any]:
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(session_id),
            effects.c.operation_id == operation_id,
            effects.c.effect_key == key,
        )
    ).one_or_none()
    frontier = tuple(item for item in selected.stack.effect_frontier if item.effect_id == key)
    if (
        row is None
        or row.owner != "read"
        or row.input_sha256 != digest
        or row.action_json is not None
        or row.provider_attempt_id is not None
        or row.physical_sequence is not None
        or row.physical_generation is not None
        or row.requested_model is not None
        or row.response_finished_at is not None
        or row.committed_state_ids_json is not None
        or row.graph_sha256 is not None
        or len(frontier) != 1
        or (frontier[0].disposition, frontier[0].receipt_id) != (row.disposition, row.receipt_key)
    ):
        raise AuditIntegrityError("attachment effect lost its actual selected read owner/frontier")
    if any(item.effect_id != key and item.disposition not in ("committed", "refused") for item in selected.stack.effect_frontier):
        raise AuditIntegrityError("attachment owner cannot consume unrelated unresolved work")
    return row


def _result(retained: ContinuationReadOwnerResult, prepared: RestoredPreparedBatchData, names: frozenset[str]) -> ToolResult:
    call = prepared.batch.current_call
    if type(call) is not ContinuationCallState:
        raise AuditIntegrityError("attachment result lost its complete prepared call")
    if (
        retained.tool_name != call.tool_name
        or retained.arguments_json != canonical_json(call.arguments)
        or retained.prescribing_provider_call_id is not None
    ):
        raise AuditIntegrityError("attachment result differs from its actual selected call")
    value = decode_tool_result_json(retained.tool_result_json, pre_state=call.pre_state, env_ref_names=names)
    if value.updated_state is not call.pre_state or value.affected_nodes:
        raise AuditIntegrityError("attachment result changed its nonmutating pre-state alias")
    return value


def require_attachment_read_audit(prepared: RestoredPreparedBatchData, observed: PortableDispatchAudit, *, names: frozenset[str]) -> None:
    from elspeth.web.composer.continuation_dispatch_audit import _binding

    encode_dispatch_audit(observed, env_ref_names=names)
    if (
        _binding(observed) != _binding(prepared.audit)
        or observed.started_at != prepared.audit.started_at
        or observed.elapsed_active_ns < prepared.audit.elapsed_active_ns
    ):
        raise AuditIntegrityError("attachment observation lost its original dispatch binding or elapsed read segment")


def publish_attachment_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    actual: CurrentAttachmentRead,
    pre_state: CompositionState,
    read_audit: PortableDispatchAudit | None = None,
) -> StoredContinuationSnapshot:
    current, names = _selected(conn, running, selected, inventory)
    creator_sequence = running.claim.segment_sequence
    creator_generation = running.claim.ownership_generation
    if type(creator_sequence) is not int or creator_sequence <= 0 or type(creator_generation) is not int or creator_generation <= 0:
        raise AuditIntegrityError("attachment publication requires its actual positive creator identity")
    _compose, frame, prepared, key, digest = _input(selected, names)
    row = _effect(conn, running, selected, key, digest)
    if read_audit is not None:
        require_attachment_read_audit(prepared, read_audit, names=names)
    if (
        row.disposition != "prepared"
        or row.receipt_key is not None
        or row.result_chunk_key is not None
        or (row.creator_sequence, row.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or frame.phase != "effect_reconciling"
        or selected.stack.pending_owner_results
        or key in frame.settled_effect_ids
    ):
        raise ContinuationCASConflict("attachment publication requires its original unconsumed preparation")
    if (
        type(actual) is not CurrentAttachmentRead
        or type(actual.result) is not ToolResult
        or type(actual.retained) is not ContinuationReadOwnerResult
        or type(pre_state) is not CompositionState
        or actual.result.updated_state is not pre_state
        or canonical_json(pre_state.to_dict()) != canonical_json(prepared.batch.state.to_dict())
    ):
        raise AuditIntegrityError("attachment publication changed actual reader/pre-state ownership")
    encoded = canonical_json(actual.retained.model_dump(mode="json"))
    retained = decode_read_owner_result(encoded, env_ref_names=names)
    _result(retained, prepared, names)
    if (
        canonical_json(encode_tool_result_value(retained.tool_name, actual.result, env_ref_names=names).model_dump(mode="json"))
        != retained.tool_result_json
    ):
        raise AuditIntegrityError("attachment publication substituted the actual reader result")
    require_read_versions_on_connection(conn, session_id=str(running.claim.session_id), result=retained, env_ref_names=names)
    result = encode_attachment_published_result(
        AttachmentPublishedResult(
            effect_key=key,
            input_sha256=digest,
            creator_sequence=creator_sequence,
            creator_generation=creator_generation,
            creator_operation_id=running.session_operation_context.fence.operation_id,
            creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
            result_json=encoded,
            read_audit_json=None if read_audit is None else encode_dispatch_audit(read_audit, env_ref_names=names),
        ),
        env_ref_names=names,
    )
    receipt = f"attachment-result:{result.reference.digest}"
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(result),
        payload=result.canonical.encode("utf-8"),
        codec=result.reference.codec,
    )
    conn.execute(
        update(effects)
        .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
        .values(disposition="committed", receipt_key=receipt, result_chunk_key=private_chunk_key(result))
    )
    committed = _effect_snapshot(encode_continuation_snapshot(selected.stack), effect_key=key, disposition="committed", receipt_id=receipt)
    root = decode_continuation_snapshot(committed).model_copy(
        update={"pending_owner_results": (RetainedOwnerResultRef(effect_key=key, kind="read", result=result.reference),)}
    )
    following = encode_continuation_snapshot(root)
    return append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=following,
        event_batch=(
            ContinuationEvent("response_retained", canonical_json({"effect_key": key, "result_digest": result.reference.digest})),
        ),
        handoff=False,
    )


def _retained_history(
    history: ContinuationAttachmentHistory, key: str, result: ContinuationReadOwnerResult
) -> ContinuationAttachmentHistory:
    if not result.attachment_inspections:
        return history
    incoming = RetainedAttachmentRead(effect_key=key, inspections=result.attachment_inspections)
    previous = tuple(item for item in history.reads if item.effect_key == key)
    if previous:
        if previous != (incoming,):
            raise AuditIntegrityError("attachment history changed the same read's complete inspections")
        return history
    return extend_attachment_history(history, reads=(incoming,))


def read_attachment_result_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> AttachmentResultCheckpoint:
    current, names = _selected(conn, running, selected, inventory)
    value = verify_attachment_result_source_on_connection(conn, running, selected=selected, snapshot=current, env_ref_names=names)
    require_read_versions_on_connection(conn, session_id=str(running.claim.session_id), result=value.retained, env_ref_names=names)
    return value


def verify_attachment_result_source_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    snapshot: StoredContinuationSnapshot,
    env_ref_names: frozenset[str],
) -> AttachmentResultCheckpoint:
    """Immutable read proof after the caller's actual current SQL admission.

    This does not authorize filesystem/current use. Historical application
    verification supplies an actual old SQL checkpoint, never a current grant.
    """
    return verify_attachment_result_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        snapshot=snapshot,
        env_ref_names=env_ref_names,
    )


def verify_attachment_result_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    snapshot: StoredContinuationSnapshot,
    env_ref_names: frozenset[str],
) -> AttachmentResultCheckpoint:
    """Original immutable read/result joins after the caller's SQL proof."""
    names = env_ref_names
    if (
        snapshot.checkpoint_id != selected.checkpoint_id
        or snapshot.cursor != selected.stack.cursor
        or snapshot.payload_json != encode_continuation_snapshot(selected.stack)
        or selected.stack.logical.session_id != str(session_id)
        or selected.stack.logical.operation_id != operation_id
    ):
        raise AuditIntegrityError("attachment immutable source changed its complete stored checkpoint")
    compose, frame, prepared, key, digest = _input(selected, names)
    row = _effect_data(conn, session_id, operation_id, selected, key, digest)
    if row.disposition != "committed" or row.result_chunk_key is None:
        raise AuditIntegrityError("attachment result is unresolved and cannot redispatch")
    pending = selected.stack.pending_owner_results
    applied = key in frame.settled_effect_ids
    if applied:
        if frame.phase != "result_retained" or pending or frame.settled_effect_ids.count(key) != 1:
            raise AuditIntegrityError("attachment application lost its exact consumed result cut")
        retained_batch = decode_attachment_history_batch(_chunk(selected, frame.accumulator), env_ref_names=names)
        matches = (encode_private_chunk(codec="composer.owner-result.v1", canonical=retained_batch.result_json, env_ref_names=names),)
    else:
        if frame.phase != "effect_reconciling" or len(pending) != 1 or pending[0].kind != "read" or pending[0].effect_key != key:
            raise AuditIntegrityError("attachment result lost its exact pending owner")
        matches = (_chunk(selected, pending[0].result),)
    if (
        len(matches) != 1
        or matches[0].reference.codec != "composer.owner-result.v1"
        or private_chunk_key(matches[0]) != row.result_chunk_key
    ):
        raise AuditIntegrityError("attachment result changed its selected immutable source")
    chunk = matches[0]
    stored_codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(session_id),
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == row.result_chunk_key,
        )
    ).scalar_one_or_none()
    if stored_codec != chunk.reference.codec or read_chunk(
        conn, session_id=str(session_id), operation_id=operation_id, key=row.result_chunk_key
    ) != chunk.canonical.encode("utf-8"):
        raise AuditIntegrityError("attachment retained bytes differ from their actual SQL source")
    if row.receipt_key != f"attachment-result:{chunk.reference.digest}":
        raise AuditIntegrityError("attachment result changed its actual retained receipt identity")
    publication = decode_attachment_published_result(chunk, env_ref_names=names)
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(session_id),
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == row.creator_sequence,
            attempts.c.generation == row.creator_generation,
        )
    ).one_or_none()
    if creator is None or (
        publication.effect_key,
        publication.input_sha256,
        publication.creator_sequence,
        publication.creator_generation,
        publication.creator_operation_id,
        publication.creator_operation_epoch,
    ) != (key, digest, row.creator_sequence, row.creator_generation, creator.session_operation_id, creator.session_operation_epoch):
        raise AuditIntegrityError("attachment result changed its original creator/selected input")
    read_audit = None if publication.read_audit_json is None else decode_dispatch_audit(publication.read_audit_json, env_ref_names=names)
    if read_audit is not None:
        require_attachment_read_audit(prepared, read_audit, names=names)
    retained = decode_read_owner_result(publication.result_json, env_ref_names=names)
    result = _result(retained, prepared, names)
    history = _history_data(conn, session_id, operation_id, selected, compose, names)
    if applied and _retained_history(history, key, retained) != history:
        raise AuditIntegrityError("consumed attachment result lacks its complete history")
    return AttachmentResultCheckpoint(snapshot, key, prepared, result, retained, history, applied, read_audit)


def apply_attachment_history_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory
) -> StoredContinuationSnapshot:
    value = read_attachment_result_on_connection(conn, running, selected=selected, inventory=inventory)
    if value.history_applied:
        return value.snapshot
    compose, frame = selected.stack.frames
    if type(compose) is not ComposeFrame or type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("attachment application lost its actual frames")
    from elspeth.web.sessions.continuation_attachment_application import load_attachment_history_sources_on_connection

    original = load_attachment_history_sources_on_connection(conn, running, selected=selected, names=inventory.names)
    if original.history != value.history:
        raise AuditIntegrityError("attachment inspection application changed its actual history source")
    history = encode_attachment_history(
        _retained_history(value.history, value.effect_key, value.retained),
        env_ref_names=inventory.names,
        source_chunks=original.chunks,
    )
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(history),
        payload=history.canonical.encode("utf-8"),
        codec=history.reference.codec,
    )
    retained_batch = encode_attachment_history_batch(
        AttachmentHistoryBatch(
            prepared_json=_chunk(selected, frame.accumulator).canonical,
            result_json=_chunk(selected, selected.stack.pending_owner_results[0].result).canonical,
        ),
        env_ref_names=inventory.names,
    )
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=private_chunk_key(retained_batch),
        payload=retained_batch.canonical.encode("utf-8"),
        codec=retained_batch.reference.codec,
    )
    # Keep complete original graph and read bytes reachable after consumption.
    # This is history-only; the actual tool application/cursor remain owed.
    changed = frame.model_copy(
        update={
            "phase": "result_retained",
            "accumulator": retained_batch.reference,
            "settled_effect_ids": (*frame.settled_effect_ids, value.effect_key),
        }
    )
    root = selected.stack.model_copy(
        update={"frames": (compose.model_copy(update={"attachment_admissions": history.reference}), changed), "pending_owner_results": ()}
    )
    return append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=value.snapshot.checkpoint_id,
        expected_cursor=value.snapshot.cursor,
        snapshot_json=encode_continuation_snapshot(root),
        event_batch=(ContinuationEvent("checkpoint", canonical_json({"effect_key": value.effect_key})),),
        handoff=False,
    )
