"""Replacement cannot discard the old generation's unfinished observer."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.web import async_workers
from elspeth.web.required_executor import RequiredExecutorGenerationCustodian
from tests.unit.web.test_required_executor_custody import QueueThenRaiseExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_fails", [False, True])
async def test_shutdown_joins_replaced_generation_observer_with_original_cancellations(monkeypatch, observer_fails: bool) -> None:
    original_observe = RequiredExecutorGenerationCustodian._observe
    retained = asyncio.Event()
    release = asyncio.Event()
    failure = LookupError("old generation observer fault")

    async def held_observe(generation):
        await original_observe(generation)
        retained.set()
        await release.wait()
        if observer_fails:
            raise failure

    monkeypatch.setattr(RequiredExecutorGenerationCustodian, "_observe", held_observe)
    executor = QueueThenRaiseExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    with pytest.raises(RuntimeError, match="submit failed after queueing"):
        await async_workers.run_sync_in_worker(lambda: pytest.fail("aborted worker executed"))
    old = async_workers._GENERATION_CUSTODIAN
    assert old is not None
    await retained.wait()
    assert old.joined.is_set() and old.drain_thread is not None and not old.drain_thread.is_alive()
    assert old.observer is not None and not old.observer.done()
    assert await async_workers.run_sync_in_worker(lambda: 7) == 7
    assert async_workers._GENERATION_CUSTODIAN is not old
    shutdown = asyncio.create_task(async_workers.shutdown_async_workers())
    try:
        await asyncio.sleep(0.05)
        assert not shutdown.done(), "executor futures joined but old recovery observer is still owned"
        shutdown.cancel("first caller cancellation")
        await asyncio.sleep(0.02)
        shutdown.cancel("second caller cancellation")
        await asyncio.sleep(0.02)
        assert not shutdown.done()
        release.set()
        with pytest.raises(BaseException) as raised:
            await shutdown

        def originals(error):
            if isinstance(error, BaseExceptionGroup):
                return [item for nested in error.exceptions for item in originals(nested)]
            return [error]

        outcomes = originals(raised.value)
        assert [str(error) for error in outcomes if isinstance(error, asyncio.CancelledError)] == [
            "first caller cancellation",
            "second caller cancellation",
        ]
        assert (failure in outcomes) is observer_fails
        assert old.observer.done() and not old.drain_thread.is_alive()
        assert not async_workers._RECOVERING_GENERATIONS
    finally:
        release.set()
        await asyncio.gather(old.observer, return_exceptions=True)
        if not shutdown.done():
            await shutdown
