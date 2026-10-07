"""Exact orphan completion and original caller cancellation during teardown."""

import asyncio

import pytest

from elspeth.web.app import _join_cancelled_orphan_task


@pytest.mark.asyncio
async def test_orphan_join_retains_repeated_caller_cancellations_and_actual_child_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep
    originals: list[asyncio.CancelledError] = []
    entered = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()
    fault = ValueError("owned local failure")

    async def orphan() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()
            raise fault from None

    async def recording_sleep(delay: float) -> None:
        try:
            await real_sleep(delay)
        except asyncio.CancelledError as original:
            originals.append(original)
            raise

    task = asyncio.create_task(orphan())
    await entered.wait()
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    join = asyncio.create_task(_join_cancelled_orphan_task(task))
    await stopping.wait()
    for count in range(2):
        join.cancel()
        while len(originals) <= count:
            await real_sleep(0.001)
        assert not join.done()
        assert not task.done()
    release.set()
    retained = await join
    assert retained == [*originals, fault]
    assert retained[0] is originals[0]
    assert retained[1] is originals[1]
    assert retained[2] is fault
    assert task.done()


@pytest.mark.asyncio
async def test_expected_owned_orphan_cancellation_is_not_a_caller_failure() -> None:
    entered = asyncio.Event()

    async def orphan() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(orphan())
    await entered.wait()
    assert await _join_cancelled_orphan_task(task) == []
    assert task.cancelled()


@pytest.mark.asyncio
async def test_completed_orphan_fault_is_retained_without_replacement() -> None:
    fault = ValueError("owned completed failure")

    async def orphan() -> None:
        raise fault

    task = asyncio.create_task(orphan())
    await asyncio.sleep(0)
    retained = await _join_cancelled_orphan_task(task)
    assert retained == [fault]
    assert retained[0] is fault
