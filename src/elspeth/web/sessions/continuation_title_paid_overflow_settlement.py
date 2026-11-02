"""One guarded V4 TITLE paid/incomplete terminal transaction.

The caller owns the locked original session transaction. This writer performs
no SDK call and cannot turn missing output into a successful title response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_title_observation import (
    TitleOverflowObservation,
    TitleOverflowUsageObservation,
    _cost_observation,
    _usage_observation,
)
from elspeth.web.composer.continuation_title_values import CapturedTitleProviderResult, decode_title_provider_result
from elspeth.web.composer.provider_audit_source import ProviderAuditSource, audit_drafts_json, capture_provider_audit_source
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    PaidOverflowCost,
    PaidOverflowReason,
    PaidOverflowReceipt,
    PaidOverflowRepresentation,
    PaidOverflowUsage,
)
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
    prepare_paid_overflow_quota_entry,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    consume_provider_terminal_hold_after_paid_overflow,
)
from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection
from elspeth.web.sessions.continuation_title_paid_overflow_tail import build_title_paid_overflow_tail_batch
from elspeth.web.sessions.continuation_title_paid_overflow_terminal import title_paid_overflow_error
from elspeth.web.sessions.continuation_title_provider import (
    PreparedTitleProvider,
    V4TitleProviderAdmission,
    _current,
    _require_physical_data,
    _source_chain,
)
from elspeth.web.sessions.continuation_v4_paid_tail import admit_v4_title_paid_tail_on_connection
from elspeth.web.sessions.continuation_v4_title_complete_tail import (
    V4TitleCompleteBatch,
    preflight_v4_title_complete_batch_on_connection,
    require_v4_title_complete_batch_matches_capture,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class TitleAggregateCapacityObservation:
    """Original complete result plus the exact failed SQL success candidate."""

    captured: CapturedTitleProviderResult
    batch: V4TitleCompleteBatch
    source: ProviderAuditSource
    attempt_id: str


def _receipt(
    *,
    admission: V4TitleProviderAdmission,
    observation: TitleOverflowObservation | TitleAggregateCapacityObservation,
    observed_at: datetime,
) -> PaidOverflowReceipt:
    reason: PaidOverflowReason
    representation: PaidOverflowRepresentation
    measurement: Literal["at_least"] = "at_least"
    if isinstance(observation, TitleAggregateCapacityObservation):
        cost = _cost_observation(observation.captured.audit_call)
        usage = _usage_observation(observation.captured.audit_call)
        reason = "aggregate_retained_bytes"
        representation = "retained_history"
        limit_bytes = admission.hold.allowance_bytes
        if not 0 < limit_bytes < CANONICAL_INTEGER_MAX:
            raise AuditIntegrityError("TITLE aggregate overflow lost its original bounded allowance")
        observed_bytes = limit_bytes + 1
    else:
        cost = observation.cost
        usage = observation.usage
        reason = "private_document_bytes"
        representation = observation.representation
        limit_bytes = observation.limit_bytes
        observed_bytes = observation.observed_bytes
    if type(observation) is TitleOverflowObservation and (
        (observation.representation == "physical_audit" and observation.document_codec != "composer.physical-audit.v1")
        or (
            observation.representation == "response"
            and observation.document_codec not in ("composer.response.v1", "composer.title-provider-result.v1")
        )
        or observation.measurement != "at_least"
        or limit_bytes != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or observed_bytes != limit_bytes + 1
    ):
        raise AuditIntegrityError("TITLE paid overflow observation changed its validated representation or saturated size")
    if type(cost) is not PaidOverflowCost or type(usage) is not TitleOverflowUsageObservation:
        raise AuditIntegrityError("TITLE paid overflow observation changed its owned cost/usage facts")
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
        raise AuditIntegrityError("TITLE paid overflow observation changed its explicit usage disposition")
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=admission.tail.identity,
        producer="title",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason=reason,
        representation=representation,
        measurement=measurement,
        observed_bytes=observed_bytes,
        limit_bytes=limit_bytes,
        cost=cost,
        usage=retained_usage,
    )


def settle_v4_title_paid_overflow_on_connection(
    conn: Connection,
    *,
    prepared: PreparedTitleProvider,
    admission: V4TitleProviderAdmission,
    observation: TitleOverflowObservation | TitleAggregateCapacityObservation,
) -> PaidOverflowReceipt:
    """Commit receipt, one terminal journal, failed rows, quota and hold once."""
    if (
        type(prepared) is not PreparedTitleProvider
        or type(admission) is not V4TitleProviderAdmission
        or type(observation) not in (TitleOverflowObservation, TitleAggregateCapacityObservation)
    ):
        raise TypeError("V4 TITLE terminal requires its nominal original admission and observed paid result")
    running, selected = prepared.running, admission.selected
    _current(conn, running, selected, owed=True)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    source = admission.hold
    identity = admission.tail.identity
    if (
        observation.source != capture_provider_audit_source(running.session_operation_context)
        or observation.attempt_id != admission.attempt.attempt_id
        or source.session_id != sid
        or source.operation_id != operation
        or source.effect_key != "provider:3:0"
        or identity.attempt_id != admission.attempt.attempt_id
        or identity.session_operation_id != running.session_operation_context.fence.operation_id
        or identity.session_operation_epoch != running.session_operation_context.fence.operation_epoch
        or identity.lease_sha256 != sha256(running.session_operation_context.fence.lease_token.encode("utf-8")).hexdigest()
        or admission.tail.admitted != selected
    ):
        raise AuditIntegrityError("V4 TITLE terminal changed its original paid custody")
    descriptor, reference, stage, _source = _source_chain(conn, running, selected, names=prepared.names)
    if stage != "admitted" or reference != prepared.input.reference:
        raise AuditIntegrityError("V4 TITLE terminal lost its original admitted input")
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
        or effect.provider_attempt_id != identity.attempt_id
        or effect.requested_model is None
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or (effect.creator_sequence, effect.creator_generation) != (source.creator_sequence, source.creator_generation)
        or effect.input_sha256 != source.source_input_sha256
        or physical_row.session_id != sid
        or physical_row.source != "auto_title"
        or physical_row.run_id is not None
        or physical_row.operation_id != source.session_operation_id
        or physical_row.operation_epoch != source.session_operation_epoch
        or sha256(physical_row.lease_token.encode("utf-8")).hexdigest() != source.lease_sha256
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or segment.generation != source.creator_generation
        or segment.state != "running"
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("V4 TITLE terminal lacks one exact dispatched owner and unsettled physical attempt")
    requested_model = preflight_v4_requested_model(
        require_original_v4_title_physical_source_on_connection(
            conn, source=source, descriptor=descriptor, effect=effect, physical=physical_row, parent=parent
        )
    )
    if type(observation) is TitleAggregateCapacityObservation:
        captured = observation.captured
        if type(captured) is not CapturedTitleProviderResult or type(observation.batch) is not V4TitleCompleteBatch:
            raise AuditIntegrityError("TITLE aggregate overflow requires its original complete result and full batch")
        decoded = decode_title_provider_result(captured.chunk, env_ref_names=prepared.names)
        if (
            decoded.snapshot != captured.snapshot
            or decoded.completion != captured.completion
            or decoded.audit_call != captured.audit_call
            or audit_drafts_json(decoded.audit_drafts) != audit_drafts_json(captured.audit_drafts)
            or decoded.source != captured.source
            or decoded.source != observation.source
            or decoded.audit_call.call_id != identity.attempt_id
            or decoded.snapshot.logical != decode_continuation_snapshot(selected.payload_json).logical
            or decoded.snapshot.actor_user_id != parent.actor_user_id
            or (decoded.snapshot.creator_sequence, decoded.snapshot.creator_generation)
            != (source.creator_sequence, source.creator_generation)
            or decoded.snapshot.input_sha256 != descriptor.input_sha256
            or observation.batch.source != source
            or observation.batch.admitted != selected
            or observation.batch.result_chunk.payload != captured.chunk.canonical.encode("utf-8")
        ):
            raise AuditIntegrityError("TITLE aggregate overflow changed its original complete paid result")
        _require_physical_data(conn, running.claim.session_id, operation, effect, decoded.audit_call, decoded.source, descriptor)
        require_v4_title_complete_batch_matches_capture(prepared=prepared, admission=admission, captured=captured, batch=observation.batch)
        try:
            preflight_v4_title_complete_batch_on_connection(conn, batch=observation.batch)
        except ContinuationRetainedCapacityExceeded:
            pass
        else:
            raise AuditIntegrityError("TITLE aggregate overflow misclassified a retainable complete result")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("V4 TITLE terminal database observation predates paid admission")
    receipt = _receipt(admission=admission, observation=observation, observed_at=observed_at)
    batch = build_title_paid_overflow_tail_batch(admission.tail, receipt=receipt, published_at=observed_at)
    error = title_paid_overflow_error(parent.request_id, receipt.cost)
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
        requested_model=requested_model,
        observed_at=observed_at,
    )
    with admit_v4_title_paid_tail_on_connection(conn, source=source, plan=admission.tail, batch=batch):
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=batch.receipt_chunk_key,
            payload=batch.receipt_json.encode("utf-8"),
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
                receipt_key=batch.receipt_key,
                result_chunk_key=batch.receipt_chunk_key,
                response_finished_at=observed_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 TITLE terminal lost its original dispatched effect CAS")
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
        consume_provider_terminal_hold_after_paid_overflow(
            conn, source=source, provider_attempt_id=identity.attempt_id, consumed_at=database_now(conn), names=prepared.names
        )
    return receipt
