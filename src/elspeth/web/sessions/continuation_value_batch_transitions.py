"""Original ordered V4 value-batch issue/result prefixes inside one admitted tail.

Preparation is detached DATA; these planners independently bind its original
paid response and current SQL source. Consumers require the full-tail capacity
scope before any issue row or retained result is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING

from sqlalchemy import Connection, insert, update

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, EffectFrontierEntry, ToolBatchFrame
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
from elspeth.web.composer.continuation_env_inventory import require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_phase_transitions import ToolIssueSource, require_tool_issue_on_connection
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_prepared_p4 import prepared_p4_members
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime, _selected_chunks
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
from elspeth.web.sessions.continuation_value_batch_refusal import (
    ValueBatchPreparationRefusal,
    read_value_batch_preparation_refusal_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatch
    from elspeth.web.required_work import RequiredPreviewProduced


def value_batch_chunk(selected: SelectedContinuationState) -> PrivateChunk:
    top = selected.stack.frames[-1]
    if type(top) is not ToolBatchFrame:
        raise AuditIntegrityError("value batch lost its original tool frame")
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == top.accumulator)
    if len(matches) != 1:
        raise AuditIntegrityError("value batch lost its exact accumulator")
    verify_private_chunk(matches[0].reference, matches[0].canonical)
    return matches[0]


def _prospective(chunks: tuple[PrivateChunk, ...]) -> tuple[ProspectiveContinuationChunk, ...]:
    for chunk in chunks:
        verify_private_chunk(chunk.reference, chunk.canonical)
    return tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec) for chunk in chunks
    )


def _stored(selected: SelectedContinuationState) -> StoredContinuationSnapshot:
    return StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)


@dataclass(frozen=True, slots=True)
class PreparedValueStepTransition:
    index: int
    predecessor: SelectedContinuationState
    issue: ToolIssueSource | None
    refusal: ValueBatchPreparationRefusal | None
    issued: SelectedContinuationState | None
    completed: SelectedContinuationState
    prefixes: tuple[PreparedTailPrefix, ...]


@dataclass(frozen=True, slots=True)
class PreparedValueBatchTransitions:
    original: SelectedContinuationState
    prepared: PreparedValueBatch
    steps: tuple[PreparedValueStepTransition, ...]
    prefixes: tuple[PreparedTailPrefix, ...]
    completed: SelectedContinuationState
    original_values: tuple[object, ...]

    def require_original(self) -> None:
        from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_value_batch

        require_prepared_value_batch(self.prepared)
        if self.original_values != (self.original, self.prepared, self.steps, self.prefixes, self.completed):
            raise AuditIntegrityError("value-batch SQL plan changed its original ordered fields")


def _capture_selection(
    capture: CapturedContinuationRuntime,
    *,
    original: SelectedContinuationState,
    preceding: SelectedContinuationState,
) -> SelectedContinuationState:
    before, root = original.stack, capture.stack
    if (
        capture.selected != original
        or root.model_copy(update={"frames": before.frames}) != before
        or root.logical != before.logical
        or root.mutation_ceiling != before.mutation_ceiling
        or root.admission_policy_json != before.admission_policy_json
        or root.policy_hash != before.policy_hash
        or root.provider_cost_policy != before.provider_cost_policy
        or root.cursor != before.cursor
        or len(root.frames) != 2
        or type(root.frames[0]) is not ComposeFrame
        or type(root.frames[-1]) is not ToolBatchFrame
        or root.pending_owner_results
        or root.pending_settlement_ids
        or root.active_action_json != before.active_action_json
        or root.active_action_source != before.active_action_source
    ):
        raise AuditIntegrityError("value-batch capture changed immutable original request or frame ownership")
    rebased = root.model_copy(update={"cursor": preceding.stack.cursor, "effect_frontier": preceding.stack.effect_frontier})
    return SelectedContinuationState(preceding.checkpoint_id, rebased, _selected_chunks(rebased, preceding.chunks, capture.chunks))


def prepare_value_batch_transitions_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    prepared: PreparedValueBatch,
    published_at: datetime,
) -> PreparedValueBatchTransitions:
    from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_value_batch

    require_prepared_value_batch(prepared)
    original, inventory = prepared.selected, prepared.inventory
    members = prepared_p4_members(prepared.prepared, env_ref_names=inventory.names)
    _require_current_continuation_running(conn, running)
    if inventory.running is not running:
        raise AuditIntegrityError("value batch changed its original current inventory owner")
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=original.checkpoint_id, expected_cursor=original.stack.cursor
    )
    root = original.stack
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    top = root.frames[-1]
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or type(top) is not ToolBatchFrame
        or top.phase != "tool_ready"
        or top.next_tool_index != 0
        or top.settled_effect_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or not prepared.steps
        or load_snapshot(conn, session_id=root.logical.session_id, operation_id=root.logical.operation_id) != _stored(original)
    ):
        raise AuditIntegrityError("value batch requires its actual wholly unissued V4 source")
    response = read_committed_primary_response_on_connection(conn, running, selected=original, reference=top.response)
    calls = decode_continuation_response(response).tool_batch.calls
    initial = decode_coupled_batch_chunk(value_batch_chunk(original), env_ref_names=inventory.names)
    if (
        initial.calls != calls
        or prepared.requested_calls != calls
        or len(members.invocations) != len(calls)
        or initial.batch.tool_outcomes
        or initial.batch.current_call is not None
    ):
        raise AuditIntegrityError("value batch changed its original paid provider call list")
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation
    if sequence is None or generation is None:
        raise AuditIntegrityError("value batch lost its current original physical owner")
    following = original
    steps: list[PreparedValueStepTransition] = []
    for index, step in enumerate(prepared.steps):
        before = following
        before_frame = before.stack.frames[-1]
        if type(before_frame) is not ToolBatchFrame or step.index != index or index >= len(calls):
            raise AuditIntegrityError("value batch changed original call order or index")
        prior = decode_coupled_batch_chunk(value_batch_chunk(before), env_ref_names=inventory.names)
        if (
            prior.calls != calls
            or prior.batch.phase != "call_ready"
            or prior.batch.next_tool_index != index
            or prior.batch.current_call is not None
            or len(prior.batch.tool_outcomes) != index
            or prior.batch.stop_issued_batch
            or before_frame.next_tool_index != index
        ):
            raise AuditIntegrityError("value batch did not preserve its original ordered dispatch-ready predecessor")
        completed_capture = _capture_selection(step.completed, original=original, preceding=before)
        frame = completed_capture.stack.frames[-1]
        if type(frame) is not ToolBatchFrame:
            raise AuditIntegrityError("value batch lost its complete result frame")
        result = decode_coupled_batch_chunk(value_batch_chunk(completed_capture), env_ref_names=inventory.names)
        if (
            result.calls != calls
            or result.batch.phase != ("dispatch_ready" if index + 1 == len(calls) else "call_ready")
            or result.batch.current_call is not None
            or result.batch.next_tool_index != index + 1
            or frame.next_tool_index != index + 1
            or frame.response != top.response
            or len(result.batch.tool_outcomes) != index + 1
            or result.batch.tool_outcomes[:-1] != prior.batch.tool_outcomes
            or result.batch.tool_outcomes[-1].call != calls[index]
        ):
            raise AuditIntegrityError("value batch changed its exact completed original result prefix")
        step_prefixes: list[PreparedTailPrefix] = []
        issue: ToolIssueSource | None = None
        refusal: ValueBatchPreparationRefusal | None = None
        issued_selection: SelectedContinuationState | None = None
        key = f"tool:{top.response.digest}:{index}"
        frontier = before.stack.effect_frontier
        settled = before_frame.settled_effect_ids
        if step.issued is not None:
            issued_capture = _capture_selection(step.issued, original=original, preceding=before)
            issued_frame = issued_capture.stack.frames[-1]
            if type(issued_frame) is not ToolBatchFrame or step.memory_completion is None:
                raise AuditIntegrityError("value batch lost its actual prepared dispatch and memory result")
            data = decode_prepared_batch_data(value_batch_chunk(issued_capture).canonical, env_ref_names=inventory.names)
            call = data.batch.current_call
            if (
                data.calls != calls
                or data.batch.tool_outcomes != prior.batch.tool_outcomes
                or call is None
                or call.phase != "prepared"
                or call.index != index
                or call.tool_call != calls[index]
                or call.effect_key is not None
                or (call.original_creator_id, call.original_creator_epoch)
                != (running.session_operation_context.fence.operation_id, running.session_operation_context.fence.operation_epoch)
            ):
                raise AuditIntegrityError("value batch changed its original prepared call or audit owner")
            memory = step.memory_completion
            if (
                memory.codec != "composer.memory-completion-batch-data.v1"
                or memory.key != f"value-memory:{sha256(memory.payload).hexdigest()}"
            ):
                raise AuditIntegrityError("value batch memory result changed its closed codec")
            memory_data = decode_memory_completion_batch_data(memory.payload.decode("utf-8"), env_ref_names=inventory.names)
            memory_call = memory_data.batch.current_call
            if (
                memory_data.calls != calls
                or memory_data.audit.model_copy(update={"elapsed_active_ns": data.audit.elapsed_active_ns}) != data.audit
                or memory_data.audit.elapsed_active_ns < data.audit.elapsed_active_ns
                or memory_data.dialect != data.dialect
                or memory_call is None
                or memory_call.tool_call != call.tool_call
                or memory_call.index != index
                or memory_call.memory_result != result.batch.tool_outcomes[-1].response
                or memory_call.owner_tool_receipt_json is None
            ):
                raise AuditIntegrityError("value batch changed its exact actual memory receipt")
            issue = ToolIssueSource(
                previous_checkpoint_id=before.checkpoint_id,
                cursor=before.stack.cursor + 1,
                sequence=sequence,
                generation=generation,
                creator_operation_id=running.session_operation_context.fence.operation_id,
                creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
                response=top.response,
                prepared=issued_frame.accumulator,
                tool_index=index,
                tool_call_id=call.tool_call.id,
                input_sha256=stable_hash(
                    {"tool_call_id": call.tool_call.id, "name": call.tool_name, "arguments": call.private_arguments_canonical}
                ),
            )
            issued_root = issued_capture.stack.model_copy(
                update={
                    "frames": (
                        *issued_capture.stack.frames[:-1],
                        issued_frame.model_copy(update={"phase": "effect_reconciling", "settled_effect_ids": settled}),
                    ),
                    "effect_frontier": (*frontier, EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None)),
                }
            )
            append = prepare_snapshot_append(
                _stored(before),
                snapshot_json=encode_continuation_snapshot(issued_root),
                published_at=published_at,
                events=(
                    ContinuationEvent(
                        "may_dispatch",
                        canonical_json(
                            {"owner": "composer_tool_phase", "transition": "tool_issued", "issue": issue.model_dump(mode="json")}
                        ),
                    ),
                ),
            )
            issued_selection = SelectedContinuationState(
                append.checkpoint_id, decode_continuation_snapshot(append.snapshot_json), issued_capture.chunks
            )
            step_prefixes.append(PreparedTailPrefix(append, _prospective(issued_capture.chunks)))
            following = issued_selection
            frontier = (
                *frontier,
                EffectFrontierEntry(effect_id=key, disposition="committed", receipt_id=f"tool-result:{frame.accumulator.digest}"),
            )
            settled = (*settled, key)
            transition = "tool_completed"
        else:
            if step.memory_completion is not None or members.invocations[index].status is not ComposerToolStatus.ARG_ERROR:
                raise AuditIntegrityError("unissued preparation error cannot acquire a memory execution receipt")
            transition = "tool_preparation_completed"
            refusal = ValueBatchPreparationRefusal(
                previous_checkpoint_id=before.checkpoint_id,
                cursor=before.stack.cursor + 1,
                sequence=sequence,
                generation=generation,
                creator_operation_id=running.session_operation_context.fence.operation_id,
                creator_operation_epoch=running.session_operation_context.fence.operation_epoch,
                response=top.response,
                predecessor=before_frame.accumulator,
                result=frame.accumulator,
                tool_index=index,
                tool_call_id=calls[index].id,
                input_sha256=stable_hash(
                    {"tool_call_id": calls[index].id, "name": calls[index].function.name, "arguments": calls[index].function.arguments}
                ),
            )
            frontier = (
                *frontier,
                EffectFrontierEntry(
                    effect_id=key, disposition="refused", receipt_id=f"value-preparation-refusal:{frame.accumulator.digest}"
                ),
            )
            settled = (*settled, key)
        completed_root = completed_capture.stack.model_copy(
            update={
                "cursor": following.stack.cursor,
                "frames": (
                    *completed_capture.stack.frames[:-1],
                    frame.model_copy(update={"phase": "tool_ready", "settled_effect_ids": settled}),
                ),
                "effect_frontier": frontier,
            }
        )
        append = prepare_snapshot_append(
            _stored(following),
            snapshot_json=encode_continuation_snapshot(completed_root),
            published_at=published_at,
            events=(
                ContinuationEvent(
                    "checkpoint",
                    canonical_json(
                        {"owner": "composer_value_batch", "transition": "preparation_refused", "source": refusal.model_dump(mode="json")}
                        if refusal is not None
                        else {
                            "owner": "composer_tool_phase",
                            "transition": transition,
                            "effect_key": key if issue else None,
                            "response": top.response.digest,
                            "batch": frame.accumulator.digest,
                        }
                    ),
                ),
            ),
        )
        following = SelectedContinuationState(
            append.checkpoint_id, decode_continuation_snapshot(append.snapshot_json), completed_capture.chunks
        )
        chunks = (
            *_prospective(completed_capture.chunks),
            *_prospective(step.source_chunks),
            *((step.memory_completion,) if step.memory_completion is not None else ()),
        )
        step_prefixes.append(PreparedTailPrefix(append, chunks))
        steps.append(PreparedValueStepTransition(index, before, issue, refusal, issued_selection, following, tuple(step_prefixes)))
    if len(steps) != len(calls):
        raise AuditIntegrityError("value batch omitted an original provider call")
    prefixes = tuple(prefix for step in steps for prefix in step.prefixes)
    values = original, prepared, tuple(steps), prefixes, following
    return PreparedValueBatchTransitions(*values, original_values=values)


def consume_value_batch_transitions_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    plan: PreparedValueBatchTransitions,
    env_ref_names: frozenset[str],
    issued_preview: RequiredPreviewProduced | None = None,
) -> StoredContinuationSnapshot:
    """Consume exact actual issue/result prefixes under full-tail reservation."""
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection

    if type(plan) is not PreparedValueBatchTransitions:
        raise TypeError("value-batch consumption requires its nominal original plan")
    plan.require_original()
    if issued_preview is None:
        _require_current_continuation_running(conn, running)
    else:
        from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_issued_preview_batch

        require_prepared_issued_preview_batch(plan.prepared, issued_preview)
        _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation = plan.original.stack.logical.session_id, plan.original.stack.logical.operation_id
    if env_ref_names != plan.prepared.inventory.names or plan.prepared.inventory.running is not running:
        raise AuditIntegrityError("value-batch consumption changed its original inventory owner")
    retained = _stored(plan.original)
    original_frame = plan.original.stack.frames[-1]
    if type(original_frame) is not ToolBatchFrame:
        raise AuditIntegrityError("value batch lost its original frame at SQL consumption")
    for step in plan.steps:
        if load_snapshot(conn, session_id=sid, operation_id=operation) != _stored(step.predecessor):
            raise AuditIntegrityError("value-batch consumption changed its next actual predecessor")
        for prefix in step.prefixes:
            if not require_prepared_tail_append(conn, session_id=sid, operation_id=operation, append=prefix.append):
                raise AuditIntegrityError("value-batch issue requires its complete pre-admitted P4 tail")
            is_issue = issued_preview is None and step.issue is not None and prefix is step.prefixes[0]
            key = f"tool:{original_frame.response.digest}:{step.index}"
            if is_issue:
                if step.issue is None:
                    raise AuditIntegrityError("value batch lost its nominal issue")
                conn.execute(
                    insert(effects).values(
                        session_id=sid,
                        operation_id=operation,
                        effect_key=key,
                        input_sha256=step.issue.input_sha256,
                        owner="tool_dispatch",
                        action_json=canonical_json(step.issue.model_dump(mode="json")),
                        disposition="dispatched",
                        creator_sequence=running.claim.segment_sequence,
                        creator_generation=running.claim.ownership_generation,
                    )
                )
            elif step.issue is not None:
                require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=env_ref_names)
            for chunk in prefix.chunks:
                put_chunk(conn, session_id=sid, operation_id=operation, key=chunk.key, payload=chunk.payload, codec=chunk.codec)
            if step.refusal is not None:
                result = value_batch_chunk(step.completed)
                conn.execute(
                    insert(effects).values(
                        session_id=sid,
                        operation_id=operation,
                        effect_key=key,
                        input_sha256=step.refusal.input_sha256,
                        owner="tool_dispatch",
                        action_json=canonical_json(step.refusal.model_dump(mode="json")),
                        disposition="refused",
                        creator_sequence=running.claim.segment_sequence,
                        creator_generation=running.claim.ownership_generation,
                        receipt_key=f"value-preparation-refusal:{result.reference.digest}",
                        result_chunk_key=private_chunk_key(result),
                    )
                )
            if not is_issue and step.issue is not None:
                result = value_batch_chunk(step.completed)
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
                        receipt_key=f"tool-result:{result.reference.digest}",
                        result_chunk_key=private_chunk_key(result),
                    )
                )
                if changed.rowcount != 1:
                    raise AuditIntegrityError("value batch lost its actual original dispatched effect CAS")
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
            selected = step.issued if is_issue else step.completed
            if selected is None:
                raise AuditIntegrityError("value batch lost its exact retained selection")
            read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
            if issued_preview is not None:
                from elspeth.web.sessions.continuation_preview_result import consume_preview_terminal_hold_on_connection

                consume_preview_terminal_hold_on_connection(
                    conn,
                    session_id=sid,
                    operation_id=operation,
                    effect_key=key,
                    returned=issued_preview.returned,
                    env_ref_names=env_ref_names,
                    consumed_at=prefix.append.published_at,
                )
            if (
                step.refusal is not None
                and read_value_batch_preparation_refusal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=operation, effect_key=key, env_ref_names=env_ref_names
                )
                != step.refusal
            ):
                raise AuditIntegrityError("value batch lost its actual retained preparation refusal")
    if retained != _stored(plan.completed):
        raise AuditIntegrityError("value batch did not consume its exact complete prefix")
    return retained


def prepare_issued_preview_transitions_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    prepared: PreparedValueBatch,
    produced: RequiredPreviewProduced,
    published_at: datetime,
) -> PreparedValueBatchTransitions:
    """Plan the actual already-issued result, never create another issue."""
    from elspeth.web.composer.continuation_tool_batch import encode_tool_result_value
    from elspeth.web.composer.continuation_value_batch_preparation import require_prepared_issued_preview_batch
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.sessions.continuation_preview_result import CompletePreviewReturn, read_preview_terminal_hold_on_connection

    require_prepared_issued_preview_batch(prepared, produced)
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if type(produced.returned) is not CompletePreviewReturn:
        raise AuditIntegrityError("preview result tail substituted an incomplete return")
    returned = produced.returned
    source = returned.original.source
    original, inventory = prepared.selected, prepared.inventory
    root, top = original.stack, original.stack.frames[-1]
    key = f"tool:{source.response.digest}:{source.tool_index}"
    contract = read_preview_terminal_hold_on_connection(
        conn, session_id=source.session_id, operation_id=source.operation_id, effect_key=key, env_ref_names=inventory.names
    )
    if (
        contract.source != source
        or inventory.running is not running
        or inventory.selection.checkpoint_id != original.checkpoint_id
        or inventory.selection.cursor != root.cursor
        or inventory.selection.snapshot_json != encode_continuation_snapshot(root)
        or type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
        or type(top) is not ToolBatchFrame
        or top.phase != "effect_reconciling"
        or top.response != source.response
        or root.cursor != source.cursor + 1
        or root.logical.session_id != source.session_id
        or root.logical.operation_id != source.operation_id
        or len(prepared.steps) != 1
        or source.tool_index != 0
        or key in top.settled_effect_ids
        or root.active_action_json is not None
        or root.active_action_source is not None
        or load_snapshot(conn, session_id=source.session_id, operation_id=source.operation_id) != _stored(original)
    ):
        raise AuditIntegrityError("preview result lost its actual reserved issued source")
    issue = require_tool_issue_on_connection(conn, running, effect_key=key, env_ref_names=inventory.names)
    prior = decode_prepared_batch_data(value_batch_chunk(original).canonical, env_ref_names=inventory.names)
    call = prior.batch.current_call
    step = prepared.steps[0]
    if (
        call is None
        or prior.calls != prepared.requested_calls
        or len(prior.calls) != 1
        or call.tool_name != "preview_pipeline"
        or call.tool_call.id != source.tool_call_id
        or call.arguments != {}
        or step.index != 0
        or step.issued is not None
        or step.memory_completion is not None
        or step.source_chunks != (returned.chunk,)
    ):
        raise AuditIntegrityError("preview result replaced its exact original prepared call")
    captured = _capture_selection(step.completed, original=original, preceding=original)
    frame = captured.stack.frames[-1]
    if type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("preview result lost its complete actual frame")
    completed = decode_coupled_batch_chunk(value_batch_chunk(captured), env_ref_names=inventory.names)
    response = completed.batch.tool_outcomes[0].response if len(completed.batch.tool_outcomes) == 1 else None
    members = prepared_p4_members(prepared.prepared, env_ref_names=inventory.names)
    if (
        completed.calls != prior.calls
        or completed.batch.phase != "dispatch_ready"
        or completed.batch.current_call is not None
        or completed.batch.next_tool_index != 1
        or len(completed.batch.tool_outcomes) != 1
        or completed.batch.tool_outcomes[0].call != prior.calls[0]
        or type(response) is not ToolResult
        or len(members.invocations) != 1
        or members.invocations[0].tool_call_id != source.tool_call_id
        or encode_tool_result_value("preview_pipeline", response, env_ref_names=inventory.names) != returned.original.data.result
    ):
        raise AuditIntegrityError("preview result changed its complete original returned DTO or audit")
    expected_frontier = tuple(entry for entry in root.effect_frontier if entry.effect_id == key)
    if expected_frontier != (EffectFrontierEntry(effect_id=key, disposition="dispatched", receipt_id=None),):
        raise AuditIntegrityError("preview result replaced its original unresolved effect")
    frontier = tuple(
        EffectFrontierEntry(effect_id=key, disposition="committed", receipt_id=f"tool-result:{frame.accumulator.digest}")
        if entry.effect_id == key
        else entry
        for entry in root.effect_frontier
    )
    completed_root = captured.stack.model_copy(
        update={
            "frames": (
                *captured.stack.frames[:-1],
                frame.model_copy(update={"phase": "tool_ready", "settled_effect_ids": (*top.settled_effect_ids, key)}),
            ),
            "effect_frontier": frontier,
        }
    )
    append = prepare_snapshot_append(
        _stored(original),
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
                        "batch": frame.accumulator.digest,
                    }
                ),
            ),
        ),
    )
    following = SelectedContinuationState(append.checkpoint_id, decode_continuation_snapshot(append.snapshot_json), captured.chunks)
    prefix = PreparedTailPrefix(append, (*_prospective(captured.chunks), *_prospective(step.source_chunks)))
    planned = PreparedValueStepTransition(0, original, issue, None, None, following, (prefix,))
    values = original, prepared, (planned,), (prefix,), following
    return PreparedValueBatchTransitions(*values, original_values=values)
