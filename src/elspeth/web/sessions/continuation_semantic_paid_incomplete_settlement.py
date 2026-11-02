"""Atomic returned paid/incomplete semantic terminal, quota and H settlement."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete, require_issued_v4_incomplete
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_paid_incomplete import (
    PaidIncompleteReceipt,
    classify_paid_incomplete_loss,
)
from elspeth.web.sessions.continuation_paid_incomplete_quota import (
    prepare_paid_incomplete_quota_entry,
    prepare_paid_incomplete_receipt_facts,
)
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal, paid_incomplete_error
from elspeth.web.sessions.continuation_paid_overflow_quota import OriginalPaidOverflowPhysicalAttempt, preflight_v4_requested_model
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission, _descriptor_data
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import build_semantic_paid_incomplete_tail_batch
from elspeth.web.sessions.continuation_semantic_paid_incomplete_terminal import (
    _read_cold_semantic_paid_incomplete,
    read_v4_semantic_paid_incomplete_terminal_on_connection,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_capacity import (
    V4SemanticResultBatch,
    admit_v4_semantic_result_batch_on_connection,
    record_semantic_hold_consumed,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.time_normalization import restore_utc


def _receipt(
    admission: V4SemanticProviderAdmission,
    incomplete: V4ProviderResultIncomplete,
    audit_call: ComposerLLMCall,
    observed_at: datetime,
) -> PaidIncompleteReceipt:
    reason, surface = classify_paid_incomplete_loss(incomplete.losses[0])
    charge = prepare_paid_incomplete_receipt_facts(incomplete, audit_call, observed_at=observed_at)
    return PaidIncompleteReceipt(
        schema_version="composer.paid-incomplete.v1",
        identity=admission.incomplete_tail.identity,
        producer="semantic_advisor",
        outcome="terminal_paid_incomplete",
        evidence_status="incomplete",
        retryable=False,
        provider_returned=True,
        reason=reason,
        capture_surface=surface,
        cost=charge.cost,
        usage=charge.usage,
    )


def _consume_after_cold_terminal(
    conn: Connection, *, source: ProviderTerminalHoldSource, provider_attempt_id: str, consumed_at: datetime
) -> None:
    verified = _read_cold_semantic_paid_incomplete(
        conn, session_id=UUID(source.session_id), operation_id=source.operation_id, effect_key=source.effect_key
    )
    if (
        verified is None
        or verified.evidence.source != source
        or verified.evidence.provider_attempt_id != provider_attempt_id
        or verified.evidence.hold_state != "active"
    ):
        raise AuditIntegrityError("Semantic paid-incomplete terminal cannot consume an unverified H")
    changed = conn.execute(
        update(holds)
        .where(
            holds.c.session_id == source.session_id,
            holds.c.operation_id == source.operation_id,
            holds.c.effect_key == source.effect_key,
            holds.c.state == "active",
            holds.c.provider_attempt_id == provider_attempt_id,
        )
        .values(state="consumed", consumed_at=consumed_at)
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("Semantic paid-incomplete hold lost its exact active row")
    record_semantic_hold_consumed(conn, session_id=source.session_id, operation_id=source.operation_id, source=source)
    post = read_v4_semantic_paid_incomplete_terminal_on_connection(
        conn, session_id=UUID(source.session_id), operation_id=source.operation_id, effect_key=source.effect_key
    )
    if post is None or post.receipt != verified.evidence.receipt or post.terminal != verified.terminal:
        raise AuditIntegrityError("Semantic paid-incomplete post-consumption cold proof changed")


def settle_v4_semantic_paid_incomplete_on_connection(
    conn: Connection,
    *,
    admission: V4SemanticProviderAdmission,
    incomplete: V4ProviderResultIncomplete,
    audit_call: ComposerLLMCall,
    running: ComposerOperationRunning,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
) -> VerifiedPaidIncompleteTerminal:
    """Close only a source-issued actual SDK return whose mandatory capture failed."""
    if (
        type(running) is not ComposerOperationRunning
        or type(admission) is not V4SemanticProviderAdmission
        or type(incomplete) is not V4ProviderResultIncomplete
        or type(audit_call) is not ComposerLLMCall
    ):
        raise TypeError("Semantic paid-incomplete SQL requires original nominal source")
    require_issued_v4_incomplete(incomplete, sdk_return=incomplete.sdk_return)
    source = admission.hold
    identity = admission.incomplete_tail.identity
    sid, operation = source.session_id, source.operation_id
    _descriptor, original, physical_input = _descriptor_data(conn, sid, operation, effect)
    if (
        original.invocation.kind != "semantic"
        or incomplete.sdk_return.attempt_id != identity.attempt_id
        or audit_call.call_id != identity.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or effect.requested_model != physical_input.options["model"]
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
    ):
        raise AuditIntegrityError("Semantic paid-incomplete SQL lacks original unsettled dispatched source")
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
        raise AuditIntegrityError("Semantic paid-incomplete SQL changed original live segment")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("Semantic paid-incomplete observation predates physical attempt")
    receipt = _receipt(admission, incomplete, audit_call, observed_at)
    tail = build_semantic_paid_incomplete_tail_batch(admission.incomplete_tail, receipt=receipt, published_at=observed_at)
    batch = V4SemanticResultBatch(source, admission.selected, (tail.prospective[0],), tail.append, tail.prospective)
    error = paid_incomplete_error(parent.request_id, receipt.cost)
    physical_carrier = OriginalPaidOverflowPhysicalAttempt(
        attempt_id=physical_row.attempt_id,
        session_id=physical_row.session_id,
        source=physical_row.source,
        run_id=physical_row.run_id,
        operation_id=physical_row.operation_id,
        operation_epoch=physical_row.operation_epoch,
        lease_token=physical_row.lease_token,
        started_at=restore_utc(physical_row.started_at),
    )
    quota_entry = prepare_paid_incomplete_quota_entry(
        physical=physical_carrier,
        context=running.session_operation_context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(effect.requested_model),
        observed_at=observed_at,
    )
    with admit_v4_semantic_result_batch_on_connection(conn, batch=batch):
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
                effects.c.effect_key == source.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == identity.attempt_id,
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
            raise AuditIntegrityError("Semantic paid-incomplete SQL lost original dispatched effect CAS")
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=admission.selected.checkpoint_id,
            expected_cursor=admission.selected.cursor,
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
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=identity.attempt_id, entry=quota_entry)
        _consume_after_cold_terminal(conn, source=source, provider_attempt_id=identity.attempt_id, consumed_at=database_now(conn))
    terminal = read_v4_semantic_paid_incomplete_terminal_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, effect_key=source.effect_key
    )
    if terminal is None or terminal.receipt != receipt or terminal.terminal != stored:
        raise AuditIntegrityError("Semantic paid-incomplete terminal changed after atomic settlement")
    return terminal
