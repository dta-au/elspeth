"""PostgreSQL row-lock proofs for atomic token-outcome validation."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic, sleep
from typing import Any

import pytest
from sqlalchemy import delete, event, insert, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.sql import Executable, Select
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]
from tests.fixtures.landscape import register_test_node
from tests.fixtures.stores import MockPayloadStore

from elspeth.contracts import ExecutionError, NodeStateStatus, NodeType, RunStatus
from elspeth.contracts.audit import DISCARD_SINK_NAME, TokenRef
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.contracts.sink_effects import (
    SinkEffectInputKind,
    SinkEffectMember,
    SinkEffectMemberCandidate,
    SinkEffectReservationRequest,
    SinkEffectRole,
)
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_identity import compute_pipeline_effect_identity, resolve_sink_effect_members
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    operations_table,
    token_lineage_frames_table,
    token_outcomes_table,
    tokens_table,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def postgres_factory(postgres_url: str) -> Iterator[tuple[LandscapeDB, RecorderFactory]]:
    db = LandscapeDB(postgres_url)
    try:
        yield db, RecorderFactory(db)
    finally:
        db.engine.dispose()


def _build_token(
    factory: RecorderFactory,
    *,
    leader_worker_id: str | None = None,
) -> tuple[str, str, str]:
    run = factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        leader_worker_id=leader_worker_id,
    )
    source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
    sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="sink")
    row = factory.data_flow.create_row(
        run_id=run.run_id,
        source_node_id=source_id,
        row_index=0,
        data={"value": 1},
        source_row_index=0,
        ingest_sequence=0,
    )
    token = factory.data_flow.create_token(row.row_id)
    return run.run_id, token.token_id, sink_id


def _pipeline_request(run_id: str, sink_id: str, members: Sequence[SinkEffectMember]) -> SinkEffectReservationRequest:
    canonical_members = tuple(
        replace(member, ordinal=ordinal, member_effect_id=None)
        for ordinal, member in enumerate(sorted(members, key=lambda member: member.ordinal))
    )
    identity = compute_pipeline_effect_identity(
        run_id=run_id,
        sink_node_id=sink_id,
        role=SinkEffectRole.PRIMARY,
        sink_config={"name": "sink"},
        target_config={"path": "out.jsonl"},
        members=canonical_members,
    )
    return SinkEffectReservationRequest(
        run_id=run_id,
        sink_node_id=sink_id,
        role=SinkEffectRole.PRIMARY,
        input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        requested_target_hash=identity.requested_target_hash,
        members=members,
        audit_export_snapshot_id=None,
        config_hash=identity.config_hash,
        replacing_target=False,
        primary_effect_id=None,
    )


def _reserve_open_effect_operation(factory: RecorderFactory, *, run_id: str, token_id: str, sink_id: str) -> str:
    factory.execution.begin_node_state(
        token_id=token_id,
        node_id=sink_id,
        run_id=run_id,
        step_index=0,
        input_data={"value": 1},
    )
    members = resolve_sink_effect_members(
        factory,
        (SinkEffectMemberCandidate(token_id=token_id, row={"value": 1}),),
    )
    effect = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members)).new_effect
    assert effect is not None
    operation = next(item for item in factory.execution.get_operations_for_run(run_id) if item.sink_effect_id == effect.effect_id)
    return operation.operation_id


def test_postgres_batch_expansion_claims_batch_once_under_contention(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    postgres_url: str,
) -> None:
    """Different parent members racing one batch produce one child set."""
    first_db, _ = postgres_factory
    first_factory = RecorderFactory(first_db, payload_store=MockPayloadStore())
    run = first_factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source_id = register_test_node(
        first_factory.data_flow,
        run.run_id,
        "expand-source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    aggregation_id = register_test_node(
        first_factory.data_flow,
        run.run_id,
        "expand-aggregation",
        node_type=NodeType.AGGREGATION,
        plugin_name="aggregation",
    )
    rows = [
        first_factory.data_flow.create_row(
            run_id=run.run_id,
            source_node_id=source_id,
            row_index=index,
            data={"value": index},
            source_row_index=index,
            ingest_sequence=index,
        )
        for index in range(2)
    ]
    parents = [first_factory.data_flow.create_token(row.row_id) for row in rows]
    batch = first_factory.execution.create_batch(
        run_id=run.run_id,
        aggregation_node_id=aggregation_id,
        batch_id="expand-batch",
    )
    for ordinal, parent in enumerate(parents):
        first_factory.execution.add_batch_member(batch.batch_id, parent.token_id, ordinal)

    second_db = LandscapeDB(postgres_url)
    second_factory = RecorderFactory(second_db, payload_store=MockPayloadStore())
    start = threading.Barrier(2)
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)

    def attempt(candidate: tuple[RecorderFactory, int]) -> str:
        factory, index = candidate
        start.wait(timeout=5)
        try:
            factory.data_flow.expand_token(
                parent_ref=TokenRef(token_id=parents[index].token_id, run_id=run.run_id),
                row_id=rows[index].row_id,
                child_payloads=[{"item": 1}, {"item": 2}],
                output_contract=contract,
                parent_path=TerminalPath.BATCH_CONSUMED,
                parent_batch_id=batch.batch_id,
            )
        except AuditIntegrityError as exc:
            assert "divergent expansion replay" in str(exc)
            return "rejected"
        return "committed"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ((first_factory, 0), (second_factory, 1))))
    finally:
        second_db.close()

    assert sorted(results) == ["committed", "rejected"]
    with first_db.read_only_connection() as conn:
        children = conn.execute(
            select(tokens_table.c.token_id).where(
                tokens_table.c.token_id.in_(
                    select(token_lineage_frames_table.c.token_id).where(token_lineage_frames_table.c.kind == "expand")
                )
            )
        ).all()
        outcomes = conn.execute(
            select(token_outcomes_table.c.path, token_outcomes_table.c.batch_id).where(
                token_outcomes_table.c.batch_id == batch.batch_id,
                token_outcomes_table.c.completed == 1,
            )
        ).all()
    assert len(children) == 2
    assert outcomes == [
        (TerminalPath.BATCH_CONSUMED.value, batch.batch_id),
        (TerminalPath.BATCH_CONSUMED.value, batch.batch_id),
    ]


def _lock_timeout_result(db: LandscapeDB, start: threading.Event, statement: Executable) -> str:
    if not start.wait(timeout=5):
        return "start-timeout"
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
            conn.execute(statement)
    except DBAPIError as exc:
        if "lock timeout" not in str(exc).lower():
            return f"unexpected:{type(exc).__name__}:{exc}"
        return "blocked"
    return "mutated"


def _record_while_mutation_contends(
    *,
    db: LandscapeDB,
    factory: RecorderFactory,
    monkeypatch: pytest.MonkeyPatch,
    ref: TokenRef,
    mutation: Executable,
    outcome: TerminalOutcome,
    path: TerminalPath,
    sink_name: str,
    error_hash: str,
    sink_node_id: str | None = None,
    artifact_id: str | None = None,
) -> None:
    """Pause after invariant evaluation and prove the competing write blocks."""
    start = threading.Event()
    outcomes = factory.data_flow.outcomes
    original_invariants = outcomes._validate_cross_table_invariants

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_lock_timeout_result, db, start, mutation)

        def pause_after_validation(
            checked_ref: TokenRef,
            checked_outcome: TerminalOutcome | None,
            checked_path: TerminalPath,
            *,
            sink_name: str | None,
            sink_node_id: str | None,
            artifact_id: str | None,
            conn: Connection | None = None,
            lock_witnesses: bool = True,
        ) -> None:
            original_invariants(
                checked_ref,
                checked_outcome,
                checked_path,
                sink_name=sink_name,
                sink_node_id=sink_node_id,
                artifact_id=artifact_id,
                conn=conn,
                lock_witnesses=lock_witnesses,
            )
            start.set()
            assert future.result(timeout=5) == "blocked"

        monkeypatch.setattr(outcomes, "_validate_cross_table_invariants", pause_after_validation)
        factory.data_flow.record_token_outcome(
            ref=ref,
            outcome=outcome,
            path=path,
            sink_name=sink_name,
            sink_node_id=sink_node_id,
            artifact_id=artifact_id,
            error_hash=error_hash,
        )


def _install_token_lock_probe(db: LandscapeDB) -> tuple[threading.Event, dict[str, int], Any]:
    attempted = threading.Event()
    backend: dict[str, int] = {}

    def before_cursor_execute(
        conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.upper()
        if "FROM TOKENS" not in normalized or "FOR UPDATE" not in normalized:
            return
        backend["pid"] = conn.connection.driver_connection.info.backend_pid
        attempted.set()

    event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
    return attempted, backend, before_cursor_execute


def _assert_postgres_backend_waits_on_lock(db: LandscapeDB, backend_pid: int) -> None:
    deadline = monotonic() + 5
    with db.engine.connect() as observer:
        while monotonic() < deadline:
            activity = observer.exec_driver_sql(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                (backend_pid,),
            ).one()
            if activity.wait_event_type == "Lock":
                return
            sleep(0.01)
    pytest.fail(f"PostgreSQL backend {backend_pid} never entered a lock wait; last activity={activity!r}")


def _record_unrouted_failure(factory: RecorderFactory, *, run_id: str, token_id: str) -> None:
    factory.data_flow.record_token_outcome(
        ref=TokenRef(token_id=token_id, run_id=run_id),
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.UNROUTED,
        error_hash="e" * 64,
    )


def test_postgres_decided_outcome_winning_finalize_race_is_not_abandoned(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decided writer holding the token lock wins before finalization.

    Finalization must re-observe the committed outcome after its PostgreSQL
    lock wait, skip ABANDONED, and still fail the independent open-effect
    operation sweep in the same fenced terminal transaction.
    """
    first_db, first_factory = postgres_factory
    leader_worker_id = "worker:postgres-finalize-race:decided-first"
    run_id, token_id, sink_id = _build_token(first_factory, leader_worker_id=leader_worker_id)
    operation_id = _reserve_open_effect_operation(
        first_factory,
        run_id=run_id,
        token_id=token_id,
        sink_id=sink_id,
    )
    second_db = LandscapeDB(postgres_url)
    second_factory = RecorderFactory(second_db)
    outcome_locked = threading.Event()
    release_outcome = threading.Event()
    outcomes = second_factory.data_flow.outcomes
    original_validate = outcomes._validate_cross_table_invariants

    def pause_after_validation(*args: Any, **kwargs: Any) -> None:
        original_validate(*args, **kwargs)
        outcome_locked.set()
        assert release_outcome.wait(timeout=5), "outcome winner was not released"

    monkeypatch.setattr(outcomes, "_validate_cross_table_invariants", pause_after_validation)
    attempted, backend, listener = _install_token_lock_probe(first_db)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcome_future = pool.submit(_record_unrouted_failure, second_factory, run_id=run_id, token_id=token_id)
            assert outcome_locked.wait(timeout=5), "decided writer never acquired its token lock"
            finalize_future = pool.submit(
                first_factory.run_lifecycle.finalize_run,
                run_id,
                RunStatus.FAILED,
                token=CoordinationToken(run_id=run_id, worker_id=leader_worker_id, leader_epoch=1),
            )
            assert attempted.wait(timeout=5), "finalizer never attempted its token lock"
            _assert_postgres_backend_waits_on_lock(first_db, backend["pid"])
            release_outcome.set()
            outcome_future.result(timeout=10)
            finalize_future.result(timeout=10)
    finally:
        release_outcome.set()
        event.remove(first_db.engine, "before_cursor_execute", listener)
        second_db.engine.dispose()

    with first_db.read_only_connection() as conn:
        outcomes_rows = conn.execute(
            select(token_outcomes_table.c.outcome, token_outcomes_table.c.path, token_outcomes_table.c.completed)
            .where(token_outcomes_table.c.token_id == token_id)
            .order_by(token_outcomes_table.c.recorded_at)
        ).all()
        operation_row = conn.execute(
            select(
                operations_table.c.status,
                operations_table.c.completed_at,
                operations_table.c.duration_ms,
                operations_table.c.error_message,
            ).where(operations_table.c.operation_id == operation_id)
        ).one()
    assert outcomes_rows == [(TerminalOutcome.FAILURE.value, TerminalPath.UNROUTED.value, 1)]
    assert operation_row.status == "failed"
    assert operation_row.completed_at is not None
    assert operation_row.duration_ms is not None and operation_row.duration_ms >= 0.0
    assert operation_row.error_message == "run finalized as non-resumable before sink effect completed"


