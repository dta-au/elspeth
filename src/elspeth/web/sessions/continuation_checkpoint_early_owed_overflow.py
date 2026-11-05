"""Original-owner EARLY completion or bounded overflow from its retained H.

Only a real capacity refusal from the original completion body selects the
fallback. Complete response DATA remains at H; it never grants another call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import Connection, select, update
from sqlalchemy.engine import RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_checkpoint_projection import ProjectedEarlyCheckpoint, require_original_early_checkpoint_projection
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.continuation import (
    _finalize_terminal_rows_after_append,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_early_retry import read_early_format_retry_on_connection
from elspeth.web.sessions.continuation_checkpoint_early_return import (
    EarlyCompleteReturnAdmission,
    VerifiedEarlyCompleteReturn,
    complete_early_checkpoint_on_connection,
    read_early_complete_return_on_connection,
    read_early_settled_return_history_on_connection,
    read_original_early_child_source_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_early_sql import EarlyCheckpointAdvanced
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import build_checkpoint_paid_overflow_tail_batch
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    _read_v4_checkpoint_terminal_on_connection,
)
from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_data_on_connection
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
    prepare_paid_overflow_quota_entry,
)
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_v4_checkpoint_paid_tail import (
    admit_v4_checkpoint_early_owed_overflow_tail_on_connection,
    record_checkpoint_paid_tail_hold_consumed,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_early_live import EarlyCheckpointLiveAdvanced, EarlyCheckpointPaidOverflowTerminal
    from elspeth.web.composer.continuation_checkpoint_early_physical import CheckpointEarlyPhysicalSource


@dataclass(frozen=True, slots=True)
class EarlyCheckpointCompletion:
    """Committed execution or independently cold-proved DATA, never authority."""

    result: EarlyCheckpointAdvanced | VerifiedCheckpointPaidOverflowTerminal


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class _CaughtCapacityRefusal:
    excess: ContinuationRetainedCapacityExceeded


@dataclass(frozen=True, slots=True)
class _CapacitySource:
    connection: Connection
    transaction: RootTransaction
    prepared: PreparedCheckpointProvider
    owed: EarlyCompleteReturnAdmission
    error_identity: int
    observed_bytes: int
    limit_bytes: int


_capacity_sources: WeakKeyDictionary[_CaughtCapacityRefusal, _CapacitySource] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class OriginalEarlyCompletionReadback:
    """Process-local original readback observation, never SQL authority."""


@dataclass(frozen=True, slots=True)
class _EarlyReadbackSource:
    source: CheckpointEarlyPhysicalSource
    advanced: EarlyCheckpointAdvanced
    returned: CheckpointReturnedResponseObservation
    owed: EarlyCompleteReturnAdmission
    projected: ProjectedEarlyCheckpoint
    ticket: RequiredWorkTicket
    outcome: RequiredSQLReturned[EarlyCheckpointCompletion | None]
    completed_checkpoint: str
    completed_cursor: int
    completed_payload: str


_readback_sources: WeakKeyDictionary[OriginalEarlyCompletionReadback, _EarlyReadbackSource] = WeakKeyDictionary()


def require_original_early_completion_readback(
    proof: OriginalEarlyCompletionReadback,
    *,
    source: CheckpointEarlyPhysicalSource,
    advanced: EarlyCheckpointAdvanced,
    returned: CheckpointReturnedResponseObservation,
    owed: EarlyCompleteReturnAdmission,
    projected: ProjectedEarlyCheckpoint,
    read_ticket: RequiredWorkTicket,
    readback: RequiredSQLReturned[EarlyCheckpointCompletion | None],
) -> None:
    original = _readback_sources.get(proof) if type(proof) is OriginalEarlyCompletionReadback else None
    if (
        original is None
        or original.source is not source
        or original.advanced is not advanced
        or original.returned is not returned
        or original.owed is not owed
        or original.projected is not projected
        or original.ticket is not read_ticket
        or original.outcome is not readback
        or type(readback) is not RequiredSQLReturned
        or type(readback.value) is not EarlyCheckpointCompletion
        or readback.value.result is not advanced
        or not read_ticket.complete
        or (advanced.completed.checkpoint_id, advanced.completed.cursor, advanced.completed.payload_json)
        != (original.completed_checkpoint, original.completed_cursor, original.completed_payload)
    ):
        raise AuditIntegrityError("EARLY cost projection lacks its original completion readback")
    _readback_sources.pop(proof)


class EarlyCheckpointCompletionDeliveryError(BaseException):
    """Keep cold-proved commit DATA alongside the original delivery failures."""

    def __init__(self, errors: Sequence[BaseException], *, committed: EarlyCheckpointCompletion) -> None:
        super().__init__("EARLY completion original delivery outcomes")
        self.committed = committed
        self.original_group = BaseExceptionGroup("EARLY completion original delivery outcomes", errors)
        self.__cause__ = self.original_group


def _failed_projection(ticket: RequiredWorkTicket, error: BaseException, errors: list[BaseException]) -> None:
    """Retain both the original delivery refusal and any cleanup refusal."""
    errors.append(error)
    if not ticket.complete:
        try:
            ticket.complete_without_submission(error)
        except BaseException as cleanup_error:
            errors.append(cleanup_error)


def checkpoint_early_owed_overflow_error(request_id: str | None, cost: PaidOverflowCost) -> ComposerOperationError:
    if type(cost) is not PaidOverflowCost:
        raise TypeError("EARLY owed overflow requires its original bounded cost")
    return ComposerOperationError(
        http_status=507,
        failure_code="http_error",
        error_type="paid_result_overflow",
        body={
            "error_type": "paid_result_overflow",
            "detail": "The paid EARLY result was retained, but its required continuation could not be retained. It was not used or retried.",
            "evidence_status": "incomplete",
            "retryable": False,
            "cost": {"status": cost.status, "amount": cost.amount},
            "request_id": request_id,
        },
        diagnostic_id=None,
    )


def read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
    names: frozenset[str],
    hold_state: Literal["active", "consumed"] = "consumed",
) -> VerifiedCheckpointPaidOverflowTerminal | None:
    result = _read_v4_checkpoint_terminal_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        effect_key=effect_key,
        disposition="paid_overflow",
        names=names,
        post_h=True,
        post_h_hold_state=hold_state,
    )
    if result is None or (type(result) is VerifiedCheckpointPaidOverflowTerminal and result.phase == "early"):
        return result
    raise AuditIntegrityError("EARLY owed overflow crossed its original phase")


def _require_original(
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> None:
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(owed) is not EarlyCompleteReturnAdmission
        or type(returned) is not CheckpointReturnedResponseObservation
        or prepared.prepared_source.phase != "early"
        or owed.original.original_source is not prepared.prepared_source
        or returned.phase != "early"
        or returned.attempt is not owed.original.attempt
        or owed.response != returned.response
    ):
        raise AuditIntegrityError("EARLY completion lost its original prepared, returned or owed source")
    require_original_v4_checkpoint_returned_response(
        returned, attempt_id=owed.original.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    require_original_early_checkpoint_projection(projected, prepared=prepared, admission=owed.original, returned=returned)


def _require_current_h(
    conn: Connection, prepared: PreparedCheckpointProvider, owed: EarlyCompleteReturnAdmission
) -> VerifiedEarlyCompleteReturn:
    running = prepared.running
    _require_current_continuation_running(conn, running)
    fresh = read_early_complete_return_on_connection(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        selected=owed.owed_selected,
        names=prepared.env_ref_names,
    )
    fence = running.session_operation_context.fence
    if (
        fresh.response != owed.response
        or fresh.new_hold != owed.owed_hold
        or fresh.terminal_tail != owed.owed_tail
        or fresh.new_hold.creator_sequence != running.claim.segment_sequence
        or fresh.new_hold.creator_generation != running.claim.ownership_generation
        or fresh.new_hold.claim_attempt != running.claim.attempt
        or fresh.new_hold.session_operation_id != fence.operation_id
        or fresh.new_hold.session_operation_epoch != fence.operation_epoch
    ):
        raise AuditIntegrityError("EARLY owed overflow requires its original current H owner")
    return fresh


def _receipt(owed: VerifiedEarlyCompleteReturn, excess: ContinuationRetainedCapacityExceeded, now: datetime) -> PaidOverflowReceipt:
    if type(excess) is not ContinuationRetainedCapacityExceeded or not 0 < excess.limit_bytes < excess.observed_bytes:
        raise AuditIntegrityError("EARLY owed overflow lost its actual capacity observation")
    if excess.limit_bytes != owed.new_hold.allowance_bytes or excess.limit_bytes >= CANONICAL_INTEGER_MAX:
        raise AuditIntegrityError("EARLY owed overflow changed its original admitted allowance")
    exact = excess.observed_bytes <= CANONICAL_INTEGER_MAX
    call = owed.call
    cost = (
        PaidOverflowCost(status="unknown", amount=None, source="not_available")
        if call.provider_cost is None
        else PaidOverflowCost(status="known", amount=encode_paid_overflow_cost_amount(call.provider_cost), source=call.provider_cost_source)
    )
    model = call.model_returned or call.model_requested
    tokens = (call.prompt_tokens, call.completion_tokens, call.cached_prompt_tokens, call.reasoning_tokens)
    usable = (
        type(model) is str
        and 0 < len(model) <= MODEL_MAX_LENGTH
        and all(value is None or (type(value) is int and 0 <= value <= LEDGER_TOKENS_MAX) for value in tokens)
    )
    usage = (
        PaidOverflowUsage(
            status="observed",
            model=model,
            prompt_tokens=call.prompt_tokens,
            completion_tokens=call.completion_tokens,
            cached_prompt_tokens=call.cached_prompt_tokens,
            reasoning_tokens=call.reasoning_tokens,
            finished_at=now.isoformat(),
        )
        if usable
        else PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        )
    )
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=owed.terminal_tail.identity,
        producer="checkpoint_advisor",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason="retained_payload_bytes",
        representation="retained_tail",
        measurement="exact" if exact else "at_least",
        observed_bytes=excess.observed_bytes if exact else excess.limit_bytes + 1,
        limit_bytes=excess.limit_bytes,
        cost=cost,
        usage=usage,
    )


def _settle_caught_overflow(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    caught: _CaughtCapacityRefusal,
) -> VerifiedCheckpointPaidOverflowTerminal:
    """Only the lexical original completion's rolled-back refusal reaches here."""
    original = _capacity_sources.get(caught) if type(caught) is _CaughtCapacityRefusal else None
    if (
        original is None
        or original.connection is not conn
        or original.transaction is not conn.get_transaction()
        or not original.transaction.is_active
        or original.prepared is not prepared
        or original.owed is not owed
        or id(caught.excess) != original.error_identity
        or (caught.excess.observed_bytes, caught.excess.limit_bytes) != (original.observed_bytes, original.limit_bytes)
    ):
        raise AuditIntegrityError("EARLY overflow requires its actual completion capacity refusal")
    excess = caught.excess
    fresh = _require_current_h(conn, prepared, owed)
    running, hold = prepared.running, fresh.new_hold
    sid, operation = fresh.session_id, fresh.operation_id
    context = running.session_operation_context
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == fresh.attempt_id).with_for_update()).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == hold.creator_sequence,
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == hold.creator_sequence,
            attempts.c.generation == hold.creator_generation,
        )
    ).one()
    if (
        attempt.settled_at is not None
        or attempt.ledger_entry_id is not None
        or parent.status != "running"
        or parent.checkpoint_id != fresh.selected.checkpoint_id
        or parent.journal_cursor != fresh.selected.cursor
        or segment.state != "running"
        or segment.generation != hold.creator_generation
        or history.revoked_at is not None
        or history.claim_attempt != running.claim.attempt
        or history.session_operation_id != context.fence.operation_id
        or history.session_operation_epoch != context.fence.operation_epoch
        or history.session_operation_lease_token != context.fence.lease_token
    ):
        raise AuditIntegrityError("EARLY overflow lost its restored unsettled original H")
    now = database_now(conn)
    receipt = _receipt(fresh, excess, now)
    cohort = checkpoint_paid_audit_cohort_for_hold(fresh.call, hold)
    admitted_audit = admit_checkpoint_paid_audit_source_on_connection(
        conn,
        service=service,
        context=context,
        owed=fresh,
        call=fresh.call,
        cohort=cohort,
        names=prepared.env_ref_names,
    )
    batch = build_checkpoint_paid_overflow_tail_batch(
        fresh.terminal_tail,
        receipt=receipt,
        published_at=now,
        private_audit_sha256=cohort.private_sha256,
    )
    entry = prepare_paid_overflow_quota_entry(
        physical=OriginalPaidOverflowPhysicalAttempt(
            attempt_id=attempt.attempt_id,
            session_id=attempt.session_id,
            source=attempt.source,
            run_id=attempt.run_id,
            operation_id=attempt.operation_id,
            operation_epoch=attempt.operation_epoch,
            lease_token=attempt.lease_token,
            started_at=restore_utc(attempt.started_at),
        ),
        context=context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(prepared.requested_model),
        observed_at=now,
    )
    with admit_v4_checkpoint_early_owed_overflow_tail_on_connection(
        conn,
        source=hold,
        plan=fresh.terminal_tail,
        batch=batch,
        names=prepared.env_ref_names,
    ):
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
                effects.c.effect_key == fresh.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == fresh.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="paid_overflow",
                receipt_key=batch.receipt_key,
                result_chunk_key=batch.receipt_chunk_key,
                response_finished_at=now,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("EARLY overflow lost its original child CAS")
        for item in decode_continuation_snapshot(fresh.selected.payload_json).effect_frontier:
            if item.effect_id != fresh.effect_key and item.disposition == "dispatched":
                changed_outer = conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == sid,
                        effects.c.operation_id == operation,
                        effects.c.effect_key == item.effect_id,
                        effects.c.disposition == "dispatched",
                        effects.c.receipt_key.is_(None),
                        effects.c.result_chunk_key.is_(None),
                    )
                    .values(disposition="unknown")
                )
                if changed_outer.rowcount != 1:
                    raise AuditIntegrityError("EARLY overflow lost its original unpaid outer CAS")
        retained = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=fresh.selected.checkpoint_id,
            expected_cursor=fresh.selected.cursor,
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
            terminal_values=_terminal_values(
                checkpoint_early_owed_overflow_error(parent.request_id, receipt.cost), now=now, settled_by="owner_terminal"
            ),
            attempt_outcome="settled",
            now=now,
        )
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=fresh.attempt_id, entry=entry)
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=context,
            cohort=cohort,
            observed_at=now,
            admitted=admitted_audit,
        )
        before = read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=fresh.effect_key,
            names=prepared.env_ref_names,
            hold_state="active",
        )
        if before is None or before.source != hold or before.receipt != receipt:
            raise AuditIntegrityError("EARLY overflow lost its pre-consumption cold proof")
        consumed = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == fresh.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == fresh.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if consumed.rowcount != 1:
            raise AuditIntegrityError("EARLY overflow lost its original hold CAS")
        record_checkpoint_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=hold)
        after = read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=fresh.effect_key,
            names=prepared.env_ref_names,
        )
        if after is None or after.receipt != before.receipt or after.terminal != before.terminal or after.hold_state != "consumed":
            raise AuditIntegrityError("EARLY overflow changed its post-consumption cold proof")
        return after


