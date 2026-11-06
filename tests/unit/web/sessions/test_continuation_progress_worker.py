"""Actual segment owners with the existing registry, before live wiring.

These controls do not claim worker/HTTP/PG progress integration. Root owns
native admission; the shared driver and drain-cleanup joins remain separate.
"""

import asyncio
from concurrent.futures import Future
from time import monotonic

import pytest

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import ProgressCleanupDeadlineExpired, ProgressRequestCleanup, ProgressSQLInvocation
from elspeth.web.composer.progress import ComposerProgressRegistry, ComposerRequestLease
from elspeth.web.coordination.composer_progress_authority import ComposerRequestLeaseLost, SessionComposerProgressAuthority
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_drain import ComposerDrainDeadline
from elspeth.web.sessions.continuation_progress_owner import ContinuationProgressOwner, ProgressCloseDeadlineExpired
from tests.unit.web.sessions import test_continuation_live_phases as phase_cases
from tests.unit.web.sessions.test_continuation_live_phases import _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


@pytest.fixture(autouse=True)
def _priced_phase_response(monkeypatch):
    """Supply real SDK usage cost for the inherited scripted PRIMARY result."""
    actual_response = phase_cases.ModelResponse

    def response(*args, **kwargs):
        usage = kwargs["usage"]
        assert type(usage) is dict and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        return actual_response(*args, **{**kwargs, "usage": {**usage, "cost": 0.01}})

    monkeypatch.setattr(phase_cases, "ModelResponse", response)


def _progress_owner(owner, running, registry, *, interval=60.0, hard_expires_at=None, drain_deadline=None):
    record = owner.authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
    assert record is not None
    return ContinuationProgressOwner(
        registry=registry,
        running=running,
        lease=owner.lease,
        record=record,
        renew_interval_seconds=interval,
        hard_expires_at=hard_expires_at,
        drain_deadline=drain_deadline,
    )


def _cancellations(error):
    if isinstance(error, asyncio.CancelledError):
        return (error,)
    if isinstance(error, BaseExceptionGroup):
        return tuple(item for child in error.exceptions for item in _cancellations(child))
    return ()


