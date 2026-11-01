"""An already-failed watcher must end the current cleanup handshake."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_async_worker import (
    _finish_operation_watcher,
    _OperationWatchObservation,
)


@pytest.mark.asyncio
async def test_actual_watcher_failure_before_join_preserves_original():
    original = AuditIntegrityError("controlled actual producer entry failed")

    async def actual_failed_entry():
        raise original

    task = asyncio.create_task(actual_failed_entry())
    # Retired: the deleted entry-receipt carrier is not part of the current watcher join.
    # The real watcher has already failed before the finisher is scheduled.
    await asyncio.gather(task, return_exceptions=True)
    assert task.done()
    observation = _OperationWatchObservation()
    joining = asyncio.create_task(_finish_operation_watcher(task, observation))
    scheduled_turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(scheduled_turn.set)
    try:
        # An already-done watcher has no legitimate suspension: its result is
        # available before this callback, which is queued after the first turn.
        await scheduled_turn.wait()
        assert joining.done(), "Completed actual watcher was ignored by cleanup"
        errors = joining.result()
        assert task.done()
        assert len(errors) == 1 and errors[0] is original
        assert task.cancelling() == 0
    finally:
        if not joining.done():
            joining.cancel()
        await asyncio.gather(joining, return_exceptions=True)
