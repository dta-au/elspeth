"""Complete selected DATA readback; no restored authorization or committed witness."""

from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import ContinuationFrameStack
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_selection import require_private_chunk_closure


@dataclass(frozen=True, slots=True)
class SelectedContinuationState:
    checkpoint_id: str
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]

    def __post_init__(self) -> None:
        if type(self.checkpoint_id) is not str or not self.checkpoint_id or type(self.chunks) is not tuple:
            raise AuditIntegrityError("selected DATA lost its checkpoint or exact chunk tuple")
        require_private_chunk_closure(self.stack, self.chunks)


async def load_selected_continuation(
    *,
    authority: ComposerAsyncOperationAuthority,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    required_work: RequiredWorkBinding,
) -> SelectedContinuationState:
    if (
        type(authority) is not ComposerAsyncOperationAuthority
        or type(running) is not ComposerOperationRunning
        or type(selected) is not StoredContinuationSnapshot
        or type(required_work) is not RequiredWorkBinding
        or required_work.running is not running
    ):
        raise AuditIntegrityError("continuation read lost its actual current SQL and required-work owners")
    required_work.validate_context(running.session_operation_context)
    custody = required_work.coordinator.authority
    if (
        custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or custody.durable_operation_id != running.claim.operation_id
        or custody.claim_attempt != running.claim.attempt
    ):
        raise AuditIntegrityError("continuation read changed its required-work logical claim")
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, authority.load_continuation_chunks, running, selected)
    except BaseException:
        projection.complete_without_submission()
        raise
    try:
        required_work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    errors: list[BaseException] = []
    result: SelectedContinuationState | None = None
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        errors.append(outcome.error)
    elif type(outcome) is RequiredSQLReturned:
        projection.begin_projection()
        try:
            result = SelectedContinuationState(selected.checkpoint_id, decode_continuation_snapshot(selected.payload_json), outcome.value)
        except BaseException as error:
            projection.complete_owned(error)
            errors.append(error)
        else:
            projection.complete_owned()
    else:
        projection.complete_without_submission()
        raise AuditIntegrityError("continuation read returned an unknown SQL outcome")
    for cancellation in outcome.deferred_cancellations:
        if all(cancellation is not original for original in errors):
            errors.append(cancellation)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise BaseExceptionGroup("Continuation selected read outcomes", errors)
    if result is None:
        raise AuditIntegrityError("continuation read lost its owned complete result")
    return result
