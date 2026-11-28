"""A refused shared-pool admission leaves run monitoring owned and active."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import structlog
from sqlalchemy import update
from structlog.testing import capture_logs

from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import runs_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.blobs.test_service_fencing import _insert_session
from tests.unit.web.execution.test_session_operation_lease import _canonical_execute_lease, _eventually, _execution_service

_SOURCE = {"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv"}, "on_validation_failure": "discard"}


async def _bounded(awaitable: Any, *, seconds: float, label: str) -> Any:
    task = asyncio.create_task(awaitable)
    done, _pending = await asyncio.wait({task}, timeout=seconds)
    if task not in done:
        # Cancel it and wait, bounded, for it to end; a task that does not
        # honour cancellation is left running.
        task.cancel()
        await asyncio.wait({task}, timeout=seconds)
    assert task in done, f"{label} did not complete within {seconds} seconds"
    return await task


@pytest_asyncio.fixture
async def execution_fixture(tmp_path: Path) -> AsyncIterator[ExecutionTestCustody]:
    custody = ExecutionTestCustody(asyncio.get_running_loop(), tmp_path)
    try:
        yield custody
    finally:
        await _bounded(custody.close(), seconds=15, label="execution fixture cleanup")


class _PoolHold:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.holders: list[asyncio.Task[bool]] = []

    async def hold(self) -> None:
        capacity = async_workers.ADMISSION_CAPACITY
        self.holders = [asyncio.create_task(run_sync_in_worker(self.release.wait, 30)) for _ in range(capacity)]
        assert await _eventually(lambda: async_workers.outstanding_admissions() == capacity, seconds=5)

    async def free(self) -> None:
        self.release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*self.holders, return_exceptions=True), timeout=10)
        assert all(outcome is True for outcome in outcomes), outcomes
        assert await _eventually(lambda: async_workers.outstanding_admissions() == 0, seconds=10)


class _Run:
    def __init__(self, service: Any, executor: Any, lease: Any, authority: Any, run_id: UUID) -> None:
        self.service = service
        self.executor = executor
        self.lease = lease
        self.authority = authority
        self.run_id = run_id
        self.shutdown_event = threading.Event()
        obligation = lease.execution_obligation
        assert obligation is not None
        self.obligation = obligation
        self.watcher = service._create_loss_watcher(lease, self.shutdown_event, run_id=run_id)
        service._submit_owned_pipeline(obligation, lease, self.watcher, partial(lambda: None))

    async def end(self, expected_originals: list[BaseException]) -> None:
        self.authority.release_allowed.set()
        if not self.executor.future.selected:
            self.executor.future.set_result(None)
        assert await _eventually(lambda: self.obligation.completion is not None and self.obligation.completion.done(), seconds=10)
        completion = self.obligation.completion
        assert completion is not None
        if completion.cancelled():
            # A CancelledError original cancels the completion handle, so the
            # handle cannot carry it; cleanup checks the registry recorded it.
            assert expected_originals, "completion was cancelled with no expected original"
            assert all(isinstance(expected, asyncio.CancelledError) for expected in expected_originals)
            return
        failure = completion.exception()
        if expected_originals:
            assert failure is not None
            assert all(any(leaf is expected for leaf in execution_custody._leaves(failure)) for expected in expected_originals)
        else:
            assert failure is None


async def _start(
    execution_fixture: ExecutionTestCustody, tmp_path: Path, *, seeded: bool = True, renew_interval: float = 10
) -> tuple[_Run, Any]:
    assert async_workers.outstanding_admissions() == 0
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    if seeded:
        session_id = _insert_session(engine)
        compose = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.COMPOSE)
        run_id = UUID(await _seed_active_run(engine, session_id, session_operation_context=compose, status="pending", source=_SOURCE))
    else:
        run_id = uuid4()
    service, session_service, executor = _execution_service(asyncio.get_running_loop(), execution_fixture)
    session_service.get_run.side_effect = sessions.get_run
    lease, authority = await _canonical_execute_lease(service, execution_fixture, session_id=uuid4(), renew_interval_seconds=renew_interval)
    return _Run(service, executor, lease, authority, run_id), engine


@asynccontextmanager
async def _case(
    execution_fixture: ExecutionTestCustody,
    tmp_path: Path,
    *,
    seeded: bool = True,
    renew_interval: float = 10,
    started: bool = False,
) -> AsyncIterator[tuple[_Run, Any, list[BaseException]]]:
    run, engine = await _start(execution_fixture, tmp_path, seeded=seeded, renew_interval=renew_interval)
    expected_originals: list[BaseException] = []
    try:
        if started:
            # Let the run executor's one worker take this run's pipeline.
            run.executor.future.queue_allowed.set()
            pipeline = run.obligation.pipeline
            assert pipeline is not None
            assert await _eventually(pipeline.running, seconds=5), "the run's pipeline did not start"
        yield run, engine, expected_originals
    except BaseException as body_failure:
        # The case's own assertion stays the reported failure. Cleanup still
        # runs; whatever it raises is noted here, and the execution fixture's
        # teardown separately reports every unwitnessed recorded failure.
        try:
            await _cleanup_case(execution_fixture, run, engine, expected_originals)
        except BaseException as cleanup_failure:
            body_failure.add_note(f"case cleanup also raised {type(cleanup_failure).__name__}: {cleanup_failure}")
        raise
    await _cleanup_case(execution_fixture, run, engine, expected_originals)


async def _cleanup_case(
    execution_fixture: ExecutionTestCustody,
    run: _Run,
    engine: Any,
    expected_originals: list[BaseException],
    *,
    earlier: _Run | None = None,
) -> None:
    try:
        try:
            if earlier is not None:
                await earlier.end([])
        finally:
            await run.end(expected_originals)
    finally:
        try:
            for original in expected_originals:
                execution_fixture.witness_cleanup_original(original)
            if expected_originals:
                with pytest.raises(BaseException) as raised:
                    await _bounded(execution_fixture.shutdown_service(run.service), seconds=10, label="service shutdown")
                leaves = execution_custody._leaves(raised.value)
                assert all(any(leaf is expected for expected in expected_originals) for leaf in leaves), leaves
                assert all(any(leaf is expected for leaf in leaves) for expected in expected_originals)
            else:
                await _bounded(execution_fixture.shutdown_service(run.service), seconds=10, label="service shutdown")
            failures = run.service.execution_lease_release_registry._failures
            assert len(failures) == len(expected_originals)
            for original in expected_originals:
                assert sum(failure is original for failure in failures) == 1
        finally:
            engine.dispose()


@pytest.mark.asyncio
async def test_unrefused_watcher_carries_peer_cancel(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path) as (run, engine, _expected):
        assert not run.watcher.done()
        with engine.begin() as connection:
            connection.execute(
                update(runs_table)
                .where(runs_table.c.id == str(run.run_id))
                .values(cancel_requested_at=datetime.now(UTC), cancellation_source="user")
            )
        assert await _eventually(run.shutdown_event.is_set, seconds=5)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_refused_poll_stays_live_and_carries_peer_cancel(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path) as (run, engine, _expected):
        refusals = 0
        actual = async_workers._acquire_admission

        async def count_refusals() -> None:
            nonlocal refusals
            try:
                await actual()
            except async_workers.AsyncWorkerAdmissionTimeoutError:
                refusals += 1
                raise

        hold = _PoolHold()
        with patch.object(async_workers, "_acquire_admission", count_refusals):
            try:
                await hold.hold()
                assert await _eventually(lambda: refusals >= 1, seconds=5)
                assert not run.watcher.done()
            finally:
                await hold.free()
        with engine.begin() as connection:
            connection.execute(
                update(runs_table)
                .where(runs_table.c.id == str(run.run_id))
                .values(cancel_requested_at=datetime.now(UTC), cancellation_source="user")
            )
        # Bound: the 5 s maximum backoff, plus scheduling and one poll.
        assert await _eventually(run.shutdown_event.is_set, seconds=8)


@pytest.mark.asyncio
@pytest.mark.timing_sensitive
async def test_lease_loss_is_seen_after_refusal(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path, renew_interval=0.05) as (run, _engine, expected):
        actual = async_workers._acquire_admission
        refused = asyncio.Event()

        async def observe_refusal() -> None:
            try:
                await actual()
            except async_workers.AsyncWorkerAdmissionTimeoutError:
                refused.set()
                raise

        hold = _PoolHold()
        with patch.object(async_workers, "_acquire_admission", observe_refusal):
            try:
                await hold.hold()
                await asyncio.wait_for(refused.wait(), timeout=5)
                assert not run.watcher.done()
                loss = RuntimeError("lease renewal failed")
                run.authority.renew_errors_once.append(loss)
                assert await asyncio.wait_for(run.lease.wait_until_lost(), timeout=5) is loss
                assert await _eventually(run.shutdown_event.is_set, seconds=3)
                expected.append(loss)
            finally:
                await hold.free()


@pytest.mark.asyncio
async def test_missing_run_stops_immediately_and_records_once(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path, seeded=False, started=True) as (run, _engine, expected):
        registry = run.service.execution_lease_release_registry
        with capture_logs() as logs:
            assert await _eventually(run.watcher.done, seconds=5)
        original = run.watcher.exception()
        assert isinstance(original, ValueError)
        assert run.shutdown_event.is_set()
        assert registry._failures == [original]
        expected.append(original)
        assert [entry for entry in logs if entry["event"] == "execution_loss_watcher_failure_deferred"] == [], (
            "a started run's watcher failure is recorded at once, not deferred"
        )


@pytest.mark.asyncio
async def test_generation_unavailable_while_live_stops_run(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path, started=True) as (run, _engine, expected):
        original = RequiredGenerationUnavailable("generation unavailable")
        run.service._session_service.get_run.side_effect = original
        assert await _eventually(run.watcher.done, seconds=5)
        registry = run.service.execution_lease_release_registry
        assert run.shutdown_event.is_set()
        assert registry._failures == [original]
        expected.append(original)


@pytest.mark.asyncio
async def test_poll_raised_cancelled_error_is_a_watcher_failure(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path, started=True) as (run, _engine, expected):
        # A CancelledError ends the watcher like any other error: on a started
        # run it stops the run and is recorded at once.
        original = asyncio.CancelledError("poll-raised")
        run.service._session_service.get_run.side_effect = original
        assert await _eventually(run.watcher.done, seconds=5)
        owner = run.service._loss_watcher_owner(run.watcher, run.lease)
        registry = run.service.execution_lease_release_registry
        assert owner.outcome == (run.watcher, original), "a watcher CancelledError is the watcher's original"
        assert run.shutdown_event.is_set()
        assert registry._failures == [original]
        expected.append(original)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_class",
    [pytest.param(RequiredGenerationUnavailable, id="generation-unavailable"), pytest.param(ValueError, id="other-error")],
)
async def test_watcher_original_survives_a_failing_stop_step(
    tmp_path: Path, execution_fixture: ExecutionTestCustody, failure_class: type[Exception]
) -> None:
    async with _case(execution_fixture, tmp_path, started=True) as (run, _engine, expected):
        # The watcher keeps its own original as its outcome before it stops the
        # run, so a stop step that raises cannot leave the join without it.
        original = failure_class("poll failed")
        stop_failure = RuntimeError("stop step failed")
        with patch("elspeth.web.execution.service._execution_pipeline_started", side_effect=stop_failure):
            run.service._session_service.get_run.side_effect = original
            assert await _eventually(run.watcher.done, seconds=5)
        owner = run.service._loss_watcher_owner(run.watcher, run.lease)
        try:
            assert run.watcher.exception() is stop_failure
            assert run.shutdown_event.is_set()
            cancellations, watcher_error = await run.service._join_loss_watcher(run.watcher, run.lease)
            assert watcher_error is original, f"the join must return the watcher's original, got {watcher_error!r}"
            assert cancellations == []
            assert owner.outcome == (run.watcher, original)
        finally:
            expected.append(original)
            if owner.outcome is None:
                # Bound for a failing run: service shutdown waits until it has
                # observed every finished watcher's outcome, and its wait cannot
                # be cancelled, so a watcher left without one would hang this
                # case's cleanup. Supply the outcome so the assertion above stays
                # the reported failure. Reached only if the watcher records its
                # outcome after its stop step.
                owner.outcome = (run.watcher, original)


@pytest.mark.asyncio
async def test_queued_run_watcher_death_waits_for_its_own_end(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    executing, engine = await _start(execution_fixture, tmp_path)
    expected: list[BaseException] = []
    queued: _Run | None = None
    try:
        # The run executor has one worker: the first run takes it and the
        # second run's pipeline waits in the queue behind it.
        executing.executor.future.queue_allowed.set()
        executing_pipeline = executing.obligation.pipeline
        assert executing_pipeline is not None
        assert await _eventually(executing_pipeline.running, seconds=5), "the executing run's pipeline did not start"
        lease, authority = await _canonical_execute_lease(executing.service, execution_fixture, session_id=uuid4())
        queued_run_id = uuid4()
        with capture_logs() as logs:
            queued = _Run(executing.service, executing.executor, lease, authority, queued_run_id)
            queued_pipeline = queued.obligation.pipeline
            assert queued_pipeline is not None and not queued_pipeline.running() and not queued_pipeline.done()
            assert await _eventually(queued.watcher.done, seconds=5)
        original = queued.watcher.exception()
        assert isinstance(original, ValueError)
        registry = executing.service.execution_lease_release_registry
        assert registry._failures == [], "a queued run's watcher failure must not be recorded while another run executes"
        assert queued.shutdown_event.is_set(), "a queued run whose watcher died must never start work"
        assert not execution_fixture.recovery.instance_draining.is_set()
        assert not executing.shutdown_event.is_set()
        assert executing_pipeline.running()
        deferred = [entry for entry in logs if entry["event"] == "execution_loss_watcher_failure_deferred"]
        assert deferred == [
            {
                "event": "execution_loss_watcher_failure_deferred",
                "log_level": "error",
                "run_id": str(queued_run_id),
                "exc_class": "ValueError",
            }
        ], "a deferred watcher failure is logged once, by class name, when it is deferred"
        await executing.end([])
        expected.append(original)
        await queued.end(expected)
        assert registry._failures == [original], "the queued run's failure is recorded once its own run ends"
    except BaseException as body_failure:
        try:
            if queued is None:
                await _cleanup_case(execution_fixture, executing, engine, expected)
            else:
                await _cleanup_case(execution_fixture, queued, engine, expected, earlier=executing)
        except BaseException as cleanup_failure:
            body_failure.add_note(f"case cleanup also raised {type(cleanup_failure).__name__}: {cleanup_failure}")
        raise
    await _cleanup_case(execution_fixture, queued, engine, expected, earlier=executing)


@pytest.mark.asyncio
async def test_normal_shutdown_closes_watcher_without_failure(tmp_path: Path, execution_fixture: ExecutionTestCustody) -> None:
    async with _case(execution_fixture, tmp_path) as (run, _engine, _expected):
        execution_fixture.recovery.begin_shutdown()
        assert await _eventually(run.watcher.done, seconds=3)
        assert run.watcher.exception() is None
    assert run.watcher.done()
    assert run.service._executor_join_task is not None and run.service._executor_join_task.done()
    assert run.service._registry_join_task is not None and run.service._registry_join_task.done()
    assert run.service.execution_lease_release_registry.executor_join_succeeded
    assert async_workers.outstanding_admissions() == 0
    assert execution_fixture.owns_lifecycle
    await _bounded(execution_fixture.close(), seconds=15, label="pool worker shutdown")
    assert async_workers._SHARED_EXECUTOR is None
    assert async_workers.outstanding_admissions() == 0
