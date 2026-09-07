"""Direct heartbeat transaction controls; PostgreSQL contention lives in testcontainer."""

from __future__ import annotations

import ast
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, ExecutionContext

from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, RunMembershipLostError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository, fenced_heartbeat_transaction
from elspeth.core.landscape.schema import run_coordination_table, run_workers_table, runs_table
from tests.helpers.run_coordination import register_run_leader
from tests.unit.core.landscape.test_run_coordination_repository import RUN_ID, WINDOW, _coordination_image, _seed_run


@pytest.fixture
def heartbeat_db(tmp_path: Path) -> Iterator[LandscapeDB]:
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'heartbeat.db'}")
    _seed_run(db.engine)
    register_run_leader(RunCoordinationRepository(db.engine), run_id=RUN_ID, worker_id="leader", window_seconds=WINDOW)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.parametrize("worker_id", ["leader", "follower"])
def test_helper_locks_seat_before_membership_and_payload(heartbeat_db: LandscapeDB, worker_id: str) -> None:
    """Observe actual statements and the PostgreSQL lock intent on their ClauseElements."""
    if worker_id == "follower":
        member = RunCoordinationRepository(heartbeat_db.engine).admit_follower(
            run_id=RUN_ID, worker_id=worker_id, config_hash="config", window_seconds=WINDOW
        )
    else:
        member = WorkerMembershipToken(run_id=RUN_ID, worker_id=worker_id)
    statements: list[str] = []
    lock_statements: list[str] = []

    def capture(conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool) -> None:
        statements.append(" ".join(statement.upper().split()))
        if context.compiled is not None:
            lock_statements.append(" ".join(str(context.compiled.statement.compile(dialect=postgresql.dialect())).upper().split()))

    event.listen(heartbeat_db.engine, "before_cursor_execute", capture)
    try:
        with fenced_heartbeat_transaction(heartbeat_db.engine, member_token=member, verb="heartbeat-test") as conn:
            conn.execute(update(runs_table).where(runs_table.c.run_id == RUN_ID).values(settings_json='{"heartbeat_probe":true}'))
    finally:
        event.remove(heartbeat_db.engine, "before_cursor_execute", capture)

    assert statements[0] == "BEGIN IMMEDIATE"
    assert statements[1].startswith("SELECT RUN_COORDINATION.RUN_ID FROM RUN_COORDINATION WHERE")
    assert statements[2].startswith("UPDATE RUN_WORKERS SET STATUS=")
    assert statements[3].startswith("UPDATE RUNS SET SETTINGS_JSON=")
    assert len(statements) == 4
    assert lock_statements[0].endswith("FOR UPDATE")
    with heartbeat_db.engine.connect() as conn:
        assert (
            conn.execute(select(runs_table.c.settings_json).where(runs_table.c.run_id == RUN_ID)).scalar_one() == '{"heartbeat_probe":true}'
        )


def test_helper_bounds_statement_waits_before_any_seat_lock() -> None:
    """The real PostgreSQL blocked-stop test covers the timeout's operational effect."""
    function = ast.parse(inspect.getsource(fenced_heartbeat_transaction)).body[0]
    assert isinstance(function, ast.FunctionDef)
    transaction = next(statement for statement in function.body if isinstance(statement, ast.With))
    first_statement = transaction.body[0]
    assert isinstance(first_statement, ast.Expr)
    assert isinstance(first_statement.value, ast.Call)
    assert isinstance(first_statement.value.func, ast.Name)
    assert first_statement.value.func.id == "_bound_heartbeat_statement_waits"
    assert [ast.unparse(argument) for argument in first_statement.value.args] == ["conn"]


@pytest.mark.parametrize("fault", ["departed", "evicted", "missing-member", "wrong-run", "missing-seat", "missing-seat-and-member"])
def test_helper_refuses_before_payload_and_preserves_durable_image(heartbeat_db: LandscapeDB, fault: str) -> None:
    member = WorkerMembershipToken(run_id=RUN_ID, worker_id="leader")
    with heartbeat_db.engine.begin() as conn:
        if fault in ("departed", "evicted"):
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == member.worker_id)
                .values(
                    status=fault,
                    departed_at=datetime(2020, 1, 1, tzinfo=UTC) if fault == "departed" else None,
                    evicted_at=datetime(2020, 1, 1, tzinfo=UTC) if fault == "evicted" else None,
                    evicted_by_worker_id="successor" if fault == "evicted" else None,
                )
            )
        if fault in ("missing-member", "missing-seat-and-member"):
            conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == member.worker_id))
        if fault in ("missing-seat", "missing-seat-and-member"):
            conn.execute(delete(run_coordination_table).where(run_coordination_table.c.run_id == RUN_ID))
    if fault == "wrong-run":
        member = WorkerMembershipToken(run_id="foreign-run", worker_id="leader")
    before = _coordination_image(heartbeat_db.engine)
    expected_error = RunMembershipLostError if fault in ("departed", "evicted") else AuditIntegrityError
    expected_message = "membership lost" if fault in ("departed", "evicted") else "unregistered"
    if fault == "missing-seat":
        expected_message = "registered membership but no coordination seat"
    with (
        pytest.raises(expected_error, match=expected_message),
        fenced_heartbeat_transaction(heartbeat_db.engine, member_token=member, verb="heartbeat-test"),
    ):
        pytest.fail("Rejected heartbeat reached its payload")
    assert _coordination_image(heartbeat_db.engine) == before


def test_helper_rolls_back_both_liveness_rows_and_payload_on_caller_failure(heartbeat_db: LandscapeDB) -> None:
    member = WorkerMembershipToken(run_id=RUN_ID, worker_id="leader")
    before = _coordination_image(heartbeat_db.engine)
    with (
        pytest.raises(RuntimeError, match="caller failed"),
        fenced_heartbeat_transaction(heartbeat_db.engine, member_token=member, verb="heartbeat-test") as conn,
    ):
        conn.execute(
            update(run_workers_table)
            .where(run_workers_table.c.worker_id == member.worker_id)
            .values(heartbeat_expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == RUN_ID)
            .values(leader_heartbeat_expires_at=datetime(2020, 1, 1, tzinfo=UTC))
        )
        conn.execute(update(runs_table).where(runs_table.c.run_id == RUN_ID).values(settings_json='{"uncommitted":true}'))
        raise RuntimeError("caller failed")
    assert _coordination_image(heartbeat_db.engine) == before


def test_helper_rejects_leader_authority_before_opening_transaction(heartbeat_db: LandscapeDB) -> None:
    # A nominal leader token is deliberately the wrong capability for this helper.
    leader = CoordinationToken(run_id=RUN_ID, worker_id="leader", leader_epoch=1)
    statements: list[str] = []

    def capture(conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool) -> None:
        statements.append(statement)

    event.listen(heartbeat_db.engine, "before_cursor_execute", capture)
    try:
        with (
            pytest.raises(TypeError, match="WorkerMembershipToken"),
            fenced_heartbeat_transaction(heartbeat_db.engine, member_token=leader, verb="heartbeat-test"),
        ):
            pytest.fail("Wrong capability reached its payload")
    finally:
        event.remove(heartbeat_db.engine, "before_cursor_execute", capture)
    assert statements == []
