"""Terminal handoff preserves engine truth and atomically commits its event."""

from uuid import UUID

import pytest
import structlog
from sqlalchemy import delete, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.enums import RunStatus
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.execution.recovery import RecoveryObservation, project_terminal
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import run_events_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers.session_fences import seed_live_operation_context
from tests.unit.web.blobs.test_service import _seed_active_run
from tests.unit.web.blobs.test_service_fencing import _insert_session
from tests.unit.web.execution.test_run_accounting_schemas import _fanout_accounting


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", ["pending", "running"])
async def test_terminal_recovery_preserves_completed_and_emits_once(initial_status):
    engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = _insert_session(engine)
    compose = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.COMPOSE)
    run_id = UUID(
        await _seed_active_run(
            engine,
            session_id,
            session_operation_context=compose,
            status=initial_status,
            source={"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv"}, "on_validation_failure": "discard"},
        )
    )
    execute = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
    observation = RecoveryObservation(RunStatus.COMPLETED, False, _fanout_accounting())
    run = await sessions.get_run(run_id)
    sessions.session_operation_authority.mutate(execute, lambda tx: project_terminal(tx, run=run, observation=observation))
    completed = await sessions.get_run(run_id)
    assert completed.status == "completed"
    assert completed.landscape_run_id == str(run_id)
    assert completed.rows_processed == 1
    assert completed.rows_succeeded == 9323
    sessions.session_operation_authority.mutate(execute, lambda tx: project_terminal(tx, run=completed, observation=observation))
    with engine.connect() as connection:
        events = connection.execute(select(run_events_table).where(run_events_table.c.run_id == str(run_id))).all()
    assert len(events) == 1
    assert events[0].event_type == "completed"
    assert events[0].data["status"] == "completed"
    # Reproduce the ordinary worker's status-before-event crash seam.
    with engine.begin() as connection:
        connection.execute(delete(run_events_table).where(run_events_table.c.run_id == str(run_id)))
    sessions.session_operation_authority.mutate(execute, lambda tx: project_terminal(tx, run=completed, observation=observation))
    with engine.connect() as connection:
        repaired = connection.execute(select(run_events_table).where(run_events_table.c.run_id == str(run_id))).all()
    assert len(repaired) == 1
    assert repaired[0].event_type == "completed"
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_leader,dispatch_accepts,rebind_refused", [(True, False, False), (False, True, False), (False, False, False), (False, False, True)]
)
async def test_live_seat_defers_and_dispatch_transfers_only_on_acceptance(monkeypatch, live_leader, dispatch_accepts, rebind_refused):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, Mock, create_autospec
    from uuid import uuid4

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.execution import recovery as recovery_module
    from elspeth.web.execution.recovery import RunRecoveryCoordinator
    from elspeth.web.execution.service import ExecutionServiceImpl
    from elspeth.web.sessions.protocol import RunRecord, SessionOperationAuthority

    run = RunRecord(
        id=uuid4(),
        session_id=uuid4(),
        state_id=uuid4(),
        status="running",
        started_at=datetime.now(UTC),
        finished_at=None,
        rows_processed=0,
        rows_succeeded=0,
        rows_failed=0,
        rows_routed_success=0,
        rows_routed_failure=0,
        rows_quarantined=0,
        error=None,
        landscape_run_id=None,
        pipeline_yaml=None,
    )
    sessions = Mock(spec=SessionServiceImpl)
    sessions.list_recoverable_run_records = AsyncMock(spec=SessionServiceImpl.list_recoverable_run_records, return_value=(run,))
    sessions.get_run = AsyncMock(spec=SessionServiceImpl.get_run, return_value=run)
    sessions.session_operation_authority = Mock(spec=SessionOperationAuthority)
    if rebind_refused:
        sessions.session_operation_authority.mutate.side_effect = SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
    sessions.session_operation_owner_instance_id = "recoverer"
    sessions.session_operation_lease_seconds = 30
    execution = create_autospec(ExecutionServiceImpl, instance=True)
    execution.get_live_run_ids.return_value = frozenset()
    execution.recover_run.return_value = dispatch_accepts
    lease = Mock(spec=SessionOperationLease)
    lease.close = AsyncMock(spec=SessionOperationLease.close)
    monkeypatch.setattr(SessionOperationLease, "acquire", AsyncMock(spec=SessionOperationLease.acquire, return_value=lease))
    monkeypatch.setattr(
        recovery_module,
        "observe_run",
        Mock(spec=recovery_module.observe_run, return_value=RecoveryObservation(RunStatus.RUNNING, live_leader, None)),
    )
    coordinator = RunRecoveryCoordinator(sessions, execution, Mock(spec=BlobServiceImpl), landscape_url="sqlite://", create_tables=False)
    await coordinator.recover()
    if live_leader or rebind_refused:
        execution.recover_run.assert_not_called()
    else:
        execution.recover_run.assert_awaited_once_with(run, lease, resume_existing=True)
    assert lease.close.await_count == (0 if not live_leader and dispatch_accepts else 1)
    if live_leader:
        sessions.session_operation_authority.mutate.assert_not_called()


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.INTERRUPTED])
def test_stale_reconciliation_leader_refuses_before_projection(tmp_path, monkeypatch, status):
    """A takeover after acquisition cannot project or finalize web outputs."""
    import asyncio
    import json
    from unittest.mock import Mock
    from uuid import uuid4

    from elspeth.contracts.coordination import mint_worker_id
    from elspeth.contracts.errors import RunLeadershipLostError
    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
    from elspeth.core.landscape.schema import metadata, run_coordination_events_table
    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.execution.recovery import RunRecoveryCoordinator
    from elspeth.web.execution.service import ExecutionServiceImpl
    from elspeth.web.sessions.protocol import RunRecord
    from tests.fixtures.landscape import leader_token_for, make_factory

    landscape_url = f"sqlite:///{tmp_path / 'reconciliation.db'}"
    run = Mock(spec=RunRecord, id=uuid4())
    sessions = Mock(spec=SessionServiceImpl)
    execution = Mock(spec=ExecutionServiceImpl)
    blobs = Mock(spec=BlobServiceImpl)
    lease = Mock(spec=SessionOperationLease)
    lease.guard_external_effect.side_effect = AssertionError("stale Landscape authority reached external effects")
    coordinator = RunRecoveryCoordinator(sessions, execution, blobs, landscape_url=landscape_url, create_tables=False)
    acquire = RunCoordinationRepository.acquire_reconciliation_leadership
    before = {}
    prior_event_seqs = set()

    with LandscapeDB.from_url(landscape_url, create_tables=True) as db:
        factory = make_factory(db)
        factory.run_lifecycle.begin_run({}, "v1", run_id=str(run.id))
        owner = leader_token_for(db, str(run.id))
        factory.run_lifecycle.complete_run(status, coordination_token=owner)
        factory.run_coordination.release_seat(token=owner)

        def supersede_before_return(repository, **kwargs):
            stale = acquire(repository, **kwargs)
            repository.release_seat(token=stale)
            acquire(
                repository,
                run_id=str(run.id),
                worker_id=mint_worker_id(str(run.id)),
                window_seconds=30,
                expected_status=status,
            )
            # Snapshot AFTER takeover: the stale invocation owes zero mutation,
            # including no release or extension of the successor's seat.
            with db.engine.connect() as connection:
                for table in metadata.sorted_tables:
                    if table is not run_coordination_events_table:
                        before[table.name] = connection.execute(select(table)).all()
                prior_event_seqs.update(connection.execute(select(run_coordination_events_table.c.seq)).scalars())
            return stale

        monkeypatch.setattr(RunCoordinationRepository, "acquire_reconciliation_leadership", supersede_before_return)
        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RunLeadershipLostError) as raised:
                coordinator._reconcile_resumable_terminal(run, status, lease, loop)
            assert raised.value.verb == "web_terminal_reconciliation"
        finally:
            loop.close()

        with db.engine.connect() as connection:
            for table in metadata.sorted_tables:
                if table is not run_coordination_events_table:
                    assert connection.execute(select(table)).all() == before[table.name], table.name
            additions = [row for row in connection.execute(select(run_coordination_events_table)).all() if row.seq not in prior_event_seqs]
        # The finally-arm release reifies its own refusal without masking the
        # projection fence error or touching the successor's authority.
        assert len(additions) == 2
        assert all(row.event_type == "fence_refusal" for row in additions)
        assert {json.loads(row.context_json)["verb"] for row in additions} == {"web_terminal_reconciliation", "release_seat"}
        assert sessions.mock_calls == []
        assert execution.mock_calls == []
        assert blobs.mock_calls == []
        assert lease.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_after_discovery", [False, True])
