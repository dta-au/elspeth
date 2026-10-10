"""Actual live-lease terminal handoff and disjoint readback controls."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.models import session_operation_fences_table
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


def _assert_actual_fence_owned(engine, lease):
    with engine.connect() as connection:
        fence = connection.execute(
            select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == lease.context.fence.session_id)
        ).one()
    assert fence.operation_id == lease.context.fence.operation_id
    assert fence.lease_token == lease.context.fence.lease_token
    assert fence.released_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_writer_readbacks", (False, True))
async def test_setup_producer_completion_is_not_terminal_and_failure_settles_before_release(
    tmp_path, monkeypatch, existing_writer_readbacks: bool
) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    owners = []
    leases = []
    coordinators = []

    async def return_without_terminal(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and setup_ticket is not None and lease.required_work is not None
        owners.append(owner)
        leases.append(lease)
        coordinators.append(lease.required_work)
        setup_ticket.complete_owned()
        if existing_writer_readbacks:
            for attempt in (0, 1):
                ticket = lease.required_work.reserve(RequiredWorkSource.TERMINAL_WRITER_READ_SQL, recurrence_ordinal=attempt)
                outcome = await run_required_sql_finish_once(
                    ticket, authority.get, session_id=running.claim.session_id, operation_id=running.claim.operation_id
                )
                assert isinstance(outcome, RequiredSQLReturned)
                assert outcome.value is not None and outcome.value.status == "running"
        return None

    monkeypatch.setattr(worker, "_run_started_under_lease", return_without_terminal)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        await worker.run_until_idle()
        terminal = authority.get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert terminal is not None and terminal.status == "failed"
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.http_status == 500 and error.error_type == "audit_integrity_error"
        assert composer.calls == 0 and len(owners) == 1
        assert owners[0].done() and isinstance(owners[0].exception(), AuditIntegrityError)
        assert leases[0].closed and leases[0]._renewal_task.done()
        coordinator = coordinators[0]
        writer_reads = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        worker_reads = [ticket for ticket in writer_reads if ticket.key.semantic_ordinal == 1]
        assert len(worker_reads) == 1  # Known missing setup return refuses before allocating its writer read.
        assert all(ticket.complete and ticket._future is not None and ticket._future.done() for ticket in worker_reads)
        if existing_writer_readbacks:
            service_reads = [ticket for ticket in writer_reads if ticket.key.semantic_ordinal == 0]
            assert len(service_reads) == 2
            assert {ticket.key for ticket in worker_reads}.isdisjoint(ticket.key for ticket in service_reads)
        failures = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL]
        release = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(failures) == 1 and failures[0].complete
        assert len(release) == 1 and release[0].complete
        # The known producer-return guard is retained on the owned Task;
        # the subsequent actual failed-terminal projection itself succeeds.
        assert isinstance(owners[0].exception(), AuditIntegrityError)
        assert not worker._jobs
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_wrong_actual_readback_row_is_refused_without_relabeling_committed_terminal(tmp_path, monkeypatch) -> None:
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    owners = {}
    coordinators = {}
    leases = {}
    original_started = worker._run_started
    original_get = authority.get
    target = await _admit(app, service, authority, operation_id=str(uuid4()))
    foreign = await _admit(app, service, authority, operation_id=str(uuid4()))
    wrong_rows = []

    async def capture_started(services, running, lease):
        owner = asyncio.current_task()
        assert owner is not None and lease.required_work is not None
        owners[running.claim.operation_id] = owner
        coordinators[running.claim.operation_id] = lease.required_work
        leases[running.claim.operation_id] = lease
        return await original_started(services, running, lease)

    def return_actual_foreign_row(*, session_id, operation_id):
        if operation_id == target.operation_id:
            row = original_get(session_id=foreign.session_id, operation_id=foreign.operation_id)
            wrong_rows.append(row)
            return row
        return original_get(session_id=session_id, operation_id=operation_id)

    monkeypatch.setattr(worker, "_run_started", capture_started)
    monkeypatch.setattr(authority, "get", return_actual_foreign_row)
    try:
        await worker.run_until_idle()
        terminal = original_get(session_id=target.session_id, operation_id=target.operation_id)
        assert terminal is not None and terminal.status == "completed"
        assert terminal.result_sha256 is not None
        assert wrong_rows and all(row is not None and row.operation_id == foreign.operation_id for row in wrong_rows)
        owner = owners[target.operation_id]
        assert owner.done() and owner.exception() is not None
        projections = [
            ticket
            for ticket in coordinators[target.operation_id].tickets
            if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION
        ]
        assert projections and all(not ticket.complete for ticket in projections)
        assert not coordinators[target.operation_id].all_completed
        _assert_actual_fence_owned(engine, leases[target.operation_id])
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinators[target.operation_id].tickets)
        assert composer.calls == 2  # Both real admitted operations dispatch once.
        unchanged = original_get(session_id=target.session_id, operation_id=target.operation_id)
        assert unchanged == terminal and not worker._jobs
    finally:
        if target.operation_id in leases:
            service.session_operation_authority.release(leases[target.operation_id].context)
        engine.dispose()


@pytest.mark.asyncio
async def test_terminal_readback_no_return_keeps_projection_and_lease_owned_until_generation_join(tmp_path, monkeypatch) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from elspeth.web import async_workers
    from elspeth.web.required_work import RequiredWorkIncomplete
    from tests.fixtures.required_executor import RecordingRequiredGenerationRecovery
    from tests.unit.web.test_required_executor_custody import QueueThenRaiseExecutor

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    entered, release = threading.Event(), threading.Event()
    holders = []
    blocker_futures = []
    executor = QueueThenRaiseExecutor(max_workers=1)
    recorder = RecordingRequiredGenerationRecovery()
    unavailable = threading.Event()
    original_get = authority.get
    original_under_lease = worker._run_started_under_lease
    actual_reads = []

    def observed_get(*args, **kwargs):
        actual_reads.append(True)
        return original_get(*args, **kwargs)

    async def stop_at_real_readback(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and setup_ticket is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        terminal = await original_under_lease(services, running, lease, settlement, setup_ticket)
        assert terminal.status == "completed"
        blocker_futures.append(ThreadPoolExecutor.submit(executor, lambda: (entered.set(), release.wait(10))))
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
        async_workers.configure_required_executor_recovery(
            drain_seconds=10,
            instance_draining=worker._instance_draining,
            generation_unavailable=unavailable,
            recovery_callback=recorder,
        )
        return terminal

    monkeypatch.setattr(worker, "_run_started_under_lease", stop_at_real_readback)
    monkeypatch.setattr(authority, "get", observed_get)
    generation = None
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(5):
            while async_workers._GENERATION_CUSTODIAN is None or async_workers._GENERATION_CUSTODIAN.state != "quarantined":
                await asyncio.sleep(0.001)
        generation = async_workers._GENERATION_CUSTODIAN
        owner, lease, coordinator = holders[0]
        read_sql = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        read_projection = [
            ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION
        ]
        assert len(read_sql) == len(read_projection) == 1
        assert not read_sql[0].complete and not read_projection[0].complete
        assert read_sql[0]._future is None  # Actual executor queued, then raised without returning it.
        assert not owner.done() and not lease.closed and not generation.joined.is_set()
        assert actual_reads == [] and not blocker_futures[0].done()
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
        with pytest.raises(RequiredWorkIncomplete):
            coordinator.prepare_lease_release()
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        async with asyncio.timeout(5):
            while not generation.recovery_finished.is_set():
                await asyncio.sleep(0.001)
        assert generation.joined.is_set() and blocker_futures[0].done()
        assert read_sql[0].complete and not read_projection[0].complete
        # The original terminal readback remains aborted, and no competing
        # failure write/read may resolve its unverified projection.
        assert len(actual_reads) == 0 and read_sql[0]._future is None
        assert composer.calls == 1
        terminal = original_get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert terminal is not None and terminal.status == "completed"
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
        assert lease.closed and lease._renewal_task.done()
        _assert_actual_fence_owned(engine, lease)
    finally:
        release.set()
        if holders and not holders[0][0].done():
            await asyncio.gather(holders[0][0], return_exceptions=True)
        if generation is not None:
            async with asyncio.timeout(5):
                while not generation.recovery_finished.is_set():
                    await asyncio.sleep(0.001)
        await async_workers.shutdown_async_workers()
        if holders:
            service.session_operation_authority.release(holders[0][1].context)
        engine.dispose()


@pytest.mark.asyncio
async def test_counterfeit_joined_carrier_retains_unknown_projection_barrier_without_late_publication(tmp_path, monkeypatch) -> None:
    from elspeth.web.required_work import RequiredWorkIncomplete
    from elspeth.web.sessions import composer_async_worker as worker_module
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    original_started = worker._run_started
    original_bridge = worker_module.run_required_sql_finish_once
    holders = []
    actual_carriers = []

    async def capture_owner(services, running, lease):
        owner = asyncio.current_task()
        assert owner is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        return await original_started(services, running, lease)

    async def replace_actual_carrier(ticket, func, *args, **kwargs):
        actual = await original_bridge(ticket, func, *args, **kwargs)
        if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL and ticket.key.semantic_ordinal == 1:
            assert isinstance(actual, RequiredSQLReturned)
            actual_carriers.append(actual)
            return RequiredSQLReturned(object(), actual.deferred_cancellations)
        return actual

    monkeypatch.setattr(worker, "_run_started", capture_owner)
    monkeypatch.setattr(worker_module, "run_required_sql_finish_once", replace_actual_carrier)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(5):
            while not holders:
                await asyncio.sleep(0.001)
        owner, _lease, coordinator = holders[0]
        await asyncio.gather(owner, return_exceptions=True)
        terminal = authority.get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert terminal is not None and terminal.status == "completed"
        assert len(actual_carriers) == 1 and actual_carriers[0].value.result_sha256 == terminal.result_sha256
        sql = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        projection = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION]
        assert len(sql) == len(projection) == 1
        assert sql[0].complete and sql[0]._future is not None and sql[0]._future.done()
        assert not projection[0].complete
        _assert_actual_fence_owned(engine, holders[0][1])
        assert all(
            ticket.key.source not in (RequiredWorkSource.TERMINAL_FAILURE_SQL, RequiredWorkSource.LEASE_RELEASE)
            for ticket in coordinator.tickets
        )
        with pytest.raises(RequiredWorkIncomplete):
            coordinator.prepare_lease_release()
        escaping = owner.exception()
        assert escaping is not None
        pending = [escaping]
        seen = []
        while pending:
            error = pending.pop()
            if any(error is earlier for earlier in seen):
                continue
            seen.append(error)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
        assert any(isinstance(error, ComposerTerminalSQLCompletionUnknown) for error in seen)
        assert any(isinstance(error, AuditIntegrityError) for error in seen)
        assert composer.calls == 1
        assert authority.get(session_id=admitted.session_id, operation_id=admitted.operation_id) == terminal
    finally:
        if holders:
            # Test-owner cleanup after actual SQL completion; no coordinator receipt
            # is fabricated for the intentionally invalid handoff.
            service.session_operation_authority.release(holders[0][1].context)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_release_fault", (False, True))
async def test_actual_parked_readback_three_original_cancellations_and_late_release_keep_terminal(
    tmp_path, monkeypatch, late_release_fault
) -> None:
    import threading

    from sqlalchemy.exc import OperationalError

    from elspeth.web.sessions import composer_async_worker as worker_module

    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    entered, release = threading.Event(), threading.Event()
    holders = []
    carriers = []
    actual_rows = []
    original_started = worker._run_started
    original_get = authority.get
    original_bridge = worker_module.run_required_sql_finish_once
    original_release = service.session_operation_authority.release
    release_failure = OperationalError("exact lease release", {}, RuntimeError("owned release driver"))

    async def capture_owner(services, running, lease):
        owner = asyncio.current_task()
        assert owner is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        return await original_started(services, running, lease)

    def park_actual_readback(*, session_id, operation_id):
        row = original_get(session_id=session_id, operation_id=operation_id)
        actual_rows.append(row)
        entered.set()
        assert release.wait(10)
        return row

    async def retain_actual_carrier(ticket, func, *args, **kwargs):
        outcome = await original_bridge(ticket, func, *args, **kwargs)
        if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL and ticket.key.semantic_ordinal == 1:
            carriers.append(outcome)
        return outcome

    def actual_release_fault(context):
        raise release_failure

    monkeypatch.setattr(worker, "_run_started", capture_owner)
    monkeypatch.setattr(authority, "get", park_actual_readback)
    monkeypatch.setattr(worker_module, "run_required_sql_finish_once", retain_actual_carrier)
    if late_release_fault:
        monkeypatch.setattr(service.session_operation_authority, "release", actual_release_fault)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        owner, lease, coordinator = holders[0]
        before = original_get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert before is not None and before.status == "completed"
        sql = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        projection = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION]
        assert len(sql) == len(projection) == 1
        assert sql[0]._future is not None and sql[0]._future.running() and not sql[0].complete
        for ordinal in range(3):
            owner.cancel(f"retained-readback-cancel-{ordinal}")
            await asyncio.sleep(0.02)
            assert not owner.done() and not lease.closed
        assert not projection[0].complete and not carriers
        assert all(ticket.key.source is not RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets)
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        assert len(carriers) == 1 and isinstance(carriers[0], RequiredSQLReturned)
        cancelled = carriers[0].deferred_cancellations
        assert len(cancelled) == 3
        assert [error.args for error in cancelled] == [(f"retained-readback-cancel-{ordinal}",) for ordinal in range(3)]
        pending = [owner.exception()]
        originals = []
        while pending:
            error = pending.pop()
            if error is None or any(error is earlier for earlier in originals):
                continue
            originals.append(error)
            if isinstance(error, BaseExceptionGroup):
                pending.extend(error.exceptions)
            if error.__cause__ is not None:
                pending.append(error.__cause__)
        assert all(any(error is original for error in originals) for original in cancelled)
        assert all(row == before for row in actual_rows)
        assert original_get(session_id=admitted.session_id, operation_id=admitted.operation_id) == before
        assert composer.calls == 1 and sql[0].complete and projection[0].complete
        releases = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(releases) == 1 and releases[0].complete
        if late_release_fault:
            assert any(error is release_failure for error in originals)
            assert any(error is release_failure for error in releases[0].errors)
        assert lease.closed and lease._renewal_task.done()
    finally:
        release.set()
        if holders and not holders[0][0].done():
            await asyncio.gather(holders[0][0], return_exceptions=True)
        if late_release_fault and holders:
            original_release(holders[0][1].context)
        engine.dispose()
