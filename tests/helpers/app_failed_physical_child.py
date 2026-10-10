"""Application cleanup observes a failed physical executor join before siblings."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import app as app_module
from elspeth.web import async_workers
from elspeth.web.app import create_app
from elspeth.web.operator_telemetry_custody import TelemetryCompletionWitness
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.test_app import _settings


def _leaves(original: BaseException) -> tuple[BaseException, ...]:
    if isinstance(original, BaseExceptionGroup):
        return tuple(leaf for child in original.exceptions for leaf in _leaves(child))
    return (original,)


async def _wait_for_thread_event(event: Event, *, label: str, seconds: float = 5) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not event.is_set():
        assert loop.time() < deadline, f"actual {label} did not enter"
        await asyncio.sleep(0.001)


async def run_failed_actual_callback_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_settings(tmp_path, composer_boot_probe_enabled=False), process_watchdog_factory=OwnedTestProcessWatchdog)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    registry = app.state.execution_lease_release_registry
    service = app.state.execution_service
    capability = registry.executor_finalizer
    generation = async_workers._GENERATION_CUSTODIAN
    assert capability is not None and generation is not None
    private = service._executor
    assert type(private) is ThreadPoolExecutor

    private_entered, release_private = Event(), Event()
    shared_entered, release_shared = Event(), Event()
    callback_failed = Event()
    membership_returned, telemetry_returned = asyncio.Event(), asyncio.Event()
    joining_entered = asyncio.Event()
    callback_original = AuditIntegrityError("actual app executor callback failed")
    actual_record_completed = generation.record_completed
    actual_join_lifecycle = app_module._join_execution_lifecycle
    membership = app.state.web_instance_membership
    telemetry = app.state.operator_telemetry
    actual_membership_stop = type(membership).stop
    actual_telemetry_shutdown = type(telemetry).shutdown
    actual_telemetry_shutdown_sync = type(telemetry).shutdown_sync
    membership_outcomes = []
    telemetry_witnesses = []
    delivered_cancellations = []
    actual_sleep = asyncio.sleep
    teardown = None
    monitor_stop = Event()
    monitor_emitted = Event()

    async def observed_sleep(delay, result=None):
        try:
            return await actual_sleep(delay, result)
        except asyncio.CancelledError as original:
            if asyncio.current_task() is teardown and joining_entered.is_set():
                delivered_cancellations.append(original)
            raise

    def held_private() -> None:
        private_entered.set()
        assert release_private.wait(15), "private executor gate not released"

    def held_shared() -> None:
        shared_entered.set()
        assert release_shared.wait(15), "shared executor gate not released"

    def failed_actual_callback(reservation) -> None:
        if reservation is capability._physical_reservation and not callback_failed.is_set():
            callback_failed.set()
            raise callback_original
        actual_record_completed(reservation)

    async def observed_lifecycle(selected_registry, selected_service):
        assert selected_registry is registry and selected_service is service
        joining_entered.set()
        return await actual_join_lifecycle(selected_registry, selected_service)

    async def observed_membership_stop(self):
        outcome = await actual_membership_stop(self)
        if self is membership:
            membership_outcomes.append(outcome)
            membership_returned.set()
            os.write(1, b"ACTUAL_MEMBERSHIP_STOP_RETURNED\n")
        return outcome

    def observed_telemetry_shutdown_sync(self):
        witness = actual_telemetry_shutdown_sync(self)
        if self is telemetry:
            telemetry_witnesses.append(witness)
            os.write(1, b"ACTUAL_TELEMETRY_WITNESS\n")
        return witness

    async def observed_telemetry_shutdown(self):
        await actual_telemetry_shutdown(self)
        if self is telemetry:
            telemetry_returned.set()

    monkeypatch.setattr(generation, "record_completed", failed_actual_callback)
    monkeypatch.setattr(app_module, "_join_execution_lifecycle", observed_lifecycle)
    monkeypatch.setattr(type(membership), "stop", observed_membership_stop)
    monkeypatch.setattr(type(telemetry), "shutdown_sync", observed_telemetry_shutdown_sync)
    monkeypatch.setattr(type(telemetry), "shutdown", observed_telemetry_shutdown)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    private_future = private.submit(held_private)
    shared_task = asyncio.create_task(async_workers.run_sync_in_worker(held_shared))

    def observe_physical_join() -> None:
        while not monitor_stop.is_set():
            if callback_failed.is_set() and private_future.done() and generation.joined.is_set():
                try:
                    observed = registry.executor_join_physically_observed
                except BaseException as failure:
                    os.write(1, f"PHYSICAL_OBSERVER_ERROR {type(failure).__name__}\n".encode())
                    return
                if observed:
                    os.write(1, b"ACTUAL_EXECUTOR_PHYSICAL_JOIN_OBSERVED\n")
                    monitor_emitted.set()
                    return
            time.sleep(0.005)

    monitor_thread = threading.Thread(target=observe_physical_join, name="app-failed-physical-control", daemon=True)
    monitor_thread.start()
    terminal_observer = None
    observed = False
    primary_failure: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    try:
        await _wait_for_thread_event(private_entered, label="private executor task")
        await _wait_for_thread_event(shared_entered, label="registered shared worker task")
        diagnostic_mode = os.environ.get("ELSPETH_APP_RECEIPT_DIAGNOSTIC")
        if diagnostic_mode is not None:
            assert diagnostic_mode == "returned"
            from tests.helpers.app_failed_receipt_terminal import ActualAppReceiptTerminalObserver

            terminal_observer = ActualAppReceiptTerminalObserver(
                scenario="returned",
                registry=registry,
                capability=capability,
                private_future=private_future,
                callback_original=callback_original,
                callback_failed=callback_failed,
                expected_generation=generation,
            )
            terminal_observer.start()
        teardown = asyncio.create_task(context.__aexit__(None, None, None))
        await asyncio.wait_for(joining_entered.wait(), 5)
        assert not registry.executor_join_physically_observed
        assert not membership_returned.is_set() and not telemetry_returned.is_set()
        release_private.set()
        callback_deadline = asyncio.get_running_loop().time() + 5
        while not callback_failed.is_set():
            assert asyncio.get_running_loop().time() < callback_deadline, "actual finalizer callback did not run"
            await asyncio.sleep(0.001)
        assert capability._physical_reservation is not None
        assert capability._physical_reservation._registering_generation is generation
        assert private_future.done()
        assert not generation.joined.is_set()
        assert not registry.executor_join_physically_observed
        assert not teardown.done()
        for index, label in enumerate(("external-one", "external-two", "external-three"), start=1):
            teardown.cancel(label)
            delivery_deadline = asyncio.get_running_loop().time() + 5
            while len(delivered_cancellations) < index:
                assert asyncio.get_running_loop().time() < delivery_deadline, "external cancellation was not delivered"
                await asyncio.sleep(0.001)
            assert not teardown.done()
        assert not membership_returned.is_set() and not telemetry_returned.is_set()
        release_shared.set()
        # These Events are set only after each original producer method returns.
        # A success-only app predicate leaves this control red at membership.
        try:
            await asyncio.wait_for(membership_returned.wait(), 5)
        except TimeoutError:
            if private_future.done() and generation.joined.is_set() and registry.executor_join_physically_observed:
                os.write(1, b"CAUSAL_MISSING_MEMBERSHIP_AFTER_PHYSICAL_JOIN\n")
                raise AssertionError("actual membership stop missing after physically observed failed executor join") from None
            raise
        await asyncio.wait_for(telemetry_returned.wait(), 5)
        done, _ = await asyncio.wait({teardown}, timeout=5)
        assert done, "application did not finish after actual sibling and generation joins"
        with pytest.raises(BaseException) as raised:
            await teardown
        observed = True
        leaves = _leaves(raised.value)
        assert any(leaf is callback_original for leaf in leaves)
        cancelled = tuple(leaf for leaf in leaves if isinstance(leaf, asyncio.CancelledError))
        assert {leaf.args for leaf in cancelled} >= {("external-one",), ("external-two",), ("external-three",)}
        assert len(delivered_cancellations) == 3
        assert all(any(leaf is original for leaf in cancelled) for original in delivered_cancellations)
        assert len({id(original) for original in delivered_cancellations}) == 3
        assert shared_task.done() and generation.joined.is_set()
        assert shared_task.result() is None
        assert registry.executor_join_physically_observed
        await _wait_for_thread_event(monitor_emitted, label="actual physical monitor marker")
        assert not registry.executor_join_succeeded
        assert membership_returned.is_set() and telemetry_returned.is_set()
        assert len(membership_outcomes) == 1
        assert len(telemetry_witnesses) == 1
        witness = telemetry_witnesses[0]
        assert type(witness) is TelemetryCompletionWitness and witness.owner is telemetry.cleanup_owner
        with pytest.raises(BaseExceptionGroup) as refusal:
            registry.assert_completed()
        assert any(type(leaf) is AuditIntegrityError for leaf in _leaves(refusal.value))
        assert not app.state.process_watchdog.completed
    except BaseException as original:
        primary_failure = original
    finally:
        physical_observed = False
        if private_future.done() and generation.joined.is_set():
            try:
                physical_observed = registry.executor_join_physically_observed
            except BaseException as original:
                cleanup_failures.append(original)
        if physical_observed:
            try:
                await _wait_for_thread_event(monitor_emitted, label="actual physical monitor marker", seconds=1)
            except BaseException as original:
                cleanup_failures.append(original)
        monitor_stop.set()
        release_private.set()
        release_shared.set()
        if teardown is None:
            teardown = asyncio.create_task(context.__aexit__(None, None, None))
        if not teardown.done():
            done, _ = await asyncio.wait({teardown}, timeout=10)
            if not done:
                cleanup_failures.append(AssertionError("application teardown remains physically unresolved"))
        if teardown.done() and not observed:
            try:
                teardown.result()
            except BaseException as original:
                if original is not primary_failure:
                    cleanup_failures.append(original)
        if not shared_task.done():
            done, _ = await asyncio.wait({shared_task}, timeout=10)
            if not done:
                cleanup_failures.append(AssertionError("registered shared task remains physically unresolved"))
        if shared_task.done():
            try:
                shared_task.result()
            except BaseException as original:
                cleanup_failures.append(original)
        private_deadline = asyncio.get_running_loop().time() + 10
        while not private_future.done() and asyncio.get_running_loop().time() < private_deadline:
            await asyncio.sleep(0.001)
        if not private_future.done():
            cleanup_failures.append(AssertionError("private executor task remains physically unresolved"))
        else:
            try:
                private_future.result()
            except BaseException as original:
                cleanup_failures.append(original)
        monitor_thread.join(timeout=1)
        if monitor_thread.is_alive():
            cleanup_failures.append(AssertionError("actual physical observer thread remains alive"))
        if terminal_observer is not None:
            try:
                await _wait_for_thread_event(terminal_observer.emitted, label="actual app receipt terminal", seconds=5)
            except BaseException as original:
                cleanup_failures.append(original)
            try:
                terminal_observer.stop_and_join()
            except BaseException as original:
                cleanup_failures.append(original)
    if primary_failure is not None:
        if cleanup_failures:
            raise BaseExceptionGroup("App control and cleanup retained originals", [primary_failure, *cleanup_failures]) from None
        raise primary_failure
    if cleanup_failures:
        raise BaseExceptionGroup("App control cleanup retained originals", cleanup_failures)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="app-failed-physical-control-") as directory:
        patcher = pytest.MonkeyPatch()
        try:
            asyncio.run(run_failed_actual_callback_control(Path(directory), patcher))
        finally:
            patcher.undo()
    os.write(1, b"APP_FAILED_PHYSICAL_CONTROL_PASS\n")
