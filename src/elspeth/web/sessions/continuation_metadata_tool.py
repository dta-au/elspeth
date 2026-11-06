"""Original metadata tool issue/completion inside a fully admitted V4 tail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from sqlalchemy import Connection, insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import EffectFrontierEntry, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import (
    decode_coupled_batch_chunk,
    decode_memory_completion_batch_data,
    decode_prepared_batch_data,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_phase_transitions import ToolIssueSource, require_tool_issue_on_connection
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_prepared_tail import PreparedTailPrefix, require_prepared_tail_append
from elspeth.web.sessions.continuation_primary import read_committed_primary_response_on_connection
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk
from elspeth.web.sessions.models import composer_continuation_effects_table as effects


def _batch_chunk(captured: CapturedContinuationRuntime) -> PrivateChunk:
    top = captured.stack.frames[-1]
    if type(top) is not ToolBatchFrame:
        raise AuditIntegrityError("prepared metadata tail lost its tool frame")
    found = tuple(chunk for chunk in captured.chunks if chunk.reference == top.accumulator)
    if len(found) != 1:
        raise AuditIntegrityError("prepared metadata tail lost its complete accumulator")
    return found[0]


def _chunks(captured: CapturedContinuationRuntime) -> tuple[ProspectiveContinuationChunk, ...]:
    for chunk in captured.chunks:
        verify_private_chunk(chunk.reference, chunk.canonical)
    return tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode(), chunk.reference.codec) for chunk in captured.chunks
    )


@dataclass(frozen=True, slots=True)
class PreparedMetadataTransitions:
    original: SelectedContinuationState
    issue: ToolIssueSource
    issue_prefix: PreparedTailPrefix
    completion_prefix: PreparedTailPrefix
    issued: SelectedContinuationState
    completed: SelectedContinuationState
    original_values: tuple[object, ...]

    def require_original(self) -> None:
        if self.original_values != (self.original, self.issue, self.issue_prefix, self.completion_prefix, self.issued, self.completed):
            raise AuditIntegrityError("prepared metadata transition changed its original complete values")


def prepare_metadata_transitions_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    original: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    issued: CapturedContinuationRuntime,
    completed: CapturedContinuationRuntime,
    memory_completion: ProspectiveContinuationChunk,
    published_at: datetime,
) -> PreparedMetadataTransitions:
    _require_current_continuation_running(conn, running)
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=original.checkpoint_id, expected_cursor=original.stack.cursor
    )
    before = original.stack
    policy = decode_continuation_policy(before.admission_policy_json, before.policy_hash)
    top = before.frames[-1]
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or type(top) is not ToolBatchFrame
        or top.phase != "tool_ready"
        or top.next_tool_index != 0
        or before.active_action_json is not None
        or before.active_action_source is not None
        or issued.selected != original
        or completed.selected != original
    ):
        raise AuditIntegrityError("prepared metadata transition lost its original unissued bounded source")
    response = read_committed_primary_response_on_connection(conn, running, selected=original, reference=top.response)
    calls = decode_continuation_response(response).tool_batch.calls
    prepared = decode_prepared_batch_data(_batch_chunk(issued).canonical, env_ref_names=inventory.names)
    finished = decode_coupled_batch_chunk(_batch_chunk(completed), env_ref_names=inventory.names)
    if (
        type(memory_completion) is not ProspectiveContinuationChunk
        or memory_completion.codec != "composer.memory-completion-batch-data.v1"
        or memory_completion.key != f"metadata-memory:{sha256(memory_completion.payload).hexdigest()}"
    ):
        raise AuditIntegrityError("prepared metadata completion changed its original DATA codec")
    memory = decode_memory_completion_batch_data(memory_completion.payload.decode("utf-8"), env_ref_names=inventory.names)
    call = prepared.batch.current_call
    if (
        len(calls) != 1
        or calls[0].function.name not in ("set_metadata", "set_pipeline")
        or prepared.calls != calls
        or finished.calls != calls
        or call is None
        or call.phase != "prepared"
        or call.index != 0
        or call.effect_key is not None
        or prepared.batch.tool_outcomes
        or finished.batch.phase != "dispatch_ready"
        or finished.batch.current_call is not None
        or finished.batch.next_tool_index != 1
        or len(finished.batch.tool_outcomes) != 1
        or finished.batch.tool_outcomes[0].call != calls[0]
        or (call.original_creator_id, call.original_creator_epoch)
        != (running.session_operation_context.fence.operation_id, running.session_operation_context.fence.operation_epoch)
    ):
        raise AuditIntegrityError("prepared metadata transition changed its original provider call or completed result")
    memory_call = memory.batch.current_call
    if (
        memory.calls != calls
        or memory_call is None
        or memory_call.tool_call != call.tool_call
        or memory_call.index != call.index
        or memory_call.memory_result != finished.batch.tool_outcomes[0].response
        or memory_call.owner_tool_receipt_json is None
    ):
        raise AuditIntegrityError("prepared metadata result lost its complete original memory receipt")
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation
    if sequence is None or generation is None:
        raise AuditIntegrityError("prepared metadata transition lost its physical owner")
    key = f"tool:{top.response.digest}:0"
    issue_frame = issued.stack.frames[-1]
    complete_frame = completed.stack.frames[-1]
    if type(issue_frame) is not ToolBatchFrame or type(complete_frame) is not ToolBatchFrame:
        raise AuditIntegrityError("prepared metadata transition lost its tool frames")
    issue = ToolIssueSource(
        previous_checkpoint_id=original.checkpoint_id,
        cursor=before.cursor + 1,
        sequence=sequence,
        generation=generation,
        creator_operation_id=running.session_operation_context.fence.operation_id,
        creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
        response=top.response,
        prepared=issue_frame.accumulator,
        tool_index=0,
        tool_call_id=call.tool_call.id,
        input_sha256=stable_hash(
            {"tool_call_id": call.tool_call.id, "name": call.tool_name, "arguments": call.private_arguments_canonical}
        ),
    )
    source = load_snapshot(conn, session_id=before.logical.session_id, operation_id=before.logical.operation_id)
    if source != StoredContinuationSnapshot(encode_continuation_snapshot(before), before.cursor, original.checkpoint_id):
        raise AuditIntegrityError("prepared metadata transition changed its current SQL head")
    issued_root = issued.stack.model_copy(
        update={
            "frames": (*issued.stack.frames[:-1], issue_frame.model_copy(update={"phase": "effect_reconciling"})),
            "effect_frontier": (*before.effect_frontier, EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None)),
        }
    )
    issue_append = prepare_snapshot_append(
        source,
        snapshot_json=encode_continuation_snapshot(issued_root),
        published_at=published_at,
        events=(
            ContinuationEvent(
                "may_dispatch",
                canonical_json({"owner": "composer_tool_phase", "transition": "tool_issued", "issue": issue.model_dump(mode="json")}),
            ),
        ),
    )
    issued_source = StoredContinuationSnapshot(issue_append.snapshot_json, issue.cursor, issue_append.checkpoint_id)
    receipt = f"tool-result:{complete_frame.accumulator.digest}"
    completed_root = completed.stack.model_copy(
        update={
            "cursor": issue.cursor,
            "frames": (
                *completed.stack.frames[:-1],
                complete_frame.model_copy(update={"phase": "tool_ready", "settled_effect_ids": (*top.settled_effect_ids, key)}),
            ),
            "effect_frontier": (*before.effect_frontier, EffectFrontierEntry(effect_id=key, disposition="committed", receipt_id=receipt)),
        }
    )
    completion_append = prepare_snapshot_append(
        issued_source,
        snapshot_json=encode_continuation_snapshot(completed_root),
        published_at=published_at,
        events=(
            ContinuationEvent(
                "checkpoint",
                canonical_json(
                    {
                        "owner": "composer_tool_phase",
                        "transition": "tool_completed",
                        "effect_key": key,
                        "response": top.response.digest,
                        "batch": complete_frame.accumulator.digest,
                    }
                ),
            ),
        ),
    )
    issue_prefix, completion_prefix = (
        PreparedTailPrefix(issue_append, _chunks(issued)),
        PreparedTailPrefix(
            completion_append,
            (
                *_chunks(completed),
                memory_completion,
            ),
        ),
    )
    issued_selection = SelectedContinuationState(
        issue_append.checkpoint_id, decode_continuation_snapshot(issue_append.snapshot_json), issued.chunks
    )
    completed_selection = SelectedContinuationState(
        completion_append.checkpoint_id, decode_continuation_snapshot(completion_append.snapshot_json), completed.chunks
    )
    values = original, issue, issue_prefix, completion_prefix, issued_selection, completed_selection
    return PreparedMetadataTransitions(*values, original_values=values)


def consume_metadata_transitions_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, plan: PreparedMetadataTransitions, env_ref_names: frozenset[str]
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection

    plan.require_original()
    _require_current_continuation_running(conn, running)
    sid, operation = plan.original.stack.logical.session_id, plan.original.stack.logical.operation_id
    key = f"tool:{plan.issue.response.digest}:0"
    for prefix, selected in ((plan.issue_prefix, plan.issued), (plan.completion_prefix, plan.completed)):
        if not require_prepared_tail_append(conn, session_id=sid, operation_id=operation, append=prefix.append):
            raise AuditIntegrityError("metadata issue requires its complete pre-admitted tail")
        if prefix is plan.issue_prefix:
            conn.execute(
                insert(effects).values(
                    session_id=sid,
                    operation_id=operation,
                    effect_key=key,
                    input_sha256=plan.issue.input_sha256,
                    owner="tool_dispatch",
                    action_json=canonical_json(plan.issue.model_dump(mode="json")),
                    disposition="dispatched",
                    creator_sequence=running.claim.segment_sequence,
                    creator_generation=running.claim.ownership_generation,
                )
            )
        else:
            require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
        for chunk in prefix.chunks:
            put_chunk(conn, session_id=sid, operation_id=operation, key=chunk.key, payload=chunk.payload, codec=chunk.codec)
        if prefix is plan.completion_prefix:
            frame = selected.stack.frames[-1]
            if type(frame) is not ToolBatchFrame:
                raise AuditIntegrityError("metadata completion lost its actual frame")
            results = tuple(chunk for chunk in selected.chunks if chunk.reference == frame.accumulator)
            if len(results) != 1:
                raise AuditIntegrityError("metadata completion lost its original result chunk")
            changed = conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == operation,
                    effects.c.effect_key == key,
                    effects.c.owner == "tool_dispatch",
                    effects.c.disposition == "dispatched",
                    effects.c.creator_sequence == running.claim.segment_sequence,
                    effects.c.creator_generation == running.claim.ownership_generation,
                )
                .values(
                    disposition="committed",
                    receipt_key=f"tool-result:{frame.accumulator.digest}",
                    result_chunk_key=private_chunk_key(results[0]),
                )
            )
            if changed.rowcount != 1:
                raise AuditIntegrityError("metadata completion lost its original issued effect")
        append = prefix.append
        retained = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=append.expected_checkpoint_id,
            expected_cursor=append.expected_cursor,
            snapshot_json=append.snapshot_json,
            event_batch=append.events,
            handoff=False,
            prepared_append=append,
        )
        read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
    return retained
