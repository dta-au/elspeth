"""Additive, opt-in actual Task controls for core3's observation helper.

Run one node at a time under the external finite process owner. These controls
do not replace the unchanged 25-case core module.
"""

from __future__ import annotations

import asyncio
from importlib import import_module

import pytest


def _observer():
    return import_module("tests.unit.web.test_execute_lease_cleanup_core")._observe_core_task


@pytest.mark.asyncio
async def test_owned_task_healthy_and_failed_outcomes() -> None:
    observe = _observer()

    async def healthy() -> None:
        return None

    healthy_task = asyncio.create_task(healthy())
    healthy_originals: list[BaseException] = []
    assert await observe(healthy_task, healthy_originals)
    assert healthy_task.done() and not healthy_task.cancelled()
    assert healthy_task.result() is None
    assert healthy_originals == []

    producer_original = RuntimeError("actual owned Task producer failure")

    async def failed() -> None:
        raise producer_original

    failed_task = asyncio.create_task(failed())
    failed_originals: list[BaseException] = []
    assert await observe(failed_task, failed_originals)
    assert failed_task.done() and not failed_task.cancelled()
    assert len(failed_originals) == 1 and failed_originals[0] is producer_original
    with pytest.raises(RuntimeError) as raised:
        failed_task.result()
    assert raised.value is producer_original


@pytest.mark.asyncio
async def test_owned_task_producer_cancel_is_one_producer_outcome() -> None:
    observe = _observer()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def producer() -> None:
        entered.set()
        await release.wait()

    child = asyncio.create_task(producer())
    await entered.wait()
    child.cancel("actual producer cancellation")
    originals: list[BaseException] = []
    try:
        assert await observe(child, originals)
        assert child.done() and child.cancelled()
        assert len(originals) == 1
        assert type(originals[0]) is asyncio.CancelledError
    finally:
        release.set()
        await asyncio.gather(child, return_exceptions=True)


@pytest.mark.asyncio
async def test_three_delivered_caller_cancellations_keep_child_pending() -> None:
    core = import_module("tests.unit.web.test_execute_lease_cleanup_core")
    observe = core._observe_core_task
    entered = asyncio.Event()
    release = asyncio.Event()

    async def producer() -> None:
        entered.set()
        await release.wait()

    child = asyncio.create_task(producer())
    await entered.wait()
    originals: list[BaseException] = []
    actual_wait = core.asyncio.wait
    wait_entries = []

    async def traced_wait(fs, *args, **kwargs):
        wait_entries.append(fs)
        return await actual_wait(fs, *args, **kwargs)

    # The wrapper forwards the one actual wait and observes its entry. It does
    # not create a substitute completion receipt or suppress an exception.
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(core.asyncio, "wait", traced_wait)
    observing = asyncio.create_task(observe(child, originals))
    try:
        for index in range(3):
            entry_deadline = asyncio.get_running_loop().time() + 2.0
            while len(wait_entries) < index + 1:
                if asyncio.get_running_loop().time() >= entry_deadline:
                    raise AssertionError("Actual asyncio.wait entry was not reached")
                await asyncio.sleep(0)
            assert wait_entries[index] == {child}
            observing.cancel(f"caller delivery {index}")
            deadline = asyncio.get_running_loop().time() + 2.0
            while len(originals) < index + 1:
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("Actual caller cancellation was not delivered")
                await asyncio.sleep(0)
            assert len(originals) == index + 1
            assert type(originals[index]) is asyncio.CancelledError
            assert all(originals[index] is not earlier for earlier in originals[:index])
            assert not child.done() and not observing.done()
    finally:
        monkeypatch.undo()
        release.set()
        # No cancellation of the child or observing Task is used to force exit.
        observed = await observing
        await child
    assert observed
    assert child.done() and child.result() is None
    assert len(originals) == 3


@pytest.mark.asyncio
async def test_wait_boundary_original_retained_while_child_pending(monkeypatch) -> None:
    core = import_module("tests.unit.web.test_execute_lease_cleanup_core")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def producer() -> None:
        entered.set()
        await release.wait()

    child = asyncio.create_task(producer())
    await entered.wait()
    wait_original = RuntimeError("controlled actual asyncio.wait boundary failure")
    actual_wait = core.asyncio.wait
    calls = []

    async def fail_wait(fs, *args, **kwargs):
        calls.append((fs, args, kwargs))
        raise wait_original

    originals: list[BaseException] = []
    try:
        monkeypatch.setattr(core.asyncio, "wait", fail_wait)
        assert not await core._observe_core_task(child, originals)
        assert len(calls) == 1 and calls[0][0] == {child}
        assert originals == [wait_original]
        assert not child.done()
    finally:
        monkeypatch.setattr(core.asyncio, "wait", actual_wait)
        release.set()
        await child
    assert child.done() and child.result() is None


@pytest.mark.asyncio
async def test_done_child_before_caller_delivery_keeps_both_originals(monkeypatch) -> None:
    core = import_module("tests.unit.web.test_execute_lease_cleanup_core")
    actual_wait = core.asyncio.wait
    entered_actual_wait = asyncio.Event()
    after_actual_wait = asyncio.Event()
    release_wait_return = asyncio.Event()
    release_failure = asyncio.Event()
    producer_original = RuntimeError("actual producer failed before caller delivery")

    async def failed() -> None:
        await release_failure.wait()
        raise producer_original

    child = asyncio.create_task(failed())

    async def held_return(fs, *args, **kwargs):
        assert fs == {child}
        entered_actual_wait.set()
        result = await actual_wait(fs, *args, **kwargs)
        after_actual_wait.set()
        await release_wait_return.wait()
        return result

    monkeypatch.setattr(core.asyncio, "wait", held_return)
    originals: list[BaseException] = []
    observing = asyncio.create_task(core._observe_core_task(child, originals))
    try:
        # The real observer must enter its forwarding wait while this exact
        # producer is still pending. An entry failure is a finite failure,
        # never an outer parent timeout masquerading as a causal result.
        await asyncio.wait_for(entered_actual_wait.wait(), timeout=2.0)
        assert not child.done() and not observing.done()
        release_failure.set()
        await asyncio.wait_for(after_actual_wait.wait(), timeout=2.0)
        assert child.done() and not observing.done()
        observing.cancel("caller delivery after actual child completion")
        assert await asyncio.wait_for(observing, timeout=2.0)
        assert len(originals) == 2
        assert type(originals[0]) is asyncio.CancelledError
        assert originals[1] is producer_original
    finally:
        release_failure.set()
        release_wait_return.set()
        monkeypatch.setattr(core.asyncio, "wait", actual_wait)
        if observing.done():
            await asyncio.gather(observing, return_exceptions=True)
        else:
            await asyncio.wait_for(observing, timeout=2.0)
        await asyncio.gather(child, return_exceptions=True)
    assert child.done()
