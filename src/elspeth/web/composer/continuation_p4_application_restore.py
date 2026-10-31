"""Finish one atomic application before projecting its cold evidence owners.

This entry reconstructs cold owners. Warm evidence retains its original live
application/failure owners through the separate historical application path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import require_admitted_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence, _restore_current_evidence
from elspeth.web.composer.continuation_evidence_restore import _require_current_reader_service
from elspeth.web.composer.continuation_owed_p4 import _require_current_read_owners
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.continuation_p4_application_store import StoredP4Application

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority


@dataclass(frozen=True, slots=True)
class RestoredP4Application:
    stored: StoredP4Application
    restored: RestoredTurnEvidence


async def apply_selected_p4_to_cold_evidence(
    *,
    authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    required_work: RequiredWorkBinding,
    service: ComposerServiceImpl | None = None,
) -> RestoredP4Application:
    running = _require_current_read_owners(authority, selected, required_work)
    if service is not None:
        _require_current_reader_service(service, authority)
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    batch = selected.stack.frames[-1]
    if len(compose) != 1 or type(batch) is not ToolBatchFrame:
        raise AuditIntegrityError("cold application lost its selected Compose/batch owners")
    evidence_chunks = tuple(chunk for chunk in selected.chunks if chunk.reference == compose[0].evidence)
    pending_chunks = tuple(chunk for chunk in selected.chunks if chunk.reference == batch.accumulator)
    if len(evidence_chunks) != 1 or len(pending_chunks) != 1:
        raise AuditIntegrityError("cold application lost its complete selected evidence/batch inputs")
    sql, projection = required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
    )
    try:
        outcome = await run_required_sql_finish_once(
            sql,
            authority.apply_continuation_p4_application,
            running,
            selected=selected,
            inventory=inventory,
            expected_evidence_chunk=evidence_chunks[0],
            expected_pending_batch_chunk=pending_chunks[0],
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: RestoredP4Application | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            if type(outcome.value) is not StoredP4Application:
                raise AuditIntegrityError("cold application replaced its actual committed SQL result")
            stored = outcome.value
            admitted = require_admitted_turn_evidence(stored.bundle.evidence_after, env_ref_names=inventory.names)
            reader = None
            if admitted.snapshot.requires_blob_reader:
                if service is None:
                    raise AuditIntegrityError("cold application requires its actual current reader service")
                _require_current_reader_service(service, authority)
                reader = CurrentTranscriptBlobReader.from_service(
                    service, session_id=str(running.claim.session_id), context=running.session_operation_context
                )
            restored = _restore_current_evidence(
                admitted,
                context=running.session_operation_context,
                reader=reader,
                env_ref_names=inventory.names,
            )
            result = RestoredP4Application(stored, restored)
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        errors.append(AuditIntegrityError("cold application returned an unknown SQL outcome"))
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Cold P4 application outcomes", errors)
    if result is None:
        raise AuditIntegrityError("cold application lost its actual projected evidence owners")
    return result
