"""PostgreSQL contention proof for atomic batch membership closure."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import leader_coordination_token
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import BatchStatus, NodeType
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.execution.batches import add_batch_member_guarded
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction

pytestmark = pytest.mark.testcontainer

_DYNAMIC_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def _seed(factory: RecorderFactory, *, suffix: str) -> tuple[CoordinationToken, str, str]:
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=f"run-{suffix}")
    leader = leader_coordination_token(factory, run.run_id)
    source = factory.data_flow.register_node(
        coordination_token=leader,
        plugin_name="source",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        node_id=f"source-{suffix}",
        schema_config=_DYNAMIC_SCHEMA,
    )
    aggregation = factory.data_flow.register_node(
        coordination_token=leader,
        plugin_name="aggregation",
        node_type=NodeType.AGGREGATION,
        plugin_version="1.0",
        config={},
        node_id=f"agg-{suffix}",
        schema_config=_DYNAMIC_SCHEMA,
    )
    _row, token = factory.data_flow.create_row_with_token(
        coordination_token=leader,
        source_node_id=source.node_id,
        row_index=0,
        data={"value": 1},
        row_id=f"row-{suffix}",
        token_id=f"token-{suffix}",
        source_row_index=0,
        ingest_sequence=0,
    )
    batch = factory.execution.create_batch(coordination_token=leader, aggregation_node_id=aggregation.node_id, batch_id=f"batch-{suffix}")
    return leader, batch.batch_id, token.token_id


def _add_member(factory: RecorderFactory, leader: CoordinationToken, batch_id: str, token_id: str) -> None:
    with fenced_leader_transaction(
        factory._db.engine, token=leader, window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, verb="test_batch_membership"
    ) as conn:
        add_batch_member_guarded(conn, batch_id=batch_id, token_id=token_id, ordinal=0, expected_run_id=leader.run_id)


def _wait_for_blocker(db: LandscapeDB, *, waiter: int, blocker: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.engine.connect() as conn:
            blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter)).scalar_one()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError(f"PostgreSQL did not report pid {waiter} waiting for {blocker}")


def _physical_postgres_connection(conn: Any) -> tuple[int, int]:
    """Return process-local DBAPI identity plus server backend PID."""
    driver_connection = conn.connection.driver_connection
    return id(driver_connection), int(driver_connection.info.backend_pid)


@pytest.mark.parametrize(
    "target_status",
    [BatchStatus.EXECUTING, BatchStatus.COMPLETED, BatchStatus.FAILED],
)
@pytest.mark.timeout(120)
def test_postgres_transition_lock_orders_racing_membership(
    postgres_url: str,
    target_status: BatchStatus,
) -> None:
    """Every membership-closing transition orders before a racing add."""
    db = LandscapeDB.from_url(postgres_url)
    factory = RecorderFactory(db)
    leader, batch_id, token_id = _seed(factory, suffix=target_status.value)

    transition_holds_lock = threading.Event()
    release_transition = threading.Event()
    member_reached_leader_fence = threading.Event()
    outcomes: dict[str, str] = {}
    physical_connections: dict[str, tuple[int, int]] = {}
    outcomes_lock = threading.Lock()

    def pause_after_transition(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        if statement.lstrip().upper().startswith("UPDATE BATCHES"):
            physical_connections["transition"] = _physical_postgres_connection(conn)
            transition_holds_lock.set()
            if not release_transition.wait(timeout=30):
                raise TimeoutError("test did not release PostgreSQL batch transition")

    def observe_member_read(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if threading.current_thread().name == "batch-member" and normalized.startswith("UPDATE RUN_COORDINATION"):
            physical_connections.setdefault("member", _physical_postgres_connection(conn))
            member_reached_leader_fence.set()

    event.listen(db.engine, "after_cursor_execute", pause_after_transition)
    event.listen(db.engine, "before_cursor_execute", observe_member_read)

    def transition() -> None:
        try:
            if target_status is BatchStatus.EXECUTING:
                factory.execution.update_batch_status(batch_id, target_status, coordination_token=leader)
            else:
                factory.execution.complete_batch(batch_id, target_status, coordination_token=leader)
        except BaseException as exc:  # pragma: no cover - asserted below
            result = f"{type(exc).__name__}: {exc}"
        else:
            result = "committed"
        with outcomes_lock:
            outcomes["transition"] = result

    def add_member() -> None:
        try:
            _add_member(factory, leader, batch_id, token_id)
        except AuditIntegrityError as exc:
            result = f"refused: {exc}"
        except BaseException as exc:  # pragma: no cover - asserted below
            result = f"{type(exc).__name__}: {exc}"
        else:
            result = "inserted"
        with outcomes_lock:
            outcomes["member"] = result

    transition_thread = threading.Thread(target=transition, name="batch-transition")
    member_thread = threading.Thread(target=add_member, name="batch-member")
    try:
        transition_thread.start()
        assert transition_holds_lock.wait(timeout=30), "transition never reached its locked UPDATE"
        member_thread.start()
        assert member_reached_leader_fence.wait(timeout=30), "member never reached its first leader fence"
        _wait_for_blocker(db, waiter=physical_connections["member"][1], blocker=physical_connections["transition"][1])
        release_transition.set()
        transition_thread.join(timeout=30)
        member_thread.join(timeout=30)
        assert not transition_thread.is_alive()
        assert not member_thread.is_alive()

        assert outcomes["transition"] == "committed"
        assert outcomes["member"].startswith("refused: ")
        assert f"status {target_status.value!r}" in outcomes["member"]
        assert set(physical_connections) == {"transition", "member"}
        transition_connection, member_connection = physical_connections["transition"], physical_connections["member"]
        assert transition_connection[0] != member_connection[0], "threads must use distinct physical DBAPI connections"
        assert transition_connection[1] != member_connection[1], "threads must use distinct PostgreSQL backend processes"
        batch = factory.execution.get_batch(batch_id)
        assert batch is not None
        assert batch.status is target_status
        assert factory.execution.get_batch_members(batch_id) == []
    finally:
        release_transition.set()
        if transition_thread.ident is not None:
            transition_thread.join(timeout=30)
        if member_thread.ident is not None:
            member_thread.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", observe_member_read)
        event.remove(db.engine, "after_cursor_execute", pause_after_transition)
        db.close()


@pytest.mark.timeout(120)
def test_postgres_membership_takes_token_lock_first_and_survives_racing_outcome(
    postgres_url: str,
) -> None:
    """Membership and outcome recording share token-first lock order (elspeth-a580f44add).

    Outcome recording locks ``tokens`` FOR UPDATE and then takes an implicit FK
    KEY SHARE on ``batches`` when it writes a batch-scoped outcome row.  Before
    the fix, membership locked ``batches`` FOR UPDATE first and its INSERT then
    waited on the token FK lock — the two paths deadlocked and PostgreSQL
    aborted one audit write.  This drives the exact interleaving: membership
    pauses holding its token-first parent lock, the racing outcome write now
    blocks at the preceding leader fence, and both must commit once membership
    is released. The captured SQL still proves token-before-batch parent order.
    """
    from elspeth.contracts.audit import TokenRef
    from elspeth.contracts.enums import TerminalPath
    from elspeth.core.landscape.schema import token_outcomes_table

    db = LandscapeDB.from_url(postgres_url)
    factory = RecorderFactory(db)
    leader, batch_id, token_id = _seed(factory, suffix="outcome-race")

    member_statements: list[str] = []
    member_holds_token_lock = threading.Event()
    release_member = threading.Event()
    outcome_entered_leader_fence = threading.Event()
    outcomes: dict[str, str] = {}
    backend_pids: dict[str, int] = {}
    outcomes_lock = threading.Lock()

    def record_member_statements(
        conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool
    ) -> None:
        if threading.current_thread().name != "batch-member":
            return
        normalized = " ".join(statement.upper().split())
        member_statements.append(normalized)

    def pause_member_after_token_lock(
        conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool
    ) -> None:
        if threading.current_thread().name != "batch-member":
            return
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("SELECT") and "FROM TOKENS" in normalized and "FOR UPDATE" in normalized:
            backend_pids["member"] = int(conn.connection.driver_connection.info.backend_pid)
            member_holds_token_lock.set()
            if not release_member.wait(timeout=30):
                raise TimeoutError("test did not release the membership transaction")

    event.listen(db.engine, "before_cursor_execute", record_member_statements)
    event.listen(db.engine, "after_cursor_execute", pause_member_after_token_lock)

    def entering_leader_fence(conn: Connection, _cursor: Any, statement: str, _parameters: Any, _context: Any, _executemany: bool) -> None:
        normalized = " ".join(statement.upper().split())
        if threading.current_thread().name == "token-outcome" and normalized.startswith("UPDATE RUN_COORDINATION"):
            backend_pids["outcome"] = _physical_postgres_connection(conn)[1]
            outcome_entered_leader_fence.set()

    def add_member() -> None:
        try:
            _add_member(factory, leader, batch_id, token_id)
        except BaseException as exc:  # pragma: no cover - asserted below
            result = f"{type(exc).__name__}: {exc}"
        else:
            result = "inserted"
        with outcomes_lock:
            outcomes["member"] = result

    def record_outcome() -> None:
        try:
            factory.data_flow.record_token_outcome_leader(
                TokenRef(token_id=token_id, run_id="run-outcome-race"),
                None,
                TerminalPath.BUFFERED,
                batch_id=batch_id,
                coordination_token=leader,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            result = f"{type(exc).__name__}: {exc}"
        else:
            result = "recorded"
        with outcomes_lock:
            outcomes["outcome"] = result

    member_thread = threading.Thread(target=add_member, name="batch-member")
    outcome_thread = threading.Thread(target=record_outcome, name="token-outcome")
    event.listen(db.engine, "before_cursor_execute", entering_leader_fence)
    try:
        member_thread.start()
        assert member_holds_token_lock.wait(timeout=30), (
            "membership never locked the token row — the PostgreSQL path must take "
            "the token-first lock shared with outcome recording (elspeth-a580f44add); "
            f"member statements so far: {member_statements}"
        )
        outcome_thread.start()
        assert outcome_entered_leader_fence.wait(timeout=30), "outcome write never reached its first leader fence"
        _wait_for_blocker(db, waiter=backend_pids["outcome"], blocker=backend_pids["member"])
        release_member.set()
        member_thread.join(timeout=60)
        outcome_thread.join(timeout=60)
        assert not member_thread.is_alive(), "membership transaction wedged"
        assert not outcome_thread.is_alive(), "outcome transaction wedged"

        assert outcomes["member"] == "inserted", f"membership must commit, got: {outcomes['member']}"
        assert outcomes["outcome"] == "recorded", f"outcome recording must commit, got: {outcomes['outcome']}"
        assert backend_pids["member"] != backend_pids["outcome"], "threads must use distinct PostgreSQL backends"

        token_lock_indexes = [
            i for i, sql in enumerate(member_statements) if sql.startswith("SELECT") and "FROM TOKENS" in sql and "FOR UPDATE" in sql
        ]
        batch_lock_indexes = [
            i for i, sql in enumerate(member_statements) if sql.startswith("SELECT") and "FROM BATCHES" in sql and "FOR UPDATE" in sql
        ]
        assert token_lock_indexes and batch_lock_indexes, f"member statements missing a parent lock: {member_statements}"
        assert token_lock_indexes[0] < batch_lock_indexes[0], f"membership must lock the token before the batch: {member_statements}"

        members = factory.execution.get_batch_members(batch_id)
        assert [(m.token_id, m.ordinal) for m in members] == [(token_id, 0)]
        with db.read_only_connection() as conn:
            outcome_rows = conn.execute(token_outcomes_table.select().where(token_outcomes_table.c.token_id == token_id)).fetchall()
        assert len(outcome_rows) == 1
        assert outcome_rows[0].batch_id == batch_id
        assert outcome_rows[0].path == TerminalPath.BUFFERED.value
    finally:
        release_member.set()
        if member_thread.ident is not None:
            member_thread.join(timeout=30)
        if outcome_thread.ident is not None:
            outcome_thread.join(timeout=30)
        event.remove(db.engine, "before_cursor_execute", entering_leader_fence)
        event.remove(db.engine, "after_cursor_execute", pause_member_after_token_lock)
        event.remove(db.engine, "before_cursor_execute", record_member_statements)
        db.close()
