"""Exact complete V4 semantic Advisor batch, before any result write."""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import Connection, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder, llm_call_audit_envelope
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot, recorder_snapshot_json
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    ContinuationDocumentTooLarge,
)
from elspeth.web.composer.continuation_provider_outcomes import encode_physical_audit_call
from elspeth.web.composer.continuation_semantic_observation import (
    SemanticCompleteResponseObservation,
    SemanticIncompleteResponseObservation,
    observe_semantic_retained_tail_overflow,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    TokenUsageEntry,
    settle_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    PreparedContinuationProvider,
    _effect_snapshot,
    append_complete_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
)
from elspeth.web.sessions.continuation_semantic_advisor import (
    SemanticAdvisorProviderOutcome,
    SemanticAdvisorProviderResult,
    V4SemanticProviderAdmission,
    _advisor,
    _canonical,
    _effect_data,
    _extended_evidence_data,
    _original_input_data,
    _Outcome,
    _outcome_event,
    _paid_evidence_root,
    _paid_result_data,
    _parent,
    _ref,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_capacity import (
    V4SemanticResultBatch,
    admit_v4_semantic_result_batch_on_connection,
    record_semantic_hold_consumed,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects


@dataclass(frozen=True, slots=True)
class V4SemanticCompletePrepared:
    batch: V4SemanticResultBatch
    chunks: tuple[ProspectiveContinuationChunk, ...]
    response: PrivateChunk
    recorder: PrivateChunk
    audit_call: ComposerLLMCall
    quota_entry: TokenUsageEntry
    receipt_key: str


def prepare_v4_semantic_complete_on_connection(
    conn: Connection,
    *,
    prepared: PreparedContinuationProvider,
    admission: V4SemanticProviderAdmission,
    observation: SemanticCompleteResponseObservation,
    recorder: BufferingRecorder,
) -> V4SemanticCompletePrepared | SemanticIncompleteResponseObservation:
    if (
        type(prepared) is not PreparedContinuationProvider
        or type(admission) is not V4SemanticProviderAdmission
        or type(observation) is not SemanticCompleteResponseObservation
        or type(recorder) is not BufferingRecorder
    ):
        raise TypeError("V4 semantic complete preflight needs nominal original values")
    running = prepared.binding.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    selected = admission.selected
    root = decode_continuation_snapshot(selected.payload_json)
    effect = _effect_data(conn, sid, operation, root, prepared.binding.effect_key)
    data, physical_input = _original_input_data(conn, sid, operation, root, effect)
    call = observation.audit_call
    if (
        effect.disposition != "dispatched"
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or effect.requested_model != physical_input.options["model"]
        or observation.source != data.invocation
        or observation.request_sha256 != effect.input_sha256
        or observation.attempt is not admission.attempt
        or call.call_id != admission.attempt.attempt_id
        or call.status is not ComposerLLMCallStatus.SUCCESS
        or _advisor(root).phase != "awaiting_response"
        or _advisor(root).response is not None
    ):
        raise AuditIntegrityError("V4 semantic complete changed original physical result or effect")
    original_parent = _parent(decode_continuation_snapshot(data.source.selected_snapshot_json))
    previous = decode_recorder_snapshot(_ref(conn, sid, operation, original_parent.audit_buffer))
    captured = recorder.snapshot_for_continuation(drained=previous.drained)
    if replace(previous, llm_calls=(*previous.llm_calls, call)) != captured or not captured.llm_calls or captured.llm_calls[-1] is not call:
        raise AuditIntegrityError("V4 semantic complete changed original recorder prefix/channels")
    recorder_json = recorder_snapshot_json(captured)
    if len(recorder_json.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        return observe_semantic_retained_tail_overflow(
            observation,
            context=running.session_operation_context,
            representation="recorder",
            limit_bytes=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
        )
    record = encode_recorder_snapshot(captured)
    try:
        extended = _extended_evidence_data(conn, sid, operation, data, call, record)
    except ContinuationDocumentTooLarge:
        return observe_semantic_retained_tail_overflow(
            observation,
            context=running.session_operation_context,
            representation="evidence_pair",
            limit_bytes=CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
        )
    except AuditIntegrityError as error:
        if str(error) not in (
            "evidence source exceeds its original complete document bound",
            "evidence and original recorder exceed their complete pair bound",
        ):
            raise
        return observe_semantic_retained_tail_overflow(
            observation,
            context=running.session_operation_context,
            representation="evidence_pair",
            limit_bytes=CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
        )
    receipt_key = str(uuid4())
    manifest = _Outcome(
        effect_key=prepared.binding.effect_key,
        input_sha256=prepared.binding.input_sha256,
        receipt_key=receipt_key,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        source_checkpoint_id=selected.checkpoint_id,
        source_cursor=selected.cursor,
        outcome=SemanticAdvisorProviderOutcome.SUCCESS,
        audit_call_json=encode_physical_audit_call(call),
        response=observation.response.reference,
        recorder=record.reference,
        decision_json=canonical_json(observation.decision.model_dump(mode="json")),
        original_failure_class=None,
    )
    manifest_json = _canonical(manifest)
    result_key = f"semantic-provider-outcome:{sha256(manifest_json.encode('utf-8')).hexdigest()}"
    chunks = (
        record,
        extended.evidence,
        *((extended.applied_batch,) if extended.applied_batch is not None else ()),
        observation.response,
    )
    result_chunks = (
        *(
            ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
            for chunk in chunks
        ),
        ProspectiveContinuationChunk(result_key, manifest_json.encode("utf-8"), "composer.semantic-provider-outcome.v1"),
    )
    parent = original_parent.model_copy(
        update={
            "audit_buffer": record.reference,
            "evidence": extended.evidence.reference,
            "advisor_calls": original_parent.advisor_calls + 1,
        }
    )
    advisor = _advisor(root).model_copy(update={"phase": "response_retained", "response": observation.response.reference})
    snapshot_json = _effect_snapshot(
        encode_continuation_snapshot(_paid_evidence_root(root, parent, advisor, extended)),
        effect_key=effect.effect_key,
        disposition="committed",
        receipt_id=receipt_key,
    )
    event = ContinuationEvent("response_retained", _outcome_event(manifest, result_key))
    append = prepare_snapshot_append(selected, snapshot_json=snapshot_json, events=(event,), published_at=database_now(conn))
    prospective = (
        *result_chunks,
        ProspectiveContinuationChunk(f"event:{selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    quota_entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
    if quota_entry is None or quota_entry.call_id != admission.attempt.attempt_id:
        raise AuditIntegrityError("V4 semantic complete lost original accounting projection")
    return V4SemanticCompletePrepared(
        V4SemanticResultBatch(admission.hold, selected, result_chunks, append, prospective),
        result_chunks,
        observation.response,
        record,
        call,
        quota_entry,
        receipt_key,
    )


def preflight_v4_semantic_complete_capacity(conn: Connection, *, prepared: V4SemanticCompletePrepared) -> bool:
    """Return true only for a measured aggregate limit excess."""
    if type(prepared) is not V4SemanticCompletePrepared:
        raise TypeError("V4 semantic complete capacity requires nominal original batch")
    source = prepared.batch.source
    try:
        admit_retained_payload_chunks(
            conn,
            session_id=source.session_id,
            operation_id=source.operation_id,
            prospective=prepared.batch.prospective,
            retained_payload_limit_bytes=source.allowance_bytes,
            consuming_hold=source,
        )
    except ContinuationRetainedCapacityExceeded:
        return False
    return True


def persist_v4_semantic_complete_on_connection(
    conn: Connection, *, prepared: PreparedContinuationProvider, admission: V4SemanticProviderAdmission, batch: V4SemanticCompletePrepared
) -> SemanticAdvisorProviderResult:
    if type(batch) is not V4SemanticCompletePrepared or type(admission) is not V4SemanticProviderAdmission:
        raise TypeError("V4 semantic complete writer requires its exact prepared batch")
    source = admission.hold
    sid, operation = source.session_id, source.operation_id
    if batch.batch.source != source or batch.batch.admitted != admission.selected or prepared.binding.effect_key != source.effect_key:
        raise AuditIntegrityError("V4 semantic complete writer changed original batch or effect")
    with admit_v4_semantic_result_batch_on_connection(conn, batch=batch.batch):
        for chunk in batch.chunks:
            put_chunk(conn, session_id=sid, operation_id=operation, key=chunk.key, payload=chunk.payload, codec=chunk.codec)
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
            .values(
                disposition="committed",
                receipt_key=batch.receipt_key,
                result_chunk_key=batch.chunks[-1].key,
                response_finished_at=batch.audit_call.finished_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("V4 semantic complete lost original dispatched effect CAS")
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=admission.selected.checkpoint_id,
            expected_cursor=admission.selected.cursor,
            snapshot_json=batch.batch.append.snapshot_json,
            event_batch=batch.batch.append.events,
            handoff=False,
            prepared_append=batch.batch.append,
        )
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=admission.attempt.attempt_id, entry=batch.quota_entry)
        actual, _data, _root = _paid_result_data(
            conn, sid, operation, stored, decode_continuation_snapshot(stored.payload_json), source.effect_key
        )
        if actual.response != batch.response or actual.recorder != batch.recorder or actual.audit_call != batch.audit_call:
            raise AuditIntegrityError("V4 semantic complete lost original cold response, recorder or audit")
        consume_provider_terminal_hold_after_complete_result(
            conn, source=source, provider_attempt_id=admission.attempt.attempt_id, consumed_at=database_now(conn)
        )
        record_semantic_hold_consumed(conn, session_id=sid, operation_id=operation, source=source)
        return actual
