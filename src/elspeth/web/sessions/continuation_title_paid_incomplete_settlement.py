"""Atomic TITLE settlement for an actual returned result lacking required evidence."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete, require_issued_v4_incomplete
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt, classify_paid_incomplete_loss
from elspeth.web.sessions.continuation_paid_incomplete_quota import (
    prepare_paid_incomplete_quota_entry,
    prepare_paid_incomplete_receipt_facts,
)
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal, paid_incomplete_error
from elspeth.web.sessions.continuation_paid_overflow_quota import OriginalPaidOverflowPhysicalAttempt, preflight_v4_requested_model
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import build_title_paid_incomplete_tail_batch
from elspeth.web.sessions.continuation_title_paid_incomplete_terminal import (
    _read_cold_title_paid_incomplete,
    read_v4_title_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection
from elspeth.web.sessions.continuation_title_provider import (
    PreparedTitleProvider,
    V4TitleProviderAdmission,
    _current,
    _require_physical_data,
    _source_chain,
)
from elspeth.web.sessions.continuation_v4_paid_tail import (
    admit_v4_title_paid_incomplete_tail_on_connection,
    record_paid_tail_hold_consumed,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc


def settle_v4_title_paid_incomplete_on_connection(
    conn: Connection,
    *,
    prepared: PreparedTitleProvider,
    admission: V4TitleProviderAdmission,
    incomplete: V4ProviderResultIncomplete,
    audit_call: ComposerLLMCall,
) -> VerifiedPaidIncompleteTerminal:
    """Settle one parser-issued returned failure, then cold-prove consumed H."""
    if (
        type(prepared) is not PreparedTitleProvider
        or type(admission) is not V4TitleProviderAdmission
        or type(incomplete) is not V4ProviderResultIncomplete
        or type(audit_call) is not ComposerLLMCall
    ):
        raise TypeError("TITLE paid-incomplete SQL requires original nominal owners")
    require_issued_v4_incomplete(incomplete, sdk_return=incomplete.sdk_return)
    running, selected = prepared.running, admission.selected
    source, identity = admission.hold, admission.incomplete_tail.identity
    sid, operation = source.session_id, source.operation_id
    _current(conn, running, selected, owed=True)
    if (
        source.session_id != str(running.claim.session_id)
        or source.operation_id != running.claim.operation_id
        or source.effect_key != "provider:3:0"
        or admission.tail.identity != identity
        or admission.no_sdk_tail.identity != identity
        or admission.tail.admitted != selected
        or admission.no_sdk_tail.admitted != selected
        or incomplete.sdk_return.attempt_id != identity.attempt_id
        or audit_call.call_id != identity.attempt_id
        or audit_call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or capture_provider_audit_source(running.session_operation_context).session_id != sid
    ):
        raise AuditIntegrityError("TITLE paid-incomplete SQL changed original returned attempt custody")
    descriptor, reference, stage, _origin = _source_chain(conn, running, selected, names=prepared.names)
    if stage != "admitted" or reference != prepared.input.reference:
        raise AuditIntegrityError("TITLE paid-incomplete SQL lost original admitted input")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == source.effect_key)
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == identity.attempt_id).with_for_update()).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
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
        effect.disposition != "dispatched"
        or effect.owner != "provider"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.input_sha256 != source.source_input_sha256
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or effect.requested_model is None
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or segment.state != "running"
        or segment.generation != source.creator_generation
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("TITLE paid-incomplete SQL lacks one original unsettled dispatched owner")
    requested_model = require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
    )
    if requested_model != admission.no_sdk_tail.requested_model or requested_model != prepared.request.kwargs["model"]:
        raise AuditIntegrityError("TITLE paid-incomplete SQL changed original requested model")
    _require_physical_data(
        conn,
        running.claim.session_id,
        operation,
        effect,
        audit_call,
        capture_provider_audit_source(running.session_operation_context),
        descriptor,
    )
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("TITLE paid-incomplete observation predates paid attempt")
    reason, surface = classify_paid_incomplete_loss(incomplete.losses[0])
    charge = prepare_paid_incomplete_receipt_facts(incomplete, audit_call, observed_at=observed_at)
    receipt = PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="title",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason=reason,
        capture_surface=surface,
        cost=charge.cost,
        usage=charge.usage,
    )
    batch = build_title_paid_incomplete_tail_batch(admission.incomplete_tail, receipt=receipt, published_at=observed_at)
    error = paid_incomplete_error(parent.request_id, receipt.cost)
    physical_attempt = OriginalPaidOverflowPhysicalAttempt(
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
        physical=physical_attempt,
        context=running.session_operation_context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(requested_model),
        observed_at=observed_at,
    )
    with admit_v4_title_paid_incomplete_tail_on_connection(
        conn,
        source=source,
        paid_plan=admission.tail,
        plan=admission.incomplete_tail,
        no_sdk_plan=admission.no_sdk_tail,
        batch=batch,
    ):
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=batch.receipt_chunk_key,
            payload=batch.receipt_json.encode("utf-8"),
            codec="composer.paid-incomplete.v1",
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == source.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == identity.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="paid_incomplete",
                receipt_key=batch.receipt_key,
                result_chunk_key=batch.receipt_chunk_key,
                response_finished_at=observed_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("TITLE paid-incomplete SQL lost original dispatched effect CAS")
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=batch.append.snapshot_json,
            event_batch=batch.append.events,
            handoff=False,
            prepared_append=batch.append,
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
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=identity.attempt_id, entry=entry)
        cold = _read_cold_title_paid_incomplete(conn, session_id=UUID(sid), operation_id=operation, names=prepared.names)
        if (
            cold is None
            or cold.receipt.source != source
            or cold.receipt.receipt != receipt
            or cold.receipt.hold_state != "active"
            or cold.terminal != stored
        ):
            raise AuditIntegrityError("TITLE paid-incomplete SQL cannot consume unproved terminal H")
        consumed = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == source.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == identity.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if consumed.rowcount != 1:
            raise AuditIntegrityError("TITLE paid-incomplete SQL lost exact active H")
        record_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
    terminal = read_v4_title_paid_incomplete_terminal_on_connection(
        conn, session_id=UUID(sid), operation_id=operation, names=prepared.names
    )
    if terminal is None or terminal.receipt != receipt or terminal.terminal != stored:
        raise AuditIntegrityError("TITLE paid-incomplete terminal changed after atomic settlement")
    return terminal
