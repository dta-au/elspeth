"""Original zero-use SQL closure for a checkpoint provider call never entered."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkRefusalPermit, CheckpointNoSdkRefusalReason
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import cancel_undispatched_provider_attempt_on_connection
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_checkpoint_no_sdk_tail import build_checkpoint_no_sdk_tail_batch
from elspeth.web.sessions.continuation_checkpoint_no_sdk_terminal import read_v4_checkpoint_no_sdk_terminal_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid import (
    PreparedCheckpointProvider,
    V4CheckpointProviderAdmission,
    require_current_checkpoint_dispatched_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_v4_checkpoint_paid_tail import (
    admit_v4_checkpoint_no_sdk_tail_on_connection,
    record_checkpoint_paid_tail_hold_consumed,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


def fixed_checkpoint_no_sdk_error(reason: CheckpointNoSdkRefusalReason) -> ComposerOperationError:
    if type(reason) is not CheckpointNoSdkRefusalReason:
        raise TypeError("checkpoint no-SDK public error needs its original closed reason")
    return ComposerOperationError(
        http_status=503,
        failure_code="http_error",
        error_type="composer_admission_refused",
        body={
            "error_type": "composer_admission_refused",
            "detail": "The provider call was not entered; this operation was closed before SDK dispatch.",
            "reason": reason.value,
            "retryable": False,
        },
        diagnostic_id=None,
    )


def refuse_v4_checkpoint_no_sdk_on_connection(
    conn: Connection, *, service: SessionServiceImpl, permit: CheckpointNoSdkRefusalPermit
) -> ComposerOperationRecord:
    """Verify issuer, cancel original attempt, retain exact refusal and consume H."""
    if type(permit) is not CheckpointNoSdkRefusalPermit:
        raise AuditIntegrityError("checkpoint no-SDK refusal requires its original nominal permit")
    permit.issuer.require_refusal_permit(permit)
    prepared = permit.prepared
    admission = permit.admission
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(admission) is not V4CheckpointProviderAdmission
        or prepared.prepared_source != admission.original_source
        or permit.observed_model != prepared.requested_model
        or admission.terminal_hold.no_sdk.requested_model != prepared.requested_model
    ):
        raise AuditIntegrityError("checkpoint no-SDK refusal changed original phase/model/attempt")
    running = prepared.running
    selected = require_current_checkpoint_dispatched_on_connection(conn, running, admission, allow_cancel=True, allow_expired_segment=True)
    source = admission.hold
    sid, operation = source.session_id, source.operation_id
    child = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == source.effect_key)
    ).one()
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id).with_for_update()).one()
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
        child.disposition != "dispatched"
        or child.provider_attempt_id != admission.attempt.attempt_id
        or child.requested_model != permit.observed_model
        or child.receipt_key is not None
        or child.result_chunk_key is not None
        or child.response_finished_at is not None
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
        raise AuditIntegrityError("checkpoint no-SDK refusal lacks one original unsettled physical owner")
    published_at = database_now(conn)
    batch = build_checkpoint_no_sdk_tail_batch(
        admission.terminal_hold.no_sdk,
        reason=permit.reason,
        receipt_key=permit.receipt_key,
        published_at=published_at,
    )
    context = running.session_operation_context

    def append_audit_event(event_id: str, content: str, created_at: datetime) -> None:
        sequence = service._reserve_sequence_range(conn, sid, count=1)
        service._insert_chat_message(
            conn,
            session_id=sid,
            role="audit",
            content=content,
            raw_content=None,
            tool_calls=None,
            sequence_no=sequence,
            writer_principal="compose_loop",
            composition_state_id=None,
            tool_call_id=None,
            parent_assistant_id=None,
            created_at=created_at,
            session_operation_context=context,
            message_id=event_id,
            audit_only=True,
        )
        with service._session_mutations(conn, session_id=sid, session_operation_context=context) as mutations:
            mutations.mark_session_updated(updated_at=created_at)

    with admit_v4_checkpoint_no_sdk_tail_on_connection(conn, source=source, hold_plan=admission.terminal_hold, batch=batch):
        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=context,
            attempt_id=admission.attempt.attempt_id,
            requested_model=permit.observed_model,
            append_audit_event=append_audit_event,
        )
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=batch.receipt_chunk_key,
            payload=batch.receipt_json.encode("utf-8"),
            codec="composer.checkpoint-no-sdk.v1",
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == source.effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == admission.attempt.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=batch.receipt_chunk_key)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("checkpoint no-SDK refusal lost original dispatched child CAS")
        root = decode_continuation_snapshot(selected.payload_json)
        unresolved = {
            item.effect_id for item in root.effect_frontier if item.effect_id != source.effect_key and item.disposition == "dispatched"
        }
        for effect_key in unresolved:
            changed_outer = conn.execute(
                update(effects)
                .where(
                    effects.c.session_id == sid,
                    effects.c.operation_id == operation,
                    effects.c.effect_key == effect_key,
                    effects.c.disposition == "dispatched",
                    effects.c.receipt_key.is_(None),
                )
                .values(disposition="unknown")
            )
            if changed_outer.rowcount != 1:
                raise AuditIntegrityError("checkpoint no-SDK refusal lost unpaid outer effect CAS")
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
            terminal_values=_terminal_values(fixed_checkpoint_no_sdk_error(permit.reason), now=published_at, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=published_at,
        )
        verified = read_v4_checkpoint_no_sdk_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if (
            verified is None
            or verified.source != source
            or verified.terminal != retained
            or verified.receipt_json != batch.receipt_json
            or verified.provider_attempt_id != admission.attempt.attempt_id
            or verified.hold_state != "active"
        ):
            raise AuditIntegrityError("checkpoint no-SDK refusal cannot consume an unverified original terminal")
        contract = _require_historical_hold_source(conn, source)
        hold = _require_hold_fields(conn, source=source, contract=contract)
        if hold.state != "active" or hold.provider_attempt_id != admission.attempt.attempt_id:
            raise AuditIntegrityError("checkpoint no-SDK refusal lost original active H")
        consumed = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == source.effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == admission.attempt.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if consumed.rowcount != 1:
            raise AuditIntegrityError("checkpoint no-SDK refusal could not consume exact H")
        record_checkpoint_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        post = read_v4_checkpoint_no_sdk_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            effect_key=source.effect_key,
            names=prepared.env_ref_names,
        )
        if (
            post is None
            or post.hold_state != "consumed"
            or post.receipt_json != verified.receipt_json
            or post.terminal != verified.terminal
        ):
            raise AuditIntegrityError("checkpoint no-SDK refusal changed after H consumption")
    final = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if final.status != "failed" or final.checkpoint_id != retained.checkpoint_id or final.journal_cursor != retained.cursor:
        raise AuditIntegrityError("checkpoint no-SDK refusal lost its atomic failed parent")
    return _record_from_row(final)
