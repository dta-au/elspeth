"""Atomic zero-use closure for one V4 semantic Advisor admission never sent to the SDK."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_semantic_no_sdk import (
    SemanticAdvisorNoSdkIssuer,
    SemanticAdvisorNoSdkRefusalReason,
)
from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import cancel_undispatched_provider_attempt_on_connection
from elspeth.web.sessions.composer_operations import ComposerOperationRecord
from elspeth.web.sessions.continuation import (
    PreparedContinuationProvider,
    _finalize_terminal_rows_after_append,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    put_chunk,
)
from elspeth.web.sessions.continuation_retained_capacity import _require_historical_hold_source, _require_hold_fields
from elspeth.web.sessions.continuation_semantic_advisor import (
    V4SemanticProviderAdmission,
    _decode,
    _effect_data,
    _guard_refusal_source,
    _NoSdk,
    _original_input_data,
    _undispatched_source,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_capacity import (
    V4SemanticResultBatch,
    admit_v4_semantic_result_batch_on_connection,
    record_semantic_hold_consumed,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import (
    build_semantic_no_sdk_tail_batch,
    fixed_v4_semantic_no_sdk_error,
)
from elspeth.web.sessions.continuation_semantic_v4_no_sdk_terminal import read_v4_semantic_no_sdk_terminal_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import composer_segment_attempts_table as history_table
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


def close_v4_semantic_no_sdk_on_connection(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    prepared: PreparedContinuationProvider,
    admission: V4SemanticProviderAdmission,
    issuer: SemanticAdvisorNoSdkIssuer,
    reason: SemanticAdvisorNoSdkRefusalReason,
    original_failure: BaseException,
) -> ComposerOperationRecord:
    """Cancel original physical intent, retain exact refusal and consume H together."""
    if (
        type(prepared) is not PreparedContinuationProvider
        or type(admission) is not V4SemanticProviderAdmission
        or type(issuer) is not SemanticAdvisorNoSdkIssuer
        or type(reason) is not SemanticAdvisorNoSdkRefusalReason
        or issuer.require_original_v4_admission(prepared) != admission
    ):
        raise AuditIntegrityError("V4 semantic no-SDK closure requires its original nominal admission")
    running = prepared.binding.running
    source = admission.hold
    sid, operation, effect_key = source.session_id, source.operation_id, source.effect_key
    if (sid, operation, effect_key) != (str(running.claim.session_id), running.claim.operation_id, prepared.binding.effect_key):
        raise AuditIntegrityError("V4 semantic no-SDK closure changed its original owner")
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    selected = load_snapshot(conn, session_id=sid, operation_id=operation)
    if selected != admission.selected:
        raise AuditIntegrityError("V4 semantic no-SDK closure lost its original admitted head")
    root = decode_continuation_snapshot(selected.payload_json)
    effect = _effect_data(conn, sid, operation, root, effect_key)
    physical_input = _original_input_data(conn, sid, operation, root, effect).physical_input
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == operation, segments.c.sequence == source.creator_sequence
        )
    ).one()
    history = conn.execute(
        select(history_table).where(
            history_table.c.session_id == sid,
            history_table.c.operation_id == operation,
            history_table.c.sequence == source.creator_sequence,
            history_table.c.generation == source.creator_generation,
        )
    ).one()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
    contract = _require_historical_hold_source(conn, source)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if (
        hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.requested_model != physical_input.options["model"]
        or effect.requested_model != admission.no_sdk_tail.requested_model
        or effect.input_sha256 != source.source_input_sha256
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.response_finished_at is not None
        or attempt.settled_at is not None
        or attempt.ledger_entry_id is not None
        or parent.status != "running"
        or parent.current_segment_sequence != source.creator_sequence
        or parent.ownership_generation != source.creator_generation
        or parent.attempt != source.claim_attempt
        or segment.state != "running"
        or segment.generation != source.creator_generation
        or history.claim_attempt != source.claim_attempt
        or history.revoked_at is not None
    ):
        raise AuditIntegrityError("V4 semantic no-SDK closure lacks its original unsettled admission")
    context = running.session_operation_context
    with service._session_write_lock(conn, sid):

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

        cancel_undispatched_provider_attempt_on_connection(
            conn,
            session_operation_context=context,
            attempt_id=admission.attempt.attempt_id,
            requested_model=effect.requested_model,
            append_audit_event=append_audit_event,
        )
    issuer.note_v4_sql_cancelled_attempt(admission)
    permit = issuer.reserve_refusal(reason=reason, original_failure=original_failure)
    published_at = database_now(conn)
    tail = build_semantic_no_sdk_tail_batch(
        admission.no_sdk_tail,
        permit=permit,
        input_sha256=effect.input_sha256,
        effect_key=effect_key,
        creator_sequence=source.creator_sequence,
        creator_generation=source.creator_generation,
        published_at=published_at,
    )
    data = _decode(_NoSdk, tail.receipt_json)
    _undispatched_source(conn, running, effect, data)
    _guard_refusal_source(conn, running, effect, data)
    batch = V4SemanticResultBatch(source, admission.selected, (tail.prospective[0],), tail.append, tail.prospective)
    error = fixed_v4_semantic_no_sdk_error(reason)
    with admit_v4_semantic_result_batch_on_connection(conn, batch=batch):
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=tail.receipt_chunk_key,
            payload=tail.receipt_json.encode("utf-8"),
            codec="composer.semantic-no-sdk-refusal.v1",
        )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == effect_key,
                effects.c.disposition == "dispatched",
                effects.c.provider_attempt_id == admission.attempt.attempt_id,
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(disposition="refused", receipt_key=permit.receipt_key, result_chunk_key=tail.receipt_chunk_key)
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 semantic no-SDK closure lost original dispatched effect CAS")
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
            terminal_values=_terminal_values(error, now=published_at, settled_by="owner_terminal"),
            attempt_outcome="settled",
            now=published_at,
        )
        verified = read_v4_semantic_no_sdk_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
        )
        if (
            verified is None
            or verified.source != source
            or verified.receipt != data
            or verified.terminal != stored
            or verified.provider_attempt_id != admission.attempt.attempt_id
            or verified.hold_state != "active"
        ):
            raise AuditIntegrityError("V4 semantic no-SDK closure cannot consume an unverified original terminal")
        consumed = conn.execute(
            update(holds)
            .where(
                holds.c.session_id == sid,
                holds.c.operation_id == operation,
                holds.c.effect_key == effect_key,
                holds.c.state == "active",
                holds.c.provider_attempt_id == admission.attempt.attempt_id,
            )
            .values(state="consumed", consumed_at=database_now(conn))
        )
        if consumed.rowcount != 1:
            raise AuditIntegrityError("V4 semantic no-SDK closure lost its exact active H")
        record_semantic_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        post = read_v4_semantic_no_sdk_terminal_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, effect_key=effect_key
        )
        if post is None or post.hold_state != "consumed" or post.receipt != verified.receipt or post.terminal != verified.terminal:
            raise AuditIntegrityError("V4 semantic no-SDK closure changed after consuming its H")
    final = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).one()
    if final.status != "failed" or final.checkpoint_id != stored.checkpoint_id or final.journal_cursor != stored.cursor:
        raise AuditIntegrityError("V4 semantic no-SDK closure lost its atomic failed parent")
    return _record_from_row(final)
