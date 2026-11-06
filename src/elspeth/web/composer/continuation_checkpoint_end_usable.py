"""Finish one original retained usable END Review on its actual action owner.

The physical call and full response are already admitted and retained at H.
This module only projects the original shared gate's consequence and submits
one exact SQL result. It never publishes a Stop or creates a retry permit.
"""

from __future__ import annotations

from functools import partial

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_capture_overflow import ContinuationCaptureOverflowWitness
from elspeth.web.composer.continuation_checkpoint_end_complete_repair import (
    ProjectedEndCompleteRepair,
    ProjectedEndCompleteRepairOverflow,
    project_end_complete_repair,
    require_original_end_complete_repair_overflow,
)
from elspeth.web.composer.continuation_checkpoint_end_complete_review import (
    ProjectedEndCompleteReview,
    ProjectedEndCompleteReviewOverflow,
    project_end_complete_review,
    require_original_end_complete_review_overflow,
)
from elspeth.web.composer.continuation_checkpoint_end_computation import compute_checkpoint_end_gate
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview, EndCheckpointRepairContinued
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import EndCompleteRepairBatch
from elspeth.web.sessions.continuation_checkpoint_end_owed import CheckpointEndOwedAdmission
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted
from elspeth.web.sessions.service import SessionServiceImpl


async def complete_v4_checkpoint_end_usable_return(
    *,
    physical: CheckpointEndPhysicalSource,
    prepared: PreparedCheckpointProvider,
    owed: CheckpointEndOwedAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> EndCheckpointCompleteReview | EndCheckpointRepairContinued | EndCheckpointPaidOverflowTerminal:
    """Retain the original Review result or its exact incomplete consequence."""
    owner = physical.phase_owner
    phases = owner.phases
    running = phases.work.running
    sessions = phases.service._sessions_service
    if (
        type(sessions) is not SessionServiceImpl
        or running is None
        or prepared.running is not running
        or owed.original.original_source != physical.prepared
        or owed.original.attempt is not returned.attempt
    ):
        raise AuditIntegrityError("END usable result lost its original paid action owner")
    selected = await load_selected_continuation(
        authority=phases.authority, running=running, selected=owed.owed_selected, required_work=phases.work
    )
    computation = compute_checkpoint_end_gate(source=physical, returned=returned)

    def project_settled_cost() -> None:
        """Project the exact committed paid call before another warm action."""
        budget = phases.provider_owner.cost_budget
        call = returned.audit_call
        accountable = call.cost_response_accountable
        if (
            budget is None
            or type(accountable) is not bool
            or owner.runtime.bindings.provider_owner is not phases.provider_owner
            or call.call_id != owed.original.attempt.attempt_id
            or budget.snapshot().physical_calls != len(budget.snapshot().charges) + 1
            or any(charge.call_id == call.call_id for charge in budget.snapshot().charges)
        ):
            raise AuditIntegrityError("END completion lost its original unsettled cost projection")
        budget.consume_settled(call, response_accountable=accountable)

    async def settle_overflow(
        failure: ContinuationCaptureOverflowWitness | ContinuationRetainedCapacityExceeded,
    ) -> EndCheckpointPaidOverflowTerminal:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                sessions.settle_v4_checkpoint_end_owed_overflow_sync,
                prepared=prepared,
                owed=owed,
                returned=returned,
                failure=failure,
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        return EndCheckpointPaidOverflowTerminal(settled)

    if computation.action == "repair_continue":
        projected_repair = await project_end_complete_repair(
            physical=physical,
            prepared=prepared,
            owed=owed,
            selected=selected,
            returned=returned,
            computation=computation,
        )
        if type(projected_repair) is ProjectedEndCompleteRepairOverflow:
            witness = require_original_end_complete_repair_overflow(
                projected_repair,
                physical=physical,
                prepared=prepared,
                owed=owed,
                selected=selected,
                returned=returned,
            )
            return await settle_overflow(witness)
        if type(projected_repair) is not ProjectedEndCompleteRepair:
            raise AuditIntegrityError("END repair projection returned an unfamiliar consequence")
        try:
            completed = await _registered(
                work=phases.work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=partial(
                    sessions.complete_v4_checkpoint_end_repair_sync,
                    physical=physical,
                    prepared=prepared,
                    owed=owed,
                    selected=selected,
                    returned=returned,
                    projected=projected_repair,
                ),
                expected=EndCompleteRepairBatch,
            )
        except ContinuationRetainedCapacityExceeded as excess:
            return await settle_overflow(excess)
        successor = StoredContinuationSnapshot(
            completed.append.snapshot_json, owed.owed_selected.cursor + len(completed.append.events), completed.append.checkpoint_id
        )
        project_settled_cost()
        return EndCheckpointRepairContinued(successor)

    projected_review = await project_end_complete_review(
        physical=physical,
        owed=owed,
        selected=selected,
        returned=returned,
        computation=computation,
    )
    if type(projected_review) is ProjectedEndCompleteReviewOverflow:
        witness = require_original_end_complete_review_overflow(
            projected_review, physical=physical, owed=owed, selected=selected, returned=returned
        )
        return await settle_overflow(witness)
    if type(projected_review) is not ProjectedEndCompleteReview:
        raise AuditIntegrityError("END Review projection returned an unfamiliar consequence")
    try:
        reviewed = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                sessions.complete_v4_checkpoint_end_review_sync,
                physical=physical,
                prepared=prepared,
                owed=owed,
                selected=selected,
                returned=returned,
                projected=projected_review,
            ),
            expected=SemanticReviewCommitted,
        )
    except ContinuationRetainedCapacityExceeded as excess:
        return await settle_overflow(excess)
    project_settled_cost()
    return EndCheckpointCompleteReview(reviewed)
