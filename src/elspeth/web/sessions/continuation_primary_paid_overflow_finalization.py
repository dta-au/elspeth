"""One V4 PRIMARY paid result transaction, including its original accounting."""

from __future__ import annotations

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_primary_observation import (
    PrimaryCompleteObservation,
    PrimaryOverflowObservation,
    PrimaryPaidObservation,
    observe_v4_primary_provider_result,
    observed_primary_aggregate_overflow,
)
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationProvider,
    _effect_snapshot,
    continuation_unpaid_terminal_reserve_on_connection,
    prepare_snapshot_append,
    private_chunk_key,
)
from elspeth.web.sessions.continuation_primary import (
    PrimaryPaidOverflowTerminal,
    PrimaryProviderResult,
    V4PrimaryProviderAdmission,
    _chunk,
    _primary_frame,
    _selected_effect,
    _v4_dispatch_action_projection,
    publish_primary_provider_on_connection,
    stored_primary_input_data_on_connection,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_settlement import settle_v4_primary_paid_overflow_on_connection
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProspectiveContinuationChunk,
    _require_historical_hold_source,
    _require_hold_fields,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.service import SessionServiceImpl


def finalize_v4_primary_provider_on_connection(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    prepared: PreparedContinuationProvider,
    admission: V4PrimaryProviderAdmission,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    observation: PrimaryPaidObservation,
    env_ref_names: frozenset[str],
) -> PrimaryProviderResult | PrimaryPaidOverflowTerminal:
    """Reprove a genuine observed result and exchange H for one exact outcome."""
    if (
        type(service) is not SessionServiceImpl
        or type(prepared) is not PreparedContinuationProvider
        or type(admission) is not V4PrimaryProviderAdmission
        or type(outcome) is not _CallModelOutcome
        or type(recorder) is not RecorderSnapshot
        or type(observation) not in (PrimaryCompleteObservation, PrimaryOverflowObservation)
    ):
        raise AuditIntegrityError("V4 PRIMARY finalizer lost its nominal paid source")
    running = prepared.binding.running
    context = running.session_operation_context
    if admission.selected.cursor != prepared.snapshot.cursor + 1 or admission.hold.effect_key != prepared.binding.effect_key:
        raise AuditIntegrityError("V4 PRIMARY finalizer changed its original admitted effect")
    selected, root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=admission.selected.checkpoint_id,
        cursor=admission.selected.cursor,
        effect_key=prepared.binding.effect_key,
        input_sha256=prepared.binding.input_sha256,
    )
    if selected != admission.selected or effect.disposition != "dispatched" or frame.phase != "awaiting_response":
        raise AuditIntegrityError("V4 PRIMARY finalizer lost its unresolved original dispatch")
    source = admission.hold
    contract = _require_historical_hold_source(conn, source)
    hold_row = _require_hold_fields(conn, source=source, contract=contract)
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one_or_none()
    parent = conn.execute(
        select(jobs).where(jobs.c.session_id == source.session_id, jobs.c.operation_id == source.operation_id)
    ).one_or_none()
    call = outcome.audit_call
    if (
        hold_row.state != "active"
        or hold_row.provider_attempt_id != admission.attempt.attempt_id
        or attempt is None
        or attempt.settled_at is not None
        or attempt.ledger_entry_id is not None
        or parent is None
        or parent.status != "running"
        or call is None
        or call.call_id != admission.attempt.attempt_id
    ):
        raise AuditIntegrityError("V4 PRIMARY finalizer lacks its original pending physical attempt and hold")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn, source=source, descriptor=descriptor, effect=effect, physical=attempt, parent=parent, call=call
    )
    evidence = decode_turn_evidence(
        _chunk(conn, running, frame.evidence),
        env_ref_names=env_ref_names,
        source_chunks=load_private_chunks(conn, session_id=source.session_id, operation_id=source.operation_id, stack=root),
    )
    if evidence.snapshot.recorder != frame.audit_buffer or evidence.recorder_chunk != _chunk(conn, running, frame.audit_buffer):
        raise AuditIntegrityError("V4 PRIMARY finalizer changed its original recorder and evidence")
    actual = observe_v4_primary_provider_result(
        outcome=outcome,
        recorder=recorder,
        previous_evidence=evidence,
        context=context,
        env_ref_names=env_ref_names,
    )
    if actual != observation:
        raise AuditIntegrityError("V4 PRIMARY finalizer changed its genuine paid observation")
    if type(actual) is PrimaryOverflowObservation:
        return settle_v4_primary_paid_overflow_on_connection(
            conn,
            admission=admission,
            observation=actual,
            running=running,
            effect=effect,
            physical_row=attempt,
            parent=parent,
            requested_model=requested_model,
        )
    if type(actual) is not PrimaryCompleteObservation:
        raise AuditIntegrityError("V4 PRIMARY result has no closed observation")
    frame = _primary_frame(root)
    changed = frame.model_copy(
        update={
            "phase": "response_retained",
            "pending_response": actual.response.reference,
            "audit_buffer": actual.recorder.reference,
            "evidence": actual.evidence.reference,
        }
    )
    committed_json = _effect_snapshot(
        encode_continuation_snapshot(root.model_copy(update={"frames": (changed,)})),
        effect_key=prepared.binding.effect_key,
        disposition="committed",
        receipt_id=call.call_id,
    )
    committed_json, action_chunk = _v4_dispatch_action_projection(
        conn, root=root, effect=effect, dispatched_json=committed_json, disposition="committed"
    )
    event = ContinuationEvent(
        "response_retained",
        canonical_json({"effect_key": prepared.binding.effect_key, "response_digest": actual.response.reference.digest}),
    )
    append = prepare_snapshot_append(selected, snapshot_json=committed_json, events=(event,), published_at=database_now(conn))
    result_chunks = tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
        for chunk in (actual.response, actual.recorder, actual.evidence, *((action_chunk,) if action_chunk is not None else ()))
    )
    prospective = (
        *result_chunks,
        ProspectiveContinuationChunk(f"event:{selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    batch = V4PrimaryResultBatch(source, selected, result_chunks, append, prospective)
    unpaid_floor = continuation_unpaid_terminal_reserve_on_connection(
        conn,
        session_id=source.session_id,
        operation_id=source.operation_id,
        post_root_json=append.snapshot_json,
        post_cursor=append.expected_cursor + len(append.events),
    )
    try:
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=(*prospective, *unpaid_floor),
            retained_payload_limit_bytes=source.allowance_bytes,
            consuming_hold=source,
        )
    except ContinuationRetainedCapacityExceeded:
        return settle_v4_primary_paid_overflow_on_connection(
            conn,
            admission=admission,
            observation=observed_primary_aggregate_overflow(complete=actual, context=context, allowance_bytes=source.allowance_bytes),
            running=running,
            effect=effect,
            physical_row=attempt,
            parent=parent,
            requested_model=requested_model,
        )
    with admit_v4_primary_result_batch_on_connection(conn, batch=batch):
        drafts = provider_attempt_audit_drafts(call, context)
        published = service._write_audit_cohort_on_connection(
            conn,
            session_id=running.claim.session_id,
            drafts=drafts,
            writer_principal="compose_loop",
            composition_state_id=None,
            session_operation_context=context,
            audit_only=True,
        )
        if len(published) != len(drafts):
            raise AuditIntegrityError("V4 PRIMARY complete result lost its exact physical audit cohort")
        result = publish_primary_provider_on_connection(
            conn,
            prepared.binding,
            selected=selected,
            outcome=outcome,
            recorder=recorder,
            env_ref_names=env_ref_names,
            complete_observation=actual,
            prepared_append=append,
        )
        if result.snapshot != batch.final:
            raise AuditIntegrityError("V4 PRIMARY complete result changed its exact final root")
        consume_provider_terminal_hold_after_complete_result(
            conn, source=source, provider_attempt_id=admission.attempt.attempt_id, consumed_at=database_now(conn)
        )
        record_primary_hold_consumed(conn, session_id=source.session_id, operation_id=source.operation_id, source=source)
        return result
