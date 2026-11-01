"""Worker owners that serve every Composer operation: owned joins and shutdown latching.

Moved unchanged from the deleted V1 worker suites
(``test_worker_owned_sql_cancellations.py`` and
``test_composer_worker_historical_controls.py``); the Continuation driver uses
the same ``_join_owned`` and ``ProcessRecovery`` owners.
"""

from __future__ import annotations

import asyncio
import signal
import threading

import pytest

from elspeth.web.process_recovery import ProcessRecovery
from elspeth.web.process_watchdog import ProcessWatchdogFailure
from elspeth.web.process_watchdog_codec import RecoveryReason
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog


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


@pytest.mark.asyncio
@pytest.mark.parametrize("watchdog_available", (True, False))
async def test_begin_shutdown_latches_before_ack_and_is_idempotent(watchdog_available: bool) -> None:
    draining = threading.Event()
    watchdog = OwnedTestProcessWatchdog(draining)
    recovery = ProcessRecovery(watchdog=watchdog, instance_draining=draining)
    if not watchdog_available:
        watchdog.completed = True
    assert not draining.is_set()
    recovery.begin_shutdown()
    assert draining.is_set()
    assert not watchdog.signals
    escalation = recovery._escalation_task
    recovery.begin_shutdown()
    assert recovery._escalation_task is escalation
    if watchdog_available:
        await recovery.join_escalation()
        assert watchdog.reasons == [RecoveryReason.NORMAL_SHUTDOWN]
        assert not watchdog.signals
    else:
        with pytest.raises(ProcessWatchdogFailure):
            await recovery.join_escalation()
        assert watchdog.signals == [signal.SIGKILL]
    assert draining.is_set()
