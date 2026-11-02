"""Atomic terminal for a complete END response whose required consequence cannot fit.

The original paid response is already durable at the END-owed head. This writer
spends only its rebound H; it never reclassifies the response as absent and
never retries the paid call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    ColdCheckpointEndConsequenceSource,
    require_original_cold_checkpoint_end_source,
)
from elspeth.web.composer.continuation_checkpoint_end_cold_repair import (
    ColdProjectedEndRepair,
    ColdProjectedEndRepairOverflow,
    require_original_cold_end_repair_overflow,
)
from elspeth.web.composer.continuation_checkpoint_end_cold_review import (
    ColdProjectedEndReview,
    ColdProjectedEndReviewOverflow,
    require_original_cold_end_review_overflow,
)
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, decode_retained_physical_audit_call
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import (
    StoredContinuationSnapshot,
    _finalize_terminal_rows_after_append,
    _require_current_continuation_running,
    append_complete_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import prepare_end_complete_repair_batch_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_complete_review import prepare_end_complete_review_on_connection
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    read_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_end_recovery import require_end_h_anchored_generation_chain_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import CheckpointPaidOverflowTailPlan, build_checkpoint_paid_overflow_tail_batch
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    checkpoint_end_owed_overflow_error,
    read_v4_checkpoint_end_owed_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_paid_overflow import (
    CANONICAL_INTEGER_MAX,
    LEDGER_TOKENS_MAX,
    MODEL_MAX_LENGTH,
    PaidOverflowCost,
    PaidOverflowReceipt,
    PaidOverflowUsage,
    encode_paid_overflow_cost_amount,
)
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
    prepare_checkpoint_end_owed_overflow_quota_entry,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProviderTerminalHoldSource,
    _require_historical_hold_source,
    _require_hold_fields,
    admit_retained_payload_chunks,
)
from elspeth.web.sessions.continuation_v4_checkpoint_paid_tail import (
    admit_v4_checkpoint_end_owed_overflow_tail_on_connection,
    record_checkpoint_paid_tail_hold_consumed,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning


@dataclass(frozen=True, slots=True)
class _EndOwedOverflowSource:
    """Only original warm or deep-cold issuers can construct this SQL source."""

    session_id: UUID
    operation_id: str
    context: SessionOperationContext
    running: ComposerOperationRunning
    names: frozenset[str]
    selected: StoredContinuationSnapshot
    hold: ProviderTerminalHoldSource
    tail: CheckpointPaidOverflowTailPlan
    response: PrivateChunk
    call: ComposerLLMCall
    requested_model: str


def _measured_missing_tail(
    failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
) -> tuple[Literal["private_document_bytes", "evidence_pair_bytes", "retained_payload_bytes"], int, int, Literal["exact", "at_least"]]:
    if type(failure) is ContinuationRetainedCapacityExceeded:
        if failure.observed_bytes <= failure.limit_bytes or failure.limit_bytes > CANONICAL_INTEGER_MAX - 1:
            raise AuditIntegrityError("END consequence capacity refusal lacks its exact admitted arithmetic")
        if failure.observed_bytes <= CANONICAL_INTEGER_MAX:
            return "retained_payload_bytes", failure.observed_bytes, failure.limit_bytes, "exact"
        return "retained_payload_bytes", failure.limit_bytes + 1, failure.limit_bytes, "at_least"
    if (
        type(failure) is not ContinuationCaptureOverflowWitness
        or failure.unit not in ("private_chunk", "evidence_pair")
        or type(failure.codec) is not str
        or not failure.codec
        or type(failure.size_is_exact) is not bool
        or failure.limit_bytes != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or failure.observed_bytes <= failure.limit_bytes
        or failure.observed_bytes > CANONICAL_INTEGER_MAX
    ):
        raise AuditIntegrityError("END consequence overflow lacks its original bounded capture witness")
    reason: Literal["private_document_bytes", "evidence_pair_bytes"] = (
        "private_document_bytes" if failure.unit == "private_chunk" else "evidence_pair_bytes"
    )
    return reason, failure.observed_bytes, failure.limit_bytes, "exact" if failure.size_is_exact else "at_least"


def _receipt(
    *,
    source: _EndOwedOverflowSource,
    failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
    observed_at: datetime,
) -> PaidOverflowReceipt:
    call = source.call
    reason, observed_bytes, limit_bytes, measurement = _measured_missing_tail(failure)
    if call.provider_cost is None:
        cost = PaidOverflowCost(status="unknown", amount=None, source="not_available")
    else:
        cost = PaidOverflowCost(
            status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source
        )
    model = call.model_returned or call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    if (
        type(model) is str
        and 0 < len(model) <= MODEL_MAX_LENGTH
        and all(value is None or (type(value) is int and 0 <= value <= LEDGER_TOKENS_MAX) for value in tokens)
    ):
        usage = PaidOverflowUsage(
            status="observed",
            model=model,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            cached_prompt_tokens=call.cached_prompt_tokens,
            reasoning_tokens=call.reasoning_tokens,
            finished_at=observed_at.isoformat(),
        )
    else:
        usage = PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        )
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=source.tail.identity,
        producer="checkpoint_advisor",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason=reason,
        representation="retained_tail",
        measurement=measurement,
        observed_bytes=observed_bytes,
        limit_bytes=limit_bytes,
        cost=cost,
        usage=usage,
    )


def settle_v4_checkpoint_end_owed_overflow_on_connection(
    conn: Connection,
    *,
    service: object,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    returned: CheckpointReturnedResponseObservation,
    failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
) -> VerifiedCheckpointPaidOverflowTerminal:
    """Settle the exact H source, failed parent, quota and audits in one cut."""
    if (
        not conn.in_transaction()
        or type(prepared) is not PreparedCheckpointProvider
        or type(owed) is not CheckpointEndOwedAdmission
        or type(returned) is not CheckpointReturnedResponseObservation
        or prepared.prepared_source != owed.original.original_source
        or returned.attempt is not owed.original.attempt
        or owed.response != returned.response
        or returned.phase != "end"
    ):
        raise AuditIntegrityError("END consequence overflow changed its original complete paid source")
    require_original_v4_checkpoint_returned_response(
        returned, attempt_id=owed.original.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    call = original_v4_checkpoint_returned_audit_call(
        returned, attempt_id=owed.original.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    running = prepared.running
    source = _EndOwedOverflowSource(
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        context=running.session_operation_context,
        running=running,
        names=prepared.env_ref_names,
        selected=owed.owed_selected,
        hold=owed.owed_hold,
        tail=owed.owed_tail,
        response=returned.response,
        call=call,
        requested_model=prepared.requested_model,
    )
    return _settle_v4_checkpoint_end_owed_overflow_source_on_connection(conn, service=service, source=source, failure=failure)


def settle_cold_v4_checkpoint_end_owed_overflow_on_connection(
    conn: Connection,
    *,
    service: object,
    cold_source: ColdCheckpointEndConsequenceSource,
    failure: ContinuationRetainedCapacityExceeded | ColdProjectedEndRepairOverflow | ColdProjectedEndReviewOverflow,
    capacity_projection: ColdProjectedEndRepair | ColdProjectedEndReview | None = None,
) -> VerifiedCheckpointPaidOverflowTerminal:
    """Consume the same exact overflow batch from a re-read H after owner loss."""
    if not conn.in_transaction() or type(cold_source) is not ColdCheckpointEndConsequenceSource:
        raise AuditIntegrityError("cold END overflow lacks its current H owner")
    require_original_cold_checkpoint_end_source(cold_source)
    if type(failure) is ColdProjectedEndRepairOverflow:
        if capacity_projection is not None:
            raise AuditIntegrityError("capture overflow cannot borrow an aggregate projection")
        if failure.source is not cold_source:
            raise AuditIntegrityError("cold END repair overflow changed its source H")
        witness: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded = require_original_cold_end_repair_overflow(
            failure
        )
    elif type(failure) is ColdProjectedEndReviewOverflow:
        if capacity_projection is not None:
            raise AuditIntegrityError("capture overflow cannot borrow an aggregate projection")
        if failure.gate.source is not cold_source:
            raise AuditIntegrityError("cold END Review overflow changed its source H")
        witness = require_original_cold_end_review_overflow(failure)
    elif type(failure) is ContinuationRetainedCapacityExceeded:
        if type(capacity_projection) is not ColdProjectedEndRepair and type(capacity_projection) is not ColdProjectedEndReview:
            raise AuditIntegrityError("cold END aggregate excess lacks its exact admitted projection")
        witness = failure
    else:
        raise AuditIntegrityError("cold END overflow lacks one original measured failure")
    work = cold_source.runtime.bindings.required_work
    running = None if work is None else work.running
    owed = cold_source.owed
    if (
        running is None
        or str(running.claim.session_id) != owed.session_id
        or running.claim.operation_id != owed.operation_id
        or owed.requested_model is None
        or owed.end_input is None
    ):
        raise AuditIntegrityError("cold END overflow lost its original running paid source")
    if type(failure) is ContinuationRetainedCapacityExceeded:
        if type(capacity_projection) is ColdProjectedEndRepair:
            if capacity_projection.source is not cold_source:
                raise AuditIntegrityError("cold END repair capacity changed its source H")
            batch = prepare_end_complete_repair_batch_on_connection(
                conn,
                physical=None,
                prepared=None,
                owed_admission=None,
                owed=owed,
                selected=cold_source.selected,
                returned=None,
                projected=capacity_projection,
                published_at=database_now(conn),
            )
            prospective = (*batch.prospective, *batch.unpaid_reserve)
        elif type(capacity_projection) is ColdProjectedEndReview:
            if capacity_projection.source is not cold_source:
                raise AuditIntegrityError("cold END Review capacity changed its source H")
            review_batch = prepare_end_complete_review_on_connection(
                conn,
                physical=None,
                prepared=None,
                owed_admission=None,
                owed=owed,
                selected=None,
                returned=None,
                projected=capacity_projection,
                observed_at=database_now(conn),
            )
            prospective = review_batch.prospective
        else:
            raise AuditIntegrityError("cold END aggregate excess changed its admitted projection")
        try:
            admit_retained_payload_chunks(
                conn,
                session_id=owed.session_id,
                operation_id=owed.operation_id,
                prospective=prospective,
                retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
                consuming_hold=owed.new_hold,
            )
        except ContinuationRetainedCapacityExceeded as measured:
            if (measured.observed_bytes, measured.limit_bytes) != (failure.observed_bytes, failure.limit_bytes):
                raise AuditIntegrityError("cold END aggregate excess changed its measured source arithmetic") from measured
        else:
            raise AuditIntegrityError("cold END aggregate excess no longer exceeds its admitted allowance")
    source = _EndOwedOverflowSource(
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        context=running.session_operation_context,
        running=running,
        names=cold_source.runtime.env_ref_names,
        selected=owed.selected,
        hold=owed.new_hold,
        tail=owed.terminal_tail,
        response=owed.response,
        call=decode_retained_physical_audit_call(owed.response),
        requested_model=owed.requested_model,
    )
    return _settle_v4_checkpoint_end_owed_overflow_source_on_connection(conn, service=service, source=source, failure=witness)


def _settle_v4_checkpoint_end_owed_overflow_source_on_connection(
    conn: Connection,
    *,
    service: object,
    source: _EndOwedOverflowSource,
    failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
) -> VerifiedCheckpointPaidOverflowTerminal:
    sid, operation = str(source.session_id), source.operation_id
    _require_current_continuation_running(conn, source.running)
    if source.context is not source.running.session_operation_context:
        raise AuditIntegrityError("END overflow substituted its current writer fence")
    hold = source.hold
    identity = source.tail.identity
    cold = read_v4_checkpoint_end_owed_on_connection(
        conn, session_id=sid, operation_id=operation, attempt_id=identity.attempt_id, names=source.names
    )
    if (
        cold is None
        or cold.selected != source.selected
        or cold.response != source.response
        or cold.new_hold != hold
        or cold.terminal_tail != source.tail
    ):
        raise AuditIntegrityError("END consequence overflow lost its complete current owed source")
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == hold.effect_key)
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == identity.attempt_id).with_for_update()).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == hold.creator_sequence
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == hold.creator_sequence,
            attempts.c.generation == source.running.claim.ownership_generation,
        )
    ).one()
    require_end_h_anchored_generation_chain_on_connection(conn, owed=cold, generation=source.running.claim.ownership_generation)
    if (
        effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.requested_model != source.requested_model
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent.status != "running"
        or parent.checkpoint_id != source.selected.checkpoint_id
        or parent.journal_cursor != source.selected.cursor
        or parent.current_segment_sequence != hold.creator_sequence
        or parent.ownership_generation != source.running.claim.ownership_generation
        or parent.attempt != source.running.claim.attempt
        or segment.state != "running"
        or segment.generation != source.running.claim.ownership_generation
        or history.claim_attempt != source.running.claim.attempt
        or history.session_operation_id != source.context.fence.operation_id
        or history.session_operation_epoch != source.context.fence.operation_epoch
        or history.session_operation_lease_token != source.context.fence.lease_token
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("END consequence overflow lacks one unsettled original paid owner")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("END consequence overflow predates its paid admission")
    receipt = _receipt(source=source, failure=failure, observed_at=observed_at)
    cohort = checkpoint_paid_audit_cohort_for_hold(source.call, hold)
    admitted_audit = admit_checkpoint_paid_audit_source_on_connection(
        conn,
        service=service,
        context=source.context,
        owed=cold,
        call=source.call,
        cohort=cohort,
        names=source.names,
    )
    batch = build_checkpoint_paid_overflow_tail_batch(
        source.tail, receipt=receipt, published_at=observed_at, private_audit_sha256=cohort.private_sha256
    )
    error = checkpoint_end_owed_overflow_error(parent.request_id, receipt.cost)
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
    entry = prepare_checkpoint_end_owed_overflow_quota_entry(
        physical=carrier,
        context=source.context,
        hold=hold,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(effect.requested_model),
        observed_at=observed_at,
    )
    with admit_v4_checkpoint_end_owed_overflow_tail_on_connection(conn, source=hold, plan=source.tail, batch=batch):
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
                effects.c.effect_key == hold.effect_key,
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
            raise AuditIntegrityError("END consequence overflow lost its child CAS")
        for item in decode_continuation_snapshot(source.selected.payload_json).effect_frontier:
            if item.effect_id != hold.effect_key and item.disposition == "dispatched":
                changed_outer = conn.execute(
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
                if changed_outer.rowcount != 1:
                    raise AuditIntegrityError("END consequence overflow lost its unpaid outer CAS")
        retained = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=source.selected.checkpoint_id,
            expected_cursor=source.selected.cursor,
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
            conn,
            service=service,
            context=source.context,
            cohort=cohort,
            observed_at=observed_at,
            admitted=admitted_audit,
        )
        verified = read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
            conn,
            session_id=source.session_id,
            operation_id=operation,
            effect_key=hold.effect_key,
            names=source.names,
            hold_state="active",
        )
        if verified is None or verified.source != hold or verified.hold_state != "active" or verified.receipt != receipt:
            raise AuditIntegrityError("END consequence overflow lost its cold terminal before H consumption")
        contract = _require_historical_hold_source(conn, hold)
        state = _require_hold_fields(conn, source=hold, contract=contract)
        if state.state != "active" or state.provider_attempt_id != identity.attempt_id:
            raise AuditIntegrityError("END consequence overflow lost its rebound active H")
        consumed_at = database_now(conn)
        changed_hold = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == hold.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == identity.attempt_id,
            )
            .values(state="consumed", consumed_at=consumed_at)
        )
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("END consequence overflow could not consume its exact rebound H")
        record_checkpoint_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=hold)
        post = read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
            conn,
            session_id=source.session_id,
            operation_id=operation,
            effect_key=hold.effect_key,
            names=source.names,
        )
        if post is None or post.hold_state != "consumed" or post.receipt != verified.receipt or post.terminal != verified.terminal:
            raise AuditIntegrityError("END consequence overflow lost its post-consumption cold proof")
        return post
