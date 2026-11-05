"""Retain one original paid V4 END Review for the later semantic Stop owner.

The shared END gate returns a result here.  It does not publish an assistant or
complete the operation: a separately paid whole-request Stop owns that step.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from weakref import ReferenceType, WeakKeyDictionary, ref

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _EndCompletionDisposition
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness
from elspeth.web.composer.continuation_checkpoint_end_complete_clean import (
    ProjectedEndCompleteClean,
    project_end_complete_clean,
    require_original_end_complete_clean,
)
from elspeth.web.composer.continuation_checkpoint_end_complete_terminal import (
    ProjectedEndCompleteTerminal,
    project_end_complete_terminal,
    require_original_end_complete_terminal,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import EndGateComputation
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    _capture_owners,
    _registered_read,
    _registered_runtime_capture,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.continuation_checkpoint_end_owed import (
    CheckpointEndOwedAdmission,
    VerifiedCheckpointEndOwed,
    read_v4_checkpoint_end_owed_on_connection,
)
from elspeth.web.sessions.continuation_end import EndReviewExecution
from elspeth.web.sessions.locking import locked_session_transaction


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEndCompleteReview:
    """Detached original result and captured owed-head mutable owners."""

    terminal: ProjectedEndCompleteTerminal | ProjectedEndCompleteClean
    execution: EndReviewExecution
    captured: CapturedContinuationRuntime
    _issued_record: _Source | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True, eq=False)
class ProjectedEndCompleteReviewOverflow:
    witness: ContinuationCaptureOverflowWitness
    _issued_record: _Source | None = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _Source:
    physical: CheckpointEndPhysicalSource
    owed: CheckpointEndOwedAdmission
    selected: SelectedContinuationState
    returned: CheckpointReturnedResponseObservation
    computation: EndGateComputation
    terminal: ProjectedEndCompleteTerminal | ProjectedEndCompleteClean
    execution: EndReviewExecution | None
    captured: CapturedContinuationRuntime | None
    overflow: ContinuationCaptureOverflowWitness | None


_issued: WeakKeyDictionary[ProjectedEndCompleteReview, ReferenceType[_Source]] = WeakKeyDictionary()
_overflows: WeakKeyDictionary[ProjectedEndCompleteReviewOverflow, ReferenceType[_Source]] = WeakKeyDictionary()


async def project_end_complete_review(
    *,
    physical: CheckpointEndPhysicalSource,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
    computation: EndGateComputation,
) -> ProjectedEndCompleteReview | ProjectedEndCompleteReviewOverflow:
    """Capture the true prescribed Review result without finalizing it."""
    prepared = physical.prepared
    if (
        type(owed) is not CheckpointEndOwedAdmission
        or type(selected) is not SelectedContinuationState
        or owed.original.original_source != prepared
        or owed.owed_selected.checkpoint_id != selected.checkpoint_id
        or decode_continuation_snapshot(owed.owed_selected.payload_json) != selected.stack
        or selected.stack.active_action_json is None
        or selected.stack.active_action_source is None
    ):
        raise AuditIntegrityError("END complete Review lost its original paid action and owed head")
    terminal: ProjectedEndCompleteTerminal | ProjectedEndCompleteClean
    if computation.action == "fallthrough":
        terminal = await project_end_complete_clean(physical=physical, returned=returned, computation=computation)
    else:
        terminal = await _registered_read(
            physical.phase_owner.phases.work,
            ProjectedEndCompleteTerminal,
            lambda: project_end_complete_terminal(physical=physical, returned=returned, computation=computation),
        )
    owner = physical.phase_owner
    detached = physical.detached
    inventory = await materialize_continuation_env_inventory(
        advisor=owner.phases.service._advisor_checkpoint,
        authority=owner.phases.authority,
        selected=selected,
        required_work=owner.phases.work,
    )
    if inventory.names != detached.env_ref_names:
        raise AuditIntegrityError("END complete Review changed its owed credential inventory")
    running = owner.phases.work.running
    if running is None:
        raise AuditIntegrityError("END complete Review lost its original running owner")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    attempt_id = owed.original.attempt.attempt_id

    def read_owed() -> VerifiedCheckpointEndOwed:
        with locked_session_transaction(owner.phases.authority._engine, sid) as conn:
            current = read_v4_checkpoint_end_owed_on_connection(
                conn, session_id=sid, operation_id=operation, attempt_id=attempt_id, names=inventory.names
            )
            if current is None or current.selected != owed.owed_selected or current.response != returned.response:
                raise AuditIntegrityError("END complete Review changed its rebound paid H")
            return current

    source = replace(detached.source, selected=selected)
    runtime = replace(detached, source=source)
    capture = await _registered_runtime_capture(
        owner.phases.work,
        read_source=read_owed,
        capture=lambda: _capture_owners(runtime, source=source, inventory=inventory),
        changed_source_message="END complete Review changed its owed head during capture",
    )
    if capture.overflow is not None:
        original = _Source(physical, owed, selected, returned, computation, terminal, None, None, capture.overflow)
        overflow = ProjectedEndCompleteReviewOverflow(capture.overflow)
        object.__setattr__(overflow, "_issued_record", original)
        _overflows[overflow] = ref(original)
        return overflow
    captured = capture.captured
    if captured is None:
        raise AuditIntegrityError("END complete Review lost captured DATA without a size witness")
    execution = EndReviewExecution(
        current=selected,
        runtime=runtime,
        public_result=terminal.result,
        publication_binding=computation.publication_review.binding,
        disposition=terminal.disposition if type(terminal) is ProjectedEndCompleteClean else _EndCompletionDisposition.BLOCKED,
    )
    projected = ProjectedEndCompleteReview(terminal, execution, captured)
    original = _Source(physical, owed, selected, returned, computation, terminal, execution, captured, None)
    object.__setattr__(projected, "_issued_record", original)
    _issued[projected] = ref(original)
    return projected


def require_original_end_complete_review(
    projected: ProjectedEndCompleteReview,
    *,
    physical: CheckpointEndPhysicalSource,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
) -> None:
    registered = _issued.get(projected) if type(projected) is ProjectedEndCompleteReview else None
    original = None if registered is None else registered()
    if (
        original is None
        or original is not projected._issued_record
        or original.physical is not physical
        or original.owed is not owed
        or original.selected is not selected
        or original.returned is not returned
        or original.terminal is not projected.terminal
        or original.execution is not projected.execution
        or original.captured is not projected.captured
        or original.overflow is not None
    ):
        raise AuditIntegrityError("END complete Review lost its original captured source")
    if isinstance(projected.terminal, ProjectedEndCompleteClean):
        require_original_end_complete_clean(projected.terminal, physical=physical, returned=returned)
    else:
        require_original_end_complete_terminal(projected.terminal, physical=physical, returned=returned)


def require_original_end_complete_review_overflow(
    overflow: ProjectedEndCompleteReviewOverflow,
    *,
    physical: CheckpointEndPhysicalSource,
    owed: CheckpointEndOwedAdmission,
    selected: SelectedContinuationState,
    returned: CheckpointReturnedResponseObservation,
) -> ContinuationCaptureOverflowWitness:
    registered = _overflows.get(overflow) if type(overflow) is ProjectedEndCompleteReviewOverflow else None
    original = None if registered is None else registered()
    if (
        original is None
        or original is not overflow._issued_record
        or original.physical is not physical
        or original.owed is not owed
        or original.selected is not selected
        or original.returned is not returned
        or original.overflow is not overflow.witness
        or original.execution is not None
        or original.captured is not None
    ):
        raise AuditIntegrityError("END Review overflow lost its original detached size witness")
    if isinstance(original.terminal, ProjectedEndCompleteClean):
        require_original_end_complete_clean(original.terminal, physical=physical, returned=returned)
    else:
        require_original_end_complete_terminal(original.terminal, physical=physical, returned=returned)
    return overflow.witness
