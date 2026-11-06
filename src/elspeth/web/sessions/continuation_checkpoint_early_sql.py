"""Original EARLY complete-return transaction entrypoints.

Keep complete H retention separate from C/K or C/R settlement. Neither step
restarts provider work or borrows another operation's session transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import ProjectedEarlyCheckpoint
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_checkpoint_early_retry import VerifiedEarlyFormatRetry, read_early_format_retry_on_connection
from elspeth.web.sessions.continuation_checkpoint_early_return import (
    EarlyCompleteReturnAdmission,
    complete_early_checkpoint_on_connection,
    read_early_complete_return_on_connection,
    retain_early_complete_return_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission
from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_data_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class EarlyCheckpointAdvanced:
    completed: StoredContinuationSnapshot
    retry: VerifiedEarlyFormatRetry | None


def retain_early_complete_return_sync(
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
) -> EarlyCompleteReturnAdmission:
    if type(service) is not SessionServiceImpl or prepared.composer._require_sessions_service() is not service:
        raise AuditIntegrityError("EARLY H changed its original session transaction owner")
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with (
        service._session_process_locked_begin(sid) as conn,
        service._session_write_lock(conn, sid),
        service._session_composer_mutation_transaction(
            conn,
            session_id=sid,
            session_operation_context=running.session_operation_context,
            expected_kind=SessionOperationKind.COMPOSE,
            audit_only=True,
        ),
    ):
        owed = retain_early_complete_return_on_connection(
            conn,
            running=running,
            prepared=prepared,
            admission=admission,
            returned=returned,
        )
    with locked_session_transaction(service._engine, sid) as conn:
        cold = read_early_complete_return_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            selected=owed.owed_selected,
            names=prepared.env_ref_names,
        )
    if cold.response != owed.response or cold.new_hold != owed.owed_hold:
        raise AuditIntegrityError("EARLY H changed after original complete-return commit")
    return owed


def complete_early_checkpoint_sync(
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    owed: EarlyCompleteReturnAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpoint,
) -> EarlyCheckpointAdvanced:
    if type(service) is not SessionServiceImpl or prepared.composer._require_sessions_service() is not service:
        raise AuditIntegrityError("EARLY C/K changed its original session transaction owner")
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with (
        service._session_process_locked_begin(sid) as conn,
        service._session_write_lock(conn, sid),
        service._session_composer_mutation_transaction(
            conn,
            session_id=sid,
            session_operation_context=running.session_operation_context,
            expected_kind=SessionOperationKind.COMPOSE,
            audit_only=True,
        ),
    ):
        completed = complete_early_checkpoint_on_connection(
            conn,
            service=service,
            running=running,
            prepared=prepared,
            owed_admission=owed,
            returned=returned,
            projected=projected,
        )
    with locked_session_transaction(service._engine, sid) as conn:
        retry = read_early_format_retry_on_connection(conn, session_id=sid, operation_id=operation, selected=completed)
        stage = read_iteration_stage_data_on_connection(
            conn, session_id=running.claim.session_id, operation_id=operation, selected=completed
        )
    expected = "early_retry_ready" if projected.retry_pending else "classify_ready"
    if stage is None or stage.current != completed or stage.stage != expected or (retry is not None) != projected.retry_pending:
        raise AuditIntegrityError("EARLY completion changed its cold original next-stage handoff")
    return EarlyCheckpointAdvanced(completed, retry)
