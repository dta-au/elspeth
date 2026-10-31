"""A real returned terminal must survive faults in later watcher cleanup.

The existing worker/turn fixture supplies its composer shim. Admission, turn
finalization, terminal SQL, readback, required-work tickets and lease closure
are real. These controls make no additional genuine-SDK/provider claim.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions import composer_async_worker as worker_module
from elspeth.web.sessions.composer_app_services import ComposerAppServices
from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN, ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.models import session_operation_fences_table
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


@dataclass
class _CleanupEvidence:
    owner: asyncio.Task[Any] | None = None
    lease: SessionOperationLease | None = None
    coordinator: RequiredWorkCoordinator | None = None
    settlement: worker_module._StartedSettlement | None = None
    returned: ComposerOperationRecord | None = None
    handoff_at_cleanup: ComposerOperationRecord | None = None
    cleanup_entries: int = 0
    cleanup_completed: bool = False
    caught_cancellation: asyncio.CancelledError | None = None
    owner_failure: BaseException | None = None
    late_fault: RuntimeError = field(default_factory=lambda: RuntimeError("owned post-terminal watcher-cleanup fault"))
    late_cause: ValueError = field(default_factory=lambda: ValueError("owned watcher-cleanup cause"))
    readbacks: list[tuple[RequiredWorkTicket, RequiredSQLReturned[ComposerOperationRecord]]] = field(default_factory=list)


def _originals(root: BaseException | None) -> list[BaseException]:
    pending = [root]
    seen: list[BaseException] = []
    while pending:
        error = pending.pop()
        if error is None or any(error is prior for prior in seen):
            continue
        seen.append(error)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        pending.extend((error.__cause__, error.__context__))
    return seen


async def _actual_terminal_then_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, late_mode: str) -> _CleanupEvidence:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    evidence = _CleanupEvidence()
    ready = asyncio.Event()
    release = asyncio.Event()
    actual_started = worker._run_started_under_lease
    actual_turn = worker_module.run_composer_turn
    actual_cleanup = worker_module._finish_operation_watcher
    actual_bridge = worker_module.run_required_sql_finish_once

    async def retain_actual_owner(
        services: ComposerAppServices,
        running: ComposerOperationRunning,
        lease: SessionOperationLease,
        settlement: worker_module._StartedSettlement,
        setup_ticket: RequiredWorkTicket | None,
    ) -> ComposerOperationRecord:
        assert evidence.owner is None and lease.required_work is not None
        evidence.owner = asyncio.current_task()
        evidence.lease = lease
        evidence.coordinator = lease.required_work
        evidence.settlement = settlement
        return await actual_started(services, running, lease, settlement, setup_ticket)

    async def retain_actual_return(
        services: ComposerAppServices,
        turn: worker_module.ComposerTurnInput,
        *,
        lease: SessionOperationLease,
        running: ComposerOperationRunning,
        request_lifecycle: Any,
        observation: worker_module.ComposerTurnObservation,
        budget_anchor: worker_module.ComposerBudgetAnchor,
    ) -> ComposerOperationRecord:
        returned = await actual_turn(
            services,
            turn,
            lease=lease,
            running=running,
            request_lifecycle=request_lifecycle,
            observation=observation,
            budget_anchor=budget_anchor,
        )
        assert type(returned) is ComposerOperationRecord and returned.status == "completed"
        assert evidence.returned is None
        evidence.returned = returned
        return returned

    async def late_actual_cleanup(
        watcher: asyncio.Task[Any],
        observation: worker_module._OperationWatchObservation,
        *,
        caller_reader: worker_module.ComposerOperationWatchReader | None = None,
    ) -> tuple[BaseException, ...]:
        target = asyncio.current_task() is evidence.owner and evidence.returned is not None and evidence.cleanup_entries == 0
        if target:
            assert evidence.settlement is not None
            evidence.cleanup_entries += 1
            evidence.handoff_at_cleanup = evidence.settlement.terminal_receipt
            # Establish the actual committed return before entering the real
            # watcher cleanup. No returned/foreign record is fabricated.
            stored = authority.get(session_id=evidence.returned.session_id, operation_id=evidence.returned.operation_id)
            assert stored == evidence.returned and stored.result_json is not None and stored.result_sha256 is not None
        outcome = await actual_cleanup(watcher, observation, caller_reader=caller_reader)
        if not target:
            return outcome
        assert watcher.done() and not outcome
        evidence.cleanup_completed = True
        ready.set()
        try:
            await release.wait()
        except asyncio.CancelledError as original:
            evidence.caught_cancellation = original
            assert late_mode == "cancel" and original.args == (COMPOSER_SHUTDOWN,)
            raise original from evidence.late_cause
        if late_mode == "fault":
            raise evidence.late_fault from evidence.late_cause
        assert late_mode == "healthy"
        return outcome

    async def retain_actual_readback(ticket: RequiredWorkTicket, func: Any, *args: Any, **kwargs: Any) -> Any:
        outcome = await actual_bridge(ticket, func, *args, **kwargs)
        if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL and ticket.key.semantic_ordinal == 1:
            assert type(outcome) is RequiredSQLReturned and type(outcome.value) is ComposerOperationRecord
            evidence.readbacks.append((ticket, outcome))
        return outcome

    monkeypatch.setattr(worker, "_run_started_under_lease", retain_actual_owner)
    monkeypatch.setattr(worker_module, "run_composer_turn", retain_actual_return)
    monkeypatch.setattr(worker_module, "_finish_operation_watcher", late_actual_cleanup)
    monkeypatch.setattr(worker_module, "run_required_sql_finish_once", retain_actual_readback)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            await ready.wait()
            assert evidence.owner is not None and not evidence.owner.done()
            assert evidence.returned is not None and evidence.returned.operation_id == admitted.operation_id
            assert evidence.cleanup_completed and evidence.cleanup_entries == 1
            if late_mode == "cancel":
                assert evidence.owner.cancel(COMPOSER_SHUTDOWN)
            else:
                release.set()
            try:
                # Observe this Task directly: gather synthesizes a new error
                # for a cancelled child and loses the original's identity.
                await evidence.owner
            except BaseException as original_owner_failure:
                evidence.owner_failure = original_owner_failure

        assert evidence.lease is not None and evidence.coordinator is not None
        assert evidence.settlement is not None and evidence.returned is not None and evidence.owner is not None
        root = evidence.owner_failure
        if late_mode == "healthy":
            assert root is None and not evidence.owner.cancelled() and evidence.caught_cancellation is None
        else:
            originals = _originals(root)
            original = evidence.caught_cancellation if late_mode == "cancel" else evidence.late_fault
            assert original is not None and original.__cause__ is evidence.late_cause
            assert any(error is original for error in originals)
            assert any(error is evidence.late_cause for error in originals)
            if late_mode == "cancel":
                assert evidence.owner.cancelled() and root is original
            else:
                assert not evidence.owner.cancelled() and evidence.owner.exception() is root
        terminal = authority.get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert terminal == evidence.returned
        assert terminal.status == "completed" and terminal.result_json == evidence.returned.result_json
        assert terminal.result_sha256 == evidence.returned.result_sha256 and composer.calls == 1
        assert len(evidence.readbacks) == 1
        sql, actual = evidence.readbacks[0]
        assert actual.value == evidence.returned and sql.complete and sql._future is not None and sql._future.done()
        coordinator = evidence.coordinator
        projection = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION]
        assert len(projection) == 1 and projection[0].complete and projection[0]._projection_started
        assert all(ticket.key.source is not RequiredWorkSource.TERMINAL_FAILURE_PROJECTION for ticket in coordinator.tickets)
        assert all(ticket.key.source is not RequiredWorkSource.TERMINAL_FAILURE_SQL for ticket in coordinator.tickets)
        releases = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(releases) == 1 and releases[0].complete
        assert coordinator.all_completed and evidence.lease.closed and evidence.lease._renewal_task.done()
        with engine.connect() as conn:
            fence = conn.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(admitted.session_id))
            ).one()
        assert fence.operation_id == evidence.lease.context.fence.operation_id
        assert fence.lease_token == evidence.lease.context.fence.lease_token and fence.released_at is not None
        assert not worker._jobs and not worker._required_coordinators
        assert not worker._recovery_coordinators and not worker._adoption_recovery_pending
        return evidence
    finally:
        # Release only this test's injection barrier. No required-work ticket,
        # terminal or fence is completed by cleanup code.
        release.set()
        if evidence.owner is not None and not evidence.owner.done():
            await asyncio.gather(evidence.owner, return_exceptions=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_actual_returned_terminal_completes_without_late_cleanup_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _actual_terminal_then_cleanup(tmp_path, monkeypatch, late_mode="healthy")


@pytest.mark.asyncio
@pytest.mark.parametrize("late_mode", ("fault", "cancel"), ids=("owned-fault", "actual-cancellation"))
async def test_actual_terminal_is_handed_off_before_late_watcher_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_mode: str
) -> None:
    evidence = await _actual_terminal_then_cleanup(tmp_path, monkeypatch, late_mode=late_mode)
    assert evidence.handoff_at_cleanup is evidence.returned, "Actual committed terminal return was not owned before watcher cleanup"
