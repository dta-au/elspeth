"""Preselection SQL failure retains an exact unresolved failure handoff."""

from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

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
        if error is None or any(error is earlier for earlier in seen):
            continue
        seen.append(error)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_selection_read", (False, True))
async def test_actual_selection_read_outcome_before_failure_projection(tmp_path, monkeypatch, fail_selection_read):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    original_get_now = authority.get_with_database_now
    armed = threading.Event()
    body_error = RuntimeError("owned body fault")
    read_error = OperationalError("actual selection read", {}, RuntimeError("retained physical driver fault"))
    observations = []
    holders = []

    def actual_read_then_original_failure(*, session_id, operation_id):
        actual = original_get_now(session_id=session_id, operation_id=operation_id)
        if armed.is_set():
            observations.append(actual)
            if fail_selection_read:
                raise read_error
        return actual

    async def fail_inside_actual_live_lease(services, running, lease, settlement, setup_ticket):
        owner = asyncio.current_task()
        assert owner is not None and setup_ticket is not None and lease.required_work is not None
        holders.append((owner, lease, lease.required_work))
        setup_ticket.complete_owned(body_error)
        settlement.failure_attempted = True
        armed.set()
        try:
            terminal = await worker._settle_failure(services, running, body_error)
        except BaseException as failure:
            raise BaseExceptionGroup("Original body and selection outcome", [body_error, failure]) from None
        settlement.terminal_receipt = terminal
        raise body_error

    monkeypatch.setattr(authority, "get_with_database_now", actual_read_then_original_failure)
    monkeypatch.setattr(worker, "_run_started_under_lease", fail_inside_actual_live_lease)
    try:
        admitted = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            while not holders:
                await asyncio.sleep(0.001)
            owner, lease, coordinator = holders[0]
            await asyncio.gather(owner, return_exceptions=True)
        armed.clear()
        assert observations[0][0].status == "running"
        if fail_selection_read:
            assert len(observations) == 1
        else:
            assert len(observations) == 2
            assert observations[1][0].status == "failed"
        assert composer.calls == 0
        originals = _originals(owner.exception())
        assert any(error is body_error for error in originals)
        projections = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION]
        releases = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.LEASE_RELEASE]
        assert len(projections) == 1
        record, verification_now = original_get_now(session_id=admitted.session_id, operation_id=admitted.operation_id)
        assert verification_now >= observations[0][1]
        assert record is not None
        if fail_selection_read:
            assert any(error is read_error for error in originals)
            assert any(isinstance(error, ComposerTerminalSQLCompletionUnknown) for error in originals)
            assert record.status == "running" and record.result_json is None
            assert not projections[0].complete and not releases
            assert all(ticket.key.source is not RequiredWorkSource.TERMINAL_FAILURE_SQL for ticket in coordinator.tickets)
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
            assert record.status == "failed" and record.result_json is not None
            assert projections[0].complete and len(releases) == 1 and releases[0].complete
            assert lease.closed and lease._renewal_task.done()
    finally:
        if fail_selection_read and holders:
            service.session_operation_authority.release(holders[0][1].context)
        engine.dispose()
