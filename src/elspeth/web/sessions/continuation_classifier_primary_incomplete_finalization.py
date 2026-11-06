"""Atomic classifier PRIMARY terminal for a genuinely returned incomplete paid result."""

from __future__ import annotations

from sqlalchemy import Connection, select, update

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import audit_drafts_json, provider_attempt_audit_drafts
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete, require_issued_v4_incomplete
from elspeth.web.composer.visible_tool_event import validate_visible_audit_row
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import (
    _finalize_terminal_rows_after_append,
    _require_current_continuation_running,
    append_complete_snapshot,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    PreparedClassifierPrimaryProvider,
    V4ClassifierProviderAdmission,
    _chain,
    _read_source,
)
from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import build_classifier_paid_incomplete_tail_batch
from elspeth.web.sessions.continuation_classifier_primary_incomplete_terminal import (
    read_v4_classifier_paid_incomplete_evidence_on_connection,
    read_v4_classifier_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt, classify_paid_incomplete_loss
from elspeth.web.sessions.continuation_paid_incomplete_quota import (
    prepare_paid_incomplete_quota_entry,
    prepare_paid_incomplete_receipt_facts,
)
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal, paid_incomplete_error
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
)
from elspeth.web.sessions.continuation_primary import _input_value
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.time_normalization import restore_utc


