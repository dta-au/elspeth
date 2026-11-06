"""Atomic paid/incomplete terminal for the original classifier PRIMARY."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.continuation_primary_observation import PrimaryOverflowObservation, PrimaryOverflowUsageObservation
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import (
    StoredContinuationSnapshot,
    _finalize_terminal_rows_after_append,
    append_complete_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission, VerifiedClassifierPaidOverflowTerminal
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import build_classifier_paid_overflow_tail_batch
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowReason, PaidOverflowReceipt, PaidOverflowUsage
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
    prepare_paid_overflow_quota_entry,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import primary_paid_overflow_error
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.time_normalization import restore_utc


def _receipt(
    *, admission: V4ClassifierProviderAdmission, observation: PrimaryOverflowObservation, observed_at: object
) -> PaidOverflowReceipt:
    from datetime import datetime

    if (
        type(observed_at) is not datetime
        or type(observation.cost) is not PaidOverflowCost
        or type(observation.usage) is not PrimaryOverflowUsageObservation
    ):
        raise AuditIntegrityError("classifier PRIMARY overflow changed its owned cost or usage")
    reason: PaidOverflowReason
    if observation.representation == "evidence_pair" and observation.document_codec == "composer.evidence.v2":
        reason = "evidence_pair_bytes"
        if observation.limit_bytes not in (
            CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
            CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
        ):
            raise AuditIntegrityError("classifier PRIMARY changed its evidence cap")
    elif observation.representation == "retained_history" and observation.document_codec == "composer.continuation.history.v4":
        reason = "aggregate_retained_bytes"
        if observation.limit_bytes != admission.hold.allowance_bytes:
            raise AuditIntegrityError("classifier PRIMARY changed its retained allowance")
    elif (
        (observation.representation == "physical_audit" and observation.document_codec == "composer.physical-audit.v1")
        or (observation.representation == "response" and observation.document_codec == "composer.response.v1")
        or (observation.representation == "recorder" and observation.document_codec == "composer.audit-buffer.v2")
    ):
        reason = "private_document_bytes"
        if observation.limit_bytes != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("classifier PRIMARY changed its private document cap")
    else:
        raise AuditIntegrityError("classifier PRIMARY overflow changed its measured representation")
    if observation.measurement != "at_least" or observation.observed_bytes != observation.limit_bytes + 1:
        raise AuditIntegrityError("classifier PRIMARY overflow invented a full length")
    usage = observation.usage
    if usage.status == "observed":
        retained_usage = PaidOverflowUsage(
            status="observed",
            model=usage.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            finished_at=observed_at.isoformat(),
        )
    elif usage.status == "unavailable" and all(
        value is None
        for value in (usage.model, usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens)
    ):
        retained_usage = PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        )
    else:
        raise AuditIntegrityError("classifier PRIMARY overflow changed its usage status")
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=admission.paid_tail.identity,
        producer="primary",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason=reason,
        representation=observation.representation,
        measurement="at_least",
        observed_bytes=observation.observed_bytes,
        limit_bytes=observation.limit_bytes,
        cost=observation.cost,
        usage=retained_usage,
    )


def settle_v4_classifier_paid_overflow_on_connection(
    conn: Connection,
    *,
    admission: V4ClassifierProviderAdmission,
    observation: PrimaryOverflowObservation,
    running: object,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
    requested_model: str,
) -> VerifiedClassifierPaidOverflowTerminal:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning

    if type(running) is not ComposerOperationRunning or type(admission) is not V4ClassifierProviderAdmission:
        raise TypeError("classifier PRIMARY settlement requires nominal original owner")
    source = admission.hold
    identity = admission.paid_tail.identity
    sid, operation = source.session_id, source.operation_id
    if (
        observation.source != capture_provider_audit_source(running.session_operation_context)
        or observation.attempt_id != admission.attempt.attempt_id
        or identity.attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
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
        raise AuditIntegrityError("classifier PRIMARY overflow lacks its unsettled original source")
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
        raise AuditIntegrityError("classifier PRIMARY overflow changed its live segment")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("classifier PRIMARY observation predates physical admission")
    receipt = _receipt(admission=admission, observation=observation, observed_at=observed_at)
    tail = build_classifier_paid_overflow_tail_batch(admission.paid_tail, receipt=receipt, published_at=observed_at)
    batch = V4PrimaryResultBatch(source, admission.dispatched, (tail.prospective[0],), tail.append, tail.prospective)
    error = primary_paid_overflow_error(parent.request_id, receipt.cost)
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
    entry = prepare_paid_overflow_quota_entry(
        physical=carrier,
        context=running.session_operation_context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(requested_model),
        observed_at=observed_at,
    )
    with admit_v4_primary_result_batch_on_connection(conn, batch=batch):
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=tail.receipt_chunk_key,
            payload=tail.receipt_json.encode("utf-8"),
            codec="composer.paid-overflow.v1",
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
                disposition="paid_overflow",
                receipt_key=tail.receipt_key,
                result_chunk_key=tail.receipt_chunk_key,
                response_finished_at=observed_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("classifier PRIMARY overflow lost its dispatched effect CAS")
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
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=identity.attempt_id, entry=entry)
        _consume_classifier_paid_hold_on_connection(conn, admission=admission, receipt=receipt, stored=stored)
    return VerifiedClassifierPaidOverflowTerminal(receipt, stored)


def _consume_classifier_paid_hold_on_connection(
    conn: Connection, *, admission: V4ClassifierProviderAdmission, receipt: PaidOverflowReceipt, stored: StoredContinuationSnapshot
) -> None:
    from elspeth.web.sessions.continuation_classifier_primary_terminal import read_v4_classifier_paid_overflow_terminal_on_connection
    from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import record_primary_hold_consumed
    from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds

    source = admission.hold
    verified = read_v4_classifier_paid_overflow_terminal_on_connection(
        conn, session_id=UUID(source.session_id), operation_id=source.operation_id, effect_key=source.effect_key
    )
    if (
        verified is None
        or verified.source != source
        or verified.receipt != receipt
        or verified.terminal != stored
        or verified.hold_state != "active"
    ):
        raise AuditIntegrityError("classifier PRIMARY paid hold lacks cold-proved original terminal")
    changed = conn.execute(
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
    if changed.rowcount != 1:
        raise AuditIntegrityError("classifier PRIMARY paid hold lost its exact active row")
    record_primary_hold_consumed(conn, session_id=source.session_id, operation_id=source.operation_id, source=source)
    post = read_v4_classifier_paid_overflow_terminal_on_connection(
        conn, session_id=UUID(source.session_id), operation_id=source.operation_id, effect_key=source.effect_key
    )
    if post is None or post.hold_state != "consumed" or post.receipt != receipt or post.terminal != stored:
        raise AuditIntegrityError("classifier PRIMARY paid hold changed after consumption")
