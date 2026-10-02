"""Database fault controls for atomic node-state writes and readback."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select

from elspeth.contracts import NodeStateStatus
from elspeth.contracts.errors import AuditIntegrityError, ExecutionError
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.errors import LandscapePostCommitError, LandscapeRecordError
from elspeth.core.landscape.schema import node_states_table
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run


@pytest.fixture
def node_fault_setup() -> Iterator[RecorderSetup]:
    value = make_recorder_with_run(run_id="run-1", source_node_id="source-0")
    value.data_flow.create_row_with_token(
        "source-0",
        0,
        {"value": 1},
        token_id="tok-0",
        source_row_index=0,
        ingest_sequence=0,
        coordination_token=value.coordination_token,
    )
    try:
        yield value
    finally:
        value.db.close()


def _trigger(node_fault_setup: RecorderSetup, *, operation: str, action: str) -> None:
    # These triggers represent a broken database writer. The normal control
    # is asserted before installing one, so a refused write is not a missing
    # fixture token or an invalid leadership fence.
    with node_fault_setup.db.write_connection() as conn:
        conn.exec_driver_sql(f"CREATE TRIGGER corrupt_state AFTER {operation} ON node_states BEGIN {action}; END")


@pytest.mark.parametrize("kind", ["completed", "quarantined"])
@pytest.mark.parametrize(
    ("action", "error", "message"),
    [
        ("DELETE FROM node_states WHERE state_id = NEW.state_id", LandscapeRecordError, "not found after insert"),
        (
            "UPDATE node_states SET status='open',output_hash=NULL,error_json=NULL,duration_ms=NULL,completed_at=NULL WHERE state_id=NEW.state_id",
            LandscapePostCommitError,
            "should be",
        ),
        ("UPDATE node_states SET duration_ms=NULL WHERE state_id=NEW.state_id", LandscapePostCommitError, "unreadable"),
        ("SELECT RAISE(ABORT, 'injected rejection')", LandscapeRecordError, "database rejected audit write"),
    ],
)
def test_atomic_insert_refuses_broken_database_readback(
    node_fault_setup: RecorderSetup, kind: str, action: str, error: type[Exception], message: str
) -> None:
    _trigger(node_fault_setup, operation="INSERT", action=action)
    with pytest.raises(error, match=message), node_fault_setup.db.write_connection() as conn:
        if kind == "completed":
            node_fault_setup.execution.node_states.record_completed_node_state_on(
                conn,
                "tok-0",
                "source-0",
                0,
                {"value": 1},
                {"value": 1},
                1,
                state_id="state-1",
                coordination_token=node_fault_setup.coordination_token,
            )
        else:
            node_fault_setup.execution.node_states.record_failed_source_quarantine_state_on(
                conn,
                token_id="tok-0",
                source_node_id="source-0",
                input_data={"value": 1},
                error=ExecutionError(exception="bad input", exception_type="ValueError"),
                state_id="state-1",
                coordination_token=node_fault_setup.coordination_token,
            )
    with node_fault_setup.db.read_only_connection() as conn:
        assert conn.execute(select(node_states_table)).all() == []


def test_quarantined_completed_state_records_noncanonical_input_hash(node_fault_setup: RecorderSetup) -> None:
    result = node_fault_setup.execution.node_states.record_completed_node_state(
        "tok-0",
        "source-0",
        0,
        {"value": float("nan")},
        {"value": "quarantined"},
        1,
        quarantined=True,
        state_id="state-1",
        coordination_token=node_fault_setup.coordination_token,
    )
    assert result.status is NodeStateStatus.COMPLETED
    assert result.input_hash is not None
    assert node_fault_setup.execution.get_node_state("state-1") == result


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize(
    ("action", "error", "message"),
    [
        ("DELETE FROM node_states WHERE state_id=NEW.state_id", LandscapeRecordError, "not found after update"),
        (
            "UPDATE node_states SET status='open',output_hash=NULL,error_json=NULL,duration_ms=NULL,completed_at=NULL WHERE state_id=NEW.state_id",
            LandscapePostCommitError,
            "should be",
        ),
        ("UPDATE node_states SET duration_ms=NULL WHERE state_id=NEW.state_id", LandscapePostCommitError, "unreadable"),
        ("SELECT RAISE(ABORT, 'injected rejection')", LandscapeRecordError, "database rejected audit update"),
    ],
)
def test_completion_refuses_broken_database_readback(
    node_fault_setup: RecorderSetup, bulk: bool, action: str, error: type[Exception], message: str
) -> None:
    node_fault_setup.execution.begin_node_state(
        "tok-0", "source-0", 0, {"value": 1}, state_id="state-1", member_token=node_fault_setup.coordination_token.membership
    )
    assert node_fault_setup.execution.get_node_state("state-1").status is NodeStateStatus.OPEN
    _trigger(node_fault_setup, operation="UPDATE", action=action)
    if bulk and action.startswith("DELETE"):
        error, message = LandscapePostCommitError, "loaded 0 states"
    with pytest.raises(error, match=message), node_fault_setup.db.write_connection() as conn:
        if bulk:
            node_fault_setup.execution.node_states.complete_node_states_completed_many([("state-1", {"value": 1}, 1)], conn=conn)
        else:
            node_fault_setup.execution.node_states.complete_node_state_on(
                run_id=node_fault_setup.run_id,
                state_id="state-1",
                status=NodeStateStatus.COMPLETED,
                output_data={"value": 1},
                duration_ms=1,
                conn=conn,
            )
    assert node_fault_setup.execution.get_node_state("state-1").status is NodeStateStatus.OPEN


def test_bulk_completion_refuses_missing_state_and_rolls_back_existing_state(node_fault_setup: RecorderSetup) -> None:
    state = node_fault_setup.execution.begin_node_state(
        "tok-0", "source-0", 0, {"value": 1}, state_id="state-1", member_token=node_fault_setup.coordination_token.membership
    )
    with pytest.raises(LandscapeRecordError, match="target rows do not exist"), node_fault_setup.db.write_connection() as conn:
        node_fault_setup.execution.node_states.complete_node_states_completed_many(
            [(state.state_id, {"value": 1}, 1), ("absent", {"value": 1}, 1)], conn=conn
        )
    assert node_fault_setup.execution.get_node_state(state.state_id).status is NodeStateStatus.OPEN


def test_completion_does_not_write_a_foreign_run_state(node_fault_setup: RecorderSetup) -> None:
    state = node_fault_setup.execution.begin_node_state(
        "tok-0", "source-0", 0, {"value": 1}, state_id="state-1", member_token=node_fault_setup.coordination_token.membership
    )
    with pytest.raises(AuditIntegrityError, match="foreign run"), node_fault_setup.db.write_connection() as conn:
        node_fault_setup.execution.node_states.complete_node_state_on(
            run_id="foreign",
            state_id=state.state_id,
            status=NodeStateStatus.COMPLETED,
            output_data={"value": 1},
            duration_ms=1,
            conn=conn,
        )
    assert node_fault_setup.execution.get_node_state(state.state_id).status is NodeStateStatus.OPEN


def test_source_reconciliation_refuses_partial_witness_insert(node_fault_setup: RecorderSetup) -> None:
    with node_fault_setup.db.write_connection() as conn:
        conn.exec_driver_sql("CREATE TRIGGER ignore_witness BEFORE INSERT ON node_states BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(LandscapeRecordError, match="incomplete witness set"), node_fault_setup.db.write_connection() as conn:
        node_fault_setup.execution.node_states.record_source_completions_on(
            conn, run_id=node_fault_setup.run_id, entries=[("tok-0", "source-0", {"value": 1})]
        )
    with node_fault_setup.db.read_only_connection() as conn:
        assert conn.execute(select(node_states_table)).all() == []


def test_source_reconciliation_refuses_different_start_and_completion_times(node_fault_setup: RecorderSetup) -> None:
    state = node_fault_setup.execution.node_states.record_completed_node_state(
        "tok-0", "source-0", 0, {"value": 1}, {"value": 1}, 0, coordination_token=node_fault_setup.coordination_token
    )
    with node_fault_setup.db.write_connection() as conn:
        assert node_fault_setup.execution.node_states.validate_existing_source_completed_node_state_on(
            conn, run_id=node_fault_setup.run_id, token_id="tok-0", source_node_id="source-0", expected_hash=stable_hash({"value": 1})
        )
        conn.execute(node_states_table.update().values(completed_at=state.started_at + timedelta(seconds=1)))
    with pytest.raises(AuditIntegrityError, match="conflicting audit evidence"), node_fault_setup.db.write_connection() as conn:
        node_fault_setup.execution.node_states.validate_existing_source_completed_node_state_on(
            conn, run_id=node_fault_setup.run_id, token_id="tok-0", source_node_id="source-0", expected_hash=stable_hash({"value": 1})
        )


def test_bulk_begin_refuses_database_rejection(node_fault_setup: RecorderSetup) -> None:
    with node_fault_setup.db.write_connection() as conn:
        conn.exec_driver_sql("CREATE TRIGGER reject_many BEFORE INSERT ON node_states BEGIN SELECT RAISE(ABORT, 'rejected'); END")
    with pytest.raises(LandscapeRecordError, match="database rejected audit write"):
        node_fault_setup.execution.node_states.begin_node_states_many(
            [("tok-0", "source-0", 0, {"value": 1})], coordination_token=node_fault_setup.coordination_token
        )
    with node_fault_setup.db.read_only_connection() as conn:
        assert conn.execute(select(node_states_table)).all() == []
