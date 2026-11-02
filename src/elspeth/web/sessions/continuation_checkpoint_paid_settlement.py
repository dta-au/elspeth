"""Original V4 checkpoint returned-overflow terminal transaction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointIncompleteResponseObservation,
    CheckpointOverflowUsageObservation,
    require_original_v4_checkpoint_returned_response,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import capture_provider_audit_source
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import build_checkpoint_paid_overflow_tail_batch
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    checkpoint_paid_overflow_error,
    read_v4_checkpoint_paid_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt, PaidOverflowUsage
from elspeth.web.sessions.continuation_paid_overflow_quota import (
    OriginalPaidOverflowPhysicalAttempt,
    preflight_v4_requested_model,
    prepare_paid_overflow_quota_entry,
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


def _receipt(
    admission: V4CheckpointProviderAdmission, observation: CheckpointIncompleteResponseObservation, observed_at: datetime
) -> PaidOverflowReceipt:
    if (
        type(observation) is not CheckpointIncompleteResponseObservation
        or type(observation.usage) is not CheckpointOverflowUsageObservation
        or observation.phase != admission.original_source.phase
        or observation.attempt_id != admission.attempt.attempt_id
        or observation.request_sha256 != admission.hold.source_input_sha256
        or observation.representation not in ("physical_audit", "response")
        or observation.document_codec
        != ("composer.physical-audit.v1" if observation.representation == "physical_audit" else "composer.response.v1")
        or observation.measurement != "at_least"
        or observation.limit_bytes != CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        or observation.observed_bytes != observation.limit_bytes + 1
        or observation.evidence_status != "incomplete"
        or observation.retryable is not False
    ):
        raise AuditIntegrityError("checkpoint returned overflow changed its original measured representation")
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
        raise AuditIntegrityError("checkpoint overflow lost its explicit original usage disposition")
    return PaidOverflowReceipt(
        schema_version="composer.paid-overflow.v1",
        identity=admission.paid_tail.identity,
        producer="checkpoint_advisor",
        outcome="terminal_paid_overflow",
        evidence_status="incomplete",
        retryable=False,
        reason="private_document_bytes",
        representation=observation.representation,
        measurement="at_least",
        observed_bytes=observation.observed_bytes,
        limit_bytes=observation.limit_bytes,
        cost=observation.cost,
        usage=retained_usage,
    )


def settle_v4_checkpoint_returned_overflow_on_connection(
    conn: Connection,
    *,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    observation: CheckpointIncompleteResponseObservation,
) -> VerifiedCheckpointPaidOverflowTerminal:
    """Commit child receipt, outer unknowns, failed parent, ledger and H once."""
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or type(observation) is not CheckpointIncompleteResponseObservation
        or prepared.prepared_source != admission.original_source
        or observation.source != capture_provider_audit_source(prepared.running.session_operation_context)
    ):
        raise AuditIntegrityError("checkpoint overflow changed its original paid invocation")
    require_original_v4_checkpoint_returned_response(
        observation,
        attempt_id=admission.attempt.attempt_id,
        request_sha256=prepared.request_sha256,
    )
    running = prepared.running
    selected = require_current_checkpoint_dispatched_on_connection(conn, running, admission)
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
    if (
        effect.disposition != "dispatched"
        or effect.provider_attempt_id != identity.attempt_id
        or effect.requested_model != prepared.requested_model
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
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
        raise AuditIntegrityError("checkpoint overflow lacks one original dispatched unsettled physical owner")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("checkpoint overflow observation predates its original paid admission")
    receipt = _receipt(admission, observation, observed_at)
    batch = build_checkpoint_paid_overflow_tail_batch(admission.paid_tail, receipt=receipt, published_at=observed_at)
    error = checkpoint_paid_overflow_error(parent.request_id, receipt.cost)
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
            raise AuditIntegrityError("checkpoint overflow lost its original dispatched child CAS")
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
                    raise AuditIntegrityError("checkpoint overflow lost its original unpaid outer effect CAS")
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
        verified = read_v4_checkpoint_paid_overflow_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if verified is None or verified.source != source or verified.hold_state != "active" or verified.receipt != receipt:
            raise AuditIntegrityError("checkpoint overflow lost its cold original terminal before H consumption")
        contract = _require_historical_hold_source(conn, source)
        state = _require_hold_fields(conn, source=source, contract=contract)
        if state.state != "active" or state.provider_attempt_id != identity.attempt_id:
            raise AuditIntegrityError("checkpoint overflow lost its original active hold")
        consumed_at = database_now(conn)
        changed_hold = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == source.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == identity.attempt_id,
            )
            .values(state="consumed", consumed_at=consumed_at)
        )
        if changed_hold.rowcount != 1:
            raise AuditIntegrityError("checkpoint overflow could not consume its exact original hold")
        record_checkpoint_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        post = read_v4_checkpoint_paid_overflow_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if post is None or post.hold_state != "consumed" or post.receipt != verified.receipt or post.terminal != verified.terminal:
            raise AuditIntegrityError("checkpoint overflow post-consumption cold proof changed")
        return post
