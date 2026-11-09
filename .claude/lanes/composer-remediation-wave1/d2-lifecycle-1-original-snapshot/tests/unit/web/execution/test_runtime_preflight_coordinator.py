"""Runtime preflight in-flight coordination tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from elspeth.web.execution.runtime_preflight import (
    RuntimePreflightCoordinator,
    RuntimePreflightFailure,
    RuntimePreflightKey,
    RuntimePreflightUnavailable,
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


@pytest.mark.asyncio
async def test_worker_cancellation_preserves_original_identity() -> None:
    coordinator = RuntimePreflightCoordinator()
    original = asyncio.CancelledError("worker cancelled itself")

    async def worker() -> ValidationResult:
        raise original

    with pytest.raises(asyncio.CancelledError) as caught:
        await coordinator.run(RuntimePreflightKey("cancel", 1, "state", "settings"), worker)
    assert caught.value is original


@pytest.mark.asyncio
async def test_worker_cancellation_after_consumed_caller_cancellation_preserves_original() -> None:
    coordinator = RuntimePreflightCoordinator()
    original = asyncio.CancelledError("worker cancelled during cleanup")

    async def worker() -> ValidationResult:
        raise original

    async def cleanup() -> None:
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel("earlier lifecycle cancellation")
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            # Lifecycle cleanup deliberately defers this earlier cancellation.
            # Its nonzero cancelling count must not misclassify a worker abort.
            with pytest.raises(asyncio.CancelledError) as caught:
                await coordinator.run(RuntimePreflightKey("cleanup", 1, "s", "k"), worker)
            assert caught.value is original
            await coordinator.seal_and_join()

    await asyncio.create_task(cleanup())


@pytest.mark.asyncio
async def test_seal_joins_admitted_worker_and_refuses_new_work() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("seal", 1, "state", "settings")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    expected = _passing_preflight()

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return expected

    caller = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    join = asyncio.create_task(coordinator.seal_and_join())
    await asyncio.sleep(0)
    assert not join.done()
    refused = await coordinator.run(key, worker)
    assert isinstance(refused, RuntimePreflightFailure)
    assert isinstance(refused.original_exc, RuntimePreflightUnavailable)
    other = await coordinator.run(RuntimePreflightKey("new-after-seal", 2, "state", "settings"), worker)
    assert isinstance(other, RuntimePreflightFailure)
    assert isinstance(other.original_exc, RuntimePreflightUnavailable)
    assert calls == 1
    release.set()
    assert await caller is expected
    await join
    await coordinator.seal_and_join()
    after_join = await coordinator.run(key, worker)
    assert isinstance(after_join, RuntimePreflightFailure)
    assert isinstance(after_join.original_exc, RuntimePreflightUnavailable)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("abandoned", [False, True])
async def test_seal_observes_failure_racing_completed_eviction_without_duplicate_delivery(abandoned: bool) -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("failure", 1, "state", "settings")
    release = asyncio.Event()
    started = asyncio.Event()
    original = ValueError("preflight failed")

    async def worker() -> ValidationResult:
        started.set()
        await release.wait()
        raise original

    caller = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    if abandoned:
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
    release.set()
    if abandoned:
        # Eviction runs before the join: close must still own its failure.
        for _ in range(4):
            await asyncio.sleep(0)
        with pytest.raises(ValueError) as caught:
            await coordinator.seal_and_join()
        assert caught.value is original
    else:
        result = await caller
        assert isinstance(result, RuntimePreflightFailure)
        assert result.original_exc is original
        await coordinator.seal_and_join()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_seal_retains_repeated_caller_cancellation_until_actual_completion(fail: bool) -> None:
    coordinator = RuntimePreflightCoordinator()
    release = asyncio.Event()
    started = asyncio.Event()
    original = ValueError("abandoned failure")

    async def worker() -> ValidationResult:
        started.set()
        await release.wait()
        if fail:
            raise original
        return _passing_preflight()

    caller = asyncio.create_task(coordinator.run(RuntimePreflightKey("join", 1, "s", "k"), worker))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    join = asyncio.create_task(coordinator.seal_and_join())
    await asyncio.sleep(0)
    join.cancel("first join cancellation")
    await asyncio.sleep(0)
    join.cancel("second join cancellation")
    await asyncio.sleep(0)
    assert not join.done()
    release.set()
    if fail:
        with pytest.raises(BaseExceptionGroup) as caught:
            await join
        assert caught.value.exceptions[0] is original
        cancellation = caught.value.exceptions[1]
    else:
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await join
        cancellation = cancelled.value
    assert isinstance(cancellation, asyncio.CancelledError)
    assert cancellation.args == ("first join cancellation",)


@pytest.mark.asyncio
async def test_empty_coordinator_can_be_sealed_repeatedly() -> None:
    coordinator = RuntimePreflightCoordinator()
    await coordinator.seal_and_join()
    await coordinator.seal_and_join()


class PreflightTaskAbort(BaseException):
    """Uncaptured task failure distinct from an ordinary result envelope."""


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [PreflightTaskAbort("task aborted"), asyncio.CancelledError("task cancelled")])
async def test_seal_retains_abandoned_actual_task_failure(original: BaseException) -> None:
    coordinator = RuntimePreflightCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    unobserved: list[object] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unobserved.append(context["exception"]))

    async def worker() -> ValidationResult:
        started.set()
        await release.wait()
        raise original

    try:
        caller = asyncio.create_task(coordinator.run(RuntimePreflightKey("abort", 1, "s", "k"), worker))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        release.set()
        for _ in range(4):
            await asyncio.sleep(0)
        with pytest.raises(type(original)) as caught:
            await coordinator.seal_and_join()
        assert caught.value is original
        assert unobserved == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_duplicate_failure_delivered_to_patient_waiter() -> None:
    coordinator = RuntimePreflightCoordinator()
    key = RuntimePreflightKey("waiters", 1, "s", "k")
    started = asyncio.Event()
    release = asyncio.Event()
    original = ValueError("one delivered result")
    calls = 0

    async def worker() -> ValidationResult:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise original

    departing = asyncio.create_task(coordinator.run(key, worker))
    await started.wait()
    patient = asyncio.create_task(coordinator.run(key, worker))
    await asyncio.sleep(0)
    departing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await departing
    join = asyncio.create_task(coordinator.seal_and_join())
    await asyncio.sleep(0)
    release.set()
    await join
    result = await patient
    assert isinstance(result, RuntimePreflightFailure)
    assert result.original_exc is original
    assert calls == 1
    await coordinator.seal_and_join()


def _run_real_worker_join_child() -> None:
    """Fresh lifecycle; never borrow or reset pytest's shared executor owner."""
    import threading

    from elspeth.web.async_workers import configure_required_executor_recovery, run_sync_in_worker, shutdown_async_workers

    def unexpected_recovery(observation: object) -> None:
        raise AssertionError(observation)

    async def main() -> None:
        configure_required_executor_recovery(
            drain_seconds=10,
            instance_draining=threading.Event(),
            generation_unavailable=threading.Event(),
            recovery_callback=unexpected_recovery,
        )
        coordinator = RuntimePreflightCoordinator()
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking() -> ValidationResult:
            started.set()
            assert release.wait(5), "outer worker release was lost"
            finished.set()
            return _passing_preflight()

        async def worker() -> ValidationResult:
            return await run_sync_in_worker(blocking)

        key = RuntimePreflightKey("real", 1, "state", "settings")
        result = await coordinator.run(key, worker, timeout=0.05)
        assert isinstance(result, RuntimePreflightFailure)
        assert isinstance(result.original_exc, TimeoutError)
        assert started.is_set()
        join = asyncio.create_task(coordinator.seal_and_join())
        await asyncio.sleep(0)
        join.cancel("outer cancellation")
        await asyncio.sleep(0)
        assert not join.done() and not finished.is_set()
        release.set()
        try:
            await join
        except asyncio.CancelledError as cancellation:
            assert cancellation.args == ("outer cancellation",)
        else:
            raise AssertionError("join lost cancellation")
        assert finished.is_set()
        await shutdown_async_workers()
        print("real worker and coordinator joined", flush=True)

    asyncio.run(main())


def test_seal_joins_real_worker_in_fresh_explicit_lifecycle(tmp_path: Path) -> None:
    log = tmp_path / "worker-child.log"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from tests.unit.web.execution.test_runtime_preflight_coordinator import _run_real_worker_join_child; _run_real_worker_join_child()",
            ],
            env=os.environ.copy(),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            code = process.wait(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            log.with_suffix(".exit").write_text(str(process.returncode), encoding="utf-8")
    assert code == 0, log.read_text(encoding="utf-8")
    assert "real worker and coordinator joined" in log.read_text(encoding="utf-8")
