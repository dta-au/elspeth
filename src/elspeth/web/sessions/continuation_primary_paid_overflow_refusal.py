"""Atomic zero-use disposition for one V4 PRIMARY attempt that never entered SDK."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkRefusalPermit
from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import cancel_undispatched_provider_attempt_on_connection
from elspeth.web.sessions.continuation import (
    PreparedContinuationProvider,
    _effect_snapshot,
    _finalize_terminal_rows_after_append,
    append_complete_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_primary import (
    PrimaryNoSdkRefusalReceipt,
    V4PrimaryProviderAdmission,
    _selected_effect,
    _v4_dispatch_action_projection,
    stored_primary_input_data_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import (
    build_primary_no_sdk_tail_batch,
    fixed_v4_primary_no_sdk_error,
    prepare_primary_no_sdk_tail_plan,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import prepare_primary_paid_overflow_tail_plan
from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical


def refuse_v4_primary_unentered_on_connection(
    conn: Connection,
    *,
    prepared: PreparedContinuationProvider,
    admission: V4PrimaryProviderAdmission,
    permit: PrimaryNoSdkRefusalPermit,
    append_audit_event: Callable[[str, str, datetime], None],
) -> PrimaryNoSdkRefusalReceipt:
    """Cancel the original attempt and consume H with a bounded failed root."""
    if (
        type(prepared) is not PreparedContinuationProvider
        or type(admission) is not V4PrimaryProviderAdmission
        or type(permit) is not PrimaryNoSdkRefusalPermit
        or permit.prepared is not prepared
        or permit.dispatched != admission.selected
        or permit.undispatched_attempt_id != admission.attempt.attempt_id
        or permit.undispatched_model != admission.no_sdk_tail.requested_model
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original admitted source")
    permit.issuer.require_refusal_permit(permit)
    running = prepared.binding.running
    context = running.session_operation_context
    selected, _root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=admission.selected.checkpoint_id,
        cursor=admission.selected.cursor,
        effect_key=prepared.binding.effect_key,
        input_sha256=prepared.binding.input_sha256,
    )
    source = admission.hold
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    if (
        selected != admission.selected
        or source.effect_key != effect.effect_key
        or source.source_input_sha256 != effect.input_sha256
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or frame.phase != "awaiting_response"
        or hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its pending physical effect and hold")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
    if descriptor.model != permit.undispatched_model:
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original requested model")
    from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection

    require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=None
    )
    paid = prepare_primary_paid_overflow_tail_plan(
        identity=admission.tail.identity, admitted=selected, creator_sequence=source.creator_sequence
    )
    if paid != admission.tail or paid.identity.attempt_id != admission.attempt.attempt_id:
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original paid tail")
    refused_projection = _effect_snapshot(
        selected.payload_json,
        effect_key=source.effect_key,
        disposition="refused",
        receipt_id="00000000-0000-4000-8000-000000000000",
    )
    _refused_json, action_chunk = _v4_dispatch_action_projection(
        conn,
        root=decode_continuation_snapshot(selected.payload_json),
        effect=effect,
        dispatched_json=refused_projection,
        disposition="refused",
    )
    no_sdk = prepare_primary_no_sdk_tail_plan(
        admitted=selected,
        attempt_id=admission.attempt.attempt_id,
        requested_model=descriptor.model,
        input_sha256=effect.input_sha256,
        effect_key=effect.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
        action_chunk=action_chunk,
    )
    incomplete_tail, reserved, combined_hash = primary_terminal_reservation(paid, no_sdk)
    if (
        no_sdk != admission.no_sdk_tail
        or incomplete_tail != admission.incomplete_tail
        or source.reserved_bytes != reserved
        or source.fallback_contract_sha256 != combined_hash
    ):
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its combined original tail reserve")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == source.session_id,
            segments.c.operation_id == source.operation_id,
            segments.c.sequence == source.creator_sequence,
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == source.session_id,
            attempts.c.operation_id == source.operation_id,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    if segment.generation != source.creator_generation or segment.state != "running" or history.revoked_at is not None:
        raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its original live segment")
    now = database_now(conn)
    batch = build_primary_no_sdk_tail_batch(
        no_sdk,
        permit=permit,
        input_sha256=effect.input_sha256,
        effect_key=effect.effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        claim_attempt=source.claim_attempt,
        operation_epoch=source.session_operation_epoch,
        published_at=now,
    )
    result_chunks = batch.prospective[: 2 if action_chunk is not None else 1]
    result_batch = V4PrimaryResultBatch(source, selected, result_chunks, batch.append, batch.prospective)
    with admit_v4_primary_result_batch_on_connection(conn, batch=result_batch):
        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=context,
            attempt_id=admission.attempt.attempt_id,
            requested_model=descriptor.model,
            append_audit_event=append_audit_event,
        )
        put_chunk(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            key=batch.receipt_chunk_key,
            payload=batch.receipt_json.encode("utf-8"),
            codec="composer.primary-no-sdk-refusal.v1",
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == source.session_id,
                effects.c.operation_id == source.operation_id,
                effects.c.effect_key == source.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == admission.attempt.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original effect CAS")
        if action_chunk is not None:
            from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

            actual_json = retain_action_execution_receipt_on_connection(
                conn,
                session_id=source.session_id,
                operation_id=source.operation_id,
                effect_key=source.effect_key,
                disposition="refused",
                snapshot_json=_effect_snapshot(
                    selected.payload_json,
                    effect_key=source.effect_key,
                    disposition="refused",
                    receipt_id=permit.receipt_key,
                ),
            )
            if actual_json != batch.append.snapshot_json and decode_continuation_snapshot(actual_json).model_copy(
                update={"cursor": batch.append.expected_cursor + 2}
            ) != decode_continuation_snapshot(batch.append.snapshot_json):
                raise AuditIntegrityError("V4 PRIMARY zero-use refusal changed its prescribed action bytes")
        stored = append_complete_snapshot(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=batch.append.snapshot_json,
            event_batch=batch.append.events,
            handoff=False,
            prepared_append=batch.append,
        )
        error = fixed_v4_primary_no_sdk_error(permit.reason)
        _finalize_terminal_rows_after_append(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            parent=parent,
            segment=segment,
            history=history,
            retained=stored,
            terminal_values=_terminal_values(error, now=now, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=now,
        )
        changed_hold = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == source.session_id,
                holds.c.operation_id == source.operation_id,
                holds.c.effect_key == source.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == admission.attempt.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its original active H")
        record_primary_hold_consumed(conn, session_id=source.session_id, operation_id=source.operation_id, source=source)
        terminal = _record_from_row(
            conn.execute(select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)).one()
        )
        if terminal.status != "failed" or terminal.journal_cursor != stored.cursor or terminal.checkpoint_id != stored.checkpoint_id:
            raise AuditIntegrityError("V4 PRIMARY zero-use refusal lost its exact failed terminal")
        return PrimaryNoSdkRefusalReceipt(terminal, stored, source.effect_key, permit.receipt_key, batch.receipt_chunk_key)
