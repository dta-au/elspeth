"""Resume one retained first usable END repair H without another SDK call."""

from __future__ import annotations

from functools import partial

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_checkpoint_end_cold_consequence import (
    compute_first_cold_checkpoint_end_gate,
    read_cold_checkpoint_end_consequence_source,
)
from elspeth.web.composer.continuation_checkpoint_end_cold_repair import (
    ColdProjectedEndRepair,
    ColdProjectedEndRepairOverflow,
    project_first_cold_end_repair,
)
from elspeth.web.composer.continuation_checkpoint_end_cold_review import (
    ColdProjectedEndReview,
    ColdProjectedEndReviewOverflow,
    project_first_cold_end_review,
)
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_end_complete_repair import EndCompleteRepairBatch
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_retained_capacity import ContinuationRetainedCapacityExceeded
from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted
from elspeth.web.sessions.service import SessionServiceImpl


async def resume_first_cold_end_repair(*, phases: object, selected: SelectedContinuationState) -> StoredContinuationSnapshot:
    """Consume a fitting first repair; other H dispositions remain closed."""
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    if type(phases) is not ContinuationLivePhases or type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("cold END resume requires its current original phase owner")
    sessions = phases.service._sessions_service
    if type(sessions) is not SessionServiceImpl:
        raise AuditIntegrityError("cold END resume lost its original sessions SQL owner")
    source = await read_cold_checkpoint_end_consequence_source(phases=phases, selected=selected)
    if source.owed.retry_ordinal == 0:
        from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
        from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointPaidOverflowTerminal
        from elspeth.web.composer.continuation_checkpoint_end_format_retry import (
            CheckpointEndFormatRetrySource,
            settle_cold_checkpoint_end_format_retry,
        )
        from elspeth.web.composer.continuation_codec import decode_continuation_response

        content = decode_continuation_response(source.owed.response).message.content
        if type(content) is not str:
            raise AuditIntegrityError("cold END retry lost its complete original returned text")
        if not _parse_advisor_checkpoint_guidance(content, phase="end").ok:
            retry = await settle_cold_checkpoint_end_format_retry(phases=phases, selected=selected)
            if type(retry) is EndCheckpointPaidOverflowTerminal:
                phases.latest_snapshot = retry.verified.terminal
                return retry.verified.terminal
            if type(retry) is not CheckpointEndFormatRetrySource:
                raise AuditIntegrityError("cold END retry lost its exact retained next-action source")
            phases.latest_snapshot = retry.proof.selected
            return retry.proof.selected
    gate = await compute_first_cold_checkpoint_end_gate(source=source)

    async def settle_overflow(
        failure: ContinuationRetainedCapacityExceeded | ColdProjectedEndRepairOverflow | ColdProjectedEndReviewOverflow,
        *,
        capacity_projection: ColdProjectedEndRepair | ColdProjectedEndReview | None = None,
    ) -> StoredContinuationSnapshot:
        settled = await _registered(
            work=phases.work,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL,
            operation=partial(
                sessions.settle_cold_v4_checkpoint_end_owed_overflow_sync,
                cold_source=source,
                failure=failure,
                capacity_projection=capacity_projection,
            ),
            expected=VerifiedCheckpointPaidOverflowTerminal,
        )
        phases.latest_snapshot = settled.terminal
        return settled.terminal

    if gate.computation.action == "repair_continue":
        projected = await project_first_cold_end_repair(gate=gate)
        if type(projected) is ColdProjectedEndRepairOverflow:
            return await settle_overflow(projected)
        if type(projected) is not ColdProjectedEndRepair:
            raise AuditIntegrityError("cold END repair lost its original H projection")
        try:
            batch = await _registered(
                work=phases.work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=partial(sessions.complete_v4_checkpoint_end_cold_repair_sync, projected=projected),
                expected=EndCompleteRepairBatch,
            )
        except ContinuationRetainedCapacityExceeded as excess:
            return await settle_overflow(excess, capacity_projection=projected)
        successor = StoredContinuationSnapshot(
            batch.append.snapshot_json, batch.owed.selected.cursor + len(batch.append.events), batch.append.checkpoint_id
        )
    else:
        projected_review = await project_first_cold_end_review(gate=gate)
        if type(projected_review) is ColdProjectedEndReviewOverflow:
            return await settle_overflow(projected_review)
        if type(projected_review) is not ColdProjectedEndReview:
            raise AuditIntegrityError("cold END Review lost its original H projection")
        try:
            reviewed = await _registered(
                work=phases.work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=partial(sessions.complete_v4_checkpoint_end_cold_review_sync, projected=projected_review),
                expected=SemanticReviewCommitted,
            )
        except ContinuationRetainedCapacityExceeded as excess:
            return await settle_overflow(excess, capacity_projection=projected_review)
        successor = reviewed.completed
    phases.latest_snapshot = successor
    return successor