def finalize_v4_classifier_incomplete_on_connection(
    conn: Connection,
    *,
    session_service: SessionServiceImpl,
    prepared: PreparedClassifierPrimaryProvider,
    admission: V4ClassifierProviderAdmission,
    incomplete: V4ProviderResultIncomplete,
    call: ComposerLLMCall,
) -> VerifiedPaidIncompleteTerminal:
    """Publish one distinct paid terminal, ledger, public audit and consumed H."""
    if (
        type(session_service) is not SessionServiceImpl
        or type(prepared) is not PreparedClassifierPrimaryProvider
        or type(admission) is not V4ClassifierProviderAdmission
        or type(incomplete) is not V4ProviderResultIncomplete
        or type(call) is not ComposerLLMCall
    ):
        raise TypeError("classifier incomplete finalizer requires original nominal owners")
    require_issued_v4_incomplete(incomplete)
    original = prepared.prepared
    running = original.binding.running
    source = admission.hold
    sid, operation, effect_key = source.session_id, source.operation_id, source.effect_key
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    selected = require_snapshot_cas(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=admission.dispatched.checkpoint_id,
        expected_cursor=admission.dispatched.cursor,
    )
    original_source = _read_source(conn, running.claim.session_id, operation, effect_key)
    if (
        selected != admission.dispatched
        or _chain(conn, original_source, dispatched=True) != selected
        or admission.dispatched.cursor != original.snapshot.cursor + 1
        or original.binding.effect_key != effect_key
        or original.binding.input_sha256 != source.source_input_sha256
    ):
        raise AuditIntegrityError("classifier incomplete changed its original dispatched source")
    selected_root = decode_continuation_snapshot(selected.payload_json)
    applied_batch_cut(selected_root.frames)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == effect_key)
    ).one()
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    descriptor = _input_value(
        read_chunk(conn, session_id=sid, operation_id=operation, key=f"provider-input:{effect.input_sha256}").decode(
            "utf-8", errors="strict"
        ),
        effect.input_sha256,
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent, call=call
    )
    if (
        hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.error_class != "V4ProviderResultIncomplete"
        or call.error_message != "paid_result_incomplete"
        or call.call_id != incomplete.sdk_return.attempt_id
        or incomplete.sdk_return.attempt_id != admission.attempt.attempt_id
        or admission.incomplete_tail.identity.attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("classifier incomplete lacks original unsettled physical/H")
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == source.creator_sequence
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == source.creator_sequence,
            attempts.c.generation == source.creator_generation,
        )
    ).one()
    if (
        segment.generation != source.creator_generation
        or segment.state != "running"
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("classifier incomplete changed original live segment")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("classifier incomplete observation predates physical attempt")
    charge = prepare_paid_incomplete_receipt_facts(incomplete, call, observed_at=observed_at)
    reason, surface = classify_paid_incomplete_loss(incomplete.losses[0])
    receipt = PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=admission.incomplete_tail.identity,
        producer="primary",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason=reason,
        capture_surface=surface,
        cost=charge.cost,
        usage=charge.usage,
    )
    tail = build_classifier_paid_incomplete_tail_batch(admission.incomplete_tail, receipt=receipt, published_at=observed_at)
    batch = V4PrimaryResultBatch(source, admission.dispatched, (tail.prospective[0],), tail.append, tail.prospective)
    error = paid_incomplete_error(parent.request_id, receipt.cost)
    carrier = OriginalPaidOverflowPhysicalAttempt(
        attempt_id=physical_row.attempt_id,
        session_id=physical_row.session_id,
        source=physical_row.source,
        run_id=physical_row.run_id,
        operation_id=physical_row.operation_id,
        operation_epoch=physical_row.operation_epoch,
        lease_token=physical_row.lease_token,
        started_at=restore_utc(physical_row.started_at),
    )
    entry = prepare_paid_incomplete_quota_entry(
        physical=carrier,
        context=running.session_operation_context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(requested_model),
        observed_at=observed_at,
    )
    drafts = provider_attempt_audit_drafts(call, running.session_operation_context)
    if (
        utf8_size(audit_drafts_json(drafts), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
        > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    ):
        raise AuditIntegrityError("classifier incomplete public audit exceeds unchanged 1 MiB bound")
    with admit_v4_primary_result_batch_on_connection(conn, batch=batch):
        # The ordinary cohort helper creates success-shaped usage accounting;
        # this terminal settles its distinct known/unknown paid entry below.
        session_service._assert_session_write_lock_held(conn, sid, caller="classifier_incomplete_audit")
        base_sequence = session_service._reserve_sequence_range(conn, sid, count=len(drafts))
        for offset, draft in enumerate(drafts):
            validate_visible_audit_row(
                envelopes=tuple(draft.tool_calls or ()),
                content=draft.content,
                role=draft.role,
                writer_principal="compose_loop",
                session_id=sid,
                context=running.session_operation_context,
            )
            session_service._insert_chat_message(
                conn,
                session_id=sid,
                role=draft.role,
                content=draft.content,
                raw_content=None,
                tool_calls=deep_thaw(draft.tool_calls) if draft.tool_calls else None,
                sequence_no=base_sequence + offset,
                writer_principal="compose_loop",
                composition_state_id=draft.effective_composition_state_id(None),
                tool_call_id=draft.tool_call_id,
                parent_assistant_id=draft.parent_assistant_id,
                created_at=observed_at,
                session_operation_context=running.session_operation_context,
                audit_only=True,
            )
        with session_service._session_mutations(
            conn, session_id=sid, session_operation_context=running.session_operation_context
        ) as mutations:
            mutations.mark_session_updated(updated_at=observed_at)
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=tail.receipt_chunk_key,
            payload=tail.receipt_json.encode("utf-8"),
            codec="composer.paid-incomplete.v1",
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == admission.attempt.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="paid_incomplete",
                receipt_key=tail.receipt_key,
                result_chunk_key=tail.receipt_chunk_key,
                response_finished_at=observed_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("classifier incomplete lost its original effect CAS")
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=admission.dispatched.checkpoint_id,
            expected_cursor=admission.dispatched.cursor,
            snapshot_json=tail.append.snapshot_json,
            event_batch=tail.append.events,
            handoff=False,
            prepared_append=tail.append,
        )
        _finalize_terminal_rows_after_append(
            conn,
            session_id=sid,
            operation_id=operation,
            parent=parent,
            segment=segment,
            history=history,
            retained=stored,
            terminal_values=_terminal_values(error, now=observed_at, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=observed_at,
        )
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=admission.attempt.attempt_id, entry=entry)
        before = read_v4_classifier_paid_incomplete_evidence_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
        )
        if before is None or before.source != source or before.hold_state != "active" or before.terminal != stored:
            raise AuditIntegrityError("classifier incomplete cannot consume unproved original terminal")
        changed_hold = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == admission.attempt.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("classifier incomplete lost original H consumption CAS")
        record_primary_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        result = read_v4_classifier_paid_incomplete_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
        )
        if result is None or result.receipt != receipt or result.terminal != stored:
            raise AuditIntegrityError("classifier incomplete changed after H consumption")
    return result
