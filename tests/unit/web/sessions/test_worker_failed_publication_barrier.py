"""Actual failed-publication SQL is not an authoritative terminal witness."""

from __future__ import annotations

import asyncio
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
@pytest.mark.parametrize("permanent_update_fault", (False, True))
async def test_failed_publication_requires_known_terminal_before_last_release(tmp_path, monkeypatch, permanent_update_fault):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    body_error = RuntimeError("owned setup fault")
    holders = []
    failed_updates = []

    async def fail_actual_owned_setup(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and setup_ticket is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        setup_ticket.complete_owned(body_error)
        raise body_error

    def observe_actual_update(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is None:
            return
        actual = compiled.statement
        if not isinstance(actual, Update) or actual.table is not composer_async_operations_table:
            return
        values = context.compiled_parameters
        if len(values) != 1 or values[0].get("status") != "failed":
            return
        if permanent_update_fault:
            failure = OperationalError("retained failed-terminal UPDATE", {}, RuntimeError("physical SQL fault"))
            failed_updates.append(failure)
            raise failure

    monkeypatch.setattr(worker, "_run_started_under_lease", fail_actual_owned_setup)
    event.listen(engine, "before_cursor_execute", observe_actual_update)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            while not holders:
                await asyncio.sleep(0.001)
            owner, lease, coordinator = holders[0]
            await asyncio.gather(owner, return_exceptions=True)
        record = authority.get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert record is not None and composer.calls == 0
        failures = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL]
        projections = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        releases = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(projections) == 1
        assert any(error is body_error for error in _originals(owner.exception()))
        if permanent_update_fault:
            assert len(failed_updates) == 2
            assert len(failures) == 2 and all(ticket.complete for ticket in failures)
            assert all(ticket._future is not None and ticket._future.done() for ticket in failures)
            assert all(any(error is original for error in _originals(owner.exception())) for original in failed_updates)
            assert record.status == "running" and record.result_json is None
            assert not projections[0].complete and not releases
            # The actual SQL authority remains held, not merely a local boolean.
            with engine.connect() as connection:
                fence = connection.execute(
                    select(session_operation_fences_table).where(
                        session_operation_fences_table.c.session_id == lease.context.fence.session_id
                    )
                ).one()
            assert fence.operation_id == lease.context.fence.operation_id
            assert fence.lease_token == lease.context.fence.lease_token
            assert fence.released_at is None
        else:
            assert failed_updates == []
            assert record.status == "failed" and record.result_json is not None
            assert len(failures) == 1 and failures[0].complete
            assert projections[0].complete
            assert len(releases) == 1 and releases[0].complete
            assert lease.closed and lease._renewal_task.done()
    finally:
        event.remove(engine, "before_cursor_execute", observe_actual_update)
        if permanent_update_fault and holders:
            # Only after all actual Futures are joined: test-owned DB cleanup.
            # This does not complete the deliberately pending projection.
            lease = holders[0][1]
            if not any(ticket.key.source is RequiredWorkSource.LEASE_RELEASE for ticket in holders[0][2].tickets):
                service.session_operation_authority.release(lease.context)
        engine.dispose()
