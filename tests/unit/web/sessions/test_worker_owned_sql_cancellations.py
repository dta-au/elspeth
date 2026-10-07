"""Three owner originals survive actual selection and failed-publication SQL joins."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.dml import Update

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.models import composer_async_operations_table, session_operation_fences_table
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


def _originals(root):
    pending = [root]
    seen = []
    while pending:
        error = pending.pop()
        if error is None or any(error is previous for previous in seen):
            continue
        seen.append(error)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ("setup", "selection", "publication"))
@pytest.mark.parametrize("physical_fault", (False, True))
async def test_three_original_owner_cancellations_with_actual_sql_outcome(tmp_path, monkeypatch, seam, physical_fault):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    holders = []
    entered = threading.Event()
    release = threading.Event()
    armed = threading.Event()
    caught_cancellations = []
    sql_failures = []
    body_error = RuntimeError("actual setup producer failure")
    original_setup = worker._run_started_under_lease
    original_get_turn = authority.get_for_turn
    original_get = authority.get_with_database_now
    original_shield = asyncio.shield

    async def observe_original_shield(awaitable):
        try:
            return await original_shield(awaitable)
        except asyncio.CancelledError as original:
            if holders and asyncio.current_task() is holders[0][0]:
                caught_cancellations.append(original)
            raise

    def actual_setup_read(*, session_id, operation_id):
        actual = original_get_turn(session_id=session_id, operation_id=operation_id)
        if armed.is_set() and seam == "setup":
            entered.set()
            assert release.wait(10)
            if physical_fault:
                original = OperationalError("actual setup SQL fault", {}, RuntimeError("driver fault"))
                sql_failures.append(original)
                raise original
        return actual

    def actual_selection(*, session_id, operation_id):
        actual = original_get(session_id=session_id, operation_id=operation_id)
        if armed.is_set() and seam == "selection":
            entered.set()
            assert release.wait(10)
            if physical_fault:
                original = OperationalError("actual selection SQL fault", {}, RuntimeError("driver fault"))
                sql_failures.append(original)
                raise original
        return actual

    def actual_publication(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is None or seam != "publication":
            return
        actual = compiled.statement
        if not isinstance(actual, Update) or actual.table is not composer_async_operations_table:
            return
        values = context.compiled_parameters
        if len(values) != 1 or values[0].get("status") != "failed":
            return
        entered.set()
        assert release.wait(10)
        if physical_fault:
            original = OperationalError("actual terminal UPDATE fault", {}, RuntimeError("driver fault"))
            sql_failures.append(original)
            raise original

    async def actual_setup_failure(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and setup_ticket is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        armed.set()
        if seam == "setup":
            return await original_setup(services, running, lease, settlement, setup_ticket)
        setup_ticket.complete_owned(body_error)
        raise body_error

    monkeypatch.setattr(asyncio, "shield", observe_original_shield)
    monkeypatch.setattr(authority, "get_for_turn", actual_setup_read)
    monkeypatch.setattr(authority, "get_with_database_now", actual_selection)
    monkeypatch.setattr(worker, "_run_started_under_lease", actual_setup_failure)
    event.listen(engine, "before_cursor_execute", actual_publication)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            while not entered.is_set():
                await asyncio.sleep(0.001)
            owner, lease, coordinator = holders[0]
            markers = [object(), object(), object()]
            for index, marker in enumerate(markers, 1):
                owner.cancel(marker)
                while len(caught_cancellations) < index:
                    await asyncio.sleep(0.001)
                assert caught_cancellations[index - 1].args == (marker,)
                assert not owner.done()
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
        armed.clear()
        roots = _originals(owner.exception())
        if seam != "setup":
            assert any(error is body_error for error in roots)
        assert all(any(error is original for error in roots) for original in caught_cancellations)
        assert len(caught_cancellations) == 3
        assert all(any(error is original for error in roots) for original in sql_failures)
        terminal, _verification_now = original_get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert terminal is not None and composer.calls == 0
        writers = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL]
        projections = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        releases = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(projections) == 1
        if physical_fault and seam != "setup":
            assert terminal.status == "running" and terminal.result_json is None
            assert not projections[0].complete and not releases
            if seam == "publication":
                assert len(sql_failures) == 2 and len(writers) == 2
                assert all(ticket.complete and ticket._future is not None and ticket._future.done() for ticket in writers)
            else:
                assert len(sql_failures) == 1 and not writers
            with engine.connect() as connection:
                fence = connection.execute(
                    select(session_operation_fences_table).where(
                        session_operation_fences_table.c.session_id == lease.context.fence.session_id
                    )
                ).one()
            assert fence.operation_id == lease.context.fence.operation_id
            assert fence.lease_token == lease.context.fence.lease_token and fence.released_at is None
        else:
            if seam == "setup" and physical_fault:
                assert len(sql_failures) == 1
            assert terminal.status == "failed" and terminal.result_json is not None
            assert len(writers) == 1 and writers[0].complete
            assert writers[0]._future is not None and writers[0]._future.done()
            assert projections[0].complete and len(releases) == 1 and releases[0].complete
            assert lease.closed and lease._renewal_task.done()
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", actual_publication)
        if holders:
            owner, lease, coordinator = holders[0]
            await asyncio.gather(owner, return_exceptions=True)
            if not any(ticket.key.source is RequiredWorkSource.LEASE_RELEASE for ticket in coordinator.tickets):
                service.session_operation_authority.release(lease.context)
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_fault", (False, True))
async def test_owned_join_retains_three_exact_originals_and_child_outcome(monkeypatch, child_fault):
    from elspeth.web.sessions.composer_async_worker import _join_owned

    release = asyncio.Event()
    original_error = RuntimeError("actual owned child outcome")
    actual_value = object()
    observed = []
    caught = []
    original_shield = asyncio.shield
    owner = None

    async def child():
        await release.wait()
        if child_fault:
            raise original_error
        return actual_value

    async def observe(awaitable):
        try:
            return await original_shield(awaitable)
        except asyncio.CancelledError as original:
            if asyncio.current_task() is owner:
                caught.append(original)
            raise

    monkeypatch.setattr(asyncio, "shield", observe)
    actual_child = asyncio.create_task(child())
    owner = asyncio.create_task(_join_owned(actual_child, cancellation_observations=observed))
    await asyncio.sleep(0)
    async with asyncio.timeout(5):
        for index in range(1, 4):
            owner.cancel(object())
            while len(caught) < index:
                await asyncio.sleep(0)
            assert not actual_child.done() and not owner.done()
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert actual_child.done() and len(observed) == 3
    assert all(actual is original for actual, original in zip(observed, caught, strict=True))
    if child_fault:
        roots = _originals(owner.exception())
        assert any(error is original_error for error in roots)
        assert all(any(error is original for error in roots) for original in caught)
    else:
        assert owner.result() is actual_value
