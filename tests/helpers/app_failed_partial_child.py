"""Exercise the preclaimed partial-constructor executor without replaying it."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import app as app_module
from elspeth.web import async_workers
from elspeth.web.app import create_app
from elspeth.web.required_executor import RequiredExecutorGenerationCustodian
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.unit.web.test_app import _settings


def _leaves(original: BaseException) -> tuple[BaseException, ...]:
    if isinstance(original, BaseExceptionGroup):
        return tuple(leaf for child in original.exceptions for leaf in _leaves(child))
    return (original,)


async def _entered(event: Event, label: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not event.is_set():
        assert loop.time() < deadline, f"actual {label} did not enter"
        await asyncio.sleep(0.001)


async def run_partial_claimed_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_settings(tmp_path, composer_boot_probe_enabled=False), process_watchdog_factory=OwnedTestProcessWatchdog)
    constructor = app_module.ExecutionServiceImpl
    join = constructor.join_executor_shutdown
    seal = app.state.application_finalizer_owner.seal
    observe = app_module._join_execution_lifecycle
    actual_record_completed = RequiredExecutorGenerationCustodian.record_completed
    constructor_original = RuntimeError("actual partial constructor failed after executor registration")
    callback_original = AuditIntegrityError("actual claimed partial callback failed")
    begin = asyncio.Event()
    private_entered, release_private = Event(), Event()
    shared_join_entered = Event()
    callback_failed = Event()
    existing: list[asyncio.Task[object]] = []
    private_futures = []
    services = []
    joins = []
    capability = None
    startup: asyncio.Task[object] | None = None
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    expected_owner_faults: list[BaseException] = []
    terminal_observer = None

    def held_private() -> None:
        private_entered.set()
        assert release_private.wait(20), "private executor barrier was not opened"

    def sealed() -> None:
        seal()
        begin.set()

    def actual_join(self) -> None:
        joins.append(self)
        shared_join_entered.set()
        return join(self)

    def failed_callback(self, reservation) -> None:
        if capability is not None and reservation is capability._physical_reservation and not callback_failed.is_set():
            callback_failed.set()
            raise callback_original
        actual_record_completed(self, reservation)

    async def existing_owner(owned_capability) -> None:
        await begin.wait()
        await async_workers.run_application_finalizer_in_worker(owned_capability)

    def partial_constructor(**kwargs):
        nonlocal capability
        service = constructor(**kwargs)
        services.append(service)
        capability = service.shutdown_finalizer
        assert type(service._executor) is ThreadPoolExecutor
        private_futures.append(service._executor.submit(held_private))
        existing.append(asyncio.create_task(existing_owner(capability)))
        raise constructor_original

    async def observed_lifecycle(registry, service):
        await _entered(shared_join_entered, "claimed shared finalizer")
        assert service is None
        assert registry is app.state.execution_lease_release_registry
        assert registry.executor_finalizer is capability
        assert registry.executor_finalizer.claimed
        assert not registry.executor_join_physically_observed
        os.write(1, b"ACTUAL_PARTIAL_CLAIMED_BEFORE_APP_JOIN\n")
        return await observe(registry, service)

    monkeypatch.setattr(app.state.application_finalizer_owner, "seal", sealed)
    monkeypatch.setattr(constructor, "join_executor_shutdown", actual_join)
    monkeypatch.setattr(RequiredExecutorGenerationCustodian, "record_completed", failed_callback)
    monkeypatch.setattr(app_module, "ExecutionServiceImpl", partial_constructor)
    monkeypatch.setattr(app_module, "_join_execution_lifecycle", observed_lifecycle)
    context = app.router.lifespan_context(app)
    try:
        startup = asyncio.create_task(context.__aenter__())
        await _entered(private_entered, "partial private Future")
        await _entered(shared_join_entered, "claimed shared finalizer")
        diagnostic_mode = os.environ.get("ELSPETH_APP_RECEIPT_DIAGNOSTIC")
        if diagnostic_mode is not None:
            assert diagnostic_mode == "partial"
            from tests.helpers.app_failed_receipt_terminal import ActualAppReceiptTerminalObserver

            assert capability is not None and capability._physical_reservation is not None
            expected_generation = capability._physical_reservation._registering_generation
            assert expected_generation is async_workers._GENERATION_CUSTODIAN
            terminal_observer = ActualAppReceiptTerminalObserver(
                scenario="partial",
                registry=app.state.execution_lease_release_registry,
                capability=capability,
                private_future=private_futures[0],
                callback_original=callback_original,
                callback_failed=callback_failed,
                expected_generation=expected_generation,
            )
            terminal_observer.start()
        registry = app.state.execution_lease_release_registry
        assert registry.executor_finalizer is capability
        assert registry.executor_finalizer.claimed
        assert not registry.executor_join_physically_observed
        assert not registry.executor_join_succeeded
        assert not startup.done()
        release_private.set()
        await _entered(callback_failed, "partial finalizer failed callback")
        assert capability._physical_reservation is not None
        generation = capability._physical_reservation._registering_generation
        assert generation is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        while not generation.joined.is_set() or not registry.executor_join_physically_observed:
            assert loop.time() < deadline, "actual partial failed physical join not observed"
            await asyncio.sleep(0.001)
        assert private_futures[0].done()
        os.write(1, b"ACTUAL_PARTIAL_PRIVATE_AND_GENERATION_JOINED\n")
        assert len(joins) == 1 and joins[0] is services[0]
        with pytest.raises(BaseException) as caught:
            await asyncio.wait_for(startup, 5)
        leaves = _leaves(caught.value)
        assert any(leaf is constructor_original for leaf in leaves)
        assert any(leaf is callback_original for leaf in leaves)
        assert len(joins) == 1
        assert registry.executor_join_physically_observed
        assert not registry.executor_join_succeeded
        assert not app.state.process_watchdog.completed
    except BaseException as original:
        primary = original
    finally:
        release_private.set()
        for task in existing:
            done, _ = await asyncio.wait({task}, timeout=10)
            if not done:
                cleanup_failures.append(AssertionError("actual existing finalizer owner remains pending"))
            else:
                try:
                    task.result()
                except BaseException as original:
                    if original is callback_original:
                        expected_owner_faults.append(original)
                    else:
                        cleanup_failures.append(original)
        if startup is not None and not startup.done():
            done, _ = await asyncio.wait({startup}, timeout=10)
            if not done:
                cleanup_failures.append(AssertionError("partial application startup remains pending"))
        for future in private_futures:
            if not future.done():
                cleanup_failures.append(AssertionError("actual partial private Future remains pending"))
            else:
                try:
                    future.result()
                except BaseException as original:
                    cleanup_failures.append(original)
        if terminal_observer is not None:
            try:
                await _entered(terminal_observer.emitted, "actual partial app receipt terminal")
            except BaseException as original:
                cleanup_failures.append(original)
            try:
                terminal_observer.stop_and_join()
            except BaseException as original:
                cleanup_failures.append(original)
    if len(existing) != 1 or len(expected_owner_faults) != 1 or expected_owner_faults[0] is not callback_original:
        cleanup_failures.append(AssertionError("actual preclaimed owner did not return exactly its callback original"))
    if primary is not None:
        if cleanup_failures:
            raise BaseExceptionGroup("Partial control and cleanup retained originals", [primary, *cleanup_failures]) from None
        raise primary
    if cleanup_failures:
        raise BaseExceptionGroup("Partial control cleanup retained originals", cleanup_failures)
    os.write(1, b"ACTUAL_PARTIAL_NO_REPLAY_CONTROL_PASS\n")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="app-failed-partial-control-") as directory:
        patcher = pytest.MonkeyPatch()
        try:
            asyncio.run(run_partial_claimed_control(Path(directory), patcher))
        finally:
            patcher.undo()
