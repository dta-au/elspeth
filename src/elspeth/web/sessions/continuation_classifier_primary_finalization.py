"""One atomic V4 classifier PRIMARY complete or paid terminal result."""

from __future__ import annotations

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
from elspeth.web.composer.continuation_primary_observation import (
    PrimaryCompleteObservation,
    PrimaryOverflowObservation,
    observe_v4_primary_provider_result,
    observed_primary_aggregate_overflow,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.provider_audit_source import provider_attempt_audit_drafts
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    _effect_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    ClassifierPrimaryResult,
    PreparedClassifierPrimaryProvider,
    V4ClassifierProviderAdmission,
    VerifiedClassifierPaidOverflowTerminal,
    _chain,
    _event,
    _PublishPermit,
    _read_source,
    publish_classifier_primary_on_connection,
)
from elspeth.web.sessions.continuation_classifier_primary_settlement import settle_v4_classifier_paid_overflow_on_connection
from elspeth.web.sessions.continuation_primary import _input_value
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
    record_primary_hold_consumed,
)
from elspeth.web.sessions.continuation_primary_paid_overflow_source import require_original_v4_primary_physical_source_on_connection
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProspectiveContinuationChunk,
    _require_historical_hold_source,
    _require_hold_fields,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.service import SessionServiceImpl


