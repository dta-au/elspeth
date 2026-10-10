"""Actual SQL custody witnesses; no engine/provider execution or app boot."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from functools import partial
from pathlib import Path
from types import MethodType
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.composer import yaml_generator
from elspeth.web.coordination.repository import _SessionOperationAuthorityRepository
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import session_operation_fences_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import seed_session_operation_fence
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.conftest import _make_session
from tests.unit.web.execution.test_session_operation_lease import _canonical_execute_lease, _execution_service
from tests.unit.web.test_app import _settings

execution_fixture = execution_custody.execution_fixture


def _insert_session(engine) -> UUID:
    """Seed only a released CREATE fence, then use actual authority acquisition."""
    session_id = uuid4()
    with engine.begin() as connection:
        _make_session(connection, session_id=str(session_id), user_id="test-user", auth_provider_type="local", title="Fenced blobs")
        seed_session_operation_fence(connection, session_id, owner_instance_id="canonical-run-fixture")
    return session_id


def _construct(custody: ExecutionTestCustody, root: Path):
    engine = create_session_engine(f"sqlite:///{root / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("constructor-controls"))
    registry = custody.registry(asyncio.get_running_loop())
    try:
        service = custody.bind(
            ExecutionServiceImpl.for_trained_operator(
                loop=asyncio.get_running_loop(),
                broadcaster=ProgressBroadcaster(asyncio.get_running_loop()),
                settings=_settings(root, composer_boot_probe_enabled=False),
                session_service=sessions,
                yaml_generator=yaml_generator,
                telemetry=build_sessions_telemetry(),
                execution_lease_release_registry=registry,
            )
        )
    except BaseException:
        engine.dispose()
        raise
    return service, registry, engine


def _reap_owned_child(process: subprocess.Popen) -> list[BaseException]:
    originals: list[BaseException] = []
    try:
        alive = process.poll() is None
    except BaseException as original:
        originals.append(original)
        alive = True  # Unknown does not establish child absence.
    if alive:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # Exact child is still waited below after this exit race.
        except BaseException as original:
            originals.append(original)
    try:
        process.wait(timeout=1 if alive else 2)
    except BaseException as original:
        originals.append(original)
        # Signal/wait faults do not skip the remaining exact owned attempts.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except BaseException as original:
            originals.append(original)
        try:
            process.wait(timeout=2)
        except BaseException as original:
            originals.append(original)
    return originals


def _project_reaping_failures(primary: BaseException | None, originals: list[BaseException]) -> None:
    if originals:
        raise BaseExceptionGroup(
            "SQL custody assertion and owned child cleanup failed", [*([primary] if primary is not None else []), *originals]
        ) from None


@pytest.mark.parametrize("mode", ["exit_race", "terminate", "kill", "wait", "poll"])
def test_exact_child_reaping_instrument_preserves_wait_and_all_originals(mode: str):
    causal = AssertionError("original custody assertion")
    signal_error = RuntimeError("private signal original")
    wait_error = subprocess.TimeoutExpired("exact owned child", 1)
    final_wait_error = RuntimeError("private wait original")
    poll_error = RuntimeError("private poll original")

    class ControlledChild:
        # Instrument double only; never grants pipeline/process receipts.
        returncode = None

        def __init__(self):
            self.actions = []

        def poll(self):
            self.actions.append("poll")
            if mode == "poll":
                raise poll_error
            return None

        def terminate(self):
            self.actions.append("terminate")
            if mode == "exit_race":
                raise ProcessLookupError("already exited")
            if mode == "terminate":
                raise signal_error

        def kill(self):
            self.actions.append("kill")
            if mode == "kill":
                raise signal_error

        def wait(self, *, timeout):
            self.actions.append(("wait", timeout))
            if mode in {"kill", "wait"}:
                if timeout == 1:
                    raise wait_error
                if mode == "wait":
                    raise final_wait_error
            self.returncode = 0
            return 0

    child = ControlledChild()
    originals = _reap_owned_child(child)
    assert ("wait", 1) in child.actions
    expected = {
        "exit_race": [],
        "terminate": [signal_error],
        "kill": [wait_error, signal_error],
        "wait": [wait_error, final_wait_error],
        "poll": [poll_error],
    }[mode]
    assert len(originals) == len(expected)
    assert all(actual is original for actual, original in zip(originals, expected, strict=True))
    if mode in {"kill", "wait"}:
        assert child.actions[-1] == ("wait", 2)
    if originals:
        with pytest.raises(BaseExceptionGroup) as grouped:
            _project_reaping_failures(causal, originals)
        assert grouped.value.exceptions == (causal, *expected)
    else:
        _project_reaping_failures(causal, originals)
        assert child.returncode == 0


def _publish_checkpoint(path: Path, proof: dict) -> None:
    staged = path.with_name(path.name + ".pending")
    staged.write_bytes(json.dumps(proof, sort_keys=True).encode())
    staged.replace(path)


async def _until(predicate) -> None:
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_canonical_sql_observation_preserves_dispatch_and_exact_committed_context(execution_fixture: ExecutionTestCustody):
    _service, registry, engine = _construct(execution_fixture, execution_fixture.data_dir)
    try:
        observed = execution_fixture.observe_authority(uuid4())
        acquire, release = observed.authority.acquire, observed.authority.release
        assert type(acquire) is MethodType and acquire.__func__ is _SessionOperationAuthorityRepository.acquire
        assert type(release) is MethodType and release.__func__ is _SessionOperationAuthorityRepository.release
        lease = await execution_fixture.acquire(
            registry, observed.authority, session_id=observed.session_id, owner_instance_id="canonical-observer-positive", lease_seconds=30
        )
        obligation = lease.execution_obligation
        assert obligation is not None
        assert observed.calls == [("acquire", lease.context)]
        assert observed.release_calls == [] and not observed.release_called.is_set()
        await lease.close()
        assert observed.calls == [("acquire", lease.context), ("release", lease.context)]
        assert observed.release_calls == [lease.context]
        submission = obligation.release_submission
        assert submission is not None and submission.future is not None and submission.future.done()
        assert submission.callback_return_observed and submission.observed
        assert submission.reservation is not None and submission.reservation.released
        assert submission.reservation.witness.snapshot().callable_finished
        assert obligation.release_succeeded
        with observed.authority._engine.connect() as connection:
            row = (
                connection.execute(
                    select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(observed.session_id))
                )
                .mappings()
                .one()
            )
        assert row["operation_id"] == lease.context.fence.operation_id and row["released_at"] is not None
        assert observed.authority.acquire.__func__ is acquire.__func__
        assert observed.authority.release.__func__ is release.__func__
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_canonical_registry_refuses_foreign_authority_before_dispatch(execution_fixture: ExecutionTestCustody):
    _service, registry, engine = _construct(execution_fixture, execution_fixture.data_dir)
    effects = []

    class ForeignAuthority:
        def acquire(self, **kwargs):
            effects.append("acquire")
            raise AssertionError("foreign acquisition entered")

        def release(self, context):
            effects.append("release")
            raise AssertionError("foreign release entered")

    try:
        with pytest.raises(AuditIntegrityError, match="exact application authority"):
            registry.admit(ForeignAuthority(), session_id=uuid4(), owner_instance_id="foreign-negative", lease_seconds=30)
        assert effects == []
        assert not registry.has_pending_physical_owners()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_foreign_callback_future_refused_before_completion_then_actual_owner_joins(execution_fixture: ExecutionTestCustody):
    service, _sessions, control = _execution_service(asyncio.get_running_loop(), execution_fixture)
    lease, observation = await _canonical_execute_lease(service, execution_fixture, session_id=uuid4())
    obligation = lease.execution_obligation
    assert obligation is not None
    watcher = service._create_loss_watcher(lease, threading.Event(), run_id=uuid4())
    service._submit_owned_pipeline(obligation, lease, watcher, partial(lambda: None))
    actual = obligation.pipeline
    assert actual is control.future.actual and actual is not None
    foreign = Future()
    with pytest.raises(AuditIntegrityError, match="replaced actual terminal owner") as caught:
        service._on_pipeline_done(foreign, session_operation_lease=lease, loss_watcher=watcher)
    assert obligation.pipeline is actual and not actual.done()
    assert obligation.completion is None and obligation.completion_task is None
    assert actual not in service._pipeline_completion_started
    assert foreign not in service._pipeline_completion_owners
    assert observation.release_calls == [] and not lease.closed
    assert any(original is caught.value for original in obligation.registry._failures)
    observation.release_allowed.set()
    control.future.set_result(None)
    await _until(lambda: obligation.completion_observed and obligation.retired)
    assert actual.done() and actual.result() is None
    assert watcher.done() and lease.closed and obligation.release_succeeded
    assert observation.release_calls == [lease.context]
    assert not obligation.registry.has_pending_physical_owners()
    execution_fixture.witness_cleanup_original(caught.value)


async def _failed_release_child(root: Path) -> None:
    custody = ExecutionTestCustody(asyncio.get_running_loop(), root)
    service, registry, engine = _construct(custody, root)
    sessions = service._session_service
    failed_sid, healthy_sid = _insert_session(engine), _insert_session(engine)
    run_ids = {}
    for session_id in (failed_sid, healthy_sid):
        # Existing fixture-only run records let the original watcher read the
        # actual session DB. This is no Composer authoring/provider assertion.
        context = sessions.session_operation_authority.acquire(
            session_id=session_id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="canonical-run-fixture", lease_seconds=300
        )
        run_ids[session_id] = await _seed_active_run(
            engine,
            session_id,
            session_operation_context=context,
            source={
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "discard",
                "options": {"path": "unused-owned-custody.csv"},
            },
        )
        sessions.session_operation_authority.release(context)
    failed_observation = custody.observe_existing_authority(sessions.session_operation_authority, failed_sid)
    # Two observations cannot replace the same private method pair: use a
    # distinct actual SQLite authority over the same actual sessions engine.
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority

    healthy_observation = custody.observe_existing_authority(SQLiteLocalSessionOperationAuthority(engine), healthy_sid)
    failed_lease = await custody.acquire(
        registry, failed_observation.authority, session_id=failed_sid, owner_instance_id="failed-real-sql", lease_seconds=30
    )
    healthy_lease = await custody.acquire(
        registry, healthy_observation.authority, session_id=healthy_sid, owner_instance_id="healthy-real-sql", lease_seconds=30
    )
    original = OSError("private-actual-release-original")
    failed_observation.release_error = original
    failed_cap, healthy_cap = failed_lease.execution_obligation, healthy_lease.execution_obligation
    assert failed_cap is not None and healthy_cap is not None
    failed_gate, healthy_gate = threading.Event(), threading.Event()

    def endpoint(gate):
        assert gate.wait(8), "actual private pipeline endpoint was not released"
        return None

    failed_watcher = service._create_loss_watcher(failed_lease, threading.Event(), run_id=UUID(run_ids[failed_sid]))
    healthy_watcher = service._create_loss_watcher(healthy_lease, threading.Event(), run_id=UUID(run_ids[healthy_sid]))
    service._submit_owned_pipeline(failed_cap, failed_lease, failed_watcher, lambda: endpoint(failed_gate))
    service._submit_owned_pipeline(healthy_cap, healthy_lease, healthy_watcher, lambda: endpoint(healthy_gate))
    failed_gate.set()
    await _until(lambda: failed_cap.completion_task is not None and failed_cap.completion_task.done())
    healthy_gate.set()
    await _until(lambda: healthy_cap.completion_task is not None and healthy_cap.completion_task.done())
    release = failed_cap.release_submission
    assert release is not None and release.future is not None and release.reservation is not None
    assert release.obligation is failed_cap
    assert failed_cap._release.__self__ is failed_observation.authority
    assert failed_cap._release.__func__ is _SessionOperationAuthorityRepository.release
    assert failed_observation.release_called.is_set()
    assert failed_observation.release_calls == [failed_lease.context]
    assert failed_lease.context is failed_cap.acquire_submission.source_value
    registry.observe_ready()
    trace = release.reservation.witness.snapshot()
    assert release.future.done() and release.future.exception() is original
    assert release.original_error is original and release.observed
    assert release.reservation.future is release.future
    assert release.reservation.released and release.callback_return_observed
    assert trace.callable_finished and trace.exited and not trace.impossible
    assert release.reservation._registering_generation is failed_cap.generation
    assert failed_cap.generation is healthy_cap.generation is async_workers._GENERATION_CUSTODIAN
    assert failed_cap.generation.executor is async_workers._SHARED_EXECUTOR
    assert failed_cap.completion_original_error is original
    assert healthy_cap.completion_original_error is None and healthy_cap.completion_observed
    assert healthy_lease.closed and healthy_cap.release_succeeded and healthy_cap.retired
    assert any(error is original for error in registry._failures)
    assert not failed_cap.release_succeeded and not failed_cap.retired
    assert registry.owns_submission(release)
    assert registry.has_pending_physical_owners()
    assert async_workers.outstanding_admissions() == 0
    custody.owner.seal()
    custody.recovery.begin_shutdown()
    shutdown = asyncio.create_task(service.shutdown())
    await _until(lambda: registry.executor_join_succeeded)
    assert not shutdown.done()
    with pytest.raises(BaseExceptionGroup) as refused:
        registry.assert_completed()
    assert any(error is original for error in refused.value.exceptions)
    assert not custody.recovery.watchdog.completed
    _publish_checkpoint(
        root / "checkpoint.json",
        {
            "actual_failed_SQL_Future_original": True,
            "actual_invocation_finished_exited": True,
            "actual_counter_and_callback_return": True,
            "exact_captured_generation": True,
            "successful_independent_release": True,
            "failed_release_retained_pending": True,
            "private_executor_actual_joined": True,
            "shutdown_remains_pending": True,
            "COMPLETE_refused": True,
            "watchdog_completion_unsent": True,
        },
    )
    # Source Unknown is bounded by the exact parent-owned process. The parent
    # terminates and waits for every physical thread through process death.
    await asyncio.Event().wait()


def test_actual_failed_release_stays_pending_while_independent_release_and_private_join_complete(tmp_path: Path):
    repo = Path(__file__).resolve().parents[4]
    env = {
        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo / "elspeth-lints/src"), str(repo))),
        "PYTHONUNBUFFERED": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    primary = None
    log = tmp_path / "failed-release-child.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--failed-release-child", str(tmp_path)],
            cwd=repo,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=False,
        )
        try:
            checkpoint = tmp_path / "checkpoint.json"
            deadline = time.monotonic() + 15
            while not checkpoint.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert checkpoint.exists(), "actual SQL custody child failed before its oracle: " + log.read_text()
            proof = json.loads(checkpoint.read_bytes())
            assert proof == {
                "actual_failed_SQL_Future_original": True,
                "actual_invocation_finished_exited": True,
                "actual_counter_and_callback_return": True,
                "exact_captured_generation": True,
                "successful_independent_release": True,
                "failed_release_retained_pending": True,
                "private_executor_actual_joined": True,
                "shutdown_remains_pending": True,
                "COMPLETE_refused": True,
                "watchdog_completion_unsent": True,
            }
            assert process.poll() is None, "failed release silently completed its actual pending custody"
        except BaseException as original:
            primary = original
            raise
        finally:
            originals = _reap_owned_child(process)
            try:
                (tmp_path / "failed-release-child.exit").write_text(str(process.returncode) + "\n")
            except BaseException as original:
                originals.append(original)
            if process.returncode is None:
                originals.append(AssertionError("exact owned SQL custody child was not waited"))
            _project_reaping_failures(primary, originals)


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "--failed-release-child"
    asyncio.run(_failed_release_child(Path(sys.argv[2])))
