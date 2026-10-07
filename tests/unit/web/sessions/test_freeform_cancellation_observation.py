"""Original cancellation observations survive a failed owned freeform child."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.sessions.routes._helpers import _join_freeform_owned_task


@pytest.mark.asyncio
@pytest.mark.parametrize("child_fails", (False, True))
async def test_actual_owned_join_observes_three_original_cancellations_before_child_outcome(monkeypatch, child_fails):
    release = asyncio.Event()
    joining = asyncio.Event()
    original_error = InvariantError("actual owned child invariant")
    actual_value = object()
    observed = []
    caught = []
    original_shield = asyncio.shield
    owner = None

    async def child():
        await release.wait()
        if child_fails:
            raise original_error
        return actual_value

    async def observe_shield(awaitable):
        try:
            return await original_shield(awaitable)
        except asyncio.CancelledError as original:
            if asyncio.current_task() is owner:
                caught.append(original)
            raise

    async def join(actual_child):
        joining.set()
        return await _join_freeform_owned_task(actual_child, cancellation_observations=observed)

    monkeypatch.setattr(asyncio, "shield", observe_shield)
    actual_child = asyncio.create_task(child())
    owner = asyncio.create_task(join(actual_child))
    await joining.wait()
    try:
        async with asyncio.timeout(5):
            for index in range(1, 4):
                marker = object()
                owner.cancel(marker)
                while len(caught) < index:
                    await asyncio.sleep(0)
                assert caught[index - 1].args == (marker,)
                assert not owner.done() and not actual_child.done()
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
        assert actual_child.done()
        if child_fails:
            assert owner.exception() is original_error
        else:
            value, first = owner.result()
            assert value is actual_value and first is caught[0]
        assert len(observed) == 3 and all(actual is original for actual, original in zip(observed, caught, strict=True))
    finally:
        release.set()
        await asyncio.gather(owner, actual_child, return_exceptions=True)


@pytest.mark.asyncio
async def test_child_self_cancellation_is_not_an_observed_owner_cancellation():
    observed = []
    original_child_cancel = asyncio.CancelledError("actual self-cancelled child")

    async def child():
        raise original_child_cancel

    actual_child = asyncio.create_task(child())
    with pytest.raises(AuditIntegrityError) as failure:
        await _join_freeform_owned_task(actual_child, cancellation_observations=observed)
    assert observed == [] and actual_child.cancelled()
    assert isinstance(failure.value.__cause__, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_default_join_tuple_and_original_child_fault_are_unchanged():
    actual_value = object()
    original_error = InvariantError("actual child error without caller cancellation")

    async def successful_child():
        return actual_value

    async def failed_child():
        raise original_error

    value, cancelled = await _join_freeform_owned_task(asyncio.create_task(successful_child()))
    assert value is actual_value and cancelled is None
    with pytest.raises(InvariantError) as failure:
        await _join_freeform_owned_task(asyncio.create_task(failed_child()))
    assert failure.value is original_error
