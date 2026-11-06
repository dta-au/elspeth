"""Retain a complete malformed END attempt before admitting its one format retry.

The receipt closes only the physical child. The original semantic Review and
its outer END effect remain outstanding; this is neither a model verdict nor
permission to publish or dispatch another call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from weakref import WeakKeyDictionary

from pydantic import ValidationError, model_validator
from sqlalchemy import Connection, LargeBinary, Text, case, cast, func, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import _CANONICAL_SAFE_INTEGER_MAX, canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.continuation import (
    ComposeFrame,
    ContinuationValue,
    EffectFrontierEntry,
    EndFrame,
    PrivateChunkRef,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_audit_buffer import (
    RECORDER_CODEC,
    RecorderSnapshot,
    decode_recorder_snapshot,
    validate_recorder_projection,
)
from elspeth.web.composer.continuation_capture_overflow import (
    ContinuationCaptureOverflowWitness,
    ContinuationMandatoryCaptureOverflow,
)
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.provider_audit_source import ProviderAuditSource
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    _usage_entry_id,
    settle_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    VerifiedCheckpointEndOwed,
    _stored_checkpoint,
    read_v4_checkpoint_end_owed_on_connection,
    read_v4_checkpoint_end_settled_owed_history_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPhaseKind, CheckpointPhaseSource, PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_private_audit import (
    admit_checkpoint_paid_audit_source_on_connection,
    checkpoint_paid_audit_cohort_for_hold,
    require_checkpoint_paid_private_audit_on_connection,
    write_checkpoint_paid_audit_cohort_on_connection,
)
from elspeth.web.sessions.continuation_cost import _closed_call
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
    consume_provider_terminal_hold_after_complete_result,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
)
from elspeth.web.sessions.continuation_selection import load_private_chunks
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import ColdCheckpointEndConsequenceSource


class EndFormatRetryMarker(ContinuationValue):
    owner: Literal["composer_checkpoint_end_format_retry"] = "composer_checkpoint_end_format_retry"
    version: Literal[2] = 2
    session_id: str
    operation_id: str
    attempt_id: str
    effect_key: str
    outer_effect_key: str
    owed_checkpoint_id: str
    owed_cursor: int
    original_phase_checkpoint_id: str
    end_input_ref: PrivateChunkRef
    request_sha256: str
    response_chunk_key: str
    receipt_key: str
    recorder: PrivateChunkRef
    evidence: PrivateChunkRef
    pass_index: int
    retry_ordinal: Literal[0] = 0
    next_retry_ordinal: Literal[1] = 1
    end_env_ref_names: tuple[str, ...]
    first_attempt_schema_valid: bool | None
    private_audit_sha256: str

    @model_validator(mode="after")
    def require_exact_retry_and_inventory(self) -> EndFormatRetryMarker:
        if (
            type(self.retry_ordinal) is not int
            or type(self.next_retry_ordinal) is not int
            or self.end_env_ref_names != tuple(sorted(set(self.end_env_ref_names)))
            or any(not name for name in self.end_env_ref_names)
        ):
            raise ValueError("END retry receipt changed its exact ordinal or credential inventory")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedEndFormatRetry:
    marker: EndFormatRetryMarker
    owed: VerifiedCheckpointEndOwed
    selected: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndFormatRetry:
    source: ColdCheckpointEndConsequenceSource
    prospective: tuple[ProspectiveContinuationChunk, ...]


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ColdProjectedEndFormatRetryOverflow:
    source: ColdCheckpointEndConsequenceSource
    witness: ContinuationCaptureOverflowWitness


_cold_projections: WeakKeyDictionary[ColdProjectedEndFormatRetry, tuple[object, ...]] = WeakKeyDictionary()
_cold_overflows: WeakKeyDictionary[ColdProjectedEndFormatRetryOverflow, tuple[object, ...]] = WeakKeyDictionary()


def require_original_cold_end_format_retry_projection(value: ColdProjectedEndFormatRetry) -> None:
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import require_original_cold_checkpoint_end_source

    original = _cold_projections.get(value) if type(value) is ColdProjectedEndFormatRetry else None
    if original is None or value.source is not original[0] or value.prospective != original[1]:
        raise AuditIntegrityError("cold END retry aggregate projection lost its original source")
    require_original_cold_checkpoint_end_source(value.source)


def require_original_cold_end_format_retry_overflow(
    value: ColdProjectedEndFormatRetryOverflow,
) -> ContinuationCaptureOverflowWitness:
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import require_original_cold_checkpoint_end_source

    original = _cold_overflows.get(value) if type(value) is ColdProjectedEndFormatRetryOverflow else None
    if original is None or value.source is not original[0] or value.witness is not original[1]:
        raise AuditIntegrityError("cold END retry capture overflow lost its original source")
    require_original_cold_checkpoint_end_source(value.source)
    return value.witness


def issue_cold_end_format_retry_overflow(
    source: ColdCheckpointEndConsequenceSource, error: AuditIntegrityError
) -> ColdProjectedEndFormatRetryOverflow:
    """Bind an actual recorder/evidence capture refusal to its issued cold H."""
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import require_original_cold_checkpoint_end_source

    require_original_cold_checkpoint_end_source(source)
    cause = error.__cause__ if type(error) is AuditIntegrityError else None
    if type(cause) is not ContinuationMandatoryCaptureOverflow or cause.witness.codec not in (RECORDER_CODEC, "composer.evidence.v2"):
        raise AuditIntegrityError("cold END retry has no original mandatory capture refusal") from error
    value = ColdProjectedEndFormatRetryOverflow(source, cause.witness)
    _cold_overflows[value] = (source, cause.witness)
    return value


def _retry_recorder(conn: Connection, owed: VerifiedCheckpointEndOwed, *, call: ComposerLLMCall) -> tuple[PrivateChunk, RecorderSnapshot]:
    root = decode_continuation_snapshot(owed.selected.payload_json)
    compose = root.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END retry lost its one original recorder owner")
    stored = read_bounded_retained_chunk(
        conn,
        session_id=owed.session_id,
        operation_id=owed.operation_id,
        key=f"value:{compose.audit_buffer.codec}:{compose.audit_buffer.digest}",
        allowance=owed.new_hold.allowance_bytes,
    )
    if stored is None or stored.codec != compose.audit_buffer.codec:
        raise AuditIntegrityError("END retry lost its bounded original recorder")
    previous = decode_recorder_snapshot(PrivateChunk(compose.audit_buffer, stored.payload.decode("utf-8", errors="strict")))
    if call.call_id != owed.attempt_id or any(item.call_id == call.call_id for item in previous.llm_calls):
        raise AuditIntegrityError("END retry duplicated or replaced its original paid audit")
    # Earlier physical audits retain their original drain semantics. The
    # finalizer independently reproves and deduplicates checkpointed calls.
    extended = replace(previous, llm_calls=(*previous.llm_calls, call))
    canonical, _restored = validate_recorder_projection(extended)
    return encode_private_chunk(codec=RECORDER_CODEC, canonical=canonical), extended


def _retry_evidence(
    conn: Connection,
    owed: VerifiedCheckpointEndOwed,
    recorder: PrivateChunk,
    *,
    call: ComposerLLMCall,
    extended: RecorderSnapshot,
    names: frozenset[str],
) -> tuple[PrivateChunk, tuple[PrivateChunk, ...]]:
    """Freshly admit the unchanged evidence graph with its exact paid recorder.

    Both private references change together. This is a DATA projection from
    the deep original H; it grants neither runtime nor provider authority.
    """
    root = decode_continuation_snapshot(owed.selected.payload_json)
    compose = root.frames[0]
    if type(compose) is not ComposeFrame:
        raise AuditIntegrityError("END retry evidence lost its original Compose owner")
    chunks = load_private_chunks(conn, session_id=owed.session_id, operation_id=owed.operation_id, stack=root)
    originals = tuple(chunk for chunk in chunks if chunk.reference == compose.evidence)
    if len(originals) != 1:
        raise AuditIntegrityError("END retry evidence lost its complete original graph")
    original = decode_turn_evidence(originals[0], env_ref_names=names, source_chunks=chunks)
    if original.snapshot.recorder != compose.audit_buffer:
        raise AuditIntegrityError("END retry evidence changed its original recorder identity")
    from elspeth.web.composer.continuation_evidence_application_data import append_call_to_evidence, refresh_selected_applied_batch_chunk

    refreshed = append_call_to_evidence(
        original,
        call=call,
        recorder=extended,
        env_ref_names=names,
        mandatory_retention=True,
    )
    if call.call_id != owed.attempt_id or refreshed.recorder_chunk != recorder:
        raise AuditIntegrityError("END retry evidence changed its exact paid recorder")
    batches = tuple(frame for frame in root.frames if type(frame) is ToolBatchFrame)
    if len(batches) > 1:
        raise AuditIntegrityError("END retry evidence has more than its original batch")
    applied: tuple[PrivateChunk, ...] = ()
    if batches and batches[0].accumulator.codec == "composer.applied-p4-batch.v2":
        batch = batches[0]
        if batch.phase != "result_retained":
            raise AuditIntegrityError("END retry changed its original applied batch phase")
        originals = tuple(chunk for chunk in chunks if chunk.reference == batch.accumulator)
        if len(originals) != 1:
            raise AuditIntegrityError("END retry lost its complete original applied batch")
        applied = (refresh_selected_applied_batch_chunk(originals[0], previous=original, updated=refreshed, env_ref_names=names),)
    return encode_turn_evidence(refreshed, env_ref_names=names, mandatory_retention=True), applied


def _retry_root(
    owed: VerifiedCheckpointEndOwed,
    receipt: str,
    recorder: PrivateChunkRef,
    evidence: PrivateChunkRef,
    *,
    applied: tuple[PrivateChunk, ...],
) -> str:
    root = decode_continuation_snapshot(owed.selected.payload_json)
    children = tuple(item for item in root.effect_frontier if item.effect_id == owed.effect_key)
    if (
        type(root.frames[0]) is not ComposeFrame
        or recorder.codec != "composer.audit-buffer.v2"
        or evidence.codec != "composer.evidence.v2"
        or len(children) != 1
        or children[0].disposition != "dispatched"
        or children[0].receipt_id is not None
        or root.pending_settlement_ids != (owed.attempt_id,)
        or root.pending_owner_results
    ):
        raise AuditIntegrityError("END format retry lost its exact outstanding physical child")
    frames = list(root.frames)
    frames[0] = root.frames[0].model_copy(update={"audit_buffer": recorder, "evidence": evidence})
    indexes = tuple(index for index, frame in enumerate(frames) if type(frame) is ToolBatchFrame)
    if len(indexes) > 1 or len(applied) > 1:
        raise AuditIntegrityError("END retry changed its exact applied batch closure")
    if indexes:
        batch = frames[indexes[0]]
        if type(batch) is not ToolBatchFrame:
            raise AuditIntegrityError("END retry lost its original batch frame")
        if batch.accumulator.codec == "composer.applied-p4-batch.v2":
            if batch.phase != "result_retained" or len(applied) != 1:
                raise AuditIntegrityError("END retry lost its refreshed applied batch chunk")
            if applied[0].reference.codec != "composer.applied-p4-batch.v2":
                raise AuditIntegrityError("END retry substituted its applied batch codec")
            frames[indexes[0]] = batch.model_copy(update={"accumulator": applied[0].reference})
        elif applied:
            raise AuditIntegrityError("END retry invented an applied batch source")
    elif applied:
        raise AuditIntegrityError("END retry invented an applied batch source")
    return encode_continuation_snapshot(
        root.model_copy(
            update={
                "frames": tuple(frames),
                "effect_frontier": tuple(
                    EffectFrontierEntry(effect_id=item.effect_id, disposition="committed", receipt_id=receipt)
                    if item.effect_id == owed.effect_key
                    else item
                    for item in root.effect_frontier
                ),
                "pending_settlement_ids": (),
            }
        )
    )


def reprepare_cold_end_format_retry_prospective_on_connection(
    conn: Connection,
    *,
    service: object,
    source: ColdCheckpointEndConsequenceSource,
) -> tuple[ProspectiveContinuationChunk, ...]:
    """Rebuild the complete required private closure without mutating current H."""
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
        ColdCheckpointEndConsequenceSource,
        require_original_cold_checkpoint_end_source,
    )
    from elspeth.web.required_work import RequiredWorkBinding
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(source) is not ColdCheckpointEndConsequenceSource or not conn.in_transaction():
        raise AuditIntegrityError("cold END retry projection lacks its session SQL owner")
    require_original_cold_checkpoint_end_source(source)
    work = source.runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding or work.running is None:
        raise AuditIntegrityError("cold END retry projection lost its claimed operation")
    running = work.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if source.runtime.materialization.service._require_sessions_service() is not service:
        raise AuditIntegrityError("cold END retry projection changed its original service")
    with service._session_write_lock(conn, sid):
        _require_current_continuation_running(conn, running)
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=source.owed.attempt_id,
            names=source.runtime.env_ref_names,
        )
        if owed is None or owed != source.owed or load_snapshot(conn, session_id=sid, operation_id=operation) != owed.selected:
            raise AuditIntegrityError("cold END retry projection replaced its original current H")
        content = decode_continuation_response(owed.response).message.content
        call = decode_retained_physical_audit_call(owed.response)
        if type(content) is not str or call.call_id != owed.attempt_id or call.messages_hash != owed.request_sha256:
            raise AuditIntegrityError("cold END retry projection lost its retained original response")
        verdict = _parse_advisor_checkpoint_guidance(content, phase="end")
        if (
            verdict.ok
            or verdict.failure_class != "malformed"
            or owed.retry_ordinal != 0
            or owed.phase_source is None
            or owed.end_input_ref is None
        ):
            raise AuditIntegrityError("cold END retry projection cannot reclassify its original response")
        cohort = checkpoint_paid_audit_cohort_for_hold(call, owed.new_hold)
        recorder, extended = _retry_recorder(conn, owed, call=call)
        evidence, applied = _retry_evidence(conn, owed, recorder, call=call, extended=extended, names=source.runtime.env_ref_names)
        receipt = f"checkpoint-end-format-retry:{owed.attempt_id}"
        marker = EndFormatRetryMarker(
            session_id=sid,
            operation_id=operation,
            attempt_id=owed.attempt_id,
            effect_key=owed.effect_key,
            outer_effect_key=owed.outer_effect_key,
            owed_checkpoint_id=owed.selected.checkpoint_id,
            owed_cursor=owed.selected.cursor,
            original_phase_checkpoint_id=owed.phase_source.checkpoint_id,
            end_input_ref=owed.end_input_ref,
            request_sha256=owed.request_sha256,
            response_chunk_key=private_chunk_key(owed.response),
            receipt_key=receipt,
            recorder=recorder.reference,
            evidence=evidence.reference,
            pass_index=owed.pass_index,
            end_env_ref_names=tuple(sorted(source.runtime.env_ref_names)),
            first_attempt_schema_valid=verdict.response_schema_valid,
            private_audit_sha256=cohort.private_sha256,
        )
        event = ContinuationEvent("checkpoint", canonical_json(marker.model_dump(mode="json")))
        append = prepare_snapshot_append(
            owed.selected,
            snapshot_json=_retry_root(owed, receipt, recorder.reference, evidence.reference, applied=applied),
            events=(event,),
            published_at=database_now(conn),
        )
        return (
            ProspectiveContinuationChunk(private_chunk_key(recorder), recorder.canonical.encode("utf-8"), recorder.reference.codec),
            ProspectiveContinuationChunk(private_chunk_key(evidence), evidence.canonical.encode("utf-8"), evidence.reference.codec),
            *(
                ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
                for chunk in applied
            ),
            ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
            ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        )


def project_cold_end_format_retry_on_connection(
    conn: Connection, *, service: object, source: ColdCheckpointEndConsequenceSource
) -> ColdProjectedEndFormatRetry | ColdProjectedEndFormatRetryOverflow:
    try:
        prospective = reprepare_cold_end_format_retry_prospective_on_connection(conn, service=service, source=source)
    except AuditIntegrityError as error:
        return issue_cold_end_format_retry_overflow(source, error)
    projected = ColdProjectedEndFormatRetry(source, prospective)
    _cold_projections[projected] = (source, prospective)
    return projected


def recheck_cold_end_format_retry_capture_overflow_on_connection(
    conn: Connection, *, service: object, failure: ColdProjectedEndFormatRetryOverflow
) -> ContinuationCaptureOverflowWitness:
    witness = require_original_cold_end_format_retry_overflow(failure)
    try:
        reprepare_cold_end_format_retry_prospective_on_connection(conn, service=service, source=failure.source)
    except AuditIntegrityError as error:
        cause = error.__cause__
        if type(cause) is ContinuationMandatoryCaptureOverflow and cause.witness == witness:
            return witness
        raise AuditIntegrityError("cold END retry capture refusal changed under the current SQL lock") from error
    raise AuditIntegrityError("cold END retry capture refusal no longer reproduces")


def retain_end_format_retry_on_connection(
    conn: Connection,
    *,
    service: object,
    prepared: PreparedCheckpointProvider | None = None,
    owed_admission: CheckpointEndOwedAdmission | None = None,
    returned: CheckpointReturnedResponseObservation | None = None,
    cold_source: ColdCheckpointEndConsequenceSource | None = None,
) -> VerifiedEndFormatRetry:
    """Close one source-proved malformed H atomically, without SDK work."""
    from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
        ColdCheckpointEndConsequenceSource,
        require_original_cold_checkpoint_end_source,
    )
    from elspeth.web.required_work import RequiredWorkBinding
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or not conn.in_transaction():
        raise AuditIntegrityError("END format retry requires its original session SQL owner")
    if cold_source is None:
        if (
            type(prepared) is not PreparedCheckpointProvider
            or type(owed_admission) is not CheckpointEndOwedAdmission
            or type(returned) is not CheckpointReturnedResponseObservation
            or prepared.retry_ordinal != 0
            or prepared.composer._require_sessions_service() is not service
        ):
            raise AuditIntegrityError("END format retry requires its first original paid attempt")
        running = prepared.running
        names = prepared.env_ref_names
        attempt_id = owed_admission.original.attempt.attempt_id
    else:
        if (
            prepared is not None
            or owed_admission is not None
            or returned is not None
            or type(cold_source) is not ColdCheckpointEndConsequenceSource
        ):
            raise AuditIntegrityError("cold END retry cannot borrow warm physical custody")
        require_original_cold_checkpoint_end_source(cold_source)
        work = cold_source.runtime.bindings.required_work
        if (
            type(work) is not RequiredWorkBinding
            or work.running is None
            or cold_source.runtime.materialization.service._require_sessions_service() is not service
            or cold_source.runtime.bindings.session_operation_context is not work.running.session_operation_context
            or cold_source.runtime.source.selected is not cold_source.selected
            or cold_source.selected.checkpoint_id != cold_source.owed.selected.checkpoint_id
        ):
            raise AuditIntegrityError("cold END retry lost its original current H owner")
        running = work.running
        names = cold_source.runtime.env_ref_names
        attempt_id = cold_source.owed.attempt_id
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with service._session_write_lock(conn, sid):
        _require_current_continuation_running(conn, running)
        owed = read_v4_checkpoint_end_owed_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            attempt_id=attempt_id,
            names=names,
        )
        if owed is None or owed.retry_ordinal != 0 or load_snapshot(conn, session_id=sid, operation_id=operation) != owed.selected:
            raise AuditIntegrityError("END format retry replaced its original retained H source")
        if cold_source is None:
            if prepared is None or owed_admission is None or returned is None:
                raise AuditIntegrityError("warm END retry lost its original attempt")
            if (
                owed.selected != owed_admission.owed_selected
                or returned.response != owed.response
                or returned.attempt != owed_admission.original.attempt
            ):
                raise AuditIntegrityError("warm END retry replaced its original returned source")
            call = original_v4_checkpoint_returned_audit_call(returned, attempt_id=owed.attempt_id, request_sha256=prepared.request_sha256)
            content = returned.message.content
            original_phase_checkpoint_id = prepared.prepared_source.source.checkpoint_id
        else:
            if (
                owed != cold_source.owed
                or owed.selected.checkpoint_id != cold_source.selected.checkpoint_id
                or decode_continuation_snapshot(owed.selected.payload_json) != cold_source.selected.stack
            ):
                raise AuditIntegrityError("cold END retry changed its original H under lock")
            call = decode_retained_physical_audit_call(owed.response)
            content = decode_continuation_response(owed.response).message.content
            if call.call_id != owed.attempt_id or call.messages_hash != owed.request_sha256 or owed.phase_source is None:
                raise AuditIntegrityError("cold END retry lost its retained physical/request source")
            original_phase_checkpoint_id = owed.phase_source.checkpoint_id
        if type(content) is not str:
            raise AuditIntegrityError("END format retry requires complete original text")
        verdict = _parse_advisor_checkpoint_guidance(content, phase="end")
        if verdict.ok or verdict.failure_class != "malformed":
            raise AuditIntegrityError("END format retry cannot replace a usable or missing provider result")
        entry = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
        if entry is None or entry.call_id != owed.attempt_id:
            raise AuditIntegrityError("END format retry lost its original paid accounting")
        if (
            owed.phase_source is None
            or owed.end_input_ref is None
            or (prepared is not None and owed.phase_source != prepared.prepared_source.source)
        ):
            raise AuditIntegrityError("END format retry lost its original phase/input closure")
        cohort = checkpoint_paid_audit_cohort_for_hold(call, owed.new_hold)
        recorder, extended = _retry_recorder(conn, owed, call=call)
        evidence, applied = _retry_evidence(conn, owed, recorder, call=call, extended=extended, names=names)
        receipt = f"checkpoint-end-format-retry:{owed.attempt_id}"
        marker = EndFormatRetryMarker(
            session_id=sid,
            operation_id=operation,
            attempt_id=owed.attempt_id,
            effect_key=owed.effect_key,
            outer_effect_key=owed.outer_effect_key,
            owed_checkpoint_id=owed.selected.checkpoint_id,
            owed_cursor=owed.selected.cursor,
            original_phase_checkpoint_id=original_phase_checkpoint_id,
            end_input_ref=owed.end_input_ref,
            request_sha256=owed.request_sha256,
            response_chunk_key=private_chunk_key(owed.response),
            receipt_key=receipt,
            recorder=recorder.reference,
            evidence=evidence.reference,
            pass_index=owed.pass_index,
            end_env_ref_names=tuple(sorted(names)),
            first_attempt_schema_valid=verdict.response_schema_valid,
            private_audit_sha256=cohort.private_sha256,
        )
        event = ContinuationEvent("checkpoint", canonical_json(marker.model_dump(mode="json")))
        now = database_now(conn)
        append = prepare_snapshot_append(
            owed.selected,
            snapshot_json=_retry_root(owed, receipt, recorder.reference, evidence.reference, applied=applied),
            events=(event,),
            published_at=now,
        )
        admit_retained_payload_chunks(
            conn,
            session_id=sid,
            operation_id=operation,
            retained_payload_limit_bytes=owed.new_hold.allowance_bytes,
            consuming_hold=owed.new_hold,
            prospective=(
                ProspectiveContinuationChunk(private_chunk_key(recorder), recorder.canonical.encode("utf-8"), recorder.reference.codec),
                ProspectiveContinuationChunk(private_chunk_key(evidence), evidence.canonical.encode("utf-8"), evidence.reference.codec),
                *(
                    ProspectiveContinuationChunk(private_chunk_key(chunk), chunk.canonical.encode("utf-8"), chunk.reference.codec)
                    for chunk in applied
                ),
                ProspectiveContinuationChunk(f"event:{owed.selected.cursor + 1}", event.payload_json.encode("utf-8"), "event.v2"),
                ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
            ),
        )
        admitted_audit = admit_checkpoint_paid_audit_source_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            owed=owed,
            call=call,
            cohort=cohort,
            names=names,
        )
        for chunk in (recorder, evidence, *applied):
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        changed = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owed.effect_key,
                effects.c.owner == "provider",
                effects.c.provider_attempt_id == owed.attempt_id,
                effects.c.disposition == "dispatched",
                effects.c.receipt_key.is_(None),
                effects.c.result_chunk_key.is_(None),
            )
            .values(
                disposition="committed",
                receipt_key=receipt,
                result_chunk_key=marker.response_chunk_key,
                response_finished_at=call.finished_at,
            )
        )
        if changed.rowcount != 1:
            raise AuditIntegrityError("END format retry lost its original child CAS")
        settle_provider_attempt_on_connection(conn, session_id=sid, attempt_id=owed.attempt_id, entry=entry)
        write_checkpoint_paid_audit_cohort_on_connection(
            conn,
            service=service,
            context=running.session_operation_context,
            cohort=cohort,
            observed_at=now,
            admitted=admitted_audit,
        )
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=owed.selected.checkpoint_id,
            expected_cursor=owed.selected.cursor,
            snapshot_json=append.snapshot_json,
            event_batch=append.events,
            handoff=False,
            prepared_append=append,
            consuming_hold=owed.new_hold,
        )
        consume_provider_terminal_hold_after_complete_result(
            conn, source=owed.new_hold, provider_attempt_id=owed.attempt_id, consumed_at=now
        )
        return read_end_format_retry_on_connection(conn, session_id=sid, operation_id=operation, selected=stored, names=names)


def retain_cold_end_format_retry_on_connection(
    conn: Connection, *, service: object, source: ColdCheckpointEndConsequenceSource
) -> VerifiedEndFormatRetry:
    """Settle a current cold H only after its original source is reissued and reproved."""
    return retain_end_format_retry_on_connection(conn, service=service, cold_source=source)


def read_end_format_retry_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str] | None,
) -> VerifiedEndFormatRetry:
    """Cold reproof of the exact complete malformed child and next retry head."""
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    if contract.version != "composer.continuation.policy.v4" or contract.allowance is None:
        raise AuditIntegrityError("END format retry requires its original bounded V4 contract")
    # Owner recovery appends only a same-sequence recovered event. Follow that
    # proved immutable lineage to the original retry marker; the current head
    # remains the selected head used by the caller and grants no SDK authority.
    marker_snapshot = semantic_recovery_source(conn, session_id, operation_id, selected).snapshot
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == marker_snapshot.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("END format retry lost its checkpoint event")
    event_chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=row.payload_chunk_key,
        allowance=contract.allowance,
    )
    if event_chunk is None or event_chunk.codec != "event.v2":
        raise AuditIntegrityError("END format retry lost its bounded original event")
    canonical = event_chunk.payload.decode("utf-8", errors="strict")
    try:
        marker = EndFormatRetryMarker.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("END format retry lost its closed original receipt") from error
    if canonical_json(marker.model_dump(mode="json")) != canonical or (marker.session_id, marker.operation_id) != (
        session_id,
        operation_id,
    ):
        raise AuditIntegrityError("END format retry changed its canonical original identity")
    original_names = frozenset(marker.end_env_ref_names)
    if names is not None and names != original_names:
        raise AuditIntegrityError("END format retry changed its original credential inventory")
    owed = read_v4_checkpoint_end_settled_owed_history_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        attempt_id=marker.attempt_id,
        owed_checkpoint_id=marker.owed_checkpoint_id,
        names=original_names,
        child_disposition="committed",
        expected_retry_ordinal=0,
    )
    if (
        _stored_checkpoint(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            checkpoint_id=marker_snapshot.checkpoint_id,
            allowance=owed.new_hold.allowance_bytes,
        )
        != marker_snapshot
    ):
        raise AuditIntegrityError("END format retry replaced its original retained checkpoint")
    call = decode_retained_physical_audit_call(owed.response)
    content = decode_continuation_response(owed.response).message.content
    if type(content) is not str:
        raise AuditIntegrityError("END format retry lost its complete returned text")
    verdict = _parse_advisor_checkpoint_guidance(content, phase="end")
    child = conn.execute(
        select(effects).where(
            effects.c.session_id == session_id,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == marker.effect_key,
        )
    ).one_or_none()
    recorder, extended = _retry_recorder(conn, owed, call=call)
    evidence, applied = _retry_evidence(conn, owed, recorder, call=call, extended=extended, names=original_names)
    stored_recorder = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=private_chunk_key(recorder),
        allowance=contract.allowance,
    )
    if (
        marker.recorder != recorder.reference
        or stored_recorder is None
        or stored_recorder.codec != recorder.reference.codec
        or stored_recorder.payload != recorder.canonical.encode("utf-8")
    ):
        raise AuditIntegrityError("END retry lost its complete ordered paid recorder prefix")
    stored_evidence = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=private_chunk_key(evidence),
        allowance=contract.allowance,
    )
    if (
        marker.evidence != evidence.reference
        or stored_evidence is None
        or stored_evidence.codec != evidence.reference.codec
        or stored_evidence.payload != evidence.canonical.encode("utf-8")
    ):
        raise AuditIntegrityError("END retry lost its complete refreshed evidence graph")
    for chunk in applied:
        stored = read_bounded_retained_chunk(
            conn, session_id=session_id, operation_id=operation_id, key=private_chunk_key(chunk), allowance=contract.allowance
        )
        if stored is None or stored.codec != chunk.reference.codec or stored.payload != chunk.canonical.encode("utf-8"):
            raise AuditIntegrityError("END retry lost its complete refreshed applied batch")
    expected = decode_continuation_snapshot(_retry_root(owed, marker.receipt_key, recorder.reference, evidence.reference, applied=applied))
    actual = decode_continuation_snapshot(marker_snapshot.payload_json)
    if (
        verdict.ok
        or verdict.failure_class != "malformed"
        or verdict.response_schema_valid != marker.first_attempt_schema_valid
        or owed.retry_ordinal != 0
        or owed.effect_key != marker.effect_key
        or owed.outer_effect_key != marker.outer_effect_key
        or owed.pass_index != marker.pass_index
        or owed.request_sha256 != marker.request_sha256
        or owed.phase_source is None
        or owed.phase_source.checkpoint_id != marker.original_phase_checkpoint_id
        or owed.end_input_ref != marker.end_input_ref
        or owed.selected.cursor != marker.owed_cursor
        or marker_snapshot.cursor != marker.owed_cursor + 1
        or private_chunk_key(owed.response) != marker.response_chunk_key
        or call.call_id != marker.attempt_id
        or marker.receipt_key != f"checkpoint-end-format-retry:{marker.attempt_id}"
        or child is None
        or child.receipt_key != marker.receipt_key
        or child.result_chunk_key != marker.response_chunk_key
        or actual != expected.model_copy(update={"cursor": marker_snapshot.cursor})
    ):
        raise AuditIntegrityError("END format retry changed its complete paid consequence")
    usage = token_usage_entry_from_llm_call_envelope(llm_call_audit_envelope(call))
    entry_id = _usage_entry_id(session_id=session_id, source="composer", run_id=None, call_id=marker.attempt_id)
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == marker.attempt_id)).one_or_none()
    entry = conn.execute(select(ledger).where(ledger.c.entry_id == entry_id)).one_or_none()
    if (
        usage is None
        or entry is None
        or attempt is None
        or entry.session_id != session_id
        or entry.identity_id != attempt.identity_id
        or entry.source != "composer"
        or entry.run_id is not None
        or entry.model != usage.model
        or entry.prompt_tokens != usage.prompt_tokens
        or entry.completion_tokens != usage.completion_tokens
        or entry.cached_prompt_tokens != usage.cached_prompt_tokens
        or entry.reasoning_tokens != usage.reasoning_tokens
        or attempt.ledger_entry_id != entry_id
        or attempt.operation_id != owed.new_hold.session_operation_id
        or attempt.operation_epoch != owed.new_hold.session_operation_epoch
        or type(attempt.lease_token) is not str
        or sha256(attempt.lease_token.encode("utf-8")).hexdigest() != owed.new_hold.lease_sha256
        or usage.recorded_at is None
        or restore_utc(entry.recorded_at) != restore_utc(usage.recorded_at)
    ):
        raise AuditIntegrityError("END format retry changed its original quota ledger")
    require_checkpoint_paid_private_audit_on_connection(
        conn,
        source=owed.new_hold,
        attempt_id=marker.attempt_id,
        private_sha256=marker.private_audit_sha256,
        incomplete=False,
    )
    public_text = cast(messages.c.tool_calls, Text)
    if conn.dialect.name == "postgresql":
        size = func.octet_length(public_text)
    elif conn.dialect.name == "sqlite":
        size = func.length(cast(messages.c.tool_calls, LargeBinary))
    else:
        raise AuditIntegrityError("END format retry audit has unsupported SQL dialect")
    bounded_calls = case((size <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES, messages.c.tool_calls), else_=None).label("tool_calls")
    public_rows = conn.execute(
        select(
            messages.c.role,
            messages.c.writer_principal,
            messages.c.content,
            messages.c.raw_content,
            messages.c.tool_call_id,
            messages.c.parent_assistant_id,
            messages.c.composition_state_id,
            bounded_calls,
        )
        .where(
            messages.c.session_id == session_id,
            messages.c.role == "audit",
            messages.c.tool_calls[0]["_kind"].as_string() == "llm_call_audit",
            messages.c.tool_calls[0]["call"]["call_id"].as_string() == marker.attempt_id,
        )
        .limit(2)
    ).all()
    if len(public_rows) != 1 or public_rows[0].tool_calls is None:
        raise AuditIntegrityError("END format retry lost its bounded original public paid audit")
    public_call = _closed_call(
        public_rows[0],
        ProviderAuditSource(session_id, owed.new_hold.session_operation_id, owed.new_hold.session_operation_epoch),
        marker.attempt_id,
    )
    if llm_call_audit_envelope(public_call) != llm_call_audit_envelope(call):
        raise AuditIntegrityError("END format retry changed its original public paid audit")
    return VerifiedEndFormatRetry(marker, owed, selected)


def read_end_format_retry_phase_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, selected: StoredContinuationSnapshot
) -> CheckpointPhaseSource | None:
    """Reprove retry-ready phase DATA; mint no gate, SDK custody or retry permit."""
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    contract = load_continuation_read_contract(conn, session_id=session_id, operation_id=operation_id)
    marker_snapshot = semantic_recovery_source(conn, session_id, operation_id, selected).snapshot
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == session_id,
            events.c.operation_id == operation_id,
            events.c.sequence == marker_snapshot.cursor,
        )
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        return None
    chunk = read_bounded_retained_chunk(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        key=row.payload_chunk_key,
        allowance=contract.allowance,
    )
    if chunk is None or chunk.codec != "event.v2":
        raise AuditIntegrityError("END retry phase lost its bounded checkpoint event")
    value, error = parse_json_strict(chunk.payload.decode("utf-8", errors="strict"))
    if error is not None or type(value) is not dict:
        raise AuditIntegrityError("END retry phase lost its owned checkpoint object")
    if value.get("owner") != "composer_checkpoint_end_format_retry":
        return None
    verified = read_end_format_retry_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected, names=None)
    original = verified.owed.phase_source
    if original is None:
        raise AuditIntegrityError("END retry phase lost its original dispatched outer source")
    root = decode_continuation_snapshot(selected.payload_json)
    kind: CheckpointPhaseKind
    if type(root.frames[-1]) is EndFrame:
        kind = "end_review"
    elif type(root.frames[-1]) is ToolBatchFrame:
        kind = "iteration_end"
    else:
        raise AuditIntegrityError("END retry phase changed its original caller frame")
    return CheckpointPhaseSource("end", kind, original, selected, verified.marker.outer_effect_key, (verified.marker.effect_key,))


def read_retry_ready_action_source_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    names: frozenset[str] | None = None,
) -> VerifiedEndFormatRetry | None:
    """Source-proved current retry cut for sizing/recovery; grants no paid call."""
    if load_snapshot(conn, session_id=session_id, operation_id=operation_id) != selected:
        raise AuditIntegrityError("END retry allowance selected another current SQL head")
    phase = read_end_format_retry_phase_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected)
    if phase is None:
        return None
    if phase.kind != "end_review" or phase.current != selected:
        raise AuditIntegrityError("END retry allowance has no supported original END Review source")
    proof = read_end_format_retry_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected, names=names)
    root = decode_continuation_snapshot(selected.payload_json)
    outer = tuple(item for item in root.effect_frontier if item.effect_id == proof.marker.outer_effect_key)
    child = tuple(item for item in root.effect_frontier if item.effect_id == proof.marker.effect_key)
    unresolved = tuple(item.effect_id for item in root.effect_frontier if item.disposition in ("prepared", "dispatched", "unknown"))
    if (
        len(root.frames) != 2
        or type(root.frames[0]) is not ComposeFrame
        or type(root.frames[1]) is not EndFrame
        or root.frames[1].phase != "review_needed"
        or root.pending_owner_results
        or root.pending_settlement_ids
        or len(outer) != 1
        or outer[0].disposition != "dispatched"
        or outer[0].receipt_id is not None
        or len(child) != 1
        or child[0].disposition != "committed"
        or child[0].receipt_id != proof.marker.receipt_key
        or unresolved != (proof.marker.outer_effect_key,)
    ):
        raise AuditIntegrityError("END retry allowance changed its original incomplete outer phase")
    parts = proof.marker.effect_key.split(":")
    suffix = parts[2] if len(parts) == 3 else ""
    if parts[:2] != ["provider", "1"] or not suffix.isascii() or not suffix.isdecimal() or len(suffix) > 16 or str(int(suffix)) != suffix:
        raise AuditIntegrityError("END retry allowance lost its canonical family-1 paid child")
    first_ordinal = int(suffix)
    next_ordinal = conn.execute(
        select(ordinals.c.next_ordinal).where(
            ordinals.c.session_id == session_id,
            ordinals.c.operation_id == operation_id,
            ordinals.c.family == 1,
        )
    ).scalar_one_or_none()
    if type(next_ordinal) is not int or first_ordinal >= _CANONICAL_SAFE_INTEGER_MAX or next_ordinal != first_ordinal + 1:
        raise AuditIntegrityError("END retry already advanced its one physical successor ordinal")
    successor = conn.execute(
        select(effects.c.effect_key).where(
            effects.c.session_id == session_id,
            effects.c.operation_id == operation_id,
            effects.c.effect_key == f"provider:1:{next_ordinal}",
        )
    ).scalar_one_or_none()
    if successor is not None:
        raise AuditIntegrityError("END retry already admitted its one successor paid child")
    return proof
