"""Detached complete EARLY checkpoint DATA, before any live application.

The phase SQL owner must admit this entire capture and the original pass in
one transaction. Nothing here settles quota, grants a retry, publishes a pass
or changes the live checkpoint caller's recorder/transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord
from elspeth.web.composer.advisor_checkpoint import (
    AdvisorCheckpointVerdict,
    _parse_advisor_checkpoint_guidance,
    checkpoint_pass_record,
    early_checkpoint_injection,
)
from elspeth.web.composer.continuation_capture_overflow import (
    ContinuationCaptureOverflowWitness,
    ContinuationMandatoryCaptureOverflow,
)
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    _capture_owners,
    _registered_read,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission


@dataclass(frozen=True, slots=True)
class CheckpointPassAccounting:
    pass_index: int
    provider_attempts: int
    first_attempt_schema_valid: bool | None
    first_attempt_accepted: bool | None
    format_reprompt_sent: bool


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEarlyCheckpoint:
    """An original detached computation; constructor access grants no custody."""

    captured: CapturedContinuationRuntime
    verdict: AdvisorCheckpointVerdict
    pass_record: AdvisorCheckpointPassRecord | None
    injection: str | None
    retry_pending: bool
    accounting: CheckpointPassAccounting


@dataclass(frozen=True, slots=True)
class _ProjectionSource:
    prepared: PreparedCheckpointProvider
    admission: V4CheckpointProviderAdmission
    returned: CheckpointReturnedResponseObservation
    captured: CapturedContinuationRuntime
    verdict: AdvisorCheckpointVerdict
    pass_record: AdvisorCheckpointPassRecord | None
    injection: str | None
    retry_pending: bool
    accounting: CheckpointPassAccounting


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEarlyCheckpointOverflow:
    witness: ContinuationCaptureOverflowWitness


@dataclass(frozen=True, slots=True)
class _ProjectionOverflowSource:
    prepared: PreparedCheckpointProvider
    admission: V4CheckpointProviderAdmission
    returned: CheckpointReturnedResponseObservation
    witness: ContinuationCaptureOverflowWitness


@dataclass(frozen=True, slots=True)
class _ProjectionCapture:
    captured: CapturedContinuationRuntime | None
    overflow: ContinuationCaptureOverflowWitness | None


_issued: WeakKeyDictionary[ProjectedEarlyCheckpoint, _ProjectionSource] = WeakKeyDictionary()
_overflows: WeakKeyDictionary[ProjectedEarlyCheckpointOverflow, _ProjectionOverflowSource] = WeakKeyDictionary()


def _require_capture_witness(witness: ContinuationCaptureOverflowWitness) -> None:
    if type(witness) is not ContinuationCaptureOverflowWitness:
        raise AuditIntegrityError("checkpoint capture overflow lost its nominal original measurement")
    private_codecs = {
        "composer.messages.v1",
        "composer.tools.v1",
        "composer.state.v1",
        "composer.audit-buffer.v2",
        "composer.evidence.v2",
        "composer.transcript.v1",
        "composer.caches.v1",
        "composer.iteration.v1",
        "composer.applied-p4-batch.v2",
        "composer.tool-batch.v1",
        "composer.coupled-batch-data.v1",
    }
    if (
        type(witness.codec) is not str
        or type(witness.observed_bytes) is not int
        or type(witness.limit_bytes) is not int
        or type(witness.size_is_exact) is not bool
        or witness.observed_bytes <= witness.limit_bytes
        or (not witness.size_is_exact and witness.observed_bytes != witness.limit_bytes + 1)
        or not (
            (
                witness.unit == "private_chunk"
                and witness.codec in private_codecs
                and witness.limit_bytes == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            )
            or (
                witness.unit == "evidence_pair"
                and witness.codec == "composer.evidence.v2"
                and witness.limit_bytes == CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
                and witness.size_is_exact
            )
        )
    ):
        raise AuditIntegrityError("checkpoint capture overflow changed its original unit or unchanged byte limit")


def _require_detached_source(detached: RestoredContinuationRuntime, original: RestoredContinuationRuntime) -> None:
    if type(detached) is not RestoredContinuationRuntime or type(original) is not RestoredContinuationRuntime:
        raise TypeError("checkpoint projection requires its actual restored runtime owners")
    if (
        type(detached.source.policy) is not ContinuationAdmissionPolicyV4
        or detached.source.selected != original.source.selected
        or detached.env_ref_names != original.env_ref_names
        or detached.bindings.required_work is not original.bindings.required_work
        or detached.bindings.provider_owner is not original.bindings.provider_owner
        or detached.bindings.session_operation_context is not original.bindings.session_operation_context
        or canonical_json(detached.live.state.to_dict()) != canonical_json(original.live.state.to_dict())
        or canonical_json(detached.live.llm_messages) != canonical_json(original.live.llm_messages)
        or detached.live.recorder.snapshot_for_continuation(drained=detached.drained)
        != original.live.recorder.snapshot_for_continuation(drained=original.drained)
    ):
        raise AuditIntegrityError("checkpoint projection changed its complete retained pre-call source")
    original_message_ids = {id(message) for message in original.live.llm_messages}
    if (
        detached is original
        or detached.live is original.live
        or detached.graph is original.graph
        or detached.materialization is original.materialization
        or detached.live.state is original.live.state
        or detached.live.recorder is original.live.recorder
        or detached.live.anti_anchor is original.live.anti_anchor
        or detached.live.turn_evidence is original.live.turn_evidence
        or detached.live.turn_evidence.transcript is original.live.turn_evidence.transcript
        or detached.live.llm_messages is original.live.llm_messages
        or any(id(message) in original_message_ids for message in detached.live.llm_messages)
        or (detached.graph.batch is not None and detached.graph.batch is original.graph.batch)
        or type(detached.detached_disclosure) is not SchemaDisclosureTracker
        or detached.detached_disclosure is original.materialization.service._schema_disclosure
    ):
        raise AuditIntegrityError("checkpoint projection reused an original mutable owner")
    detached_batch, original_batch = detached.graph.batch, original.graph.batch
    if (
        detached_batch is not None
        and original_batch is not None
        and (
            detached_batch.batch is original_batch.batch
            or detached_batch.accumulator is original_batch.accumulator
            or detached_batch.batch.tool_outcomes is original_batch.batch.tool_outcomes
            or detached_batch.batch.private_authorings_by_call is original_batch.batch.private_authorings_by_call
            or detached_batch.batch.decoded_args_by_call_id is original_batch.batch.decoded_args_by_call_id
            or detached_batch.batch.refused_resources is original_batch.batch.refused_resources
            or detached_batch.batch.promises_by_call is original_batch.batch.promises_by_call
        )
    ):
        raise AuditIntegrityError("checkpoint projection reused an original mutable batch owner")


async def _capture_dispatched_projection(
    *,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    detached: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
) -> _ProjectionCapture:
    from elspeth.web.sessions.continuation_checkpoint_paid import require_current_checkpoint_dispatched_on_connection

    before = detached.source.selected.stack
    current = selected.stack
    if (
        detached.source.selected.checkpoint_id != prepared.prepared_source.prepared_selected.checkpoint_id
        or before != decode_continuation_snapshot(prepared.prepared_source.prepared_selected.payload_json)
        or admission.prepared_selected != prepared.prepared_source.prepared_selected
        or selected.checkpoint_id != admission.selected.checkpoint_id
        or current != decode_continuation_snapshot(admission.selected.payload_json)
        or current.model_copy(
            update={
                "cursor": before.cursor,
                "effect_frontier": before.effect_frontier,
                "pending_settlement_ids": before.pending_settlement_ids,
            }
        )
        != before
        or inventory.running is not prepared.running
        or inventory.names != detached.env_ref_names
        or detached.bindings.required_work is not prepared.work
        or detached.materialization.authority is not prepared.authority
    ):
        raise AuditIntegrityError("checkpoint projection replaced its exact pre-call graph at dispatch")

    def capture() -> _ProjectionCapture:
        with locked_session_transaction(prepared.authority._engine, str(prepared.running.claim.session_id)) as conn:
            if require_current_checkpoint_dispatched_on_connection(conn, prepared.running, admission) != admission.selected:
                raise AuditIntegrityError("checkpoint projection lost its actual pending source")
            # Ordinary runtime reads correctly refuse unresolved quota attempts.
            # Only the exact original held attempt may bind its already decoded
            # pre-call DATA to the current dispatch frontier for this capture.
            source = replace(detached.source, selected=selected)
            try:
                result = _ProjectionCapture(_capture_owners(detached, source=source, inventory=inventory), None)
            except (ComposerContextUnsupported, AuditIntegrityError) as error:
                cause = error.__cause__
                if type(cause) is not ContinuationMandatoryCaptureOverflow:
                    raise
                _require_capture_witness(cause.witness)
                # This is only a size witness. Authority comes from the exact
                # original source/call checks and held-attempt proof around the
                # actual capture, never from arbitrary encoder exceptions.
                result = _ProjectionCapture(None, cause.witness)
            if require_current_checkpoint_dispatched_on_connection(conn, prepared.running, admission) != admission.selected:
                raise AuditIntegrityError("checkpoint projection changed its pending source while capturing")
            return result

    return await _registered_read(prepared.work, _ProjectionCapture, capture)


async def project_early_checkpoint_result(
    *,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    detached: RestoredContinuationRuntime,
    original: RestoredContinuationRuntime,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    custody: ProviderCallCustody,
    returned: CheckpointReturnedResponseObservation,
    accounting: CheckpointPassAccounting,
) -> ProjectedEarlyCheckpoint | ProjectedEarlyCheckpointOverflow:
    """Measure the original call and exact EARLY consequence on an isolated graph.

    Size errors retain their original encoder classification. They must be
    settled by the paid owner; this function never retries or substitutes a
    model outcome when capture fails.
    """
    _require_detached_source(detached, original)
    if (
        type(returned) is not CheckpointReturnedResponseObservation
        or returned.phase != "early"
        or prepared.prepared_source.phase != "early"
        or type(accounting) is not CheckpointPassAccounting
        or accounting.pass_index != prepared.prepared_source.pass_index
        or accounting.provider_attempts not in (1, 2)
        or accounting.provider_attempts != prepared.retry_ordinal + 1
        or (
            accounting.provider_attempts == 1
            and (
                accounting.first_attempt_schema_valid is not None
                or accounting.first_attempt_accepted is not None
                or accounting.format_reprompt_sent
            )
        )
        or (
            accounting.provider_attempts == 2
            and (
                type(accounting.first_attempt_schema_valid) is not bool
                or accounting.first_attempt_accepted is not False
                or not accounting.format_reprompt_sent
            )
        )
        or returned.attempt is not admission.attempt
        or custody.attempt is not admission.attempt
        or custody.call is not returned.audit_call
        or custody.required_work is not prepared.work
        or returned.audit_call.messages_hash != prepared.request_sha256
        or any(call.call_id == returned.audit_call.call_id for call in detached.live.recorder.llm_calls)
        or type(returned.message.content) is not str
    ):
        raise AuditIntegrityError("EARLY projection changed its original physical attempt or pass")
    completeness = returned.provider_metadata.v4_completeness
    if type(completeness) is not V4ProviderResultCompleteness:
        raise AuditIntegrityError("EARLY projection lacks original V4 raw-result completeness")
    custody.require_v4_result_completeness(completeness)
    completeness.require_result(returned.provider_metadata, content=returned.message.content, tool_calls=())
    verdict = _parse_advisor_checkpoint_guidance(returned.message.content, phase="early")
    first_valid = verdict.response_schema_valid if accounting.provider_attempts == 1 else accounting.first_attempt_schema_valid
    first_accepted = verdict.ok if accounting.provider_attempts == 1 else accounting.first_attempt_accepted
    retry_pending = not verdict.ok and accounting.provider_attempts < 2
    pass_record = (
        None
        if retry_pending
        else checkpoint_pass_record(
            verdict,
            phase="early",
            source="model",
            state=detached.live.state,
            pass_index=accounting.pass_index,
            provider_attempts=accounting.provider_attempts,
            first_attempt_schema_valid=first_valid,
            first_attempt_accepted=first_accepted,
            format_reprompt_sent=accounting.format_reprompt_sent,
        )
    )
    injection = early_checkpoint_injection(verdict)
    detached.live.recorder.record_llm_call(returned.audit_call, provider_custody=custody)
    if injection is not None:
        detached.live.llm_messages.append({"role": "user", "content": injection})
        detached.live.advisor_repair_context_introduced = True
    detached.live.turn_evidence.transcript.observe_messages(detached.live.llm_messages)
    capture = await _capture_dispatched_projection(
        prepared=prepared, admission=admission, detached=detached, selected=selected, inventory=inventory
    )
    if capture.overflow is not None:
        overflow = ProjectedEarlyCheckpointOverflow(capture.overflow)
        _overflows[overflow] = _ProjectionOverflowSource(prepared, admission, returned, capture.overflow)
        return overflow
    captured = capture.captured
    if captured is None:
        raise AuditIntegrityError("checkpoint capture lost its complete DATA without an original size observation")
    observed_accounting = replace(accounting, first_attempt_schema_valid=first_valid, first_attempt_accepted=first_accepted)
    projected = ProjectedEarlyCheckpoint(captured, verdict, pass_record, injection, retry_pending, observed_accounting)
    _issued[projected] = _ProjectionSource(
        prepared, admission, returned, captured, verdict, pass_record, injection, retry_pending, observed_accounting
    )
    return projected


def require_original_early_checkpoint_projection(
    projected: ProjectedEarlyCheckpoint,
    *,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> CapturedContinuationRuntime:
    """Reprove live producer custody before the SQL owner admits any batch."""
    source = _issued.get(projected) if type(projected) is ProjectedEarlyCheckpoint else None
    if (
        source is None
        or source.prepared is not prepared
        or source.admission is not admission
        or source.returned is not returned
        or source.captured is not projected.captured
        or source.verdict is not projected.verdict
        or source.pass_record is not projected.pass_record
        or source.injection != projected.injection
        or source.retry_pending is not projected.retry_pending
        or source.accounting is not projected.accounting
    ):
        raise AuditIntegrityError("EARLY checkpoint completion requires its original detached projection")
    return projected.captured


def require_original_early_checkpoint_overflow(
    projected: ProjectedEarlyCheckpointOverflow,
    *,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> ContinuationCaptureOverflowWitness:
    source = _overflows.get(projected) if type(projected) is ProjectedEarlyCheckpointOverflow else None
    if (
        source is None
        or source.prepared is not prepared
        or source.admission is not admission
        or source.returned is not returned
        or source.witness is not projected.witness
    ):
        raise AuditIntegrityError("EARLY overflow requires its original returned-call capture boundary")
    _require_capture_witness(projected.witness)
    return projected.witness
