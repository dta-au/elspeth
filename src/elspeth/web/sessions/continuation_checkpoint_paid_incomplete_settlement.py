"""Atomic terminal owner for an original returned checkpoint with missing fields."""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_incomplete_tail import build_checkpoint_paid_incomplete_tail_batch
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    checkpoint_paid_audit_cohort,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    read_v4_checkpoint_paid_incomplete_terminal_on_connection,
    read_v4_checkpoint_paid_incomplete_verified_on_connection,
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
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_v4_checkpoint_paid_tail import (
    admit_v4_checkpoint_paid_tail_on_connection,
    record_checkpoint_paid_tail_hold_consumed,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc


def settle_v4_checkpoint_returned_incomplete_on_connection(
    conn: Connection,
    *,
    service: object,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    custody: ProviderCallCustody,
    incomplete: V4ProviderResultIncomplete,
) -> VerifiedPaidIncompleteTerminal:
    """Close the original paid attempt, outer effects, ledger, journal and H once."""
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or type(custody) is not ProviderCallCustody
        or type(incomplete) is not V4ProviderResultIncomplete
        or custody.continuation_checkpoint_v4 is not prepared
        or custody.continuation_checkpoint_v4_admission is not admission
        or custody.attempt is not admission.attempt
        or prepared.prepared_source != admission.original_source
    ):
        raise AuditIntegrityError("checkpoint incomplete changed its original paid owner")
    custody.require_v4_result_incomplete(incomplete)
    call = custody.call
    if (
        type(call) is not ComposerLLMCall
        or call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
        or call.call_id != admission.attempt.attempt_id
        or call.messages_hash != prepared.request_sha256
        or call.model_requested != prepared.requested_model
        or incomplete.sdk_return.attempt_id != call.call_id
    ):
        raise AuditIntegrityError("checkpoint incomplete lost its original malformed physical audit")
    running = prepared.running
    selected = require_current_checkpoint_dispatched_on_connection(conn, running, admission, allow_cancel=True, allow_expired_segment=True)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    source = admission.hold
    identity = admission.paid_tail.identity
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
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if (
        hold.state != "active"
        or hold.provider_attempt_id != identity.attempt_id
        or effect.owner != "provider"
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.input_sha256 != source.source_input_sha256
        or effect.requested_model != prepared.requested_model
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or physical_row.session_id != sid
        or physical_row.source != "composer"
        or physical_row.run_id is not None
        or physical_row.operation_id != source.session_operation_id
        or physical_row.operation_epoch != source.session_operation_epoch
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or segment.state != "running"
        or segment.generation != source.creator_generation
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("checkpoint incomplete lacks one original dispatched unsettled physical owner")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("checkpoint incomplete observation predates its paid admission")
    facts = prepare_paid_incomplete_receipt_facts(incomplete=incomplete, audit_call=call, observed_at=observed_at)
    reason, surface = classify_paid_incomplete_loss(incomplete.losses[0])
    receipt = PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=identity,
        producer="checkpoint_advisor",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason=reason,
        capture_surface=surface,
        cost=facts.cost,
        usage=facts.usage,
    )
    cohort = checkpoint_paid_audit_cohort(call, running.session_operation_context)
    batch = build_checkpoint_paid_incomplete_tail_batch(
        admission.terminal_hold.paid_incomplete,
        receipt=receipt,
        published_at=observed_at,
        private_audit_sha256=cohort.private_sha256,
    )
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
        requested_model=preflight_v4_requested_model(effect.requested_model),
        observed_at=observed_at,
    )
    with admit_v4_checkpoint_paid_tail_on_connection(conn, source=source, hold_plan=admission.terminal_hold, batch=batch):
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
            raise AuditIntegrityError("checkpoint incomplete lost its dispatched child CAS")
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

        for item in decode_continuation_snapshot(selected.payload_json).effect_frontier:
            if item.effect_id != source.effect_key and item.disposition == "dispatched":
                outer = conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == sid,
                        effects.c.operation_id == operation,
                        effects.c.effect_key == item.effect_id,
                        effects.c.disposition == "dispatched",
                        effects.c.receipt_key.is_(None),
                    )
                    .values(disposition="unknown")
                )
                if outer.rowcount != 1:
                    raise AuditIntegrityError("checkpoint incomplete lost its unpaid outer effect CAS")
        retained = append_complete_snapshot(
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
            retained=retained,
            terminal_values=_terminal_values(error, now=observed_at, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=observed_at,
        )
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=identity.attempt_id, entry=entry)
        write_checkpoint_paid_audit_cohort_on_connection(
            conn, service=service, context=running.session_operation_context, cohort=cohort, observed_at=observed_at
        )
        verified = read_v4_checkpoint_paid_incomplete_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if verified is None or verified.source != source or verified.hold_state != "active" or verified.receipt != receipt:
            raise AuditIntegrityError("checkpoint incomplete lost its cold terminal before H consumption")
        changed_hold = conn.execute(
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
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("checkpoint incomplete could not consume original H")
        record_checkpoint_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        result = read_v4_checkpoint_paid_incomplete_verified_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if result is None or result.receipt != receipt or result.terminal != retained:
            raise AuditIntegrityError("checkpoint incomplete changed after H consumption")
        return result
