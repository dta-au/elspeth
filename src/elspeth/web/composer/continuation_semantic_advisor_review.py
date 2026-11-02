"""Execute one paid-admitted semantic Review through the shared END gates.

This consumer has no provider or publication shortcut. The END owner issues
the actual review effect; the result owner retains its full gate output before
another paid decision can authorize whole-request publication.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import ReviewContinuationAction
from elspeth.web.composer.continuation import EndFrame
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointNoSdkTerminal,
    EndCheckpointPaidOverflowTerminal,
    EndCheckpointUnboundRefusalTerminal,
)
from elspeth.web.composer.continuation_checkpoint_end_terminal import (
    EndCheckpointCompleteReview,
    EndCheckpointFormatRetryReady,
    EndCheckpointPaidIncompleteTerminal,
    EndCheckpointRepairContinued,
)
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_end_effects import semantic_end_effect_key
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_runtime import capture_continuation_runtime_owners
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_end import EndReviewContinued, EndReviewExecution, execute_continuation_end_review
from elspeth.web.sessions.continuation_semantic_actions import SemanticActionAdmission, require_consumed_semantic_review_on_connection
from elspeth.web.sessions.continuation_semantic_review_result import (
    SemanticReviewCommitted,
    read_original_semantic_review_result_on_connection,
    retain_semantic_review_result_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.composer_app_services import ComposerAppServices


def _read_review_admission(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    semantic_effect_key: str,
) -> SemanticActionAdmission:
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(authority._engine, sid) as conn:
        stored = load_snapshot(conn, session_id=sid, operation_id=operation)
        if stored.checkpoint_id != selected.checkpoint_id or stored.payload_json != encode_continuation_snapshot(selected.stack):
            raise AuditIntegrityError("prescribed Review changed its selected current END cut")
        admitted = require_consumed_semantic_review_on_connection(conn, running, selected=stored, effect_key=semantic_effect_key)
        if type(admitted.action) is not ReviewContinuationAction or admitted.review_candidate is None:
            raise AuditIntegrityError("prescribed Review lost its original paid candidate/action")
        return admitted


def _retain_review_result(
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    semantic_effect_key: str,
    owner_effect_key: str,
    execution: EndReviewExecution,
    captured: CapturedContinuationRuntime,
) -> SemanticReviewCommitted:
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(authority._engine, sid) as conn:
        dispatched = load_snapshot(conn, session_id=sid, operation_id=operation)
        retained = retain_semantic_review_result_on_connection(
            conn,
            running,
            selected=dispatched,
            semantic_effect_key=semantic_effect_key,
            execution=execution,
            captured=captured,
        )
        cold = read_original_semantic_review_result_on_connection(
            conn,
            session_id=running.claim.session_id,
            operation_id=operation,
            review_effect_key=owner_effect_key,
        )
        if (
            cold.completed != retained
            or cold.result != execution.public_result
            or cold.outcome != execution.disposition.value
            or cold.publication_binding != (execution.publication_binding if cold.outcome == "clean" else None)
        ):
            raise AuditIntegrityError("prescribed Review lost its complete actual returned gate output")
        return cold


async def review_prescribed_continuation_end(
    phases: ContinuationLivePhases, services: ComposerAppServices, selected: SelectedContinuationState
) -> (
    EndCheckpointPaidIncompleteTerminal
    | EndCheckpointPaidOverflowTerminal
    | EndCheckpointNoSdkTerminal
    | EndCheckpointUnboundRefusalTerminal
    | None
):
    """Retain a real END Review result without claiming final publication."""
    running = phases.work.running
    top = selected.stack.frames[-1]
    source = selected.stack.active_action_source
    if (
        type(running) is not ComposerOperationRunning
        or running.session_operation_context is not phases.lease.context
        or type(top) is not EndFrame
        or top.phase != "review_needed"
        or source is None
        or selected.stack.active_action_json is None
    ):
        raise AuditIntegrityError("prescribed Review requires its actual claimed END action cut")
    admitted = await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_READ_SQL,
        operation=partial(_read_review_admission, phases.authority, running, selected, source.effect_key),
        expected=SemanticActionAdmission,
    )
    candidate = admitted.review_candidate
    if candidate is None or admitted.action_key is None:
        raise AuditIntegrityError("prescribed Review lost its original paid END candidate")
    execution = await execute_continuation_end_review(phases, selected)
    if type(execution) is EndCheckpointFormatRetryReady:
        if execution.completed.cursor <= selected.stack.cursor:
            raise AuditIntegrityError("prescribed Review format retry lost its committed successor")
        phases.latest_snapshot = execution.completed
        return None
    if type(execution) is EndCheckpointCompleteReview:
        if execution.review.original_source != source or execution.review.completed.cursor <= selected.stack.cursor:
            raise AuditIntegrityError("prescribed Review lost its exact retained paid END action")
        phases.latest_snapshot = execution.review.completed
        return None
    if type(execution) is EndCheckpointRepairContinued:
        if execution.completed.cursor <= selected.stack.cursor:
            raise AuditIntegrityError("prescribed Review repair lost its committed successor")
        phases.latest_snapshot = execution.completed
        return None
    if isinstance(
        execution,
        (
            EndCheckpointPaidIncompleteTerminal,
            EndCheckpointPaidOverflowTerminal,
            EndCheckpointNoSdkTerminal,
            EndCheckpointUnboundRefusalTerminal,
        ),
    ):
        return execution
    if type(execution) is EndReviewContinued:
        return None
    if type(execution) is not EndReviewExecution:
        raise AuditIntegrityError("prescribed Review returned an unfamiliar shared END result")
    captured = await capture_continuation_runtime_owners(
        execution.runtime,
        selected=execution.current,
        inventory=execution.runtime.materialization.inventory,
    )
    await _registered(
        work=phases.work,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL,
        operation=partial(
            _retain_review_result,
            phases.authority,
            running,
            source.effect_key,
            semantic_end_effect_key(candidate.response, admitted.action_key),
            execution,
            captured,
        ),
        expected=SemanticReviewCommitted,
    )
    return None
