"""V2 adoption keeps the started owner until an explicit disposition."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import uuid4

import pytest
from tests.unit.web.coordination.test_session_operation_lifecycle import _FakeAuthority

from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from elspeth.web.sessions.protocol import SessionOperationAuthority


def _adoption(authority: _FakeAuthority) -> ContinuationLeaseAdoption:
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, authority.context, str(uuid4()), 1))
    return ContinuationLeaseAdoption(
        cast(SessionOperationAuthority, authority), authority.context, lease_seconds=30, required_work=coordinator
    )


@pytest.mark.asyncio
async def test_continuation_adoption_failure_retains_fence_without_implicit_release() -> None:
    authority = _FakeAuthority()
    original = RuntimeError("controlled CAS failure")
    authority.compare_and_swap_error = original
    adoption = _adoption(authority)
    try:
        with pytest.raises(RuntimeError) as raised:
            await adoption.join()
        assert raised.value is original
        assert authority.release_calls == []
        assert adoption.lease is not None and not adoption.lease.closed
        assert adoption.outcome is not None
    finally:
        assert adoption.lease is not None
        await adoption.lease.close()


@pytest.mark.asyncio
async def test_cancelled_continuation_adoption_joins_actual_cas_without_releasing() -> None:
    authority = _FakeAuthority()
    authority.compare_and_swap_allowed.clear()
    adoption = _adoption(authority)
    joining = asyncio.create_task(adoption.join())
    while not authority.compare_and_swap_started.is_set():
        await asyncio.sleep(0)
    joining.cancel("controlled post-start cancellation")
    await asyncio.sleep(0)
    assert not joining.done()
    authority.compare_and_swap_allowed.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await joining
        assert authority.compare_and_swap_finished.is_set()
        assert authority.release_calls == []
        assert adoption.cancellations
        assert adoption.lease is not None and not adoption.lease.closed
    finally:
        authority.compare_and_swap_allowed.set()
        assert adoption.lease is not None
        await adoption.lease.close()


@pytest.mark.asyncio
async def test_continuation_stage_transfers_the_original_producer_cancellation() -> None:
    adoption = _adoption(_FakeAuthority())
    lease = await adoption.join()
    original = asyncio.CancelledError("actual phase cancellation")

    async def phase() -> None:
        raise original

    try:
        stage = lease.create_continuation_stage(phase(), name="test-original-continuation-phase")
        with pytest.raises(asyncio.CancelledError) as raised:
            await lease.join_continuation_stage(stage)
        assert raised.value is original
        assert stage.original is original and stage.observed
    finally:
        await lease.close()


@pytest.mark.asyncio
async def test_continuation_finish_timeout_retains_one_real_close_until_its_child_finishes() -> None:
    authority = _FakeAuthority()
    adoption = _adoption(authority)
    lease = await adoption.join()
    release = asyncio.Event()

    async def phase() -> None:
        await release.wait()

    lease.create_continuation_stage(phase(), name="test-retained-continuation-phase")
    deadline = ComposerDrainDeadline.from_start(started_at=asyncio.get_running_loop().time() - 1, drain_seconds=0.1)
    try:
        with pytest.raises(TimeoutError, match="still owns work"):
            await lease.close(deadline=deadline)
        close = lease._close_task
        assert close is not None and not close.done()
        assert authority.release_calls == [] and not lease.continuation_finished
    finally:
        release.set()
        await lease.close()
    assert lease._close_task is close
    assert authority.release_calls == [authority.context]
    assert lease.continuation_finished


@pytest.mark.asyncio
async def test_failed_continuation_cleanup_remains_unknown_instead_of_complete() -> None:
    authority = _FakeAuthority()
    adoption = _adoption(authority)
    lease = await adoption.join()
    authority.release_error = RuntimeError("controlled physical release failure")
    with pytest.raises(RuntimeError) as first:
        await lease.close()
    assert first.value is authority.release_error
    assert not lease.continuation_finished
    retained = lease._close_task
    with pytest.raises(RuntimeError) as second:
        await lease.close()
    assert second.value is authority.release_error
    assert lease._close_task is retained
    assert len(authority.release_attempts) == 1
