"""Detached END repair consequence from one original retained paid return.

This projection mutates only the independently restored pre-call graph. It
neither settles the paid attempt nor changes live Composer state or SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness
from elspeth.web.composer.continuation_checkpoint_end_computation import (
    EndGateComputation,
    require_original_checkpoint_end_computation,
)
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    CheckpointEndPhysicalSource,
    checkpoint_end_physical_custody,
    require_checkpoint_end_detached_source,
    require_original_checkpoint_end_physical_source,
)
from elspeth.web.composer.continuation_checkpoint_observation import (
    CheckpointReturnedResponseObservation,
    original_v4_checkpoint_returned_audit_call,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    _capture_owners,
    _registered_read,
    _registered_runtime_capture,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.continuation_checkpoint_end_complete_withheld import (
    PreparedEndRepairWithheldReply,
    prepare_end_repair_withheld_reply,
)
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    VerifiedCheckpointEndOwed,
    read_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEndCompleteRepair:
    """Measured repair continuation, without SQL or provider authority."""

    captured: CapturedContinuationRuntime
    computation: EndGateComputation
    withheld: PreparedEndRepairWithheldReply | None
    _issued_record: _RepairProjectionSource | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEndCompleteRepairOverflow:
    witness: ContinuationCaptureOverflowWitness
    _issued_record: _RepairProjectionSource | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _RepairProjectionSource:
    physical: CheckpointEndPhysicalSource
    prepared: PreparedCheckpointProvider
    owed: CheckpointEndOwedAdmission
    selected: SelectedContinuationState
    returned: CheckpointReturnedResponseObservation
    computation: EndGateComputation
    captured: CapturedContinuationRuntime | None
    withheld: PreparedEndRepairWithheldReply | None
    overflow: ContinuationCaptureOverflowWitness | None


_issued: WeakKeyDictionary[ProjectedEndCompleteRepair, ReferenceType[_RepairProjectionSource]] = WeakKeyDictionary()
_overflows: WeakKeyDictionary[ProjectedEndCompleteRepairOverflow, ReferenceType[_RepairProjectionSource]] = WeakKeyDictionary()


def _require_original_sources(
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
    computation: EndGateComputation,
) -> CheckpointEndPhysicalSource:
    require_checkpoint_end_detached_source(physical)
    require_original_checkpoint_end_computation(computation, source=physical, returned=returned)
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(owed) is not CheckpointEndOwedAdmission
        or type(selected) is not SelectedContinuationState
        or prepared.prepared_source != physical.prepared
        or owed.original.original_source != physical.prepared
        or owed.original.attempt is not returned.attempt
        or owed.owed_selected.checkpoint_id != selected.checkpoint_id
        or decode_continuation_snapshot(owed.owed_selected.payload_json) != selected.stack
        or owed.response != returned.response
        or computation.action != "repair_continue"
        or computation.repair_dialogue_content is None
        or computation.advisor_injection_index is None
        or computation.advisor_injection_index != len(physical.detached.live.llm_messages)
    ):
        raise AuditIntegrityError("END repair projection changed its original paid and prepared source")
    original_v4_checkpoint_returned_audit_call(
        returned, attempt_id=owed.original.attempt.attempt_id, request_sha256=prepared.request_sha256
    )
    return physical


async def project_end_complete_repair(
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
    computation: EndGateComputation,
) -> ProjectedEndCompleteRepair | ProjectedEndCompleteRepairOverflow:
    """Capture the exact repair successor against the rebound owed head."""

    def validate_sources() -> CheckpointEndPhysicalSource:
        return _require_original_sources(
            physical=physical, prepared=prepared, owed=owed, selected=selected, returned=returned, computation=computation
        )

    await _registered_read(prepared.work, CheckpointEndPhysicalSource, validate_sources)
    detached = physical.detached
    live = detached.live
    owner = physical.phase_owner
    inventory = await materialize_continuation_env_inventory(
        advisor=owner.phases.service._advisor_checkpoint,
        authority=owner.phases.authority,
        selected=selected,
        required_work=owner.phases.work,
    )
    if inventory.names != detached.env_ref_names:
        raise AuditIntegrityError("END repair projection changed the owed-head credential inventory")
    injection_index = computation.advisor_injection_index
    repair_content = computation.repair_dialogue_content
    if injection_index is None or repair_content is None:
        raise AuditIntegrityError("END repair projection lost its computed dialogue append")
    superseded_index = owner.superseded_reply_index
    if superseded_index is not None and (
        not 0 <= superseded_index < len(live.llm_messages)
        or live.llm_messages[superseded_index] != {"role": "assistant", "content": computation.candidate}
    ):
        raise AuditIntegrityError("END repair projection changed its original superseded PRIMARY reply")
    withheld = prepare_end_repair_withheld_reply(computation.candidate) if superseded_index is not None else None
    custody = checkpoint_end_physical_custody(physical)
    if any(call.call_id == returned.audit_call.call_id for call in live.recorder.llm_calls):
        raise AuditIntegrityError("END repair projection duplicated its original paid audit call")
    live.recorder.record_llm_call(returned.audit_call, provider_custody=custody)
    live.advisor_review_state = computation.review_state
    live.advisor_checkpoint_passes_used += computation.passes_delta
    live.llm_messages.append({"role": "user", "content": repair_content})
    live.advisor_repair_context_introduced = True
    if owner.elide_advisor_exchange:
        live.pending_advisor_elision_indices.append(injection_index)
        if superseded_index is not None:
            live.pending_advisor_elision_indices.append(superseded_index)
    live.turn_evidence.transcript.observe_messages(live.llm_messages)
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    attempt_id = owed.original.attempt.attempt_id

    def read_cold_owed() -> VerifiedCheckpointEndOwed:
        with locked_session_transaction(prepared.authority._engine, sid) as conn:
            cold = read_v4_checkpoint_end_owed_on_connection(
                conn, session_id=sid, operation_id=operation, attempt_id=attempt_id, names=prepared.env_ref_names
            )
            if cold is None or cold.selected != owed.owed_selected or cold.response != returned.response or cold.new_hold != owed.owed_hold:
                raise AuditIntegrityError("END repair projection lost its rebound owed head")
            return cold

    source = replace(detached.source, selected=selected)
    result = await _registered_runtime_capture(
        prepared.work,
        read_source=read_cold_owed,
        capture=lambda: _capture_owners(detached, source=source, inventory=inventory),
        changed_source_message="END repair projection changed its owed head during capture",
    )
    original = _RepairProjectionSource(
        physical, prepared, owed, selected, returned, computation, result.captured, withheld, result.overflow
    )
    if result.overflow is not None:
        overflow = ProjectedEndCompleteRepairOverflow(result.overflow)
        object.__setattr__(overflow, "_issued_record", original)
        _overflows[overflow] = ref(original)
        return overflow
    if result.captured is None:
        raise AuditIntegrityError("END repair projection lost complete DATA without an original size observation")
    projected = ProjectedEndCompleteRepair(result.captured, computation, withheld)
    object.__setattr__(projected, "_issued_record", original)
    _issued[projected] = ref(original)
    return projected


def require_original_end_complete_repair(
    projected: ProjectedEndCompleteRepair,
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
) -> None:
    registered = _issued.get(projected) if type(projected) is ProjectedEndCompleteRepair else None
    source = None if registered is None else registered()
    if (
        source is None
        or source is not projected._issued_record
        or source.physical is not physical
        or source.prepared is not prepared
        or source.owed is not owed
        or source.selected is not selected
        or source.returned is not returned
        or source.captured is not projected.captured
        or source.computation is not projected.computation
        or source.withheld is not projected.withheld
        or source.overflow is not None
    ):
        raise AuditIntegrityError("END repair complete lost its original detached projection")
    # The issuer checked the pre-call detached graph before applying the
    # repair. Rechecking that pre-call capture now would reject the intended
    # detached post-call delta; source identity and original computation remain
    # independently checkable without borrowing live mutable owners.
    require_original_checkpoint_end_physical_source(physical)
    require_original_checkpoint_end_computation(projected.computation, source=physical, returned=returned)


def require_original_end_complete_repair_overflow(
    overflow: ProjectedEndCompleteRepairOverflow,
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
) -> ContinuationCaptureOverflowWitness:
    registered = _overflows.get(overflow) if type(overflow) is ProjectedEndCompleteRepairOverflow else None
    source = None if registered is None else registered()
    if (
        source is None
        or source is not overflow._issued_record
        or source.physical is not physical
        or source.prepared is not prepared
        or source.owed is not owed
        or source.selected is not selected
        or source.returned is not returned
        or source.captured is not None
        or source.overflow is not overflow.witness
    ):
        raise AuditIntegrityError("END repair overflow lost its original detached size witness")
    require_original_checkpoint_end_physical_source(physical)
    require_original_checkpoint_end_computation(source.computation, source=physical, returned=returned)
    return overflow.witness
