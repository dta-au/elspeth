"""Atomic continuation application and immutable journal verification."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_application_graph import ApplicationGraph, decode_application_graph, encode_application_graph
from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot, decode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import (
    PortableP4ApplicationBundle,
    build_portable_p4_application,
    require_portable_p4_application,
    require_selected_applied_batch_chunk,
    retained_effects,
)
from elspeth.web.composer.continuation_evidence_application_values import P4EffectProducerData
from elspeth.web.composer.continuation_owed_evidence import read_owed_p4_evidence_on_connection
from elspeth.web.composer.continuation_p4_batch import decode_p4_pending_batch_chunk, encode_p4_pending_batch_chunk
from elspeth.web.composer.continuation_prepared_p4 import decode_prepared_p4, prepared_p4_members
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_p4 import decode_p4_receipt, verify_p4_immutable_receipt_on_connection
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


@dataclass(frozen=True, slots=True)
class StoredP4Application:
    snapshot: StoredContinuationSnapshot
    bundle: PortableP4ApplicationBundle


def _private_chunk_on_connection(conn: Connection, running: ComposerOperationRunning, reference: PrivateChunkRef) -> PrivateChunk:
    try:
        text = read_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=f"value:{reference.codec}:{reference.digest}",
        ).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("application storage is not strict UTF-8") from error
    verify_private_chunk(reference, text)
    return PrivateChunk(reference, text)


def _compose_frame_index(selected: SelectedContinuationState) -> int:
    indexes = tuple(index for index, frame in enumerate(selected.stack.frames) if type(frame) is ComposeFrame)
    if len(indexes) != 1:
        raise AuditIntegrityError("P4 application requires its one original Compose frame")
    return indexes[0]


def require_p4_application_journal_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    evidence: TurnEvidenceSnapshot,
    env_ref_names: frozenset[str],
) -> None:
    """Re-read and recompute every retained application after current admission."""
    entry = evidence.application_entry
    if entry is None:
        if evidence.historical_application_count or evidence.effect_locators:
            raise AuditIntegrityError("evidence omitted its actual historical application journal")
        return
    if evidence.logical != selected.stack.logical or entry.logical != selected.stack.logical:
        raise AuditIntegrityError("application journal changed its current logical owner")
    graph = decode_application_graph(entry.graph_json, env_ref_names=env_ref_names)
    if len(graph.cuts) != len(entry.applications):
        raise AuditIntegrityError("application journal changed its exact complete cut cohort")
    current_batch = selected.stack.frames[-1]
    if type(current_batch) is ToolBatchFrame:
        if current_batch.next_tool_index != graph.selected.batch.next_tool_index:
            raise AuditIntegrityError("application journal changed its selected tool cursor")
        actual_batch = _private_chunk_on_connection(conn, running, current_batch.accumulator)
        response = decode_continuation_response(_private_chunk_on_connection(conn, running, current_batch.response))
        if response.tool_batch.calls != graph.selected.calls:
            raise AuditIntegrityError("application journal changed its selected response calls")
        if graph.selected.batch.phase == "complete":
            if (
                current_batch.phase != "result_retained"
                or entry.applications[-1].effect_key not in current_batch.settled_effect_ids
                or current_batch.response != entry.applications[-1].response
            ):
                raise AuditIntegrityError("application journal lost its selected settled batch")
            require_selected_applied_batch_chunk(actual_batch, evidence=evidence, env_ref_names=env_ref_names)
        elif graph.selected.batch.phase == "p4_pending":
            pending_batch = encode_p4_pending_batch_chunk(
                batch=graph.selected.batch, accumulator=graph.selected.accumulator, calls=graph.selected.calls, env_ref_names=env_ref_names
            )
            if current_batch.phase != "effect_reconciling" or actual_batch != pending_batch:
                raise AuditIntegrityError("application journal changed its selected pending batch")
        elif graph.selected.batch.phase in ("call_ready", "call_active", "dispatch_ready"):
            from elspeth.web.composer.continuation_selected_batch_graph import require_selected_active_batch_graph

            require_selected_active_batch_graph(selected, evidence, env_ref_names=env_ref_names)
        else:
            raise AuditIntegrityError("application journal has no supported selected batch cut")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    for index, record in enumerate(entry.applications):
        effect = conn.execute(
            select(effects).where(
                effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == record.effect_key
            )
        ).one_or_none()
        if (
            effect is None
            or effect.owner != "session_sql"
            or effect.disposition != "committed"
            or effect.input_sha256 != record.input_sha256
            or effect.receipt_key != record.receipt_id
            or effect.result_chunk_key != f"value:{record.result.codec}:{record.result.digest}"
            or effect.creator_sequence != record.creator_sequence
            or effect.creator_generation != record.creator_generation
        ):
            raise AuditIntegrityError("application journal lost its actual committed effect")
        creator = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == operation_id,
                attempts.c.sequence == effect.creator_sequence,
                attempts.c.generation == effect.creator_generation,
            )
        ).one_or_none()
        if (
            creator is None
            or creator.session_operation_id != record.creator_operation_id
            or creator.session_operation_epoch != record.creator_operation_epoch
        ):
            raise AuditIntegrityError("application journal changed its actual original producer")
        result_chunk = _private_chunk_on_connection(conn, running, record.result)
        pending_chunk = _private_chunk_on_connection(conn, running, record.pending_batch)
        response_chunk = _private_chunk_on_connection(conn, running, record.response)
        prepared_chunk = _private_chunk_on_connection(conn, running, record.prepared_input)
        if (
            result_chunk.canonical != record.result_json
            or pending_chunk.canonical != record.pending_batch_json
            or response_chunk.canonical != record.response_json
            or prepared_chunk.canonical != record.prepared_input_json
        ):
            raise AuditIntegrityError("application journal changed immutable input/result bytes")
        pending = decode_p4_pending_batch_chunk(pending_chunk, env_ref_names=env_ref_names)
        if pending.batch.p4_input_json != prepared_chunk.canonical:
            raise AuditIntegrityError("application journal changed its actual complete prepared input")
        prepared = decode_prepared_p4(prepared_chunk.canonical, env_ref_names=env_ref_names)
        if (
            prepared.session_id != sid
            or prepared.creator_operation_id != record.creator_operation_id
            or prepared_p4_members(prepared, env_ref_names=env_ref_names).mutation_ceiling != selected.stack.mutation_ceiling.materialize()
        ):
            raise AuditIntegrityError("application journal changed original input/session/ceiling")
        receipt, outcome = decode_p4_receipt(result_chunk.canonical, env_ref_names=env_ref_names)
        result = verify_p4_immutable_receipt_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation_id,
            effect_key=record.effect_key,
            input_sha256=effect.input_sha256,
            receipt_key=effect.receipt_key,
            creator_sequence=effect.creator_sequence,
            creator_generation=effect.creator_generation,
            receipt=receipt,
            data=outcome,
            selected=prepared,
            env_ref_names=env_ref_names,
        )
        old = conn.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == operation_id,
                checkpoints.c.checkpoint_id == record.checkpoint_id,
            )
        ).one_or_none()
        if old is None or old.cursor != record.cursor or old.cursor >= selected.stack.cursor:
            raise AuditIntegrityError("application journal lost its actual selected predecessor checkpoint")
        try:
            old_text = read_chunk(conn, session_id=sid, operation_id=operation_id, key=old.snapshot_chunk_key).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("application predecessor is not strict UTF-8") from error
        old_root = decode_continuation_snapshot(old_text)
        if (
            old_root.logical != selected.stack.logical
            or old_root.mutation_ceiling != selected.stack.mutation_ceiling
            or old_root.policy_hash != selected.stack.policy_hash
            or old_root.admission_policy_json != selected.stack.admission_policy_json
            or old_root.cursor != record.cursor
            or len(old_root.pending_owner_results) != 1
            or old_root.pending_owner_results[0].effect_key != record.effect_key
            or old_root.pending_owner_results[0].kind != "persisted_turn"
            or old_root.pending_owner_results[0].result != record.result
        ):
            raise AuditIntegrityError("application predecessor changed its original complete pending cut")
        old_batch = old_root.frames[-1]
        old_compose = tuple(frame for frame in old_root.frames if type(frame) is ComposeFrame)
        if (
            type(old_batch) is not ToolBatchFrame
            or old_batch.phase != "effect_reconciling"
            or old_batch.response != record.response
            or old_batch.accumulator != record.pending_batch
            or old_batch.next_tool_index != pending.batch.next_tool_index
            or record.effect_key in old_batch.settled_effect_ids
            or len(old_compose) != 1
        ):
            raise AuditIntegrityError("application predecessor changed its selected frame inputs")
        before_snapshot = decode_turn_evidence(
            _private_chunk_on_connection(conn, running, old_compose[0].evidence), env_ref_names=env_ref_names
        )
        cut = graph.cuts[index]
        before_graph = ApplicationGraph(cut.data, cut.effects_before, cut.recorder_before, graph.cuts[:index])
        retained_graph = (
            before_snapshot.application_entry.graph_json
            if before_snapshot.application_entry is not None
            else before_snapshot.joint_graph_json
        )
        if (
            before_snapshot.logical != entry.logical
            or before_snapshot.actor_user_id != evidence.actor_user_id
            or retained_graph != encode_application_graph(before_graph, env_ref_names=env_ref_names)
            or before_snapshot.effects != retained_effects(cut.effects_before)
            or before_snapshot.recorder_json != encode_recorder_snapshot(cut.recorder_before).canonical
        ):
            raise AuditIntegrityError("application journal changed its actual retained before graph")
        frontier = tuple(item for item in selected.stack.effect_frontier if item.effect_id == record.effect_key)
        if len(frontier) != 1 or frontier[0].disposition != "committed" or frontier[0].receipt_id != record.receipt_id:
            raise AuditIntegrityError("application journal lost its current committed frontier")
        producer = P4EffectProducerData(
            effect.creator_sequence, effect.creator_generation, creator.session_operation_id, creator.session_operation_epoch
        )
        require_portable_p4_application(entry, result_chunk=result_chunk, result=result, producer=producer, env_ref_names=env_ref_names)


def apply_p4_application_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    expected_evidence_chunk: PrivateChunk,
    expected_pending_batch_chunk: PrivateChunk,
) -> StoredP4Application:
    """Consume one checked result and append its complete successor atomically."""
    compose_index = _compose_frame_index(selected)
    compose = selected.stack.frames[compose_index]
    batch = selected.stack.frames[-1]
    if type(compose) is not ComposeFrame or type(batch) is not ToolBatchFrame or batch.phase != "effect_reconciling":
        raise AuditIntegrityError("P4 application lost its actual selected frame owners")
    if type(expected_evidence_chunk) is not PrivateChunk or type(expected_pending_batch_chunk) is not PrivateChunk:
        raise AuditIntegrityError("P4 application requires complete original private input chunks")
    effect_key = f"batch:{batch.response.digest}:p4"
    checked = read_owed_p4_evidence_on_connection(conn, running, selected=selected, inventory=inventory, effect_key=effect_key)
    frontier = tuple(item for item in selected.stack.effect_frontier if item.effect_id == effect_key)
    actual_receipt_id = (
        str(checked.result.receipt.assistant_id) if checked.result.receipt.assistant_id is not None else f"unavailable:{effect_key}"
    )
    if len(frontier) != 1 or frontier[0].disposition != "committed" or frontier[0].receipt_id != actual_receipt_id:
        raise AuditIntegrityError("P4 application changed its actual committed result frontier")
    if (
        expected_evidence_chunk.reference != compose.evidence
        or expected_pending_batch_chunk.reference != batch.accumulator
        or _private_chunk_on_connection(conn, running, compose.evidence) != expected_evidence_chunk
        or _private_chunk_on_connection(conn, running, batch.accumulator) != expected_pending_batch_chunk
    ):
        raise AuditIntegrityError("P4 application changed its expected complete selected evidence/batch")
    require_p4_application_journal_on_connection(conn, running, selected=selected, evidence=checked.snapshot, env_ref_names=inventory.names)
    response = _private_chunk_on_connection(conn, running, batch.response)
    producer = P4EffectProducerData(
        checked.creator_sequence, checked.creator_generation, checked.creator_operation_id, checked.creator_operation_epoch
    )
    bundle = build_portable_p4_application(
        logical=selected.stack.logical,
        checkpoint_id=selected.checkpoint_id,
        cursor=selected.stack.cursor,
        response_chunk=response,
        pending_batch_chunk=expected_pending_batch_chunk,
        evidence_snapshot=checked.snapshot,
        result_chunk=checked.result_chunk,
        result=checked.result,
        producer=producer,
        env_ref_names=inventory.names,
    )
    current_state_id = bundle.evidence_after.effects.latest_committed_state_id
    if not checked.result.outcome.unwind_audit_failed and current_state_id != checked.result.outcome.current_state_id:
        raise AuditIntegrityError("P4 application changed its actual persisted state frontier")
    if effect_key in batch.settled_effect_ids:
        raise AuditIntegrityError("P4 application already belongs to its selected settled batch")
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
    )
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    retained = [bundle.evidence_chunk, bundle.batch_chunk, bundle.audit_chunk, bundle.state_chunk]
    for record in bundle.entry.applications:
        retained.extend(
            (
                PrivateChunk(record.response, record.response_json),
                PrivateChunk(record.pending_batch, record.pending_batch_json),
                PrivateChunk(record.prepared_input, record.prepared_input_json),
                PrivateChunk(record.result, record.result_json),
            )
        )
    for chunk in retained:
        verify_private_chunk(chunk.reference, chunk.canonical)
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    following = list(selected.stack.frames)
    following[compose_index] = compose.model_copy(
        update={
            "evidence": bundle.evidence_chunk.reference,
            "audit_buffer": bundle.audit_chunk.reference,
            "state": bundle.state_chunk.reference,
            "current_state_id": current_state_id,
        }
    )
    following[-1] = batch.model_copy(
        update={
            "phase": "result_retained",
            "accumulator": bundle.batch_chunk.reference,
            "settled_effect_ids": (*batch.settled_effect_ids, effect_key),
        }
    )
    root = selected.stack.model_copy(
        update={
            "frames": tuple(following),
            "pending_owner_results": tuple(item for item in selected.stack.pending_owner_results if item.effect_key != effect_key),
        }
    )
    snapshot = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
        snapshot_json=encode_continuation_snapshot(root),
        event_batch=(
            ContinuationEvent(
                "checkpoint",
                canonical_json(
                    {
                        "phase": "p4_application_committed",
                        "effect_key": effect_key,
                        "selected_checkpoint_id": selected.checkpoint_id,
                        "selected_cursor": selected.stack.cursor,
                        "evidence_digest": bundle.evidence_chunk.reference.digest,
                        "batch_digest": bundle.batch_chunk.reference.digest,
                    }
                ),
            ),
        ),
        handoff=False,
    )
    return StoredP4Application(snapshot, bundle)
