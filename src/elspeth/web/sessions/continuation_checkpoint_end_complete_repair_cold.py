"""Cold post-write proof for the exact END repair complete transaction."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import require_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_withheld import require_end_repair_withheld_reply_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import require_checkpoint_paid_private_audit_on_connection
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_retained_capacity import (
    _require_hold_fields,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import EndCompleteRepairBatch
    from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider


def require_end_complete_repair_postwrite_on_connection(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    batch: EndCompleteRepairBatch,
    pass_sequence: int,
    withheld_sequence: int | None,
    observed_at: datetime,
    hold_state: Literal["active", "consumed"],
) -> SelectedContinuationState:
    """Re-read all result links before H consumption or after it in this lock."""
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
    from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import EndCompleteRepairBatch

    if type(batch) is not EndCompleteRepairBatch or hold_state not in ("active", "consumed"):
        raise AuditIntegrityError("END repair cold check lost its nominal complete batch")
    sid, operation = batch.owed.session_id, batch.owed.operation_id
    running = prepared.running
    if sid != str(running.claim.session_id) or operation != running.claim.operation_id:
        raise AuditIntegrityError("END repair cold check changed its original running operation")
    require_end_complete_pass_on_connection(
        conn,
        session_id=sid,
        prepared=batch.pass_audit,
        sequence_no=pass_sequence,
        created_at=observed_at,
    )
    stored = load_snapshot(conn, session_id=sid, operation_id=operation)
    if (
        stored.checkpoint_id != batch.append.checkpoint_id
        or stored.cursor != batch.append.expected_cursor + 1
        or stored.payload_json != batch.append.snapshot_json
    ):
        raise AuditIntegrityError("END repair cold check lost its complete successor root")
    root = decode_continuation_snapshot(stored.payload_json)
    if root.pending_settlement_ids or root.pending_owner_results or len(root.frames) != 1:
        raise AuditIntegrityError("END repair cold successor retains an unpaid or orphaned owner")
    compose = root.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END repair cold successor lost its single Compose frame")
    if root.active_action_json is not None or root.active_action_source is not None:
        raise AuditIntegrityError("END repair cold successor retained a consumed prescribed action")
    preview = batch.action_preview
    if preview is not None:
        action_chunk = read_bounded_retained_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"value:{preview.frontier.reference.codec}:{preview.frontier.reference.digest}",
            allowance=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
        )
        if (
            root.semantic_frontier != preview.frontier.reference
            or action_chunk is None
            or action_chunk.codec != preview.frontier.reference.codec
            or action_chunk.payload != preview.frontier.canonical.encode("utf-8")
        ):
            raise AuditIntegrityError("END repair cold successor lost its exact semantic action receipt")
    elif root.semantic_frontier != batch.after_base.semantic_frontier:
        raise AuditIntegrityError("END repair cold successor changed its inactive semantic frontier")
    predecessor = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == batch.owed.selected.checkpoint_id,
        )
    ).one_or_none()
    owed_snapshot = (
        read_bounded_retained_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"snapshot:{batch.owed.selected.checkpoint_id}",
            allowance=batch.owed.new_hold.allowance_bytes,
        )
        if predecessor is not None
        else None
    )
    response = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=batch.response_chunk_key,
        allowance=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    if (
        predecessor is None
        or predecessor.cursor != batch.owed.selected.cursor
        or predecessor.snapshot_chunk_key != f"snapshot:{batch.owed.selected.checkpoint_id}"
        or owed_snapshot is None
        or owed_snapshot.codec != "snapshot.v2"
        or owed_snapshot.payload != batch.owed.selected.payload_json.encode("utf-8")
        or response is None
        or response.codec != batch.owed.response.reference.codec
        or response.payload != batch.owed.response.canonical.encode("utf-8")
    ):
        raise AuditIntegrityError("END repair cold check lost the original owed head or complete paid response")
    original_response = PrivateChunk(batch.owed.response.reference, response.payload.decode("utf-8", errors="strict"))
    if (
        decode_retained_physical_audit_call(original_response) != batch.original_call
        or decode_continuation_response(original_response).tool_batch.calls
    ):
        raise AuditIntegrityError("END repair cold check changed its original paid response")
    event = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == stored.cursor
        )
    ).one_or_none()
    expected_event = batch.append.events[0]
    actual_event = (
        read_bounded_retained_chunk(
            conn, session_id=sid, operation_id=operation, key=f"event:{stored.cursor}", allowance=batch.owed.new_hold.allowance_bytes
        )
        if event is not None
        else None
    )
    if (
        event is None
        or event.tag != "checkpoint"
        or event.payload_chunk_key != f"event:{stored.cursor}"
        or actual_event is None
        or actual_event.codec != "event.v2"
        or actual_event.payload != expected_event.payload_json.encode("utf-8")
    ):
        raise AuditIntegrityError("END repair cold check lost its exact original consequence event")
    inventory = tuple(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation
            )
        ).tuples()
    )
    if set(inventory) != {(item.effect_id, item.disposition, item.receipt_id) for item in root.effect_frontier}:
        raise AuditIntegrityError("END repair cold check changed its complete effect inventory")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == batch.owed.effect_key
        )
    ).one()
    outer = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == batch.owed.outer_effect_key
        )
    ).one()
    if (
        child.disposition != "committed"
        or child.provider_attempt_id != batch.owed.attempt_id
        or child.receipt_key != batch.child_receipt
        or child.result_chunk_key != batch.response_chunk_key
        or child.response_finished_at is None
        or outer.disposition != "committed"
        or outer.receipt_key != batch.outer_receipt
        or outer.result_chunk_key != f"value:{compose.iteration.codec}:{compose.iteration.digest}"
    ):
        raise AuditIntegrityError("END repair cold check lost its original child or outer effect receipt")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == batch.owed.attempt_id)).one_or_none()
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=batch.owed.attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    usage = batch.quota_entry
    settled_before_call = False
    if physical_row is not None and entry is not None and physical_row.settled_at is not None:
        finished_at = restore_utc(entry.recorded_at)
        settled_at = restore_utc(physical_row.settled_at)
        if conn.dialect.name == "sqlite":
            # database_now uses SQLite CURRENT_TIMESTAMP, which has whole-second
            # precision; a provider completion in that second is still earlier
            # than the settlement even when its microseconds compare greater.
            finished_at = finished_at.replace(microsecond=0)
        elif conn.dialect.name != "postgresql":
            raise AuditIntegrityError("END repair quota proof has unsupported SQL dialect")
        settled_before_call = settled_at < finished_at
    if (
        physical_row is None
        or physical_row.session_id != sid
        # The quota attempt belongs to the enclosing fenced session operation;
        # `operation` above is the distinct Continuation journal identity.
        or physical_row.operation_id != running.session_operation_context.fence.operation_id
        or physical_row.operation_epoch != running.session_operation_context.fence.operation_epoch
        or physical_row.lease_token != running.session_operation_context.fence.lease_token
        or physical_row.settled_at is None
        or physical_row.ledger_entry_id != entry_id
        or entry is None
        or entry.session_id != sid
        or entry.identity_id != physical_row.identity_id
        or entry.source != "composer"
        or entry.run_id is not None
        or entry.model != usage.model
        or entry.prompt_tokens != usage.prompt_tokens
        or entry.completion_tokens != usage.completion_tokens
        or entry.cached_prompt_tokens != usage.cached_prompt_tokens
        or entry.reasoning_tokens != usage.reasoning_tokens
        or usage.recorded_at is None
        or restore_utc(entry.recorded_at) != restore_utc(usage.recorded_at)
        or settled_before_call
    ):
        raise AuditIntegrityError("END repair cold check lost its original quota ledger")
    public_text = cast(messages.c.tool_calls, Text)
    if conn.dialect.name == "postgresql":
        size = func.octet_length(public_text)
    elif conn.dialect.name == "sqlite":
        size = func.length(cast(messages.c.tool_calls, LargeBinary))
    else:
        raise AuditIntegrityError("END repair cold audit has unsupported SQL dialect")
    bound = case((size <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, messages.c.tool_calls), else_=None).label("tool_calls")
    public_rows = conn.execute(
        select(
            messages.c.id,
            messages.c.session_id,
            messages.c.role,
            messages.c.content,
            messages.c.raw_content,
            bound,
            messages.c.tool_call_id,
            messages.c.parent_assistant_id,
            messages.c.composition_state_id,
            messages.c.writer_principal,
        )
        .where(
            messages.c.session_id == sid,
            messages.c.role == "audit",
            messages.c.content == "Provider call result recorded.",
            public_text.like(f"%{batch.owed.attempt_id}%"),
        )
        .limit(2)
    ).all()
    if len(public_rows) != 1 or public_rows[0].tool_calls is None:
        raise AuditIntegrityError("END repair cold check lost its one bounded original public audit")
    public_call = _closed_call(
        public_rows[0],
        ProviderAuditSource(sid, batch.owed.new_hold.session_operation_id, batch.owed.new_hold.session_operation_epoch),
        batch.owed.attempt_id,
    )
    if (
        llm_call_audit_envelope(public_call) != llm_call_audit_envelope(batch.original_call)
        or public_call.status is not ComposerLLMCallStatus.SUCCESS
    ):
        raise AuditIntegrityError("END repair cold public audit differs from the original paid result")
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=batch.owed.new_hold,
        attempt_id=batch.owed.attempt_id,
        private_sha256=batch.audit_cohort.private_sha256,
        incomplete=False,
    )
    if batch.projected.withheld is not None:
        if withheld_sequence is None:
            raise AuditIntegrityError("END repair cold check lost its superseded reply sequence")
        require_end_repair_withheld_reply_on_connection(
            conn,
            session_id=sid,
            prepared=batch.projected.withheld,
            sequence_no=withheld_sequence,
            created_at=observed_at,
        )
    elif withheld_sequence is not None:
        raise AuditIntegrityError("END repair cold check gained an unexpected superseded reply")
    segment = conn.execute(
        select(segments.c.next_action_json).where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
    ).one_or_none()
    if segment is None or segment.next_action_json != batch.next_action_json:
        raise AuditIntegrityError("END repair cold successor lost its PRIMARY next action")
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    state = _require_hold_fields(conn, source=batch.owed.new_hold, contract=contract)
    if state.state != hold_state or state.provider_attempt_id != batch.owed.attempt_id:
        raise AuditIntegrityError("END repair cold check changed its rebound original H")
    selected = SelectedContinuationState(
        stored.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )
    read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=prepared.env_ref_names)
    return selected
