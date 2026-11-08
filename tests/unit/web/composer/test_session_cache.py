"""Deterministic ownership, boundedness and worker coalescing proofs."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from types import MappingProxyType

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web import async_workers
from elspeth.web.composer._response_json import FrozenResponseJSON
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload
from elspeth.web.composer.session_cache import (
    ComposerSessionCache,
    ComposerSessionCacheRegistry,
    DiscoveryKey,
    MemoResult,
    MemoSource,
    ProjectionKey,
    SessionCacheKey,
    SessionCacheLimits,
    discovery_weight_bytes,
    projection_weight_bytes,
)


def _key(name: str = "one") -> SessionCacheKey:
    return SessionCacheKey("principal", name)


def _get(cache: ComposerSessionCache, name: str, compute: Callable[[], str]) -> MemoResult[FrozenResponseJSON]:
    key = ProjectionKey(name, "policy", "generation")
    return cache.projections.get_or_compute(key, compute, key_weight=key.weight_bytes(), value_weight=lambda _value: 64)


async def _get_async(cache: ComposerSessionCache, name: str, compute: Callable[[], str]) -> MemoResult[FrozenResponseJSON]:
    key = ProjectionKey(name, "policy", "generation")
    return await cache.projections.get_or_compute_async(key, compute, key_weight=key.weight_bytes(), value_weight=lambda _value: 64)


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
def small_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-proof") as executor:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
        yield executor


def test_principal_and_session_identity_and_private_fallback() -> None:
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(_key()) as first, registry.borrow(_key()) as second:
        assert first.cache is second.cache
        assert first.cache is not None
        assert _get(first.cache, "k", lambda: "one").source is MemoSource.MISS
        assert _get(second.cache, "k", lambda: "wrong").value == "one"
        with registry.borrow(SessionCacheKey("other", "one")) as other, registry.borrow(_key("two")) as another:
            assert other.cache is not first.cache
            assert another.cache is not first.cache
        assert registry.borrow(None).cache is None
    assert registry.snapshot().borrows == 0


def test_idle_ttl_lru_and_active_owner_preservation() -> None:
    clock = [0.0]
    registry = ComposerSessionCacheRegistry(limits=replace(SessionCacheLimits(), max_sessions=2, idle_seconds=10), clock=lambda: clock[0])
    active = registry.borrow(_key("active"))
    old = registry.borrow(_key("old"))
    old.close()
    with registry.borrow(_key("third")) as third:
        assert third.cache is not None
        assert registry.snapshot().active_sessions == 2
        assert registry.borrow(_key("blocked")).cache is None
    clock[0] = 20
    with registry.borrow(_key("fresh")) as fresh:
        assert fresh.cache is not None
        assert registry.borrow(_key("active")).cache is active.cache
    active.close()


def test_byte_entry_count_and_oversize_pressure_fallback() -> None:
    limits = replace(SessionCacheLimits(), projection_entries=2, session_weight_bytes=1600, total_weight_bytes=1800, entry_weight_bytes=900)
    registry = ComposerSessionCacheRegistry(limits=limits)
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None
        for name in ("a", "b", "c"):
            assert _get(cache, name, lambda: "value").value == "value"
        assert registry.snapshot().retained_entries <= 2
        assert registry.snapshot().weight_bytes <= limits.total_weight_bytes
        key = ProjectionKey("large", "", "")
        for _ in range(2):
            result = cache.projections.get_or_compute(key, lambda: "x" * 1000, key_weight=100, value_weight=lambda _v: 1000)
            assert result.source is MemoSource.MISS
        assert registry.snapshot().pending_entries == 0
        assert registry.snapshot().pins == 0
        assert registry.borrow(SessionCacheKey("x" * 1000, "oversize")).cache is None


def test_disabled_results_and_bounded_policy_disclosure() -> None:
    registry = ComposerSessionCacheRegistry(enabled=False, limits=replace(SessionCacheLimits(), schema_entries=1))
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None
        assert _get(cache, "x", lambda: "a").source is MemoSource.BYPASS
        assert _get(cache, "x", lambda: "b").value == "b"
        assert cache.mark_schema_loaded("policy-1", "source", "csv")
        assert not cache.mark_schema_loaded("policy-1", "source", "json")
        assert cache.schemas_loaded("policy-1") == frozenset({("source", "csv")})
        assert cache.schemas_loaded("policy-2") == frozenset()
        assert cache.mark_schema_loaded("policy-2", "sink", "csv")
        assert cache.schemas_loaded("policy-2") == frozenset({("sink", "csv")})


def test_cold_archive_reservation_overlaps_all_principals_and_pressure() -> None:
    registry = ComposerSessionCacheRegistry(limits=replace(SessionCacheLimits(), retirement_entries=1))
    first = registry.retire_session("cold")
    second = registry.retire_session("cold")
    assert registry.borrow(_key("cold")).cache is None
    assert registry.borrow(SessionCacheKey("other", "cold")).cache is None
    overflow = registry.retire_session("overflow")
    assert registry.borrow(_key("unrelated")).cache is None
    first.close()
    overflow.close()
    assert registry.borrow(_key("cold")).cache is None
    second.close()
    assert registry.snapshot().retirements == 0
    assert registry.snapshot().weight_bytes == 0
    with registry.borrow(_key("cold")) as fresh:
        assert fresh.cache is not None


def test_retired_owner_drain_cannot_recreate_before_archive_finishes() -> None:
    registry = ComposerSessionCacheRegistry()
    lease = registry.borrow(_key())
    cache = lease.cache
    assert cache is not None
    _get(cache, "x", lambda: "old")
    retirement = registry.retire_session("one")
    assert registry.snapshot().retained_entries == 0
    lease.close()
    assert registry.snapshot().retained_sessions == 0
    assert registry.borrow(_key()).cache is None
    assert _get(cache, "x", lambda: "uncached").source is MemoSource.BYPASS
    retirement.close()
    with registry.borrow(_key()) as fresh:
        assert fresh.cache is not cache


def test_retired_owner_reserves_identity_after_archive_attempt_ends() -> None:
    registry = ComposerSessionCacheRegistry()
    old = registry.borrow(_key())
    retirement = registry.retire_session("one")
    retirement.close()
    assert registry.borrow(_key()).cache is None
    assert registry.snapshot().retained_sessions == 1
    old.close()
    with registry.borrow(_key()) as fresh:
        assert fresh.cache is not None
        assert fresh.cache is not old.cache


def test_exceptions_and_baseexceptions_never_reside() -> None:
    class ComputationAborted(BaseException):
        pass

    registry = ComposerSessionCacheRegistry()
    with registry.borrow(_key()) as lease:
        assert lease.cache is not None

        def fail() -> str:
            raise ComputationAborted("failed")

        with pytest.raises(ComputationAborted):
            _get(lease.cache, "x", fail)
        assert registry.snapshot().pending_entries == registry.snapshot().pins == 0
        assert _get(lease.cache, "x", lambda: "retry").source is MemoSource.MISS


def test_projection_takes_owned_copy_of_readonly_view() -> None:
    registry = ComposerSessionCacheRegistry()
    backing: dict[str, FrozenResponseJSON] = {"value": "original"}
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None
        key = ProjectionKey("x", "p", "g")
        result = cache.projections.get_or_compute(
            key, lambda: MappingProxyType(backing), key_weight=key.weight_bytes(), value_weight=projection_weight_bytes
        )
        backing["value"] = "changed"
        cached = cache.projections.get_or_compute(key, lambda: "wrong", key_weight=key.weight_bytes(), value_weight=projection_weight_bytes)
        assert result.value == cached.value == MappingProxyType({"value": "original"})
        assert cached.source is MemoSource.HIT
        mutable_key = ProjectionKey("mutable", "p", "g")
        with pytest.raises(FrameworkBugError, match="mutable"):
            cache.projections.get_or_compute(
                mutable_key, lambda: backing, key_weight=mutable_key.weight_bytes(), value_weight=projection_weight_bytes
            )
        assert registry.snapshot().pending_entries == registry.snapshot().pins == 0


def test_legacy_raw_discovery_payload_is_returned_without_retention() -> None:
    registry = ComposerSessionCacheRegistry()
    key = DiscoveryKey("tool", "{}", "g")
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None
        for _ in range(2):
            result = cache.discovery.get_or_compute(
                key,
                lambda: CachedDiscoveryPayload(True, (), {"mutable": []}),
                key_weight=key.weight_bytes(),
                value_weight=discovery_weight_bytes,
            )
            assert result.source is MemoSource.MISS
        assert registry.snapshot().retained_entries == 0


@pytest.mark.asyncio
async def test_cache_job_cancelled_before_first_instruction_releases_reservation() -> None:
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(_key()) as lease:
        assert lease.cache is not None
        request = asyncio.create_task(_get_async(lease.cache, "x", lambda: "never"))
        # FIFO loop callbacks: the request creates its owner task, then this
        # continuation runs before that task's first instruction/submission.
        await asyncio.sleep(0)
        job = next(iter(registry._jobs))
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await _until(lambda: registry.snapshot().queued_jobs == 0)
        assert registry.snapshot().pending_entries == registry.snapshot().pins == 0
        assert _get(lease.cache, "x", lambda: "retry").value == "retry"


def test_sync_many_callers_coalesce_without_computation_under_lock() -> None:
    registry = ComposerSessionCacheRegistry()
    started, release = threading.Event(), threading.Event()
    calls = [0]
    with registry.borrow(_key()) as lease, ThreadPoolExecutor(max_workers=5) as executor:
        cache = lease.cache
        assert cache is not None

        def compute() -> str:
            calls[0] += 1
            started.set()
            assert release.wait(5)
            return "shared"

        futures = [executor.submit(_get, cache, "x", compute) for _ in range(5)]
        try:
            assert started.wait(5)
            # A different key proceeds while the producer is parked.
            assert _get(cache, "independent", lambda: "fast").value == "fast"
        finally:
            release.set()
        assert all(future.result(timeout=5).value == "shared" for future in futures)
    assert calls == [1]
    assert registry.snapshot().pins == registry.snapshot().pending_entries == 0


@pytest.mark.asyncio
async def test_async_request_cancellation_does_not_cancel_shared_producer(small_pool: ThreadPoolExecutor) -> None:
    registry = ComposerSessionCacheRegistry()
    lease = registry.borrow(_key())
    cache = lease.cache
    assert cache is not None
    started, release = threading.Event(), threading.Event()
    calls = [0]

    def compute() -> str:
        calls[0] += 1
        started.set()
        assert release.wait(5)
        return "shared"

    first = asyncio.create_task(_get_async(cache, "x", compute))
    try:
        await _until(started.is_set)
        second = asyncio.create_task(_get_async(cache, "x", compute))
        await _until(lambda: registry.snapshot().joins == 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        lease.close()
        assert registry.snapshot().pins >= 1
        release.set()
        assert (await second).source is MemoSource.JOIN
        assert calls == [1]
        await registry.finish_shutdown()
        assert registry.snapshot().weight_bytes == registry.snapshot().pins == 0
    finally:
        release.set()


@pytest.mark.asyncio
async def test_sync_worker_claims_key_whose_async_wrapper_is_queued(small_pool: ThreadPoolExecutor) -> None:
    registry = ComposerSessionCacheRegistry()
    started = [threading.Event(), threading.Event()]
    proceed, release_second = threading.Event(), threading.Event()
    calls = [0]
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None

        def compute() -> str:
            calls[0] += 1
            return "inline"

        def occupying_worker() -> str:
            started[0].set()
            assert proceed.wait(5)
            return str(_get(cache, "x", compute).value)

        def blocker() -> None:
            started[1].set()
            assert release_second.wait(5)

        first = asyncio.create_task(async_workers.run_sync_in_worker(occupying_worker))
        second = asyncio.create_task(async_workers.run_sync_in_worker(blocker))
        try:
            await _until(lambda: all(event.is_set() for event in started))
            queued = asyncio.create_task(_get_async(cache, "x", compute))
            await _until(lambda: registry.snapshot().queued_jobs == 1)
            proceed.set()
            assert await asyncio.wait_for(first, timeout=3) == "inline"
            assert (await asyncio.wait_for(queued, timeout=3)).value == "inline"
            assert calls == [1]
        finally:
            proceed.set()
            release_second.set()
            await second
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_actual_worker_pin_survives_repeated_job_cancellation_and_retirement(small_pool: ThreadPoolExecutor) -> None:
    registry = ComposerSessionCacheRegistry()
    lease = registry.borrow(_key())
    cache = lease.cache
    assert cache is not None
    started, release = threading.Event(), threading.Event()

    def compute() -> str:
        started.set()
        assert release.wait(5)
        return "late"

    request = asyncio.create_task(_get_async(cache, "x", compute))
    try:
        await _until(started.is_set)
        job = next(iter(registry._jobs))
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        retirement = registry.retire_session("one")
        lease.close()
        retirement.close()
        assert registry.borrow(_key()).cache is None
        job.cancel()
        await asyncio.sleep(0)
        job.cancel()
        await asyncio.sleep(0)
        assert not job.done()
        assert registry.snapshot().retained_sessions == 1
        assert registry.snapshot().pins >= 1
        release.set()
        await job
        await _until(lambda: registry.snapshot().queued_jobs == 0)
        assert registry.snapshot().retained_sessions == registry.snapshot().retained_entries == 0
        assert registry.snapshot().weight_bytes == 0
        with registry.borrow(_key()) as fresh:
            assert fresh.cache is not None
            assert fresh.cache is not cache
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cancelled_queued_job_releases_pin_without_running_body(small_pool: ThreadPoolExecutor) -> None:
    registry = ComposerSessionCacheRegistry()
    release = threading.Event()
    started = [threading.Event(), threading.Event()]
    calls = [0]

    def block(index: int) -> None:
        started[index].set()
        assert release.wait(5)

    blockers = [asyncio.create_task(async_workers.run_sync_in_worker(block, i)) for i in range(2)]
    with registry.borrow(_key()) as lease:
        assert lease.cache is not None

        def compute() -> str:
            calls[0] += 1
            return "never"

        try:
            await _until(lambda: all(event.is_set() for event in started))
            request = asyncio.create_task(_get_async(lease.cache, "x", compute))
            await _until(lambda: async_workers.outstanding_admissions() == 3)
            next(iter(registry._jobs)).cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await _until(lambda: registry.snapshot().queued_jobs == 0)
            assert calls == [0]
            assert registry.snapshot().pins == registry.snapshot().pending_entries == 0
        finally:
            release.set()
            await asyncio.gather(*blockers)
    await registry.finish_shutdown()
    assert registry.snapshot().weight_bytes == 0


@pytest.mark.asyncio
async def test_running_before_callable_entry_keeps_actual_worker_pin(
    small_pool: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ComposerSessionCacheRegistry()
    future_running, enter_callable = threading.Event(), threading.Event()
    actual_submit = small_pool.submit

    def delayed_submit[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        def delayed_entry() -> T:
            # The actual executor future is already RUNNING, but its original
            # callable has not executed its first instruction yet.
            future_running.set()
            assert enter_callable.wait(5)
            return fn(*args, **kwargs)

        return actual_submit(delayed_entry)

    monkeypatch.setattr(small_pool, "submit", delayed_submit)
    lease = registry.borrow(_key())
    cache = lease.cache
    assert cache is not None
    request = asyncio.create_task(_get_async(cache, "x", lambda: "after-entry"))
    try:
        await _until(future_running.is_set)
        job = next(iter(registry._jobs))
        job.cancel()
        registry.retire_session("one").close()
        lease.close()
        await asyncio.sleep(0)
        assert registry.snapshot().pending_entries == 0
        assert registry.snapshot().pins == 1
        assert registry.snapshot().retained_sessions == 1
        assert registry.borrow(_key()).cache is None
        enter_callable.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await _until(lambda: registry.snapshot().queued_jobs == 0)
        assert registry.snapshot().retained_sessions == registry.snapshot().pins == registry.snapshot().weight_bytes == 0
    finally:
        enter_callable.set()


@pytest.mark.asyncio
async def test_admission_rejection_releases_cache_job_and_retry_succeeds(
    small_pool: ThreadPoolExecutor, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ComposerSessionCacheRegistry()
    with registry.borrow(_key()) as lease:
        cache = lease.cache
        assert cache is not None

        async def reject() -> None:
            raise async_workers.AsyncWorkerAdmissionTimeoutError("saturated")

        with monkeypatch.context() as patch:
            patch.setattr(async_workers, "_acquire_admission", reject)
            with pytest.raises(async_workers.AsyncWorkerAdmissionTimeoutError):
                await _get_async(cache, "x", lambda: "never")
        await _until(lambda: registry.snapshot().queued_jobs == 0)
        assert registry.snapshot().pins == registry.snapshot().pending_entries == 0
        assert (await _get_async(cache, "x", lambda: "retry")).value == "retry"
    await registry.finish_shutdown()


@pytest.mark.asyncio
async def test_queued_job_pressure_and_shutdown_preserve_running_work(small_pool: ThreadPoolExecutor) -> None:
    registry = ComposerSessionCacheRegistry(limits=replace(SessionCacheLimits(), queued_jobs=1))
    started, release = threading.Event(), threading.Event()
    lease = registry.borrow(_key())
    cache = lease.cache
    assert cache is not None

    def compute() -> str:
        started.set()
        assert release.wait(5)
        return "running"

    first = asyncio.create_task(_get_async(cache, "x", compute))
    try:
        await _until(started.is_set)
        assert (await _get_async(cache, "y", lambda: "fallback")).source is MemoSource.BYPASS
        registry.begin_shutdown()
        assert registry.borrow(_key()).cache is None
        lease.close()
        draining = asyncio.create_task(registry.finish_shutdown())
        await asyncio.sleep(0)
        assert not draining.done()
        assert registry.snapshot().pins >= 1
        release.set()
        assert (await first).value == "running"
        await draining
        assert registry.snapshot().weight_bytes == registry.snapshot().retained_entries == registry.snapshot().pins == 0
    finally:
        release.set()