def finalize_v4_classifier_provider_on_connection(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    prepared: PreparedClassifierPrimaryProvider,
    admission: V4ClassifierProviderAdmission,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    permit: _PublishPermit,
) -> ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal:
    """Reprove the live response and exchange H for exactly one original result."""
    if (
        type(service) is not SessionServiceImpl
        or type(prepared) is not PreparedClassifierPrimaryProvider
        or type(admission) is not V4ClassifierProviderAdmission
        or type(outcome) is not _CallModelOutcome
        or type(recorder) is not RecorderSnapshot
        or type(permit) is not _PublishPermit
    ):
        raise AuditIntegrityError("V4 classifier finalizer lost its nominal paid source")
    original = prepared.prepared
    running = original.binding.running
    source = _read_source(conn, running.claim.session_id, running.claim.operation_id, original.binding.effect_key)
    selected = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=admission.dispatched.checkpoint_id,
        expected_cursor=admission.dispatched.cursor,
    )
    if (
        selected != admission.dispatched
        or _chain(conn, source, dispatched=True) != selected
        or admission.dispatched.cursor != original.snapshot.cursor + 1
        or admission.effect_key != original.binding.effect_key
    ):
        raise AuditIntegrityError("V4 classifier finalizer changed its original paid dispatch")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == admission.effect_key,
        )
    ).one()
    contract = _require_historical_hold_source(conn, admission.hold)
    hold = _require_hold_fields(conn, source=admission.hold, contract=contract)
    physical_row = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one_or_none()
    parent = conn.execute(
        select(jobs).where(jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id)
    ).one_or_none()
    call = outcome.audit_call
    if (
        hold.state != "active"
        or hold.provider_attempt_id != admission.attempt.attempt_id
        or effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or physical_row is None
        or physical_row.settled_at is not None
        or physical_row.ledger_entry_id is not None
        or parent is None
        or parent.status != "running"
        or call is None
        or call.call_id != admission.attempt.attempt_id
    ):
        raise AuditIntegrityError("V4 classifier finalizer lacks its original pending physical and hold")
    descriptor = _input_value(
        read_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=f"provider-input:{effect.input_sha256}",
        ).decode("utf-8", errors="strict"),
        effect.input_sha256,
    )
    requested_model = require_original_v4_primary_physical_source_on_connection(
        conn,
        source=admission.hold,
        descriptor=descriptor,
        effect=effect,
        physical=physical_row,
        parent=parent,
        call=call,
    )
    root = decode_continuation_snapshot(selected.payload_json)
    selected_state = SelectedContinuationState(
        selected.checkpoint_id,
        root,
        load_snapshot_private_data_on_connection(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, selected=selected
        ),
    )
    names = frozenset(source.env_ref_names)
    evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected_state,
        names=names,
    )
    observation = observe_v4_primary_provider_result(
        outcome=outcome,
        recorder=recorder,
        previous_evidence=evidence,
        context=running.session_operation_context,
        env_ref_names=names,
    )
    if type(observation) is PrimaryOverflowObservation:
        return settle_v4_classifier_paid_overflow_on_connection(
            conn,
            admission=admission,
            observation=observation,
            running=running,
            effect=effect,
            physical_row=physical_row,
            parent=parent,
            requested_model=requested_model,
        )
    if type(observation) is not PrimaryCompleteObservation:
        raise AuditIntegrityError("V4 classifier result lacks a closed complete or overflow observation")
    extended = append_primary_call_to_evidence(evidence, call=call, recorder=recorder, env_ref_names=names)
    expected_evidence = encode_turn_evidence(extended, env_ref_names=names)
    if expected_evidence != observation.evidence or extended.recorder_chunk != observation.recorder:
        raise AuditIntegrityError("V4 classifier observation changed original complete P4 evidence")
    entry = extended.snapshot.application_entry
    if entry is None:
        raise AuditIntegrityError("V4 classifier complete result lost its applied P4 graph")
    applied = encode_private_chunk(codec="composer.applied-p4-batch.v2", canonical=entry.graph_json, env_ref_names=names)
    compose = source.compose.model_copy(update={"audit_buffer": observation.recorder.reference, "evidence": observation.evidence.reference})
    committed_json = _effect_snapshot(
        encode_continuation_snapshot(
            root.model_copy(update={"frames": (compose, source.batch.model_copy(update={"accumulator": applied.reference}))})
        ),
        effect_key=admission.effect_key,
        disposition="committed",
        receipt_id=call.call_id,
    )
    event = ContinuationEvent("response_retained", _event(source))
    append = prepare_snapshot_append(selected, snapshot_json=committed_json, events=(event,), published_at=database_now(conn))
    result_chunks = tuple(
        ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
        for chunk in (observation.response, observation.recorder, observation.evidence, applied)
    )
    prospective = (
        *result_chunks,
        ProspectiveContinuationChunk(f"event:{selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    batch = V4PrimaryResultBatch(admission.hold, selected, result_chunks, append, prospective)
    try:
        admit_retained_payload_chunks(
            conn,
            session_id=admission.hold.session_id,
            operation_id=admission.hold.operation_id,
            prospective=prospective,
            retained_payload_limit_bytes=admission.hold.allowance_bytes,
            consuming_hold=admission.hold,
        )
    except ContinuationRetainedCapacityExceeded:
        return settle_v4_classifier_paid_overflow_on_connection(
            conn,
            admission=admission,
            observation=observed_primary_aggregate_overflow(
                complete=observation, context=running.session_operation_context, allowance_bytes=admission.hold.allowance_bytes
            ),
            running=running,
            effect=effect,
            physical_row=physical_row,
            parent=parent,
            requested_model=requested_model,
        )
    with admit_v4_primary_result_batch_on_connection(conn, batch=batch):
        drafts = provider_attempt_audit_drafts(call, running.session_operation_context)
        published = service._write_audit_cohort_on_connection(
            conn,
            session_id=running.claim.session_id,
            drafts=drafts,
            writer_principal="compose_loop",
            composition_state_id=None,
            session_operation_context=running.session_operation_context,
            audit_only=True,
        )
        if len(published) != len(drafts):
            raise AuditIntegrityError("V4 classifier complete result lost its physical audit cohort")
        result = publish_classifier_primary_on_connection(
            conn,
            original,
            selected=admission.dispatched,
            outcome=outcome,
            recorder=recorder,
            permit=permit,
            v4_admission=admission,
            prepared_append=append,
        )
        if result.snapshot != batch.final:
            raise AuditIntegrityError("V4 classifier complete result changed its exact root")
        consume_provider_terminal_hold_after_complete_result(
            conn, source=admission.hold, provider_attempt_id=admission.attempt.attempt_id, consumed_at=database_now(conn)
        )
        record_primary_hold_consumed(
            conn, session_id=admission.hold.session_id, operation_id=admission.hold.operation_id, source=admission.hold
        )
        return result
