"""Actual returned/readback equality cannot replace exact running authority."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown
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
@pytest.mark.parametrize("mutation", ("attempt", "fence_id", "fence_epoch", "sql_raised", "hash"))
async def test_unverified_terminal_keeps_projection_and_actual_fence_owned(tmp_path, monkeypatch, mutation):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    original_under_lease = worker._run_started_under_lease
    original_get = authority.get
    holders = []
    actual_terminals = []
    returned = []
    readback = []
    sql_error = OperationalError("owned terminal readback", {}, RuntimeError("actual retained SQL driver"))
    foreign_id = str(uuid4())

    def corrupt_authority(record):
        if mutation == "attempt":
            return replace(record, attempt=record.attempt + 1)
        if mutation == "fence_id":
            return replace(record, session_operation_id=foreign_id)
        if mutation == "fence_epoch":
            return replace(record, session_operation_epoch=record.session_operation_epoch + 1)
        return record

    async def actual_turn_then_corrupt_return(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        terminal = await original_under_lease(services, running, lease, settlement, setup_ticket)
        actual_terminals.append(terminal)
        projected = corrupt_authority(terminal)
        returned.append(projected)
        return projected

    def actual_sql_then_corrupt_readback(*, session_id, operation_id):
        record = original_get(session_id=session_id, operation_id=operation_id)
        assert record is not None and record.status == "completed"
        if mutation == "sql_raised":
            raise sql_error
        projected = corrupt_authority(record)
        if mutation == "hash":
            projected = replace(projected)
            # Controlled corruption of a nominal owned frozen DTO: constructor
            # revalidation must reject it before any durable witness dispatch.
            object.__setattr__(projected, "result_sha256", "0" * 64)
        readback.append(projected)
        return projected

    monkeypatch.setattr(worker, "_run_started_under_lease", actual_turn_then_corrupt_return)
    monkeypatch.setattr(authority, "get", actual_sql_then_corrupt_readback)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            while not holders:
                await asyncio.sleep(0.001)
            owner, lease, coordinator = holders[0]
            await asyncio.gather(owner, return_exceptions=True)
        original = original_get(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert original is not None and original.status == "completed"
        assert composer.calls == 1 and len(actual_terminals) == len(returned) == 1
        assert original.result_json == actual_terminals[0].result_json
        assert original.result_sha256 == actual_terminals[0].result_sha256
        if mutation in ("attempt", "fence_id", "fence_epoch"):
            assert len(readback) == 1
            assert returned[0].attempt == readback[0].attempt
            assert returned[0].session_operation_id == readback[0].session_operation_id
            assert returned[0].session_operation_epoch == readback[0].session_operation_epoch
        originals = _originals(owner.exception())
        assert any(isinstance(error, ComposerTerminalSQLCompletionUnknown) for error in originals)
        if mutation == "sql_raised":
            assert any(error is sql_error for error in originals)
        else:
            assert any(isinstance(error, AuditIntegrityError) for error in originals)
        sql = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_SQL]
        projection = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION]
        assert len(sql) == len(projection) == 1
        assert sql[0].complete and sql[0]._future is not None and sql[0]._future.done()
        assert not projection[0].complete
        assert all(
            ticket.key.source not in (RequiredWorkSource.LEASE_RELEASE, RequiredWorkSource.TERMINAL_FAILURE_SQL)
            for ticket in coordinator.tickets
        )
        with engine.connect() as connection:
            fence = connection.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == lease.context.fence.session_id)
            ).one()
        assert fence.operation_id == lease.context.fence.operation_id
        assert fence.lease_token == lease.context.fence.lease_token
        assert fence.released_at is None
        assert original_get(session_id=admitted.session_id, operation_id=admitted.operation_id) == original
    finally:
        if holders:
            owner, lease, _coordinator = holders[0]
            if not owner.done():
                await asyncio.gather(owner, return_exceptions=True)
            # Test-owned cleanup after all actual physical work completed.
            service.session_operation_authority.release(lease.context)
        engine.dispose()
