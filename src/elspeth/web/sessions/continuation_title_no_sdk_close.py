"""Original zero-use SQL closure for a TITLE provider call never entered."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_title_no_sdk import TitleNoSdkRefusalPermit, TitleNoSdkRefusalReason
from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import cancel_undispatched_provider_attempt_on_connection
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord
from elspeth.web.sessions.continuation import _finalize_terminal_rows_after_append, append_complete_snapshot, put_chunk
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_title_no_sdk_tail import build_title_no_sdk_tail_batch
from elspeth.web.sessions.continuation_title_no_sdk_terminal import read_v4_title_no_sdk_terminal_on_connection
from elspeth.web.sessions.continuation_title_provider import (
    PreparedTitleProvider,
    V4TitleProviderAdmission,
    _current,
    _source_chain,
)
from elspeth.web.sessions.continuation_v4_paid_tail import (
    admit_v4_title_no_sdk_tail_on_connection,
    record_paid_tail_hold_consumed,
)
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


def fixed_title_no_sdk_error(reason: TitleNoSdkRefusalReason) -> ComposerOperationError:
    if type(reason) is not TitleNoSdkRefusalReason:
        raise TypeError("TITLE no-SDK public error needs its original closed reason")
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


def refuse_v4_title_no_sdk_on_connection(
    conn: Connection, *, service: SessionServiceImpl, permit: TitleNoSdkRefusalPermit
) -> ComposerOperationRecord:
    """Verify issuer, cancel original attempt, retain exact refusal and consume H."""
    if type(permit) is not TitleNoSdkRefusalPermit:
        raise AuditIntegrityError("TITLE no-SDK refusal requires its original nominal permit")
    permit.issuer.require_refusal_permit(permit)
    prepared = permit.prepared
    if service is not prepared.composer._require_sessions_service():
        raise AuditIntegrityError("TITLE no-SDK refusal requires its original sessions writer")
    # Cancellation allocates and inserts the canonical audit message. Acquire
    # the allocator's transaction-scoped session lock before touching any
    # physical/ledger/terminal rows, and retain it for the complete closure.
    with service._session_write_lock(conn, str(prepared.running.claim.session_id)):
        return _refuse_v4_title_no_sdk_locked_on_connection(conn, service=service, permit=permit)


def _refuse_v4_title_no_sdk_locked_on_connection(
    conn: Connection, *, service: SessionServiceImpl, permit: TitleNoSdkRefusalPermit
) -> ComposerOperationRecord:
    permit.issuer.require_refusal_permit(permit)
    prepared = permit.prepared
    admission = permit.admission
    if (
        type(prepared) is not PreparedTitleProvider
        or type(admission) is not V4TitleProviderAdmission
        or admission.tail.admitted != admission.selected
        or permit.observed_model != prepared.request.kwargs["model"]
        or admission.no_sdk_tail.requested_model != prepared.request.kwargs["model"]
    ):
        raise AuditIntegrityError("TITLE no-SDK refusal changed original phase/model/attempt")
    running = prepared.running
    selected = admission.selected
    _current(conn, running, selected, owed=True)
    descriptor, reference, stage, _origin = _source_chain(conn, running, selected, names=prepared.names)
    if stage != "admitted" or reference != prepared.input.reference:
        raise AuditIntegrityError("TITLE no-SDK refusal lost its original admitted input")
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
        raise AuditIntegrityError("TITLE no-SDK refusal lacks one original unsettled physical owner")
    from elspeth.web.sessions.continuation_title_paid_overflow_source import require_original_v4_title_physical_source_on_connection

    requested_model = require_original_v4_title_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=child, physical=physical_row, parent=parent
    )
    if requested_model != permit.observed_model:
        raise AuditIntegrityError("TITLE no-SDK refusal changed its original physical requested model")
    published_at = database_now(conn)
    batch = build_title_no_sdk_tail_batch(
        admission.no_sdk_tail,
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

    with admit_v4_title_no_sdk_tail_on_connection(
        conn, source=source, paid_plan=admission.tail, incomplete_plan=admission.incomplete_tail, plan=admission.no_sdk_tail, batch=batch
    ):
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
            codec="composer.title-no-sdk.v1",
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
            raise AuditIntegrityError("TITLE no-SDK refusal lost original dispatched child CAS")
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
            terminal_values=_terminal_values(fixed_title_no_sdk_error(permit.reason), now=published_at, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=published_at,
        )
        verified = read_v4_title_no_sdk_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            names=prepared.names,
        )
        if (
            verified is None
            or verified.source != source
            or verified.terminal != retained
            or verified.receipt_json != batch.receipt_json
            or verified.provider_attempt_id != admission.attempt.attempt_id
            or verified.hold_state != "active"
        ):
            raise AuditIntegrityError("TITLE no-SDK refusal cannot consume an unverified original terminal")
        contract = _require_historical_hold_source(conn, source)
        hold = _require_hold_fields(conn, source=source, contract=contract)
        if hold.state != "active" or hold.provider_attempt_id != admission.attempt.attempt_id:
            raise AuditIntegrityError("TITLE no-SDK refusal lost original active H")
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
            raise AuditIntegrityError("TITLE no-SDK refusal could not consume exact H")
        record_paid_tail_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        post = read_v4_title_no_sdk_terminal_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            names=prepared.names,
        )
        if (
            post is None
            or post.hold_state != "consumed"
            or post.receipt_json != verified.receipt_json
            or post.terminal != verified.terminal
        ):
            raise AuditIntegrityError("TITLE no-SDK refusal changed after H consumption")
    final = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if final.status != "failed" or final.checkpoint_id != retained.checkpoint_id or final.journal_cursor != retained.cursor:
        raise AuditIntegrityError("TITLE no-SDK refusal lost its atomic failed parent")
    return _record_from_row(final)