def _read_committed(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> EarlyCheckpointCompletion | None:
    _require_original(prepared, owed, returned, projected)
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    overflow = read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=operation,
        effect_key=owed.original.effect_key,
        names=prepared.env_ref_names,
    )
    if overflow is not None:
        if overflow.source != owed.owed_hold or overflow.provider_attempt_id != returned.attempt.attempt_id:
            raise AuditIntegrityError("EARLY readback substituted a foreign paid terminal")
        return EarlyCheckpointCompletion(overflow)
    selected = load_snapshot(conn, session_id=sid, operation_id=operation)
    if selected == owed.owed_selected:
        return None
    h = read_early_settled_return_history_on_connection(
        conn,
        session_id=sid,
        operation_id=operation,
        selected=owed.owed_selected,
        names=prepared.env_ref_names,
    )
    child_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.cursor == owed.owed_selected.cursor + 1,
        )
    ).one()
    from elspeth.web.sessions.continuation_checkpoint_source_replay import _checkpoint_at

    child = read_original_early_child_source_on_connection(
        conn,
        session_id=sid,
        operation_id=operation,
        selected=_checkpoint_at(conn, h.new_hold, child_row.checkpoint_id),
        names=prepared.env_ref_names,
    )
    stage = read_iteration_stage_data_on_connection(conn, session_id=running.claim.session_id, operation_id=operation, selected=selected)
    retry = read_early_format_retry_on_connection(conn, session_id=sid, operation_id=operation, selected=selected)
    expected = "early_retry_ready" if projected.retry_pending else "classify_ready"
    if (
        child.owed != h
        or h.response != returned.response
        or h.new_hold != owed.owed_hold
        or child.accounting != projected.accounting
        or child.retry_pending != projected.retry_pending
        or selected.cursor != owed.owed_selected.cursor + 2
        or decode_continuation_snapshot(selected.payload_json).frames != projected.captured.stack.frames
        or stage is None
        or stage.current != selected
        or stage.stage != expected
        or (retry is not None) != projected.retry_pending
    ):
        raise AuditIntegrityError("EARLY completion readback changed its original complete consequence")
    return EarlyCheckpointCompletion(EarlyCheckpointAdvanced(selected, retry))