def test_postgres_finalize_winning_outcome_race_refuses_late_decision(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fenced finalizer that records ABANDONED wins the token lock.

    The already-started decided writer must block, re-observe ABANDONED after
    the terminal transaction commits, and be refused rather than create the
    contradictory decided+ABANDONED history.
    """
    first_db, first_factory = postgres_factory
    leader_worker_id = "worker:postgres-finalize-race:finalize-first"
    run_id, token_id, sink_id = _build_token(first_factory, leader_worker_id=leader_worker_id)
    operation_id = _reserve_open_effect_operation(
        first_factory,
        run_id=run_id,
        token_id=token_id,
        sink_id=sink_id,
    )
    second_db = LandscapeDB(postgres_url)
    second_factory = RecorderFactory(second_db)
    abandonment_inserted = threading.Event()
    release_finalizer = threading.Event()
    outcomes = first_factory.run_lifecycle._outcomes_repo
    original_record = outcomes.record_token_outcome

    def pause_after_abandonment(*args: Any, **kwargs: Any) -> str:
        outcome_id = original_record(*args, **kwargs)
        abandonment_inserted.set()
        assert release_finalizer.wait(timeout=5), "finalizer winner was not released"
        return outcome_id

    monkeypatch.setattr(outcomes, "record_token_outcome", pause_after_abandonment)
    attempted, backend, listener = _install_token_lock_probe(second_db)

    def attempt_decision() -> Exception | None:
        try:
            _record_unrouted_failure(second_factory, run_id=run_id, token_id=token_id)
        except Exception as exc:
            return exc
        return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            finalize_future = pool.submit(
                first_factory.run_lifecycle.finalize_run,
                run_id,
                RunStatus.FAILED,
                token=CoordinationToken(run_id=run_id, worker_id=leader_worker_id, leader_epoch=1),
            )
            assert abandonment_inserted.wait(timeout=5), "finalizer never inserted ABANDONED"
            outcome_future = pool.submit(attempt_decision)
            assert attempted.wait(timeout=5), "decided writer never attempted its token lock"
            _assert_postgres_backend_waits_on_lock(second_db, backend["pid"])
            release_finalizer.set()
            finalize_future.result(timeout=10)
            decision_error = outcome_future.result(timeout=10)
    finally:
        release_finalizer.set()
        event.remove(second_db.engine, "before_cursor_execute", listener)
        second_db.engine.dispose()

    assert isinstance(decision_error, (AuditIntegrityError, LandscapeRecordError))
    with first_db.read_only_connection() as conn:
        outcomes_rows = conn.execute(
            select(token_outcomes_table.c.outcome, token_outcomes_table.c.path, token_outcomes_table.c.completed).where(
                token_outcomes_table.c.token_id == token_id
            )
        ).all()
        operation_status = conn.execute(
            select(operations_table.c.status).where(operations_table.c.operation_id == operation_id)
        ).scalar_one()
    assert outcomes_rows == [(None, TerminalPath.ABANDONED.value, 0)]
    assert operation_status == "failed"


def test_postgres_locks_discard_node_states_until_outcome_insert(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, factory = postgres_factory
    run_id, token_id, sink_id = _build_token(factory)
    state = factory.execution.begin_node_state(
        token_id=token_id,
        node_id=sink_id,
        run_id=run_id,
        step_index=0,
        input_data={"value": 1},
    )
    factory.execution.complete_node_state(
        state_id=state.state_id,
        status=NodeStateStatus.FAILED,
        error=ExecutionError(exception="discard", exception_type="TestDiscard", phase="sink_write"),
        duration_ms=1.0,
    )
    mutation = (
        update(node_states_table).where(node_states_table.c.state_id == state.state_id).values(status=NodeStateStatus.COMPLETED.value)
    )
    _record_while_mutation_contends(
        db=db,
        factory=factory,
        monkeypatch=monkeypatch,
        ref=TokenRef(token_id=token_id, run_id=run_id),
        mutation=mutation,
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.SINK_DISCARDED,
        sink_name=DISCARD_SINK_NAME,
        error_hash="discard-error",
    )

    with db.read_only_connection() as conn:
        assert conn.execute(select(node_states_table.c.status).where(node_states_table.c.state_id == state.state_id)).scalar_one() == (
            NodeStateStatus.FAILED.value
        )
        assert len(conn.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.token_id == token_id)).all()) == 1


def test_postgres_token_lock_blocks_phantom_node_state_until_outcome_insert(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, factory = postgres_factory
    run_id, token_id, sink_id = _build_token(factory)
    mutation = insert(node_states_table).values(
        state_id="phantom-completed-state",
        token_id=token_id,
        run_id=run_id,
        node_id=sink_id,
        step_index=0,
        attempt=0,
        status=NodeStateStatus.COMPLETED.value,
        input_hash="0" * 64,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    _record_while_mutation_contends(
        db=db,
        factory=factory,
        monkeypatch=monkeypatch,
        ref=TokenRef(token_id=token_id, run_id=run_id),
        mutation=mutation,
        outcome=TerminalOutcome.FAILURE,
        path=TerminalPath.SINK_DISCARDED,
        sink_name=DISCARD_SINK_NAME,
        error_hash="discard-error",
    )

    with db.read_only_connection() as conn:
        assert (
            conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.state_id == "phantom-completed-state")).all() == []
        )
        assert len(conn.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.token_id == token_id)).all()) == 1


def test_postgres_locks_failsink_artifact_witness_until_outcome_insert(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, factory = postgres_factory
    run_id, token_id, sink_id = _build_token(factory)
    state = factory.execution.begin_node_state(
        token_id=token_id,
        node_id=sink_id,
        run_id=run_id,
        step_index=0,
        input_data={"value": 1},
    )
    factory.execution.complete_node_state(
        state_id=state.state_id,
        status=NodeStateStatus.COMPLETED,
        output_data={"written": True},
        duration_ms=1.0,
    )
    artifact = factory.execution.register_artifact(
        run_id=run_id,
        state_id=state.state_id,
        sink_node_id=sink_id,
        artifact_type="test",
        path="memory://failsink/artifact",
        content_hash="deadbeef" * 8,
        size_bytes=0,
    )
    mutation = delete(artifacts_table).where(artifacts_table.c.artifact_id == artifact.artifact_id)
    _record_while_mutation_contends(
        db=db,
        factory=factory,
        monkeypatch=monkeypatch,
        ref=TokenRef(token_id=token_id, run_id=run_id),
        mutation=mutation,
        outcome=TerminalOutcome.TRANSIENT,
        path=TerminalPath.SINK_FALLBACK_TO_FAILSINK,
        sink_name="failsink",
        sink_node_id=sink_id,
        artifact_id=artifact.artifact_id,
        error_hash="failsink-error",
    )

    with db.read_only_connection() as conn:
        assert (
            conn.execute(select(artifacts_table.c.artifact_id).where(artifacts_table.c.artifact_id == artifact.artifact_id)).scalar_one()
            == artifact.artifact_id
        )
        assert len(conn.execute(select(token_outcomes_table.c.outcome_id).where(token_outcomes_table.c.token_id == token_id)).all()) == 1


def test_bulk_state_completion_lock_order_is_sorted_across_distinct_postgres_backends(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    postgres_url: str,
) -> None:
    """Reversed bulk callers take state locks in one order and cannot deadlock."""
    first_db, first_factory = postgres_factory
    second_db = LandscapeDB(postgres_url)
    second_factory = RecorderFactory(second_db)
    run = first_factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )
    source_id = register_test_node(first_factory.data_flow, run.run_id, "source-state-lock", node_type=NodeType.SOURCE)
    sink_id = register_test_node(first_factory.data_flow, run.run_id, "sink-state-lock", node_type=NodeType.SINK)
    states = []
    for index, state_id in enumerate(("bulk-lock-state-a", "bulk-lock-state-b")):
        data = {"value": index}
        row = first_factory.data_flow.create_row(
            run_id=run.run_id,
            source_node_id=source_id,
            row_index=index,
            data=data,
            source_row_index=index,
            ingest_sequence=index,
        )
        token = first_factory.data_flow.create_token(row.row_id)
        states.append(
            first_factory.execution.begin_node_state(
                token_id=token.token_id,
                node_id=sink_id,
                run_id=run.run_id,
                step_index=0,
                input_data=data,
                state_id=state_id,
            )
        )

    expected_order = tuple(sorted(state.state_id for state in states))
    target_state_ids = set(expected_order)
    lock_attempted = {name: threading.Event() for name in ("first", "second")}
    first_lock_acquired = {name: threading.Event() for name in ("first", "second")}
    release_first = threading.Event()
    backend_pids: dict[str, int] = {}
    lock_orders: dict[str, list[tuple[str, ...]]] = {"first": [], "second": []}

    def locked_state_ids(statement: str, parameters: Any) -> tuple[str, ...]:
        if "FROM node_states" not in statement or "FOR UPDATE" not in statement.upper():
            return ()
        if not isinstance(parameters, dict):
            return ()
        return tuple(str(value) for value in parameters.values() if value in target_state_ids)

    listeners: list[tuple[Any, str, Any]] = []

    def install_lock_probe(name: str, db: LandscapeDB) -> None:
        def before_cursor_execute(
            conn: Any,
            _cursor: Any,
            statement: str,
            parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            if not locked_state_ids(statement, parameters):
                return
            driver_connection = conn.connection.driver_connection
            backend_pids.setdefault(name, driver_connection.info.backend_pid)
            lock_attempted[name].set()

        def after_cursor_execute(
            _conn: Any,
            _cursor: Any,
            statement: str,
            parameters: Any,
            _context: Any,
            _executemany: bool,
        ) -> None:
            state_ids = locked_state_ids(statement, parameters)
            if not state_ids:
                return
            lock_orders[name].append(state_ids)
            if len(lock_orders[name]) == 1:
                first_lock_acquired[name].set()
                if name == "first":
                    assert release_first.wait(timeout=5), "first contender was not released after acquiring its state locks"

        event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
        event.listen(db.engine, "after_cursor_execute", after_cursor_execute)
        listeners.extend(
            (
                (db.engine, "before_cursor_execute", before_cursor_execute),
                (db.engine, "after_cursor_execute", after_cursor_execute),
            )
        )

    install_lock_probe("first", first_db)
    install_lock_probe("second", second_db)

    first_completions = (
        (states[1].state_id, {"winner": "first-b"}, 1.0),
        (states[0].state_id, {"winner": "first-a"}, 1.0),
    )
    second_completions = tuple(reversed(first_completions))

    def complete(
        factory: RecorderFactory,
        completions: tuple[tuple[str, dict[str, str], float], ...],
    ) -> LandscapeRecordError | None:
        try:
            factory.execution.complete_node_states_completed_many(completions)
        except LandscapeRecordError as exc:
            return exc
        return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(complete, first_factory, first_completions)
            assert first_lock_acquired["first"].wait(timeout=5), "first contender never acquired its first state lock"

            second_future = pool.submit(complete, second_factory, second_completions)
            assert lock_attempted["second"].wait(timeout=5), "second contender never attempted its first state lock"
            assert backend_pids["first"] != backend_pids["second"]

            deadline = monotonic() + 5
            with first_db.engine.connect() as observer:
                while monotonic() < deadline:
                    wait_row = observer.exec_driver_sql(
                        "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s",
                        (backend_pids["second"],),
                    ).one()
                    if wait_row.wait_event_type == "Lock":
                        break
                else:
                    pytest.fail(f"second backend never entered a PostgreSQL lock wait; last activity={wait_row!r}")

            release_first.set()
            results = (first_future.result(timeout=10), second_future.result(timeout=10))
    finally:
        release_first.set()
        for engine, identifier, listener in listeners:
            event.remove(engine, identifier, listener)
        second_db.engine.dispose()

    assert results[0] is None
    assert isinstance(results[1], LandscapeRecordError)
    assert "already terminal" in str(results[1])
    assert "40P01" not in str(results[1])
    assert lock_orders == {"first": [expected_order], "second": [expected_order]}

    with first_db.read_only_connection() as conn:
        terminal_rows = conn.execute(
            select(node_states_table.c.state_id, node_states_table.c.status, node_states_table.c.output_hash)
            .where(node_states_table.c.state_id.in_(expected_order))
            .order_by(node_states_table.c.state_id)
        ).all()
    assert terminal_rows == [
        ("bulk-lock-state-a", NodeStateStatus.COMPLETED.value, stable_hash({"winner": "first-a"})),
        ("bulk-lock-state-b", NodeStateStatus.COMPLETED.value, stable_hash({"winner": "first-b"})),
    ]


def test_postgres_outcome_dependency_lock_chunks_large_flushes(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 1200-token composed flush locks in ascending 500-id chunks: every
    statement stays under the dialect bound-parameter ceilings and the
    concatenated chunk ids form one globally ascending acquisition order, so
    chunking cannot reintroduce a lock-order inversion (elspeth-a2e1e511ea)."""
    db, factory = postgres_factory
    outcomes = factory.data_flow.outcomes
    chunks: list[list[str]] = []
    original = outcomes._execute_lock_query

    def spy(conn: Connection, query: Any, *, operation: str) -> list[Any]:
        compiled = query.compile(dialect=conn.dialect)
        # The IN clause rides in one "expanding" bind parameter whose value is
        # the chunk's id list itself.
        chunk_ids = [value for value in compiled.params.values() if isinstance(value, (list, tuple))]
        assert len(chunk_ids) == 1
        chunks.append([str(token_id) for token_id in chunk_ids[0]])
        return original(conn, query, operation=operation)

    monkeypatch.setattr(outcomes, "_execute_lock_query", spy)
    refs = tuple(TokenRef(token_id=f"tok-{index:05d}", run_id="chunk-run") for index in range(1200))
    with db.engine.begin() as conn:
        outcomes.lock_token_outcome_dependencies(refs, conn=conn)

    assert [len(chunk) for chunk in chunks] == [500, 500, 200]
    flattened = [token_id for chunk in chunks for token_id in chunk]
    assert flattened == sorted(ref.token_id for ref in refs)


def test_postgres_bulk_state_completion_prelock_chunks_large_batches(
    postgres_factory: tuple[LandscapeDB, RecorderFactory],
) -> None:
    """A 1200-state bulk completion prelocks in ascending 500-id chunks: every
    FOR UPDATE statement stays under the dialect bound-parameter ceilings and
    the concatenated chunk ids form one globally ascending acquisition order,
    so chunking cannot reintroduce a lock-order inversion."""
    db, factory = postgres_factory
    completions = tuple((f"bulk-chunk-state-{index:05d}", {"value": index}, 1.0) for index in range(1200))
    chunks: list[list[str]] = []

    with db.engine.begin() as conn:
        original_execute = conn.execute

        def spy(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            # Only a SELECT ... FOR UPDATE is a prelock: narrow to the concrete
            # Select before reading the with_for_update() marker off it.
            if isinstance(stmt, Select) and stmt._for_update_arg is not None:
                compiled = stmt.compile(dialect=conn.dialect)
                chunk_ids = [value for value in compiled.params.values() if isinstance(value, (list, tuple))]
                assert len(chunk_ids) == 1
                chunks.append([str(state_id) for state_id in chunk_ids[0]])
            return original_execute(stmt, *args, **kwargs)

        conn.execute = spy  # type: ignore[method-assign]
        with pytest.raises(LandscapeRecordError, match="target rows do not exist"):
            factory.execution.complete_node_states_completed_many(completions, conn=conn)

    assert [len(chunk) for chunk in chunks] == [500, 500, 200]
    flattened = [state_id for chunk in chunks for state_id in chunk]
    assert flattened == sorted(state_id for state_id, _output_data, _duration_ms in completions)
