"""Cancellation-safe finish for the original unpaid END request-preparation refusal."""

from __future__ import annotations

from functools import partial

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_checkpoint_end_dispositions import EndCheckpointUnboundRefusalTerminal
from elspeth.web.composer.continuation_checkpoint_end_physical import (
    CheckpointEndPhysicalSource,
    _CheckpointUnboundRefusalObserved,
    require_original_checkpoint_unbound_refusal,
)
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.continuation_checkpoint_unbound_refusal import VerifiedCheckpointUnboundRefusal
from elspeth.web.sessions.service import SessionServiceImpl


async def finalize_checkpoint_end_unbound_refusal(
    control: _CheckpointUnboundRefusalObserved, *, source: CheckpointEndPhysicalSource
) -> EndCheckpointUnboundRefusalTerminal:
    """Close the original prepared source without inventing a physical attempt."""
    if type(control) is not _CheckpointUnboundRefusalObserved or control.permit.source is not source:
        raise AuditIntegrityError("END source refusal changed its original physical-free control")
    require_original_checkpoint_unbound_refusal(control.permit)
    owner = source.phase_owner
    work = owner.phases.work
    sessions = owner.phases.service._sessions_service
    if type(sessions) is not SessionServiceImpl or work.running is None:
        raise AuditIntegrityError("END source refusal lost its original SQL worker")
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    try:
        outcome = await run_required_sql_finish_once(sql, partial(sessions.refuse_v4_checkpoint_unbound_source_sync, permit=control.permit))
        work.coordinator.validate_continuation_sql_outcome(
            ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
        )
    except BaseException:
        projection.complete_without_submission()
        raise
    if type(outcome) is RequiredSQLRaised:
        projection.complete_without_submission()
        if outcome.deferred_cancellations:
            raise BaseExceptionGroup("END source refusal SQL and deferred cancellation", [outcome.error, *outcome.deferred_cancellations])
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned:
        projection.complete_without_submission()
        raise AuditIntegrityError("END source refusal returned no original SQL outcome")
    projection.begin_projection()
    try:
        verified = outcome.value
        if type(verified) is not VerifiedCheckpointUnboundRefusal:
            raise AuditIntegrityError("END source refusal lost its original cold SQL result")
        terminal = EndCheckpointUnboundRefusalTerminal(verified, outcome.deferred_cancellations)
    except BaseException as error:
        projection.complete_owned(error)
        raise
    projection.complete_owned()
    return terminal