def complete_early_checkpoint_or_overflow_sync(
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> EarlyCheckpointCompletion:
    """Attempt original C/K/R once; only its real aggregate failure spends H."""
    _require_original(prepared, owed, returned, projected)
    if type(service) is not SessionServiceImpl or prepared.composer._require_sessions_service() is not service:
        raise AuditIntegrityError("EARLY completion changed its original session transaction owner")
    running = prepared.running
    sid = str(running.claim.session_id)
    with (
        service._session_process_locked_begin(sid) as conn,
        service._session_write_lock(conn, sid),
        service._session_composer_mutation_transaction(
            conn,
            session_id=sid,
            session_operation_context=running.session_operation_context,
            expected_kind=SessionOperationKind.COMPOSE,
            audit_only=True,
        ),
    ):
        _require_current_h(conn, prepared, owed)
        try:
            with conn.begin_nested():
                completed = complete_early_checkpoint_on_connection(
                    conn,
                    service=service,
                    running=running,
                    prepared=prepared,
                    owed_admission=owed,
                    returned=returned,
                    projected=projected,
                )
        except ContinuationRetainedCapacityExceeded as excess:
            transaction = conn.get_transaction()
            if type(transaction) is not RootTransaction or not transaction.is_active:
                raise AuditIntegrityError("EARLY capacity refusal lost its original root transaction") from excess
            caught = _CaughtCapacityRefusal(excess)
            _capacity_sources[caught] = _CapacitySource(
                conn,
                transaction,
                prepared,
                owed,
                id(excess),
                excess.observed_bytes,
                excess.limit_bytes,
            )
            try:
                actual = EarlyCheckpointCompletion(
                    _settle_caught_overflow(conn, service=service, prepared=prepared, owed=owed, caught=caught)
                )
            finally:
                _capacity_sources.pop(caught, None)
        else:
            retry = read_early_format_retry_on_connection(conn, session_id=sid, operation_id=running.claim.operation_id, selected=completed)
            actual = EarlyCheckpointCompletion(EarlyCheckpointAdvanced(completed, retry))
    try:
        with locked_session_transaction(service._engine, sid) as conn:
            cold = _read_committed(conn, prepared=prepared, owed=owed, returned=returned, projected=projected)
    except BaseException as readback_error:
        delivery = EarlyCheckpointCompletionDeliveryError([readback_error], committed=actual)
        raise delivery from delivery.original_group
    if cold is None or cold != actual:
        mismatch = AuditIntegrityError("EARLY completion changed its independent committed readback")
        delivery = EarlyCheckpointCompletionDeliveryError([mismatch], committed=actual)
        raise delivery from delivery.original_group
    return actual


async def complete_early_checkpoint_or_overflow(
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> EarlyCheckpointLiveAdvanced | EarlyCheckpointPaidOverflowTerminal:
    """Keep original finish-once result and cancellation identities together."""
    from elspeth.web.composer.continuation_checkpoint_early_live import EarlyCheckpointLiveAdvanced, EarlyCheckpointPaidOverflowTerminal
    from elspeth.web.composer.continuation_checkpoint_early_physical import (
        CheckpointEarlyPhysicalSource,
        require_original_checkpoint_early_physical_source,
    )

    _require_original(prepared, owed, returned, projected)
    source = prepared.physical_source()
    if type(source) is not CheckpointEarlyPhysicalSource:
        raise AuditIntegrityError("EARLY completion lost its actual physical source")
    require_original_checkpoint_early_physical_source(source)
    work = source.phase_owner.phases.work
    if type(work) is not RequiredWorkBinding or work.running is not prepared.running:
        raise AuditIntegrityError("EARLY completion requires its original durable work binding")
    deadline = work.coordinator.sql_deadline_owner
    if deadline is None:
        raise AuditIntegrityError("EARLY completion lost its original SQL deadline")
    deadline.require_action_clock(source.clock)
    deadline.begin_settlement()
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            lambda: complete_early_checkpoint_or_overflow_sync(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=projected,
            ),
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    deferred: list[asyncio.CancelledError] = []
    actual: EarlyCheckpointCompletion | None = None
    deferred.extend(outcome.deferred_cancellations)
    try:
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            actual_outcome=outcome,
        )
        if type(outcome) is RequiredSQLReturned:
            if type(outcome.value) is not EarlyCheckpointCompletion:
                raise AuditIntegrityError("EARLY completion lost its actual nominal SQL result")
            actual = outcome.value
            projection.begin_projection()
            projection.complete_owned()
        elif type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
        else:
            raise AuditIntegrityError("EARLY completion lost its original finish-once outcome")
    except BaseException as delivery_error:
        _failed_projection(projection, delivery_error, errors)
    if type(outcome) is RequiredSQLRaised:
        errors.append(outcome.error)

    def read() -> EarlyCheckpointCompletion | None:
        with locked_session_transaction(service._engine, str(prepared.running.claim.session_id)) as conn:
            return _read_committed(conn, prepared=prepared, owed=owed, returned=returned, projected=projected)

    read_projection: RequiredWorkTicket | None = None
    read_sql: RequiredWorkTicket | None = None
    qualified_readback: RequiredSQLReturned[EarlyCheckpointCompletion | None] | None = None
    try:
        read_sql, read_projection = work.reserve_pair(
            RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
        )
        readback = await run_required_sql_finish_once(read_sql, read)
        deferred.extend(item for item in readback.deferred_cancellations if all(item is not prior for prior in deferred))
        if type(readback) is RequiredSQLRaised:
            errors.append(readback.error)
        work.coordinator.validate_continuation_sql_outcome(
            ticket=read_sql,
            expected_source=RequiredWorkSource.CONTINUATION_READ_SQL,
            actual_outcome=readback,
        )
        if type(readback) is RequiredSQLReturned:
            cold = readback.value
            if cold is not None and type(cold) is not EarlyCheckpointCompletion:
                raise AuditIntegrityError("EARLY completion cold read lost its nominal result")
            if actual is not None and cold != actual:
                raise AuditIntegrityError("EARLY completion cold result differs from its actual returned candidate")
            actual = cold
            qualified_readback = readback
            read_projection.begin_projection()
            read_projection.complete_owned()
        elif type(readback) is RequiredSQLRaised:
            read_projection.complete_without_submission()
        else:
            raise AuditIntegrityError("EARLY completion lost its readback finish-once outcome")
    except BaseException as readback_error:
        if read_projection is not None:
            _failed_projection(read_projection, readback_error, errors)
        else:
            errors.append(readback_error)
    if actual is not None and type(actual.result) is EarlyCheckpointAdvanced and qualified_readback is not None and read_sql is not None:
        proof = OriginalEarlyCompletionReadback()
        advanced = actual.result
        _readback_sources[proof] = _EarlyReadbackSource(
            source,
            advanced,
            returned,
            owed,
            projected,
            read_sql,
            qualified_readback,
            advanced.completed.checkpoint_id,
            advanced.completed.cursor,
            advanced.completed.payload_json,
        )
        try:
            from elspeth.web.composer.continuation_checkpoint_early_adoption import project_committed_early_cost

            project_committed_early_cost(
                source,
                advanced=advanced,
                returned=returned,
                owed=owed,
                projected=projected,
                read_ticket=read_sql,
                readback=qualified_readback,
                proof=proof,
            )
        except BaseException as cost_error:
            errors.append(cost_error)
        finally:
            _readback_sources.pop(proof, None)
    originals: list[BaseException] = []
    for item in (*errors, *deferred):
        if all(item is not prior for prior in originals):
            originals.append(item)
    if errors:
        if actual is not None:
            raise EarlyCheckpointCompletionDeliveryError(originals, committed=actual)
        if len(originals) == 1:
            raise originals[0]
        raise BaseExceptionGroup("EARLY completion original SQL/readback failures", originals)
    if actual is None:
        raise AuditIntegrityError("EARLY completion produced no committed source")
    if type(actual.result) is VerifiedCheckpointPaidOverflowTerminal:
        return EarlyCheckpointPaidOverflowTerminal(actual.result, tuple(deferred))
    if type(actual.result) is not EarlyCheckpointAdvanced:
        raise AuditIntegrityError("EARLY completion crossed its original result alternatives")
    if deferred:
        raise EarlyCheckpointCompletionDeliveryError(deferred, committed=actual)
    return EarlyCheckpointLiveAdvanced(source, actual.result, returned)
