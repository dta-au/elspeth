"""Owned child for the actual failed-finalizer physical-join control."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import elspeth
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web.application_finalizers import ApplicationFinalizerKind
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from tests.unit.web.test_execute_lease_cleanup_core import _core, _lease


def _mark(label: str) -> None:
    os.write(1, f"{label}\n".encode())


async def _wait_event(event: threading.Event, label: str, seconds: float = 10) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not event.is_set():
        assert loop.time() < deadline, f"actual {label} phase did not enter"
        await asyncio.sleep(0.001)


async def _wait_task(task: asyncio.Task[object], label: str, seconds: float = 10) -> object:
    done, _ = await asyncio.wait({task}, timeout=seconds)
    assert done, f"actual {label} Task did not finish"
    return await task


async def run_failed_callback_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert Path(elspeth.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2] / "src")
    fixture, registry = await _core(tmp_path)
    service = fixture.app.state.session_service
    first, first_lease = await _lease(fixture, registry, fixture.session_id)
    other = await service.create_session("transport-user", "Actual independent sibling", "local")
    second, second_lease = await _lease(fixture, registry, other.id)
    private = None
    private_gate = threading.Event()
    private_started = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    callback_failed = threading.Event()
    private_join_returned = threading.Event()
    second_sql_exited = threading.Event()
    monitor_stop = threading.Event()
    monitor_emitted = threading.Event()
    callback_original = AuditIntegrityError("actual executor finalizer callback failed once")
    delivered = []
    closing_first = None
    closing_second = None
    joining = None
    first_issuance_started = False
    second_issuance_started = False
    finalizer_issuance_started = False
    joined_original = None
    primary: BaseException | None = None
    cleanup_failures: list[BaseException] = []
    actual_retain = async_workers._retain_cancellation
    generation = async_workers._GENERATION_CUSTODIAN
    assert generation is not None
    actual_record = generation.record_completed
    actual_transaction = SQLiteLocalSessionOperationAuthority._locked_transaction

    def private_work():
        private_started.set()
        assert private_gate.wait(20), "actual private work gate did not open"

    def actual_join():
        assert private is not None
        private.shutdown(wait=True, cancel_futures=False)
        registry.record_executor_join_return(capability, private)
        private_join_returned.set()
        _mark("ACTUAL_PRIVATE_JOIN_RETURNED")

    capability = registry.owner.register(ApplicationFinalizerKind.EXECUTION_EXECUTOR_JOIN, actual_join)
    registry.bind_executor_finalizer(capability)
    registry.declare_executor_allocation(capability)
    private = ThreadPoolExecutor(max_workers=1)
    registry.bind_execution_executor(capability, private)
    private_future = private.submit(private_work)

    def fail_once(reservation):
        if reservation is capability._physical_reservation and not callback_failed.is_set():
            callback_failed.set()
            raise callback_original
        actual_record(reservation)

    def retain_delivered(target, original):
        actual_retain(target, original)
        if asyncio.current_task() is joining and all(original is not earlier for earlier in delivered):
            delivered.append(original)

    @contextmanager
    def hold_actual_second(self, session_id):
        with actual_transaction(self, session_id) as connection:
            if session_id == str(other.id):
                second_started.set()
                assert release_second.wait(20), "actual second SQL release gate did not open"
            yield connection
        if session_id == str(other.id):
            second_sql_exited.set()
            _mark("ACTUAL_SECOND_SQL_TRANSACTION_EXITED")

    monkeypatch.setattr(generation, "record_completed", fail_once)
    monkeypatch.setattr(async_workers, "_retain_cancellation", retain_delivered)
    monkeypatch.setattr(SQLiteLocalSessionOperationAuthority, "_locked_transaction", hold_actual_second)
    registry.seal()
    registry.owner.seal()
    fixture.app.state.process_recovery.begin_shutdown()

    def observe_terminal() -> None:
        try:
            while not monitor_stop.is_set():
                drain = generation.drain_thread
                if (
                    callback_failed.is_set()
                    and private_join_returned.is_set()
                    and second_sql_exited.is_set()
                    and closing_second is not None
                    and closing_second.done()
                    and second.retired
                    and drain is not None
                    and not drain.is_alive()
                ):
                    reservation = capability._physical_reservation
                    assert reservation is not None and reservation._registering_generation is generation
                    future = capability._physical_future
                    trace = reservation.witness.snapshot()
                    status = {
                        "private_join_returned": private_join_returned.is_set(),
                        "second_sql_exited": second_sql_exited.is_set(),
                        "second_release_retired": closing_second.done() and second.retired,
                        "callback_entered": callback_failed.is_set(),
                        "future_done": future is not None and future.done() and future is reservation.future,
                        "counter_returned": reservation.released and capability._physical_admission_release_return_observed,
                        "callback_exit_receipt": capability._physical_callback_failure_exit_observed,
                        "callback_original_retained": any(error is callback_original for error in capability.physical_failure_originals),
                        "invocation_exited": trace.callable_finished and trace.exited and not trace.impossible,
                        "generation_joined": generation.joined.is_set(),
                        "drain_error": type(generation.drain_error).__name__ if generation.drain_error is not None else None,
                        "getter": registry.executor_join_physically_observed,
                    }
                    os.write(1, ("ACTUAL_TERMINAL_SNAPSHOT " + json.dumps(status, sort_keys=True) + "\n").encode())
                    monitor_emitted.set()
                    return
                time.sleep(0.005)
        except BaseException as original:
            os.write(1, f"ACTUAL_TERMINAL_OBSERVER_ERROR {type(original).__name__}\n".encode())

    monitor = threading.Thread(target=observe_terminal, name="actual-getter-terminal-observer", daemon=True)
    monitor.start()
    try:
        first_issuance_started = True
        closing_first = asyncio.create_task(first_lease.close())
        await _wait_task(closing_first, "first SQL release")
        _mark("ACTUAL_FIRST_SQL_RELEASE_RETURNED")
        registry.observe_ready()
        assert first.retired and not second.retired
        # Complete the first close before starting the held second transaction.
        # This removes an avoidable competing-order dependency in this control.
        second_issuance_started = True
        closing_second = asyncio.create_task(second_lease.close())
        finalizer_issuance_started = True
        joining = asyncio.create_task(async_workers.run_application_finalizer_in_worker(capability))
        await _wait_event(private_started, "private worker")
        await _wait_event(second_started, "second SQL release")
        _mark("ACTUAL_PRIVATE_AND_SECOND_SQL_ENTERED")
        assert not registry.executor_join_physically_observed
        assert not closing_second.done() and not joining.done()
        private_gate.set()
        await _wait_event(callback_failed, "failed finalizer callback")
        _mark("ACTUAL_FINALIZER_CALLBACK_ENTERED")
        assert private_future.done()
        assert not generation.joined.is_set()
        assert not registry.executor_join_physically_observed
        for index in range(3):
            joining.cancel(f"actual failed physical join cancellation {index}")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while len(delivered) != index + 1:
                assert loop.time() < deadline, f"actual cancellation {index} was not delivered"
                await asyncio.sleep(0.001)
            _mark(f"ACTUAL_CANCELLATION_DELIVERED_{index}")
        # Failure cannot prevent observation of this independently initiated
        # exact lease release. Shared generation join still waits for it.
        release_second.set()
        await _wait_task(closing_second, "second SQL release")
        _mark("ACTUAL_SECOND_SQL_RELEASE_RETURNED")
        registry.observe_ready()
        assert second.retired
        with pytest.raises(BaseExceptionGroup) as raised:
            await _wait_task(joining, "failed physical finalizer")
        joined_original = raised.value
        expected = (*delivered, callback_original)
        assert len(raised.value.exceptions) == len(expected)
        assert all(actual is original for actual, original in zip(raised.value.exceptions, expected, strict=True))
        registry.record_failure(raised.value)
        assert generation.joined.is_set()
        await _wait_event(monitor_emitted, "actual terminal snapshot")
        assert registry.executor_join_physically_observed
        assert not registry.executor_join_succeeded
        assert registry.executor_join_failures == (callback_original,)
        assert registry.joined_pipeline_submission_failures() == ()
        assert async_workers.outstanding_admissions() == 0
        with pytest.raises(BaseExceptionGroup) as refusal:
            registry.assert_completed()
        assert any(original is raised.value for original in refusal.value.exceptions)
        fixture.app.state.process_recovery.watchdog.assert_watching()
    except BaseException as original:
        primary = original
    finally:
        private_gate.set()
        release_second.set()
        if (
            private_join_returned.is_set()
            and second_sql_exited.is_set()
            and generation.drain_thread is not None
            and not generation.drain_thread.is_alive()
        ):
            try:
                await _wait_event(monitor_emitted, "actual terminal snapshot", seconds=1)
            except BaseException as original:
                cleanup_failures.append(original)
        monitor_stop.set()
        monitor.join(timeout=1)
        if monitor.is_alive():
            cleanup_failures.append(AssertionError("actual terminal observer thread remains alive"))
        for label, started, task in (
            ("first close", first_issuance_started, closing_first),
            ("second close", second_issuance_started, closing_second),
            ("finalizer", finalizer_issuance_started, joining),
        ):
            if started and task is None:
                cleanup_failures.append(AssertionError(f"actual {label} Task allocation outcome Unknown"))
                continue
            if task is None:
                continue
            if not task.done():
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    cleanup_failures.append(AssertionError("actual release or finalizer Task remains unresolved"))
            if task.done():
                try:
                    task.result()
                except BaseException as original:
                    if task is not joining or original is not joined_original:
                        cleanup_failures.append(original)
        try:
            private.shutdown(wait=True, cancel_futures=False)
        except BaseException as original:
            cleanup_failures.append(original)
        try:
            fixture.app.state.session_engine.dispose()
        except BaseException as original:
            cleanup_failures.append(original)
    if primary is not None:
        if cleanup_failures:
            raise BaseExceptionGroup("Physical getter control and cleanup retained originals", [primary, *cleanup_failures]) from None
        raise primary
    if cleanup_failures:
        raise BaseExceptionGroup("Physical getter cleanup retained originals", cleanup_failures)
    _mark("ACTUAL_FAILED_PHYSICAL_GETTER_CONTROL_PASS")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="actual-getter-control-") as directory:
        patcher = pytest.MonkeyPatch()
        try:
            asyncio.run(run_failed_callback_control(Path(directory), patcher))
        finally:
            patcher.undo()