@pytest.mark.asyncio
async def test_segment_progress_binds_exact_owner_without_advancing_semantics(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        registry = ComposerProgressRegistry()
        progress = _progress_owner(owner, running, registry)
        before, calls = owner.runtime.source.selected, tuple(sdk)
        deadline = owner.runtime.bindings.deadline
        await progress.start()
        saved_sink = progress.sink
        try:
            for phase in ("starting", "calling_model", "using_tools"):
                await saved_sink(ComposerProgressEvent(phase=phase, headline="Safe current segment progress"))
            fence = running.session_operation_context.fence
            snapshot = await registry.get_for_operation(
                session_id=str(running.claim.session_id),
                user_id=progress.record.actor_user_id,
                operation_id=running.claim.operation_id,
                session_operation_id=fence.operation_id,
                session_operation_epoch=fence.operation_epoch,
            )
            assert snapshot is not None and snapshot.phase == "using_tools"
            assert snapshot.request_id == str(progress.record.reserved_user_message_id)
            assert progress.request_lease is not None and snapshot.request_token == progress.request_lease.request_token
            assert snapshot.inflight_requests == 1
            assert owner.runtime.source.selected is before and owner.runtime.bindings.deadline == deadline
            assert tuple(sdk) == calls
        finally:
            await progress.close()
        assert progress.unregistered and progress.cleanup_failure is None
        await saved_sink(ComposerProgressEvent(phase="complete", headline="A retired writer must not publish"))
        latest = await registry.get_latest(str(running.claim.session_id))
        assert latest.phase == "using_tools" and latest.inflight_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("advisory", (False, True), ids=("native-defect", "advisory-lease-loss"))
async def test_progress_heartbeat_distinguishes_advisory_loss_from_native_defect(operation_store, tmp_path, monkeypatch, advisory):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        registry = ComposerProgressRegistry()
        entered = asyncio.Event()
        cause = RuntimeError("original private cause")
        original = ComposerRequestLeaseLost("actual progress lease loss") if advisory else RuntimeError("actual progress defect")
        original.__cause__ = cause

        async def renew(lease, *, deadline=None):
            assert progress.request_lease is lease
            assert deadline is None
            entered.set()
            raise original

        progress = _progress_owner(owner, running, registry, interval=0.001)
        selected, calls = owner.runtime.source.selected, tuple(sdk)
        current = asyncio.current_task()
        assert current is not None
        cancelling = current.cancelling()
        await progress.start()
        assert progress.request_lease is not None
        monkeypatch.setattr(registry, "renew_request", renew)
        await asyncio.wait_for(entered.wait(), timeout=5)
        if advisory:
            await progress.sink(ComposerProgressEvent(phase="calling_model", headline="Disabled advisory writer"))
            await progress.close()
            assert progress.advisory_failures == [original]
        else:
            with pytest.raises(RuntimeError) as caught:
                await progress.close()
            assert caught.value is original and caught.value.__cause__ is cause
            assert progress.advisory_failures == []
        assert current.cancelling() == cancelling
        assert progress.unregistered and progress.cleanup_failure is None
        assert owner.runtime.source.selected is selected and tuple(sdk) == calls
        assert (await registry.get_latest(str(running.claim.session_id))).inflight_requests == 0


@pytest.mark.asyncio
async def test_progress_retirement_joins_actual_publication_through_repeated_cancellation(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, _sdk):
        registry = ComposerProgressRegistry()
        actual_claim = registry.claim_request
        entered, release = asyncio.Event(), asyncio.Event()

        async def claim(**kwargs):
            actual = await actual_claim(**kwargs)

            async def publish(event):
                entered.set()
                await release.wait()
                await actual(event)

            return publish

        monkeypatch.setattr(registry, "claim_request", claim)
        progress = _progress_owner(owner, running, registry)
        await progress.start()
        publishing = asyncio.create_task(progress.sink(ComposerProgressEvent(phase="calling_model", headline="Owned publication")))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            publishing.cancel()
            await asyncio.sleep(0)
            publishing.cancel()
            closing = asyncio.create_task(progress.close())
            await asyncio.sleep(0)
            assert not publishing.done() and not closing.done()
            assert (await registry.get_latest(str(running.claim.session_id))).inflight_requests == 1
            release.set()
            with pytest.raises(BaseException) as caught:
                await publishing
            assert len(_cancellations(caught.value)) == 2
            await closing
            assert progress.unregistered
            latest = await registry.get_latest(str(running.claim.session_id))
            assert latest.phase == "calling_model" and latest.inflight_requests == 0
        finally:
            release.set()
            await asyncio.gather(publishing, return_exceptions=True)
            await asyncio.gather(closing if closing is not None else progress.close(), return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_cleanup_refusal_is_visible_without_admission_bypass(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, _sdk):
        registry = ComposerProgressRegistry()
        actual_finish = registry.finish_request
        original = RequiredGenerationUnavailable("progress cleanup refused by current executor admission")
        progress = _progress_owner(owner, running, registry)
        await progress.start()

        async def finish(lease, *, deadline=None, on_cleanup=None):
            assert lease is progress.request_lease
            assert deadline is None and on_cleanup is not None
            raise original

        monkeypatch.setattr(registry, "finish_request", finish)
        with pytest.raises(RequiredGenerationUnavailable) as caught:
            await progress.close()
        assert caught.value is original and progress.cleanup_failure is original
        assert not progress.unregistered
        assert (await registry.get_latest(str(running.claim.session_id))).inflight_requests == 1
        # Test teardown uses the exact original registry API. This is not a
        # production bypass and does not claim real begin_drain coverage.
        assert progress.request_lease is not None
        await actual_finish(progress.request_lease)


@pytest.mark.asyncio
async def test_cancelled_retained_close_cannot_release_pending_late_begin_custody(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, _sdk):
        registry = ComposerProgressRegistry()
        progress = _progress_owner(owner, running, registry, hard_expires_at=monotonic() + 0.05)
        authority = object.__new__(SessionComposerProgressAuthority)
        submission = ProgressSQLInvocation(authority.begin_request, (str(running.claim.session_id), progress.record.actor_user_id))
        native: Future[ComposerRequestLease] = Future()
        submission.future = native
        progress._retain_start_submission(submission)
        try:
            with pytest.raises(ProgressCloseDeadlineExpired):
                await progress.close()
            assert progress._closing is not None and registry.pending_owner_count == 1
            progress._closing.cancel("retained close task interrupted")
            await asyncio.gather(progress._closing, return_exceptions=True)
            await asyncio.sleep(0)
            assert progress.request_lease is None and registry.pending_owner_count == 1
            with pytest.raises(AuditIntegrityError, match="unfinished segment custody"):
                registry.assert_pending_settled()
            native.set_result(ComposerRequestLease("late-token", str(running.claim.session_id), progress.record.actor_user_id))
            submission.observe_if_done()
            assert type(submission.late_outcome) is ComposerRequestLease
            assert registry.pending_owner_count == 1
        finally:
            registry.release_pending_owner(progress)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_progress_close_retains_stalled_coroutine_when_later_drain_shortens_original_cutoff(
    operation_store, tmp_path, monkeypatch, cancel_close
):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, _sdk):
        registry = ComposerProgressRegistry()
        actual_claim = registry.claim_request
        entered, release = asyncio.Event(), asyncio.Event()
        drain: list[ComposerDrainDeadline | None] = [None]

        async def claim(**kwargs):
            actual = await actual_claim(**kwargs)

            async def publish(event):
                entered.set()
                await release.wait()
                await actual(event)

            return publish

        monkeypatch.setattr(registry, "claim_request", claim)
        progress = _progress_owner(
            owner,
            running,
            registry,
            hard_expires_at=monotonic() + 20,
            drain_deadline=lambda: drain[0],
        )
        await progress.start()
        publishing = asyncio.create_task(progress.sink(ComposerProgressEvent(phase="calling_model", headline="Owned publication")))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            drain[0] = ComposerDrainDeadline(expires_at=monotonic() + 0.05)
            if cancel_close:
                closing = asyncio.create_task(progress.close())
                async with asyncio.timeout(5):
                    while progress._closing is None:
                        await asyncio.sleep(0)
                closing.cancel("retained close cancellation")
                with pytest.raises(BaseExceptionGroup) as caught_group:
                    await closing
                assert _cancellations(caught_group.value)
                assert any(type(error) is ProgressCloseDeadlineExpired for error in caught_group.value.exceptions)
            else:
                with pytest.raises(ProgressCloseDeadlineExpired) as caught:
                    await progress.close()
                assert caught.value.owner is progress
            assert progress.contained_pending and not progress.unregistered
            assert registry.pending_owner_count == 1
            assert type(progress.cleanup_failure) is ProgressCloseDeadlineExpired
            assert progress.request_lease is not None
            assert (await registry.get_latest(str(running.claim.session_id))).inflight_requests == 1
            release.set()
            await asyncio.gather(publishing, return_exceptions=True)
            assert progress._closing is not None
            async with asyncio.timeout(5):
                await asyncio.gather(progress._closing, return_exceptions=True)
            assert progress.unregistered
            assert registry.pending_owner_count == 0
            assert (await registry.get_latest(str(running.claim.session_id))).inflight_requests == 0
        finally:
            release.set()
            await asyncio.gather(publishing, return_exceptions=True)


@pytest.mark.asyncio
async def test_progress_owner_reconciles_late_exact_cleanup_without_erasing_original_timeout(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, _sdk):
        registry = ComposerProgressRegistry()
        actual_finish = registry.finish_request
        progress = _progress_owner(owner, running, registry, hard_expires_at=monotonic() + 10)
        await progress.start()
        authority = object.__new__(SessionComposerProgressAuthority)
        observed: list[tuple[ProgressRequestCleanup, Future[None]]] = []

        async def delayed_finish(lease, *, deadline=None, on_cleanup=None):
            capability = ProgressRequestCleanup(authority.end_request, lease)
            assert on_cleanup is not None
            on_cleanup(capability)
            native: Future[None] = Future()
            capability.future = native
            observed.append((capability, native))
            raise ProgressCleanupDeadlineExpired(capability, native_pending=True)

        monkeypatch.setattr(registry, "finish_request", delayed_finish)
        with pytest.raises(ProgressCleanupDeadlineExpired) as caught:
            await progress.close()
        assert len(observed) == 1 and caught.value.cleanup is observed[0][0]
        assert progress.contained_pending and not progress.unregistered
        assert registry.pending_owner_count == 1
        assert progress.cleanup_failure is caught.value
        observed[0][1].set_result(None)
        with pytest.raises(ProgressCleanupDeadlineExpired) as repeated:
            await progress.close()
        assert repeated.value is caught.value
        assert progress.unregistered and progress.cleanup_failure is caught.value
        assert registry.pending_owner_count == 0
        assert progress.request_lease is not None
        await actual_finish(progress.request_lease)
