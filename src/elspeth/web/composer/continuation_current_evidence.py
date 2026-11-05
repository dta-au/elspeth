"""Join a current message producer to the complete current evidence graph.

The original graph restore remains available for untrusted plain-wire shelves.
This owner performs every current producer read inside registered required work
and reconstructs the transcript's shared objects once with that producer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, _begin_current_evidence, _finish_current_evidence
from elspeth.web.composer.continuation_evidence_restore import _require_current_reader_service
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.continuation_retention_memo import borrow_v4_preparation_memo_scope
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRunning

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


def _restore_current_message_evidence(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    materialization: CurrentContinuationMessageProducers,
) -> RestoredTurnEvidence:
    if (
        type(selected) is not SelectedContinuationState
        or type(inventory) is not CurrentContinuationEnvInventory
        or type(materialization) is not CurrentContinuationMessageProducers
        or materialization.service is not service
        or materialization.authority is not authority
        or materialization.selected is not selected
        or materialization.inventory is not inventory
    ):
        raise AuditIntegrityError("current evidence crossed its actual selected message producers")
    _require_current_reader_service(service, authority)
    selected.__post_init__()
    if materialization.finish_read is not None:
        materialization.finish_read.require_selection(selected, inventory)
    running = inventory.running
    materialization.require_context(
        session_id=selected.stack.logical.session_id,
        context=running.session_operation_context,
        actor_user_id=materialization.inputs.bootstrap.actor_user_id,
    )
    # Use this actual just-validated complete read, not the owner's older input
    # DATA. Reconstruction remains bracketed by an independent fresh final read.
    with borrow_v4_preparation_memo_scope():
        inputs = materialization._require_current()
        admitted = inputs.admitted_evidence
        snapshot = admitted.snapshot
        reader = None
        if snapshot.requires_blob_reader:
            from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader

            reader = CurrentTranscriptBlobReader.from_service(
                service, session_id=str(running.claim.session_id), context=running.session_operation_context
            )
        # The outer current reads bracket this whole reconstruction, including
        # policy callbacks. Reuse their actual inputs for assembly instead of
        # nesting restore_shelf's second pair of complete current-history reads.
        owner, initial, latest = _begin_current_evidence(snapshot, context=running.session_operation_context)
        history = AdvisorTranscriptOwner._continuation_history(
            snapshot.transcript,
            session_id=snapshot.session_id,
            actor_user_id=snapshot.actor_user_id,
            request_id=snapshot.request_id,
            request=snapshot.transcript.request,
        )
        shelf = materialization._rebuild_shelf(snapshot.transcript, inputs=inputs)
        owner.transcript, messages = AdvisorTranscriptOwner._from_continuation_shelf(
            snapshot.transcript, request=snapshot.transcript.request, history=history, shelf=shelf
        )
        restored = _finish_current_evidence(
            admitted,
            context=running.session_operation_context,
            owner=owner,
            initial=initial,
            latest=latest,
            messages=messages,
            reader=reader,
            env_ref_names=inventory.names,
        )
        materialization._require_current()
        return restored


async def restore_current_turn_evidence(
    *,
    service: ComposerServiceImpl,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    materialization: CurrentContinuationMessageProducers,
    required_work: RequiredWorkBinding,
) -> RestoredTurnEvidence:
    """Restore the actual shared shelf; this grants no phase or provider action."""
    if type(required_work) is not RequiredWorkBinding or type(inventory) is not CurrentContinuationEnvInventory:
        raise AuditIntegrityError("current evidence requires its actual required-work inventory")
    running = required_work.running
    if type(running) is not ComposerOperationRunning or inventory.running is not running:
        raise AuditIntegrityError("current evidence changed its actual running owner")
    required_work.validate_context(running.session_operation_context)
    if materialization.finish_read is not None:
        materialization.finish_read.require_selection(selected, inventory)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("current evidence changed its required-work claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            _restore_current_message_evidence,
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            materialization=materialization,
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    return _finish_evidence_projection(required_work, sql, projection, outcome, expected=RestoredTurnEvidence)


def _finish_evidence_projection[T](
    required_work: RequiredWorkBinding,
    sql: RequiredWorkTicket,
    projection: RequiredWorkTicket,
    outcome: RequiredSQLFinishOnce[T],
    *,
    expected: type[T],
) -> T:
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: T | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not expected:
                raise AuditIntegrityError("current evidence lost its actual complete graph")
            result = outcome.value
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("current evidence returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Current evidence original outcomes", errors)
    if result is None:
        raise AuditIntegrityError("current evidence lost its projected graph")
    return result