async def test_archived_terminal_session_does_not_block_other_recovery(tmp_path, monkeypatch, archive_after_discovery):
    """Archive before discovery or in its acquisition race; another run progresses."""
    from unittest.mock import create_autospec

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.coordination.contracts import RecoveryRequiredReason, RunSagaState
    from elspeth.web.execution.recovery import RunRecoveryCoordinator
    from elspeth.web.execution.service import ExecutionServiceImpl

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))

    async def seed_run(*, completed):
        session_id = _insert_session(engine)
        compose = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.COMPOSE)
        run_id = UUID(
            await _seed_active_run(
                engine,
                session_id,
                session_operation_context=compose,
                status="pending",
                source={"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv"}, "on_validation_failure": "discard"},
            )
        )
        execute = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.EXECUTE)
        await sessions.update_run_status(run_id, "running", session_operation_context=execute)
        if completed:
            await sessions.update_run_status(run_id, "completed", landscape_run_id=str(run_id), session_operation_context=execute)
        sessions.session_operation_authority.release(execute)
        return await sessions.get_run(run_id)

    archived_run = await seed_run(completed=True)
    healthy_run = await seed_run(completed=False)
    discover = sessions.list_recoverable_run_records
    assert {run.id for run in await discover()} == {archived_run.id, healthy_run.id}
    if archive_after_discovery:

        async def discover_then_archive():
            candidates = await discover()
            await sessions.archive_session(archived_run.session_id)
            return candidates

        monkeypatch.setattr(sessions, "list_recoverable_run_records", discover_then_archive)
    else:
        await sessions.archive_session(archived_run.session_id)
        assert {run.id for run in await discover()} == {healthy_run.id}
    landscape_url = f"sqlite:///{tmp_path / 'landscape.db'}"
    with LandscapeDB.from_url(landscape_url):
        pass
    execution = create_autospec(ExecutionServiceImpl, instance=True)
    execution.get_live_run_ids.return_value = frozenset()
    coordinator = RunRecoveryCoordinator(
        sessions, execution, BlobServiceImpl(engine, tmp_path), landscape_url=landscape_url, create_tables=False
    )
    try:
        await coordinator.recover()
        healthy = await sessions.get_run(healthy_run.id)
        assert healthy.saga_state is RunSagaState.RECOVERY_REQUIRED
        assert healthy.recovery_required_reason is RecoveryRequiredReason.MISSING_BASELINE
        assert (await sessions.get_run(archived_run.id)).status == "completed"
        execution.recover_run.assert_not_called()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_recovery_acquisition_propagates_missing_fence_integrity_failure(tmp_path, monkeypatch):
    from unittest.mock import create_autospec

    from elspeth.web.blobs.service import BlobServiceImpl
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
    from elspeth.web.execution.recovery import RunRecoveryCoordinator
    from elspeth.web.execution.service import ExecutionServiceImpl
    from elspeth.web.sessions.models import session_operation_fences_table

    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    sessions = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))
    session_id = _insert_session(engine)
    compose = seed_live_operation_context(engine, session_id, operation_kind=SessionOperationKind.COMPOSE)
    await _seed_active_run(
        engine,
        session_id,
        session_operation_context=compose,
        status="pending",
        source={"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv"}, "on_validation_failure": "discard"},
    )
    sessions.session_operation_authority.release(compose)
    discover = sessions.list_recoverable_run_records

    async def discover_then_corrupt():
        candidates = await discover()
        assert len(candidates) == 1
        with engine.begin() as connection:
            connection.execute(delete(session_operation_fences_table).where(session_operation_fences_table.c.session_id == str(session_id)))
        return candidates

    monkeypatch.setattr(sessions, "list_recoverable_run_records", discover_then_corrupt)
    execution = create_autospec(ExecutionServiceImpl, instance=True)
    execution.get_live_run_ids.return_value = frozenset()
    coordinator = RunRecoveryCoordinator(
        sessions, execution, BlobServiceImpl(engine, tmp_path), landscape_url="sqlite://", create_tables=False
    )
    try:
        with pytest.raises(SessionOperationFenceLost) as caught:
            await coordinator.recover()
        assert caught.value.reason is FenceLossReason.MISSING
        execution.recover_run.assert_not_called()
    finally:
        engine.dispose()
