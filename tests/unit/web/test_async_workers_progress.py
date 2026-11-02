"""Exact Progress SQL admission cutoffs before native submission."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from time import monotonic

import pytest

from elspeth.web import async_workers
from elspeth.web.async_workers import (
    ProgressCleanupDeadlineExpired,
    ProgressRequestCleanup,
    ProgressSQLDeadlineExpired,
    ProgressSQLInvocation,
)
from elspeth.web.composer.progress import ComposerRequestLease
from elspeth.web.coordination.composer_progress_authority import SessionComposerProgressAuthority


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["begin", "cleanup"])
async def test_progress_expiry_after_admission_wait_never_arms_sql(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    # The exact authority method is present, but no engine is installed: any
    # actual invocation would fail this no-submission control immediately.
    authority = object.__new__(SessionComposerProgressAuthority)
    entered, release = asyncio.Event(), asyncio.Event()
    actual_admission = async_workers._acquire_admission
    baseline = async_workers.outstanding_admissions()

    async def parked_admission() -> None:
        entered.set()
        await release.wait()
        await actual_admission()

    monkeypatch.setattr(async_workers, "_acquire_admission", parked_admission)
    expiry = [monotonic() + 10]
    if kind == "begin":
        invocation = ProgressSQLInvocation(authority.begin_request, ("session", "owner"))
        run = asyncio.create_task(async_workers.run_progress_sql_in_worker(invocation, deadline=lambda: expiry[0]))
    else:
        cleanup = ProgressRequestCleanup(authority.end_request, ComposerRequestLease("token", "session", "owner"))
        run = asyncio.create_task(async_workers.run_progress_cleanup_in_worker(cleanup, deadline=lambda: expiry[0]))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
        expiry[0] = monotonic() - 1
        release.set()
        if kind == "begin":
            with pytest.raises(ProgressSQLDeadlineExpired) as caught:
                await run
            assert caught.value.invocation is invocation and not caught.value.native_pending
            assert invocation.future is None and invocation.generation is None
        else:
            with pytest.raises(ProgressCleanupDeadlineExpired) as caught:
                await run
            assert caught.value.cleanup is cleanup and not caught.value.native_pending
            assert cleanup.future is None and cleanup.generation is None
        assert async_workers.outstanding_admissions() == baseline
    finally:
        release.set()
        await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["begin", "cleanup"])
@pytest.mark.parametrize("scenario", ["expiry", "cancellation"])
async def test_progress_completion_winning_cancel_race_does_not_quarantine(
    monkeypatch: pytest.MonkeyPatch, kind: str, scenario: str
) -> None:
    authority = object.__new__(SessionComposerProgressAuthority)
    expiry = [monotonic() + 10]
    first_done = asyncio.Event()
    actual_lease = ComposerRequestLease("token", "session", "owner")

    class CompleteAtCancel(Future[object]):
        def done(self) -> bool:
            if not first_done.is_set():
                first_done.set()
                if scenario == "expiry":
                    expiry[0] = monotonic() - 1
                return False
            return super().done()

        def cancel(self) -> bool:
            self.set_result(actual_lease if kind == "begin" else None)
            return False

    future = CompleteAtCancel()

    async def submit(_callable, **_kwargs):
        return future

    monkeypatch.setattr(async_workers, "_submit_shared", submit)
    if kind == "begin":
        invocation = ProgressSQLInvocation(authority.begin_request, ("session", "owner"))
        operation = asyncio.create_task(async_workers.run_progress_sql_in_worker(invocation, deadline=lambda: expiry[0]))
    else:
        cleanup = ProgressRequestCleanup(authority.end_request, actual_lease)
        operation = asyncio.create_task(async_workers.run_progress_cleanup_in_worker(cleanup, deadline=lambda: expiry[0]))
    async with asyncio.timeout(5):
        await first_done.wait()
    if scenario == "cancellation":
        operation.cancel("original Progress cancellation")
        with pytest.raises(asyncio.CancelledError, match="original Progress cancellation"):
            await operation
    elif kind == "begin":
        assert await operation is actual_lease
    else:
        assert await operation is None
    assert future.done()
