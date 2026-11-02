"""Cold proof of one complete END paid result and its shared terminal."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import EndFrame
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.coordination.composer_operation_authority import _read, _record_from_row
from elspeth.web.coordination.quota_authority import _usage_entry_id
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_pass import require_end_complete_pass_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import require_checkpoint_paid_private_audit_on_connection
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_retained_capacity import (
    _require_hold_fields,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord
    from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_prepare import EndCompleteTerminalBatch
    from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider


def _require_snapshot_and_event(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    checkpoint_id: str,
    cursor: int,
    snapshot_json: str,
    event_tag: str,
    event_payload: str,
    allowance: int,
) -> None:
    row = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == session_id,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == checkpoint_id,
        )
    ).one_or_none()
    snapshot = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=f"snapshot:{checkpoint_id}",
        allowance=allowance,
    )
    event = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id, events.c.operation_id == operation_id, events.c.sequence == cursor
        )
    ).one_or_none()
    payload = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=f"event:{cursor}",
        allowance=allowance,
    )
    if (
        row is None
        or row.cursor != cursor
        or row.snapshot_chunk_key != f"snapshot:{checkpoint_id}"
        or snapshot is None
        or snapshot.codec != "snapshot.v2"
        or snapshot.payload != snapshot_json.encode("utf-8")
        or event is None
        or event.tag != event_tag
        or event.payload_chunk_key != f"event:{cursor}"
        or payload is None
        or payload.codec != "event.v2"
        or payload.payload != event_payload.encode("utf-8")
    ):
        raise AuditIntegrityError("END terminal cold chain lost an exact prepared snapshot or event")


def _require_prefix(
    conn: Connection,
    *,
    batch: EndCompleteTerminalBatch,
    pass_sequence: int,
    prefix_sequences: tuple[int, ...],
) -> None:
    sid = batch.owed.session_id
    require_end_complete_pass_on_connection(
        conn,
        session_id=sid,
        prepared=batch.pass_audit,
        sequence_no=pass_sequence,
        created_at=batch.assistant_created_at,
    )
    if prefix_sequences != tuple(range(pass_sequence + 1, pass_sequence + 1 + len(batch.prefix_rows))):
        raise AuditIntegrityError("END terminal changed the original pass/consequence audit order")
    for prepared, sequence in zip(batch.prefix_rows, prefix_sequences, strict=True):
        row = conn.execute(select(messages).where(messages.c.session_id == sid, messages.c.id == prepared.message_id)).one_or_none()
        draft = prepared.draft
        if (
            row is None
            or row.sequence_no != sequence
            or row.role != "audit"
            or row.content != draft.content
            or row.raw_content is not None
            or row.tool_calls != deep_thaw(draft.tool_calls)
            or row.writer_principal != "compose_loop"
            or row.composition_state_id != draft.effective_composition_state_id(None)
            or row.tool_call_id is not None
            or row.parent_assistant_id is not None
            or restore_utc(row.created_at) != restore_utc(batch.assistant_created_at)
        ):
            raise AuditIntegrityError("END terminal lost its exact withheld, disclosure or publication row")
        duplicates = tuple(
            item.id
            for item in conn.execute(
                select(messages.c.id, messages.c.tool_calls).where(
                    messages.c.session_id == sid,
                    messages.c.role == "audit",
                    messages.c.content == draft.content,
                    messages.c.sequence_no >= pass_sequence,
                )
            )
            if item.tool_calls == deep_thaw(draft.tool_calls)
        )
        if duplicates != (prepared.message_id,):
            raise AuditIntegrityError("END terminal duplicated a gate consequence audit row")


def _require_paid_custody(conn: Connection, *, prepared: PreparedCheckpointProvider, batch: EndCompleteTerminalBatch) -> None:
    sid = batch.owed.session_id
    operation = batch.owed.operation_id
    owed_checkpoint = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == batch.owed.selected.checkpoint_id,
        )
    ).one_or_none()
    owed_snapshot = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"snapshot:{batch.owed.selected.checkpoint_id}",
        allowance=batch.owed.new_hold.allowance_bytes,
    )
    if (
        owed_checkpoint is None
        or owed_checkpoint.cursor != batch.owed.selected.cursor
        or owed_checkpoint.snapshot_chunk_key != f"snapshot:{batch.owed.selected.checkpoint_id}"
        or owed_snapshot is None
        or owed_snapshot.codec != "snapshot.v2"
        or owed_snapshot.payload != batch.owed.selected.payload_json.encode("utf-8")
    ):
        raise AuditIntegrityError("END terminal lost its original same-attempt owed head")
    response = read_bounded_retained_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=batch.response_chunk_key,
        allowance=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    )
    if (
        response is None
        or response.codec != batch.owed.response.reference.codec
        or response.payload != batch.owed.response.canonical.encode("utf-8")
    ):
        raise AuditIntegrityError("END terminal lost its complete retained original paid response")
    original_response = PrivateChunk(batch.owed.response.reference, response.payload.decode("utf-8", errors="strict"))
    if (
        decode_retained_physical_audit_call(original_response) != batch.original_call
        or decode_continuation_response(original_response).tool_batch.calls
    ):
        raise AuditIntegrityError("END terminal changed the original paid no-tool result")
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == batch.owed.attempt_id)).one_or_none()
    entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=batch.owed.attempt_id)
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    usage = batch.quota_entry
    settled_before_call = False
    if physical_row is not None and entry is not None and physical_row.settled_at is not None:
        finished_at = restore_utc(entry.recorded_at)
        settled_at = restore_utc(physical_row.settled_at)
        if conn.dialect.name == "sqlite":
            finished_at = finished_at.replace(microsecond=0)
        elif conn.dialect.name != "postgresql":
            raise AuditIntegrityError("END terminal quota proof has unsupported SQL dialect")
        settled_before_call = settled_at < finished_at
    fence = prepared.running.session_operation_context.fence
    if (
        physical_row is None
        or physical_row.session_id != sid
        or physical_row.operation_id != fence.operation_id
        or physical_row.operation_epoch != fence.operation_epoch
        or physical_row.lease_token != fence.lease_token
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
        raise AuditIntegrityError("END terminal lost its original paid quota ledger")
    public_text = cast(messages.c.tool_calls, Text)
    if conn.dialect.name == "postgresql":
        size = func.octet_length(public_text)
    elif conn.dialect.name == "sqlite":
        size = func.length(cast(messages.c.tool_calls, LargeBinary))
    else:
        raise AuditIntegrityError("END terminal public audit has unsupported SQL dialect")
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
            messages.c.sequence_no,
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
        raise AuditIntegrityError("END terminal lost its one bounded original public paid audit")
    public_call = _closed_call(
        public_rows[0],
        ProviderAuditSource(sid, batch.owed.new_hold.session_operation_id, batch.owed.new_hold.session_operation_epoch),
        batch.owed.attempt_id,
    )
    if (
        llm_call_audit_envelope(public_call) != llm_call_audit_envelope(batch.original_call)
        or public_call.status is not ComposerLLMCallStatus.SUCCESS
        or not batch.pass_audit.message_id
    ):
        raise AuditIntegrityError("END terminal public paid audit differs from the original provider result")
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=batch.owed.new_hold,
        attempt_id=batch.owed.attempt_id,
        private_sha256=batch.paid_audit.private_sha256,
        incomplete=False,
    )
    if not batch.prefix_rows or not (
        batch.prefix_rows[-1].message_id
        and batch.assistant.sequence_no is not None
        and public_rows[0].sequence_no > 0
        and public_rows[0].sequence_no < batch.assistant.sequence_no
    ):
        raise AuditIntegrityError("END terminal paid audit lost its position before the final assistant")


def require_end_complete_terminal_postwrite_on_connection(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    batch: EndCompleteTerminalBatch,
    record: ComposerOperationRecord,
    pass_sequence: int,
    prefix_sequences: tuple[int, ...],
) -> None:
    """Independently re-read H, paid custody, both appends and public result."""
    from elspeth.web.sessions.continuation_checkpoint_end_complete_terminal_prepare import EndCompleteTerminalBatch

    if type(batch) is not EndCompleteTerminalBatch:
        raise AuditIntegrityError("END terminal cold check lost its nominal complete proposal")
    sid, operation = batch.owed.session_id, batch.owed.operation_id
    if sid != str(prepared.running.claim.session_id) or operation != prepared.running.claim.operation_id:
        raise AuditIntegrityError("END terminal cold check changed its original running operation")
    _require_prefix(conn, batch=batch, pass_sequence=pass_sequence, prefix_sequences=prefix_sequences)
    _require_paid_custody(conn, prepared=prepared, batch=batch)
    current = load_snapshot(conn, session_id=sid, operation_id=operation)
    if (
        current.checkpoint_id != batch.terminal.append.checkpoint_id
        or current.cursor != batch.terminal.append.expected_cursor + len(batch.terminal.append.events)
        or current.payload_json != batch.terminal.append.snapshot_json
    ):
        raise AuditIntegrityError("END terminal cold check lost its exact terminal root")
    preterminal = decode_continuation_snapshot(batch.preterminal.snapshot_json)
    final = decode_continuation_snapshot(current.payload_json)
    if (
        preterminal.pending_settlement_ids
        or preterminal.pending_owner_results
        or final.pending_settlement_ids
        or final.pending_owner_results
        or len(final.frames) != 2
        or type(final.frames[-1]) is not EndFrame
    ):
        raise AuditIntegrityError("END terminal cold chain retained an unpaid owner or wrong frame")
    _require_snapshot_and_event(
        conn,
        session_id=sid,
        operation_id=operation,
        checkpoint_id=batch.preterminal.checkpoint_id,
        cursor=batch.preterminal.expected_cursor + len(batch.preterminal.events),
        snapshot_json=batch.preterminal.snapshot_json,
        event_tag=batch.preterminal.events[-1].tag,
        event_payload=batch.preterminal.events[-1].payload_json,
        allowance=batch.owed.new_hold.allowance_bytes,
    )
    _require_snapshot_and_event(
        conn,
        session_id=sid,
        operation_id=operation,
        checkpoint_id=batch.terminal.append.checkpoint_id,
        cursor=current.cursor,
        snapshot_json=batch.terminal.append.snapshot_json,
        event_tag=batch.terminal.append.events[-1].tag,
        event_payload=batch.terminal.append.events[-1].payload_json,
        allowance=batch.owed.new_hold.allowance_bytes,
    )
    inventory = tuple(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation
            )
        ).tuples()
    )
    if set(inventory) != {(item.effect_id, item.disposition, item.receipt_id) for item in final.effect_frontier}:
        raise AuditIntegrityError("END terminal cold check changed its complete effect inventory")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == batch.owed.effect_key
        )
    ).one_or_none()
    outer = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == batch.owed.outer_effect_key
        )
    ).one_or_none()
    end_frame = final.frames[-1]
    if type(end_frame) is not EndFrame:
        raise AuditIntegrityError("END terminal lost its original review frame")
    if (
        child is None
        or child.disposition != "committed"
        or child.provider_attempt_id != batch.owed.attempt_id
        or child.receipt_key != batch.child_receipt
        or child.result_chunk_key != batch.response_chunk_key
        or restore_utc(child.response_finished_at) != restore_utc(batch.original_call.finished_at)
        or outer is None
        or outer.disposition != "committed"
        or outer.receipt_key != str(batch.assistant.id)
        or outer.result_chunk_key != f"value:{end_frame.candidate.codec}:{end_frame.candidate.digest}"
    ):
        raise AuditIntegrityError("END terminal cold check lost its original child or public outer receipt")
    assistant_row = conn.execute(
        select(messages).where(messages.c.session_id == sid, messages.c.id == str(batch.assistant.id))
    ).one_or_none()
    if assistant_row is None or chat_message_record_from_row(assistant_row) != batch.assistant:
        raise AuditIntegrityError("END terminal cold check changed its public assistant")
    if len(batch.finalization.audit_cohort) != 1 or not batch.audit_message_ids:
        raise AuditIntegrityError("END terminal cold check lost its one graph-review audit proposal")
    graph_draft = batch.finalization.audit_cohort[0]
    graph_row = conn.execute(
        select(messages).where(messages.c.session_id == sid, messages.c.id == batch.audit_message_ids[0])
    ).one_or_none()
    if (
        graph_row is None
        or graph_row.role != graph_draft.role
        or graph_row.content != graph_draft.content
        or graph_row.tool_calls != deep_thaw(graph_draft.tool_calls)
        or graph_row.writer_principal != "compose_loop"
        or graph_row.composition_state_id != graph_draft.effective_composition_state_id(None)
        or graph_row.sequence_no <= batch.assistant.sequence_no
        or restore_utc(graph_row.created_at) != restore_utc(batch.assistant_created_at)
    ):
        raise AuditIntegrityError("END terminal cold check lost its exact final graph-review fact")
    if batch.response.message.id != str(batch.assistant.id) or batch.response.message.content != batch.assistant.content:
        raise AuditIntegrityError("END terminal cold check changed its predicted public assistant response")
    stored_row = _read(conn, prepared.running.claim.session_id, operation)
    if stored_row is None or _record_from_row(stored_row) != record:
        raise AuditIntegrityError("END terminal cold check changed its operation record")
    if (
        record.status != "completed"
        or record.result_json != batch.terminal.outcome_json
        or record.checkpoint_id != batch.terminal.append.checkpoint_id
        or record.journal_cursor != current.cursor
        or batch.terminal.outcome != batch.response
    ):
        raise AuditIntegrityError("END terminal cold check changed its exact public response")
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    hold = _require_hold_fields(conn, source=batch.owed.new_hold, contract=contract)
    if hold.state != "consumed" or hold.provider_attempt_id != batch.owed.attempt_id:
        raise AuditIntegrityError("END terminal cold check lost its consumed original H")
