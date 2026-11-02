"""Original V3 tool call refusal before preparation, issue, or execution."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, insert, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall
from elspeth.web.composer.continuation import ContinuationValue, EffectFrontierEntry, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, decode_coupled_batch_chunk
from elspeth.web.composer.continuation_codec import (
    decode_continuation_response,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory, require_current_env_inventory_on_connection
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV3, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_primary import (
    read_committed_primary_response_on_connection,
    read_original_committed_primary_response_data_on_connection,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events


class ToolNoExecutionRefusal(ContinuationValue):
    schema_version: Literal["composer.tool-no-execution-refusal.v1"] = "composer.tool-no-execution-refusal.v1"
    session_id: str
    operation_id: str
    effect_key: str
    input_sha256: str
    response_digest: str
    tool_index: int
    tool_call_id: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    operation_epoch: int
    source_checkpoint_id: str
    source_cursor: int
    receipt_key: str
    reason: Literal["retained_tail_unbounded"]
    public_error_json: str


def _event(data: ToolNoExecutionRefusal, result_chunk_key: str) -> str:
    return canonical_json(
        {
            "version": 1,
            "effect_key": data.effect_key,
            "input_sha256": data.input_sha256,
            "receipt_key": data.receipt_key,
            "result_chunk_key": result_chunk_key,
            "source_disposition": "unissued",
        }
    )


def _selected_call(
    conn: Connection, running: ComposerOperationRunning, selected: SelectedContinuationState, names: frozenset[str]
) -> tuple[ToolBatchFrame, RestoredBatchData, _AdmittedToolCall, str]:
    root = selected.stack
    if len(root.frames) != 2 or type(root.frames[-1]) is not ToolBatchFrame:
        raise AuditIntegrityError("V3 tool refusal requires its original tool-ready frame")
    frame = root.frames[-1]
    if frame.phase != "tool_ready" or root.pending_owner_results or root.pending_settlement_ids:
        raise AuditIntegrityError("V3 tool refusal lost its unissued complete source")
    matches = tuple(chunk for chunk in selected.chunks if chunk.reference == frame.accumulator)
    if len(matches) != 1:
        raise AuditIntegrityError("V3 tool refusal lost its exact selected batch chunk")
    data = decode_coupled_batch_chunk(matches[0], env_ref_names=names)
    response = read_committed_primary_response_on_connection(conn, running, selected=selected, reference=frame.response)
    calls = decode_continuation_response(response).tool_batch.calls
    if (
        data.calls != calls
        or data.batch.phase != "call_ready"
        or data.batch.current_call is not None
        or data.batch.next_tool_index != frame.next_tool_index
        or frame.next_tool_index >= len(calls)
    ):
        raise AuditIntegrityError("V3 tool refusal lost its exact next provider-authored call")
    call = calls[frame.next_tool_index]
    effect_key = f"tool:{frame.response.digest}:{frame.next_tool_index}"
    if any(entry.effect_id == effect_key for entry in root.effect_frontier):
        raise AuditIntegrityError("V3 tool refusal found an already issued current call")
    return frame, data, call, effect_key


def refuse_v3_tool_no_execution_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> ToolNoExecutionRefusal:
    """One fenced terminal for the original provider-authored unissued tool call."""
    from elspeth.web.coordination.composer_operation_authority import _select_failure_on_connection, settle_composer_operation_on_connection
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    if type(running) is not ComposerOperationRunning or type(selected) is not SelectedContinuationState or inventory.running is not running:
        raise AuditIntegrityError("V3 tool refusal requires its actual original live owners")
    _require_current_continuation_running(conn, running)
    root = selected.stack
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=root.cursor
    )
    source = load_snapshot(conn, session_id=sid, operation_id=operation)
    if source.checkpoint_id != selected.checkpoint_id or source.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("V3 tool refusal changed its exact current selected root")
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if (
        type(policy) is not ContinuationAdmissionPolicyV3
        or parent.status != "running"
        or parent.continuation_policy_version != policy.schema_version
        or parent.continuation_policy_sha256 != policy.policy_hash
        or parent.continuation_retained_payload_limit_bytes != policy.max_retained_payload_bytes
    ):
        raise AuditIntegrityError("V3 tool refusal lacks its original persisted admission")
    frame, _data, call, effect_key = _selected_call(conn, running, selected, inventory.names)
    if conn.execute(
        select(effects.c.effect_key).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect_key
        )
    ).first():
        raise AuditIntegrityError("V3 tool refusal found an existing current effect")
    input_hash = stable_hash({"tool_call_id": call.id, "name": call.function.name, "arguments": call.function.arguments})
    public_error = project_composer_operation_error(
        AuditIntegrityError("V3 tool result has no proven finite retained tail"), request_id=parent.request_id
    )
    selected_error = _select_failure_on_connection(parent, public_error, authoritative_failure=False)
    receipt = ToolNoExecutionRefusal(
        session_id=sid,
        operation_id=operation,
        effect_key=effect_key,
        input_sha256=input_hash,
        response_digest=frame.response.digest,
        tool_index=frame.next_tool_index,
        tool_call_id=call.id,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=running.session_operation_context.fence.operation_epoch,
        source_checkpoint_id=source.checkpoint_id,
        source_cursor=source.cursor,
        receipt_key=f"tool:no-execution:{input_hash}",
        reason="retained_tail_unbounded",
        public_error_json=canonical_json(selected_error.model_dump(mode="json")),
    )
    chunk = encode_private_chunk(codec="composer.tool-no-execution-refusal.v1", canonical=canonical_json(receipt.model_dump(mode="json")))
    result_chunk_key = private_chunk_key(chunk)
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=result_chunk_key,
        payload=chunk.canonical.encode("utf-8"),
        codec=chunk.reference.codec,
    )
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation,
            effect_key=effect_key,
            input_sha256=input_hash,
            owner="tool_dispatch",
            disposition="refused",
            receipt_key=receipt.receipt_key,
            result_chunk_key=result_chunk_key,
            creator_sequence=receipt.creator_sequence,
            creator_generation=receipt.creator_generation,
        )
    )
    refused_root = root.model_copy(
        update={
            "effect_frontier": (
                *root.effect_frontier,
                EffectFrontierEntry(effect_id=effect_key, disposition="refused", receipt_id=receipt.receipt_key),
            )
        }
    )
    refused = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=source.checkpoint_id,
        expected_cursor=source.cursor,
        snapshot_json=encode_continuation_snapshot(refused_root),
        event_batch=(ContinuationEvent("effect_refused", _event(receipt, result_chunk_key)),),
        handoff=False,
    )
    terminal = settle_composer_operation_on_connection(conn, running, outcome=selected_error, authoritative_failure=True)
    if terminal.status != "failed" or terminal.journal_cursor != refused.cursor + 1:
        raise AuditIntegrityError("V3 tool refusal did not atomically settle its failed terminal")
    return receipt


def read_v3_tool_no_execution_refusal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> ToolNoExecutionRefusal | None:
    """Cold terminal proof for one provider-authored call that never issued."""
    sid = str(session_id)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one_or_none()
    if parent is None or parent.status != "failed":
        return None
    terminal = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    root = decode_continuation_snapshot(terminal.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV3:
        return None
    if (
        parent.continuation_policy_version != policy.schema_version
        or parent.continuation_policy_sha256 != policy.policy_hash
        or parent.continuation_retained_payload_limit_bytes != policy.max_retained_payload_bytes
    ):
        raise AuditIntegrityError("V3 tool refusal changed its original persisted admission")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)
    ).one_or_none()
    if effect is None or effect.disposition != "refused":
        return None
    if (
        effect.owner != "tool_dispatch"
        or effect.action_json is not None
        or effect.committed_state_ids_json is not None
        or effect.graph_sha256 is not None
        or effect.provider_attempt_id is not None
        or effect.physical_sequence is not None
        or effect.physical_generation is not None
        or effect.requested_model is not None
        or effect.response_finished_at is not None
        or effect.receipt_key is None
        or effect.result_chunk_key is None
    ):
        raise AuditIntegrityError("V3 tool refusal changed its unissued effect")
    codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == effect.result_chunk_key
        )
    ).scalar_one_or_none()
    if codec != "composer.tool-no-execution-refusal.v1":
        raise AuditIntegrityError("V3 tool refusal lost its exact result codec")
    try:
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode(
            "utf-8", errors="strict"
        )
        receipt = ToolNoExecutionRefusal.model_validate_json(canonical, strict=True)
        failure = ComposerOperationError.model_validate_json(receipt.public_error_json, strict=True)
    except (UnicodeDecodeError, ValidationError, ValueError) as error:
        raise AuditIntegrityError("V3 tool refusal violates its closed result") from error
    expected_chunk = encode_private_chunk(codec=codec, canonical=canonical)
    if (
        canonical != canonical_json(receipt.model_dump(mode="json"))
        or private_chunk_key(expected_chunk) != effect.result_chunk_key
        or (receipt.session_id, receipt.operation_id, receipt.effect_key, receipt.input_sha256)
        != (sid, operation_id, effect.effect_key, effect.input_sha256)
        or (receipt.creator_sequence, receipt.creator_generation) != (effect.creator_sequence, effect.creator_generation)
        or (receipt.claim_attempt, receipt.operation_epoch) != (parent.attempt, parent.session_operation_epoch)
        or receipt.receipt_key != effect.receipt_key
        or receipt.public_error_json != canonical_json(failure.model_dump(mode="json"))
        or parent.result_json != receipt.public_error_json
        or parent.failure_code != failure.failure_code
        or parent.settled_by != "owner_terminal"
        or terminal.cursor != receipt.source_cursor + 2
    ):
        raise AuditIntegrityError("V3 tool refusal changed its original terminal identity")
    source = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == receipt.source_checkpoint_id,
            checkpoints.c.cursor == receipt.source_cursor,
        )
    ).one_or_none()
    refusal = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.cursor == receipt.source_cursor + 1
        )
    ).one_or_none()
    refused_event = conn.execute(
        select(events).where(
            events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == receipt.source_cursor + 1
        )
    ).one_or_none()
    settled_event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation_id, events.c.sequence == terminal.cursor)
    ).one_or_none()
    if source is None or refusal is None or refused_event is None or settled_event is None:
        raise AuditIntegrityError("V3 tool refusal lost its original source/refusal/terminal journal")
    try:
        source_root = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation_id, key=source.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
        refusal_root = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation_id, key=refusal.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("V3 tool refusal lost strict source snapshot bytes") from error
    if len(source_root.frames) != 2 or type(source_root.frames[-1]) is not ToolBatchFrame:
        raise AuditIntegrityError("V3 tool refusal lost its original tool-ready frame")
    frame = source_root.frames[-1]
    response = read_original_committed_primary_response_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        source_checkpoint_id=receipt.source_checkpoint_id,
        source_cursor=receipt.source_cursor,
        response=frame.response,
    )
    calls = decode_continuation_response(response).tool_batch.calls
    if (
        source.kind != "transition"
        or refusal.kind != "transition"
        or source_root.admission_policy_json != root.admission_policy_json
        or source_root.policy_hash != root.policy_hash
        or frame.phase != "tool_ready"
        or source_root.pending_owner_results
        or source_root.pending_settlement_ids
        or frame.next_tool_index != receipt.tool_index
        or frame.response.digest != receipt.response_digest
        or receipt.effect_key != f"tool:{frame.response.digest}:{frame.next_tool_index}"
        or frame.next_tool_index >= len(calls)
        or calls[frame.next_tool_index].id != receipt.tool_call_id
        or stable_hash(
            {
                "tool_call_id": calls[frame.next_tool_index].id,
                "name": calls[frame.next_tool_index].function.name,
                "arguments": calls[frame.next_tool_index].function.arguments,
            }
        )
        != receipt.input_sha256
        or any(entry.effect_id == effect_key for entry in source_root.effect_frontier)
        or refusal_root.model_copy(update={"cursor": root.cursor}) != root
        or source_root.model_copy(
            update={
                "cursor": root.cursor,
                "effect_frontier": (
                    *source_root.effect_frontier,
                    EffectFrontierEntry(effect_id=effect_key, disposition="refused", receipt_id=receipt.receipt_key),
                ),
            }
        )
        != root
        or refused_event.tag != "effect_refused"
        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=refused_event.payload_chunk_key)
        != _event(receipt, effect.result_chunk_key).encode("utf-8")
        or settled_event.tag != "settled"
        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=settled_event.payload_chunk_key)
        != canonical_json(
            {
                "version": 1,
                "sequence": receipt.creator_sequence,
                "generation": receipt.creator_generation,
                "status": "failed",
                "failure_code": failure.failure_code,
            }
        ).encode("utf-8")
    ):
        raise AuditIntegrityError("V3 tool refusal journal does not prove an unissued call")
    return receipt
