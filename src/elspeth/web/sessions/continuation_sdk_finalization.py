"""One atomic SDK terminal writer, behind original nominal family source proofs."""

from __future__ import annotations

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import settle_provider_attempt_on_connection
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_paid_overflow_quota import OriginalPaidOverflowPhysicalAttempt, preflight_v4_requested_model
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4SdkResultBatch,
    admit_v4_sdk_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_sdk_dispatch import require_sdk_live_source_on_connection
from elspeth.web.sessions.continuation_sdk_quota import prepare_sdk_terminal_quota_entry, prepare_sdk_terminal_receipt
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, build_sdk_tail_batch
from elspeth.web.sessions.continuation_sdk_terminal_cold import (
    read_v4_sdk_terminal_on_connection,
    read_v4_sdk_terminal_verified_on_connection,
)
from elspeth.web.sessions.continuation_sdk_terminal_result import VerifiedSdkTerminal, sdk_terminal_error
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.time_normalization import restore_utc


def finalize_v4_sdk_terminal_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> VerifiedSdkTerminal:
    require_original_v4_sdk_failure(observation, custody=custody)
    live = require_sdk_live_source_on_connection(conn, custody=custody, observation=observation)
    data, running = live.source, live.running
    source, identity, sdk_tail = data.hold, data.identity, data.tail
    selected, requested_model, call = data.admitted, data.requested_model, custody.call
    sid, operation = source.session_id, source.operation_id
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    effect = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == source.effect_key)
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == identity.attempt_id)).one()
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if (
        custody.context is not running.session_operation_context
        or hold.state != "active"
        or hold.provider_attempt_id != identity.attempt_id
        or observation.attempt is not custody.attempt
        or identity.attempt_id != observation.attempt.attempt_id
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
        raise AuditIntegrityError("SDK terminal lacks its original unsettled dispatched source")
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
        raise AuditIntegrityError("V4 PRIMARY SDK terminal changed its original live segment")
    observed_at = database_now(conn)
    if observed_at < restore_utc(physical_row.started_at):
        raise AuditIntegrityError("V4 PRIMARY terminal observation predates paid admission")
    receipt = prepare_sdk_terminal_receipt(
        observation=observation, custody=custody, identity=identity, call=call, observed_at=observed_at, producer=data.family.producer
    )
    tail = build_sdk_tail_batch(sdk_tail, receipt=receipt, published_at=observed_at)
    batch = V4SdkResultBatch(source, sdk_tail, tail)
    error = sdk_terminal_error(parent.request_id, receipt)
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
    entry = prepare_sdk_terminal_quota_entry(
        physical=carrier,
        context=running.session_operation_context,
        receipt=receipt,
        requested_model=preflight_v4_requested_model(requested_model),
        observed_at=observed_at,
    )
    with admit_v4_sdk_result_batch_on_connection(conn, batch=batch):
        # The bounded immutable receipt is this terminal's audit evidence.
        # Never fabricate a successful full provider cohort when no SDK result exists.
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=tail.receipt_chunk_key,
            payload=tail.receipt_json.encode("utf-8"),
            codec="composer.sdk-terminal.v1",
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
                disposition="sdk_terminal",
                receipt_key=tail.receipt_key,
                result_chunk_key=tail.receipt_chunk_key,
                response_finished_at=observed_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 PRIMARY SDK terminal lost its original dispatched effect CAS")
        if data.family is SdkTerminalFamily.CHECKPOINT_END:
            for key in data.unresolved_outer_effects:
                outer = conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == sid,
                        effects.c.operation_id == operation,
                        effects.c.effect_key == key,
                        effects.c.owner.in_(("composer_end", "composer_iteration")),
                        effects.c.disposition == "dispatched",
                        effects.c.provider_attempt_id.is_(None),
                        effects.c.receipt_key.is_(None),
                        effects.c.result_chunk_key.is_(None),
                    )
                    .values(disposition="unknown")
                )
                if outer.rowcount != 1:
                    raise AuditIntegrityError("SDK END lost its original unpaid outer effect CAS")
        elif data.unresolved_outer_effects:
            raise AuditIntegrityError("SDK terminal borrowed checkpoint outer-effect authority")
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
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
        verified = read_v4_sdk_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            family=data.family,
            names=data.env_ref_names,
        )
        if verified is None or verified.source != source or verified.hold_state != "active" or verified.terminal != stored:
            raise AuditIntegrityError("PRIMARY SDK terminal cannot consume an unproved original terminal")
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
            raise AuditIntegrityError("PRIMARY SDK terminal lost its original hold CAS")
        record_primary_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        result = read_v4_sdk_terminal_verified_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            family=data.family,
            names=data.env_ref_names,
        )
        if result is None or result.receipt != receipt or result.terminal != stored:
            raise AuditIntegrityError("PRIMARY SDK terminal post-consumption proof changed")
    return result
