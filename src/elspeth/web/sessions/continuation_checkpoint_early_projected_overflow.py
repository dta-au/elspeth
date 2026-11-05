"""Close original EARLY D when its issued complete-result projection cannot fit."""

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation_checkpoint_observation import CheckpointReturnedResponseObservation
from elspeth.web.composer.continuation_checkpoint_projection import ProjectedEarlyCheckpointOverflow
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission
from elspeth.web.sessions.continuation_checkpoint_paid_settlement import settle_v4_checkpoint_returned_overflow_on_connection
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.service import SessionServiceImpl


def settle_early_projected_overflow_sync(
    *,
    service: SessionServiceImpl,
    prepared: PreparedCheckpointProvider,
    admission: V4CheckpointProviderAdmission,
    returned: CheckpointReturnedResponseObservation,
    projected: ProjectedEarlyCheckpointOverflow,
) -> VerifiedCheckpointPaidOverflowTerminal:
    """Use the original D hold; the SQL sink must independently prove issuance."""
    if (
        type(service) is not SessionServiceImpl
        or type(prepared) is not PreparedCheckpointProvider
        or prepared.composer._require_sessions_service() is not service
    ):
        raise AuditIntegrityError("EARLY projected overflow changed its original session transaction owner")
    running = prepared.running
    sid = str(running.claim.session_id)
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
        return settle_v4_checkpoint_returned_overflow_on_connection(
            conn,
            service=service,
            prepared=prepared,
            admission=admission,
            observation=returned,
            projected_early=projected,
        )
