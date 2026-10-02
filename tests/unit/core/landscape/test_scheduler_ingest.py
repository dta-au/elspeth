"""The fixed source-ingest composition owns every write in one leader transaction."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Connection, ExecutionContext

from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.errors import RunLeadershipLostError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.scheduler import SourceIngestSpec, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.core.landscape.schema import (
    node_states_table,
    rows_table,
    run_coordination_table,
    runs_table,
    scheduler_events_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import RecorderSetup, leader_coordination_token, make_recorder_with_run, register_test_node


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    setup = make_recorder_with_run()
    register_test_node(setup.factory.data_flow, setup.run_id, "normalize")
    try:
        yield setup
    finally:
        setup.db.close()


def _source(setup: RecorderSetup) -> SourceIngestSpec:
    return SourceIngestSpec(
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        row_id="ingest-row",
        token_id="ingest-token",
        data={"id": 1},
    )


def _ingest(setup: RecorderSetup, source: SourceIngestSpec, *, authority: CoordinationToken | None = None):
    token = leader_coordination_token(setup.factory, setup.run_id) if authority is None else authority
    return setup.factory.scheduler.ingest_row_with_initial_claim(
        coordination_token=token,
        source=source,
        data_flow=setup.factory.data_flow,
        execution=setup.factory.execution,
        node_id="normalize",
        step_index=1,
        row_payload_json=setup.factory.scheduler.serialize_row_payload(
            PipelineRow(deep_thaw(source.data), SchemaContract(mode="OBSERVED", fields=(), locked=True))
        ),
        lease_owner=token.worker_id,
        lease_seconds=60,
    )


def _image(setup: RecorderSetup):
    with setup.db.engine.connect() as conn:
        return tuple(
            tuple(conn.execute(select(table)).all())
            for table in (
                rows_table,
                tokens_table,
                node_states_table,
                token_work_items_table,
                scheduler_events_table,
                run_coordination_table,
            )
        )


def test_source_ingest_commits_matching_row_token_source_completion_and_claim(setup: RecorderSetup) -> None:
    source = _source(setup)
    row, token, work = _ingest(setup, source)
    assert (row.row_id, token.token_id, work.row_id, work.token_id) == (source.row_id, source.token_id, source.row_id, source.token_id)
    assert (row.run_id, token.run_id, work.run_id) == (setup.run_id, setup.run_id, setup.run_id)
    assert work.status is TokenWorkStatus.LEASED
    assert work.lease_owner == leader_coordination_token(setup.factory, setup.run_id).worker_id
    with setup.db.engine.connect() as conn:
        completed = conn.execute(select(node_states_table)).mappings().one()
        events = conn.execute(select(scheduler_events_table.c.event_type).order_by(scheduler_events_table.c.seq)).scalars().all()
    assert (completed["token_id"], completed["node_id"], completed["step_index"], completed["status"], completed["duration_ms"]) == (
        source.token_id,
        source.source_node_id,
        0,
        "completed",
        0,
    )
    assert completed["input_hash"] == completed["output_hash"] == row.source_data_hash == stable_hash(source.data)
    assert events == ["enqueue", "claim_ready"]


@pytest.mark.parametrize("table_name", ["ROWS", "TOKENS", "NODE_STATES", "TOKEN_WORK_ITEMS", "SCHEDULER_EVENTS"])
def test_source_ingest_failure_after_each_write_rolls_back_every_component(setup: RecorderSetup, table_name: str) -> None:
    source = _source(setup)
    before = _image(setup)
    injected: list[bool] = []

    def fail_after_write(
        conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool
    ) -> None:
        if statement.upper().startswith(f"INSERT INTO {table_name} "):
            injected.append(True)
            raise RuntimeError("injected source ingest crash")

    event.listen(setup.db.engine, "after_cursor_execute", fail_after_write)
    try:
        with pytest.raises(RuntimeError, match="injected source ingest crash"):
            _ingest(setup, source)
    finally:
        event.remove(setup.db.engine, "after_cursor_execute", fail_after_write)
    assert injected == [True]
    assert _image(setup) == before


def test_stale_source_ingest_has_no_partial_payload(setup: RecorderSetup) -> None:
    token = leader_coordination_token(setup.factory, setup.run_id)
    with setup.db.engine.begin() as conn:
        conn.execute(
            update(run_coordination_table)
            .where(run_coordination_table.c.run_id == setup.run_id)
            .values(leader_epoch=token.leader_epoch + 1)
        )
    before = _image(setup)
    with pytest.raises(RunLeadershipLostError):
        _ingest(setup, _source(setup), authority=token)
    assert _image(setup) == before


class _ImpostorDependency:
    def insert_row_with_token_on(self, conn: Connection, **kwargs):
        conn.execute(update(runs_table).values(settings_json='{"impostor":true}'))
        raise AssertionError("impostor dependency was invoked")

    def record_completed_node_state_on(self, conn: Connection, **kwargs):
        conn.execute(update(runs_table).values(settings_json='{"impostor":true}'))
        raise AssertionError("impostor dependency was invoked")


class _MaliciousDataFlow(DataFlowRepository):
    def __init__(self) -> None:
        pass

    def insert_row_with_token_on(self, conn: Connection, **kwargs):
        conn.execute(update(runs_table).values(settings_json='{"subclass":true}'))
        raise AssertionError("repository subclass was invoked")


class _MaliciousExecution(ExecutionRepository):
    def __init__(self) -> None:
        pass

    def record_completed_node_state_on(self, conn: Connection, **kwargs):
        conn.execute(update(runs_table).values(settings_json='{"subclass":true}'))
        raise AssertionError("repository subclass was invoked")


@pytest.mark.parametrize("dependency", ["data-flow-impostor", "execution-impostor", "data-flow-subclass", "execution-subclass"])
def test_source_ingest_rejects_unowned_dependencies_before_begin(setup: RecorderSetup, dependency: str) -> None:
    token = leader_coordination_token(setup.factory, setup.run_id)
    data_flow = setup.factory.data_flow
    execution = setup.factory.execution
    if dependency == "data-flow-impostor":
        data_flow = _ImpostorDependency()
    elif dependency == "execution-impostor":
        execution = _ImpostorDependency()
    elif dependency == "data-flow-subclass":
        data_flow = _MaliciousDataFlow()
    else:
        execution = _MaliciousExecution()
    statements: list[str] = []

    def capture(conn: Connection, cursor: object, statement: str, parameters: object, context: ExecutionContext, executemany: bool) -> None:
        statements.append(statement)

    event.listen(setup.db.engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(TypeError, match="requires an exact"):
            setup.factory.scheduler.ingest_row_with_initial_claim(
                coordination_token=token,
                source=_source(setup),
                data_flow=data_flow,
                execution=execution,
                node_id="normalize",
                step_index=1,
                row_payload_json="{}",
                lease_owner=token.worker_id,
                lease_seconds=60,
            )
    finally:
        event.remove(setup.db.engine, "before_cursor_execute", capture)
    assert statements == []


def test_source_ingest_spec_detaches_nested_source_data(setup: RecorderSetup) -> None:
    nested = {"nested": "before"}
    original = {"values": [1, nested]}
    spec = SourceIngestSpec(
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        row_id="row",
        token_id="token",
        data=original,
    )
    before = stable_hash(spec.data)
    original["values"].append(2)
    nested["nested"] = "after"
    assert stable_hash(spec.data) == before
    row, _, _ = _ingest(setup, spec)
    assert row.source_data_hash == before
    with pytest.raises(TypeError):
        spec.data["changed"] = True
