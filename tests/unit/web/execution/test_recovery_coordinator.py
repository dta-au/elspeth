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
    from unittest.mock import AsyncMock, Mock
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
    sessions.list_recoverable_run_records = AsyncMock(return_value=(run,))
    sessions.get_run = AsyncMock(return_value=run)
    sessions.session_operation_authority = Mock(spec=SessionOperationAuthority)
    if rebind_refused:
        sessions.session_operation_authority.mutate.side_effect = SessionOperationFenceLost(FenceLossReason.OWNER_INACTIVE)
    sessions.session_operation_owner_instance_id = "recoverer"
    sessions.session_operation_lease_seconds = 30
    execution = Mock(spec=ExecutionServiceImpl)
    execution.get_live_run_ids.return_value = frozenset()
    execution.recover_run = AsyncMock(return_value=dispatch_accepts)
    lease = Mock(spec=SessionOperationLease)
    lease.close = AsyncMock()
    monkeypatch.setattr(SessionOperationLease, "acquire", AsyncMock(return_value=lease))
    monkeypatch.setattr(recovery_module, "observe_run", Mock(return_value=RecoveryObservation(RunStatus.RUNNING, live_leader, None)))
    coordinator = RunRecoveryCoordinator(sessions, execution, Mock(spec=BlobServiceImpl), landscape_url="sqlite://", create_tables=False)
    await coordinator.recover()
    if live_leader or rebind_refused:
        execution.recover_run.assert_not_called()
    else:
        execution.recover_run.assert_awaited_once_with(run, lease, resume_existing=True)
    assert lease.close.await_count == (0 if not live_leader and dispatch_accepts else 1)
    if live_leader:
        sessions.session_operation_authority.mutate.assert_not_called()
