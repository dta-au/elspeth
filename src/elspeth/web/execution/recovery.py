"""Automatic durable run handoff and authoritative terminal reconciliation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, mint_worker_id
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.contracts import RecoveryRequiredReason, SessionOperationFenceLost, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import SessionOperationConflictError
from elspeth.web.execution.accounting import load_run_accounting_from_db
from elspeth.web.execution.schemas import CancelledData, CompletedData, FailedData, RunAccounting
from elspeth.web.sessions.protocol import (
    SESSION_TERMINAL_RUN_STATUS_VALUES,
    RunRecord,
    SessionOperationMutationTransaction,
    SessionRunStatus,
)

if TYPE_CHECKING:
    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.execution.service import ExecutionServiceImpl
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True)
class RecoveryObservation:
    """Advisory Landscape observation; the engine still owns takeover CAS."""

    status: RunStatus | None
    live_leader: bool
    accounting: RunAccounting | None


def observe_run(landscape_url: str, run_id: str, *, create_tables: bool, passphrase: str | None = None) -> RecoveryObservation:
    with LandscapeDB.from_url(landscape_url, create_tables=create_tables, passphrase=passphrase) as db:
        repositories = RecorderFactory(db)
        run = repositories.run_lifecycle.get_run(run_id)
        if run is None:
            return RecoveryObservation(None, False, None)
        if run.status == RunStatus.RUNNING:
            return RecoveryObservation(run.status, repositories.run_coordination.live_leader(run_id=run_id) is not None, None)
        if run.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
            return RecoveryObservation(
                run.status,
                repositories.run_coordination.live_leader(run_id=run_id) is not None,
                load_run_accounting_from_db(db, landscape_run_id=run_id),
            )
        return RecoveryObservation(run.status, False, load_run_accounting_from_db(db, landscape_run_id=run_id))


def project_terminal(transaction: SessionOperationMutationTransaction, *, run: RunRecord, observation: RecoveryObservation) -> None:
    """Commit status, audited counters and one terminal event together."""
    accounting = observation.accounting
    assert accounting is not None
    assert observation.status is not None and observation.status != RunStatus.RUNNING
    status = cast(SessionRunStatus, "cancelled" if observation.status == RunStatus.INTERRUPTED else observation.status.value)
    if run.status in SESSION_TERMINAL_RUN_STATUS_VALUES and run.status != status:
        raise AuditIntegrityError("Recovered Sessions terminal status contradicts Landscape")
    error = "Recovered authoritative failed Landscape run" if status == "failed" else None
    counters = {
        "rows_processed": accounting.source.rows_processed,
        "rows_succeeded": accounting.tokens.succeeded,
        "rows_failed": accounting.tokens.failed,
        "rows_routed_success": accounting.routing.routed_success,
        "rows_routed_failure": accounting.routing.routed_failure,
        "rows_quarantined": accounting.routing.quarantined,
    }
    if run.status == "pending":
        transaction.runs.transition_run_status(
            run_id=run.id,
            status="running",
            error=None,
            landscape_run_id=str(run.id) if run.landscape_run_id is None else None,
            **counters,
        )
    if run.status not in SESSION_TERMINAL_RUN_STATUS_VALUES:
        transaction.runs.transition_run_status(
            run_id=run.id,
            status=status,
            error=error,
            landscape_run_id=str(run.id) if run.status != "pending" and run.landscape_run_id is None else None,
            **counters,
        )
    if status == "failed":
        payload = FailedData(detail=cast(str, error), node_id=None).model_dump(mode="json")
        event_type = "failed"
    elif status == "cancelled":
        payload = CancelledData(
            source_rows_processed=accounting.source.rows_processed,
            tokens_succeeded=accounting.tokens.succeeded,
            tokens_failed=accounting.tokens.failed,
            tokens_quarantined=accounting.routing.quarantined,
            tokens_routed_success=accounting.routing.routed_success,
            tokens_routed_failure=accounting.routing.routed_failure,
        ).model_dump(mode="json")
        event_type = "cancelled"
    else:
        payload = CompletedData(
            status=cast(Literal["completed", "completed_with_failures", "empty"], status),
            accounting=accounting,
            landscape_run_id=str(run.id),
        ).model_dump(mode="json")
        event_type = "completed"
    transaction.runs.append_terminal_run_event_once(
        run_id=run.id,
        timestamp=datetime.now(UTC),
        event_type=cast(Literal["failed", "cancelled", "completed"], event_type),
        data=payload,
    )


class RunRecoveryCoordinator:
    """Claim expired web ownership, then reconcile or transfer to an executor."""

    def __init__(
        self,
        session_service: SessionServiceImpl,
        execution_service: ExecutionServiceImpl,
        blob_service: BlobServiceImpl,
        *,
        landscape_url: str,
        create_tables: bool,
        landscape_passphrase: str | None = None,
    ) -> None:
        self._sessions = session_service
        self._execution = execution_service
        self._blobs = blob_service
        self._landscape_url = landscape_url
        self._create_tables = create_tables
        self._landscape_passphrase = landscape_passphrase

    async def recover(self) -> None:
        candidates = await self._sessions.list_recoverable_run_records()
        for candidate in candidates:
            if str(candidate.id) in self._execution.get_live_run_ids():
                continue
            await self._recover_candidate(candidate)

    async def _rebind(self, run: RunRecord, lease: SessionOperationLease) -> bool:
        try:
            await run_sync_in_worker(
                self._sessions.session_operation_authority.mutate,
                lease.context,
                lambda tx: tx.runs.rebind_run_ownership(run_id=run.id),
            )
        except SessionOperationFenceLost:
            return False
        return True

    async def _reconcile_terminal(self, run: RunRecord, observation: RecoveryObservation, lease: SessionOperationLease) -> None:
        if not await self._rebind(run, lease):
            return
        await run_sync_in_worker(
            self._sessions.session_operation_authority.mutate,
            lease.context,
            lambda tx: project_terminal(tx, run=run, observation=observation),
        )
        result = await self._blobs.finalize_run_output_blobs(
            run.id,
            success=observation.status != RunStatus.INTERRUPTED,
            session_operation_context=lease.context,
        )
        if not result.errors:
            await run_sync_in_worker(
                self._sessions.session_operation_authority.mutate,
                lease.context,
                lambda tx: tx.runs.mark_recovery_outputs_finalized(run_id=run.id),
            )

    def _reconcile_resumable_terminal(
        self,
        run: RunRecord,
        expected_status: RunStatus,
        lease: SessionOperationLease,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Hold Landscape authority across projection and output finalization.

        The worker owns the Landscape transaction; Sessions transactions run
        briefly on the event loop's workers. No Sessions transaction spans a
        Landscape call. The seat row lock prevents CLI takeover even if this
        process pauses beyond its lease window.
        """
        with LandscapeDB.from_url(
            self._landscape_url,
            create_tables=self._create_tables,
            passphrase=self._landscape_passphrase,
        ) as db:
            repositories = RecorderFactory(db)
            try:
                token = repositories.run_coordination.acquire_reconciliation_leadership(
                    run_id=str(run.id),
                    worker_id=mint_worker_id(str(run.id)),
                    window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
                    expected_status=expected_status,
                )
            except NonResumableRunError:
                return
            try:
                with fenced_leader_transaction(
                    db.engine,
                    token=token,
                    window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
                    verb="web_terminal_reconciliation",
                ):
                    current = repositories.run_lifecycle.get_run(str(run.id))
                    if current is None or current.status != expected_status:
                        raise AuditIntegrityError("Landscape status changed under terminal reconciliation authority")
                    observation = RecoveryObservation(
                        current.status,
                        False,
                        load_run_accounting_from_db(db, landscape_run_id=str(run.id)),
                    )
                    lease.guard_external_effect()
                    asyncio.run_coroutine_threadsafe(self._reconcile_terminal(run, observation, lease), loop).result()
            finally:
                repositories.run_coordination.release_seat(token=token)

    async def _recover_candidate(self, candidate: RunRecord) -> None:
        try:
            lease = await SessionOperationLease.acquire(
                self._sessions.session_operation_authority,
                session_id=candidate.session_id,
                operation_kind=SessionOperationKind.EXECUTE,
                owner_instance_id=self._sessions.session_operation_owner_instance_id,
                lease_seconds=self._sessions.session_operation_lease_seconds,
            )
        except SessionOperationConflictError:
            return
        transferred = False
        try:
            run = await self._sessions.get_run(candidate.id)
            observation = await run_sync_in_worker(
                observe_run,
                self._landscape_url,
                str(run.id),
                create_tables=self._create_tables,
                passphrase=self._landscape_passphrase,
            )
            lease.guard_external_effect()
            if observation.live_leader:
                return
            if observation.status is not None and observation.status != RunStatus.RUNNING:
                if observation.status in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
                    # Separate from the bounded shared worker pool: the lock
                    # owner waits for async Sessions/blob work on that pool.
                    task = lease.create_task(
                        asyncio.to_thread(
                            self._reconcile_resumable_terminal,
                            run,
                            observation.status,
                            lease,
                            asyncio.get_running_loop(),
                        ),
                        name="run-terminal-reconciliation",
                    )
                    await asyncio.shield(task)
                else:
                    await self._reconcile_terminal(run, observation, lease)
                return
            if not await self._rebind(run, lease):
                return
            if observation.status is None and await self._sessions.get_run_execution_input(run.id) is None:
                await run_sync_in_worker(
                    self._sessions.session_operation_authority.mutate,
                    lease.context,
                    lambda tx: tx.runs.mark_recovery_required(run_id=run.id, reason=RecoveryRequiredReason.MISSING_BASELINE),
                )
                return
            transferred = await self._execution.recover_run(run, lease, resume_existing=observation.status is not None)
        finally:
            if not transferred:
                await lease.close()
