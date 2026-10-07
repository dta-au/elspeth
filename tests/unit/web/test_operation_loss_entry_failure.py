"""Actual failed-before-entry Task must end the selected cleanup handshake."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_async_worker import (
    _finish_operation_watcher,
    _OperationLossWatchOwner,
    _OperationWatchObservation,
)


@pytest.mark.asyncio
async def test_actual_loss_task_failure_before_entry_receipt_preserves_original():
    original = AuditIntegrityError("controlled actual producer entry failed")
    receipt = _OperationLossWatchOwner(asyncio.Event())

    async def actual_failed_entry():
        raise original

    task = asyncio.create_task(actual_failed_entry())
    receipt.bind_returned_task(task)
    # The real producer has already exited before its entry receipt. Consume
    # its original once here; this is an ordinary exception, not a canceled
    # Task whose repeated result() could manufacture another cancellation.
    await asyncio.gather(task, return_exceptions=True)
    assert task.done() and not receipt.entered.is_set()
    observation = _OperationWatchObservation(child_loop_cancellations=[], child_owner=receipt)
    joining = asyncio.create_task(_finish_operation_watcher(task, observation))
    scheduled_turn = asyncio.Event()
    asyncio.get_running_loop().call_soon(scheduled_turn.set)
    try:
        # A selected helper with an already-done actual producer has no
        # legitimate suspension. The callback is queued after its first Task
        # turn. An Event-only mutant suspends and reaches this exact assertion
        # still pending: timeout/forced process death is not the red oracle.
        await scheduled_turn.wait()
        assert joining.done(), "Completed actual producer was ignored behind an unissued entry receipt"
        errors = joining.result()
        assert task.done()
        assert not receipt.entered.is_set()
        assert len(errors) == 1 and errors[0] is original
        assert task.cancelling() == 0
    finally:
        if not joining.done():
            # Test-only mutant cleanup: its actual producer has already failed
            # before entry. Open the incorrectly required Event after the red
            # assertion, so the faulty waiter is joined without canceling it.
            # This does not represent a production producer-entry receipt.
            receipt.entered.set()
        await joining
