"""Runtime preflight in-flight coordination tests."""

from __future__ import annotations

import asyncio

import pytest

from elspeth.web.execution.runtime_preflight import (
    RuntimePreflightCoordinator,
    RuntimePreflightFailure,
    RuntimePreflightKey,
)
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult


def _passing_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


@pytest.mark.asyncio
async def test_coordinator_deduplicates_concurrent_same_session_state_settings() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey(
        session_scope="session:abc123",
        state_version=7,
        state_content_hash="state-hash",
        settings_hash="settings-hash",
    )
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    expected = _passing_preflight()

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return expected

    first_task = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    second_task = asyncio.create_task(coordinator.run(key, worker))

    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first is expected
    assert second is expected
    assert calls == 1


@pytest.mark.asyncio
async def test_coordinator_deduplicates_concurrent_failure_for_same_key() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey(
        session_scope="session:abc123",
        state_version=7,
        state_content_hash="state-hash",
        settings_hash="settings-hash",
    )
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    original = RuntimeError("constructor failed")

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise original

    first_task = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    second_task = asyncio.create_task(coordinator.run(key, worker))

    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert isinstance(first, RuntimePreflightFailure)
    assert isinstance(second, RuntimePreflightFailure)
    assert first.original_exc is original
    assert second.original_exc is original
    assert calls == 1


@pytest.mark.asyncio
async def test_coordinator_evicts_inflight_entry_when_only_awaiter_is_cancelled() -> None:
    """Mid-flight cancellation must not leak the in-flight dict entry.

    The common composer path: a per-compose timeout fires, the awaiter is
    cancelled while the worker is still running, and no future caller arrives
    with the same key (because state_version rotates on every state mutation).
    Without an eviction-on-done callback, the dict entry would stay for the
    life of the process.
    """
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey(
        session_scope="session:abc123",
        state_version=11,
        state_content_hash="state-hash",
        settings_hash="settings-hash",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker() -> ValidationResult:
        started.set()
        await release.wait()
        return _passing_preflight()

    awaiter = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    assert key in coordinator._inflight

    awaiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await awaiter

    # Worker is still running; awaiter is cancelled. Release the worker and
    # let its done-callback fire on the event loop.
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if key not in coordinator._inflight:
            break

    assert key not in coordinator._inflight


@pytest.mark.asyncio
async def test_coordinator_does_not_share_different_session_scopes() -> None:
    coordinator = RuntimePreflightCoordinator()
    calls = 0

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        return _passing_preflight()

    await asyncio.gather(
        coordinator.run(RuntimePreflightKey("session:http", 1, "state", "settings"), worker),
        coordinator.run(RuntimePreflightKey("session:mcp", 1, "state", "settings"), worker),
    )

    assert calls == 2


# ---------------------------------------------------------------------------
# elspeth-5269b43bca — admission follows the WORKER's lifetime, not the
# awaiter's. A caller whose budget expires receives a timeout envelope, but
# the in-flight entry stays until the underlying worker actually finishes so
# a same-key retry joins it instead of submitting a duplicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timed_out_awaiter_keeps_entry_admitted_until_worker_completes() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("session:abc123", 3, "state-hash", "settings-hash")
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    expected = _passing_preflight()

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return expected

    first = await coordinator.run(key, worker, timeout=0.05)
    assert isinstance(first, RuntimePreflightFailure)
    assert isinstance(first.original_exc, TimeoutError)
    assert calls == 1
    # The worker is still running: the entry MUST remain admitted.
    assert key in coordinator._inflight
    assert not coordinator._inflight[key].done()

    # A retry storm with the same key joins the running worker — no second
    # submission, and each retry still gets its own bounded timeout.
    for _ in range(5):
        retry = await coordinator.run(key, worker, timeout=0.02)
        assert isinstance(retry, RuntimePreflightFailure)
        assert isinstance(retry.original_exc, TimeoutError)
    assert calls == 1
    assert key in coordinator._inflight

    # A patient awaiter that arrives after the others gave up receives the
    # real result once the worker completes.
    patient = asyncio.create_task(coordinator.run(key, worker, timeout=5.0))
    await asyncio.sleep(0)
    release.set()
    assert await patient is expected
    assert calls == 1

    # Exactly-once settlement: the entry is evicted at ACTUAL completion,
    # not before and not never.
    for _ in range(10):
        await asyncio.sleep(0)
        if key not in coordinator._inflight:
            break
    assert key not in coordinator._inflight


@pytest.mark.asyncio
async def test_exhausted_budget_does_not_admit_a_new_worker() -> None:
    """A caller with no remaining budget must not submit work it will not await."""
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("session:abc123", 4, "state-hash", "settings-hash")
    calls = 0

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        return _passing_preflight()

    entry = await coordinator.run(key, worker, timeout=0.0)
    assert isinstance(entry, RuntimePreflightFailure)
    assert isinstance(entry.original_exc, TimeoutError)
    assert calls == 0
    assert key not in coordinator._inflight


@pytest.mark.asyncio
async def test_exhausted_budget_still_joins_a_completed_worker() -> None:
    """Zero budget must still return an already-settled in-flight result."""
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("session:abc123", 5, "state-hash", "settings-hash")
    expected = _passing_preflight()
    release = asyncio.Event()

    async def worker() -> ValidationResult:
        await release.wait()
        return expected

    holder = asyncio.create_task(coordinator.run(key, worker, timeout=5.0))
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0)
    # The task has settled but its eviction callback may not have run yet.
    assert await coordinator.run(key, worker, timeout=0.0) is expected
    assert await holder is expected


@pytest.mark.asyncio
async def test_untimed_run_awaits_worker_completion() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("session:abc123", 6, "state-hash", "settings-hash")
    expected = _passing_preflight()

    async def worker() -> ValidationResult:
        await asyncio.sleep(0.05)
        return expected

    assert await coordinator.run(key, worker) is expected
