"""K056: call lineage and verification decisions remain durable and run bound."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError, RunLeadershipLostError
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataState
from elspeth.core.landscape.schema import call_verifications_table, calls_table
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run, register_test_node
from tests.helpers.state_engine import capture_state_engine_image


def _two_runs() -> tuple[RecorderFactory, str, str]:
    setup = make_recorder_with_run(run_id="source", source_node_id="source-node")
    factory = setup.factory
    factory.run_lifecycle.begin_run(
        config={},
        canonical_version="v1",
        run_id="current",
        run_mode=RunMode.VERIFY,
        replay_from_run_id="source",
    )
    register_test_node(factory.data_flow, "current", "source-node", node_type=NodeType.SOURCE, plugin_name="source")
    source_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "source")
    )
    current_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "current")
    )
    return factory, source_operation.operation_id, current_operation.operation_id


def _record(factory: RecorderFactory, run_id: str, operation_id: str, *, source_call_id: str | None = None):
    return factory.execution.record_operation_call(
        operation_id,
        CallType.HTTP,
        CallStatus.SUCCESS,
        request_data=RawCallPayload({"url": "https://example.test/a"}),
        response_data=RawCallPayload({"status": 200}),
        coordination_token=leader_coordination_token(factory, run_id),
        source_call_id=source_call_id,
    )


def test_run_and_call_lineage_round_trip_with_verified_request() -> None:
    factory, source_operation, current_operation = _two_runs()
    source_call = _record(factory, "source", source_operation)
    current_call = _record(factory, "current", current_operation, source_call_id=source_call.call_id)

    run = factory.run_lifecycle.get_run("current")
    assert run is not None
    assert (run.run_mode, run.replay_from_run_id) == (RunMode.VERIFY, "source")
    assert factory.execution.get_operation_calls(current_operation)[0].source_call_id == source_call.call_id
    found = factory.execution.find_call_for_current_parent(
        source_run_id="source",
        call_type=CallType.HTTP,
        request_hash=source_call.request_hash,
        current_state_id=None,
        current_operation_id=current_operation,
        current_call_index=0,
    )
    assert found is not None and found.call_id == source_call.call_id
    request = factory.execution.get_call_request_data(source_call.call_id)
    assert request.state is CallDataState.AVAILABLE
    assert request.data == {"url": "https://example.test/a"}

    decision = factory.execution.record_verification_decision(
        current_run_id="current",
        current_call_id=current_call.call_id,
        source_run_id="source",
        source_call_id=source_call.call_id,
        is_match=False,
        differences_json='{"status":{"expected":201,"actual":200}}',
        coordination_token=leader_coordination_token(factory, "current"),
    )
    persisted = factory.execution.get_verification_decision(current_call.call_id)
    assert persisted is not None
    assert (persisted.current_call_id, persisted.source_call_id, persisted.is_match, persisted.differences_json) == (
        decision.current_call_id,
        decision.source_call_id,
        False,
        decision.differences_json,
    )
    assert [item.current_call_id for item in factory.execution.get_verification_decisions_for_run("current")] == [current_call.call_id]
    current_image = capture_state_engine_image(factory, run_id="current")
    source_image = capture_state_engine_image(factory, run_id="source")
    assert [row["current_call_id"] for row in current_image.tables["call_verifications"]] == [current_call.call_id]
    assert source_image.tables["call_verifications"] == ()
    with factory._db.engine.connect() as conn:
        assert conn.execute(select(call_verifications_table.c.current_call_id)).scalar_one() == current_call.call_id


def test_rejects_wrong_run_source_call_and_verification_without_partial_write() -> None:
    factory, source_operation, current_operation = _two_runs()
    source_call = _record(factory, "source", source_operation)
    current_call = _record(factory, "current", current_operation)
    with pytest.raises(AuditIntegrityError, match="source call"):
        _record(factory, "current", current_operation, source_call_id=current_call.call_id)
    with pytest.raises(AuditIntegrityError, match="source call"):
        factory.execution.record_verification_decision(
            current_run_id="current",
            current_call_id=current_call.call_id,
            source_run_id="source",
            source_call_id=current_call.call_id,
            is_match=True,
            differences_json="{}",
            coordination_token=leader_coordination_token(factory, "current"),
        )
    assert factory.execution.get_verification_decision(current_call.call_id) is None
    assert [call.call_id for call in factory.execution.get_operation_calls(current_operation)] == [current_call.call_id]
    with factory._db.engine.connect() as conn:
        assert conn.execute(select(calls_table.c.call_id).where(calls_table.c.source_call_id == source_call.call_id)).all() == []


def test_verification_decision_requires_current_live_leader_before_write() -> None:
    factory, source_operation, current_operation = _two_runs()
    source_call = _record(factory, "source", source_operation)
    current_call = _record(factory, "current", current_operation)
    current_token = leader_coordination_token(factory, "current")
    kwargs = {
        "current_run_id": "current",
        "current_call_id": current_call.call_id,
        "source_run_id": "source",
        "source_call_id": source_call.call_id,
        "is_match": True,
        "differences_json": "{}",
    }

    with pytest.raises(AuditIntegrityError, match="token does not belong"):
        factory.execution.record_verification_decision(**kwargs, coordination_token=leader_coordination_token(factory, "source"))
    with pytest.raises(RunLeadershipLostError):
        factory.execution.record_verification_decision(
            **kwargs,
            coordination_token=replace(current_token, leader_epoch=current_token.leader_epoch + 1),
        )
    assert factory.execution.get_verification_decision(current_call.call_id) is None


@pytest.mark.parametrize("read_many", [False, True], ids=["single", "run"])
@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE call_verifications SET differences_json = '[]'",
        "UPDATE call_verifications SET differences_json = 'broken'",
        "UPDATE call_verifications SET differences_json = X'7b7d'",
        "UPDATE call_verifications SET differences_json = '{\"nested\": [NaN]}'",
        "UPDATE call_verifications SET differences_json = '{\"nested\": [1e999]}'",
        'UPDATE call_verifications SET differences_json = \'{"x": 1, "x": 2}\'',
        "UPDATE call_verifications SET differences_json = '{\"unexpected\": true}'",
        "UPDATE call_verifications SET is_match = 2",
        "UPDATE call_verifications SET is_match = 'truthy'",
        "UPDATE call_verifications SET source_call_id = NULL",
        "UPDATE call_verifications SET source_call_id = current_call_id",
        "UPDATE calls SET call_type = 'llm' WHERE operation_id = (SELECT operation_id FROM operations WHERE run_id = 'source')",
        "UPDATE calls SET call_type = 'unknown' WHERE operation_id = (SELECT operation_id FROM operations WHERE run_id = 'source')",
        "UPDATE runs SET run_mode = 'live' WHERE run_id = 'current'",
        "UPDATE runs SET replay_from_run_id = NULL WHERE run_id = 'current'",
        "UPDATE call_verifications SET current_run_id = 'source'",
        "UPDATE calls SET operation_id = (SELECT operation_id FROM operations WHERE run_id = 'source'), "
        "call_index = 1 WHERE call_id = (SELECT current_call_id FROM call_verifications)",
    ],
)
def test_verification_read_rejects_persisted_corruption(corruption: str, read_many: bool) -> None:
    factory, source_operation, current_operation = _two_runs()
    source_call = _record(factory, "source", source_operation)
    current_call = _record(factory, "current", current_operation)
    factory.execution.record_verification_decision(
        current_run_id="current",
        current_call_id=current_call.call_id,
        source_run_id="source",
        source_call_id=source_call.call_id,
        is_match=True,
        differences_json="{}",
        coordination_token=leader_coordination_token(factory, "current"),
    )
    # Model corrupted persisted Tier-1 data, bypassing write-time checks deliberately.
    with factory._db.engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.execute(text(corruption))
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    with pytest.raises(AuditIntegrityError):
        if read_many:
            factory.execution.get_verification_decisions_for_run("current")
        else:
            factory.execution.get_verification_decision(current_call.call_id)


@pytest.mark.parametrize("is_match", [True, False, None])
def test_verification_read_preserves_each_valid_verdict(is_match: bool | None) -> None:
    factory, source_operation, current_operation = _two_runs()
    source_call = _record(factory, "source", source_operation)
    current_call = _record(factory, "current", current_operation)
    source_call_id = None if is_match is None else source_call.call_id
    differences = '{"missing": true}' if is_match is None else "{}"
    factory.execution.record_verification_decision(
        current_run_id="current",
        current_call_id=current_call.call_id,
        source_run_id="source",
        source_call_id=source_call_id,
        is_match=is_match,
        differences_json=differences,
        coordination_token=leader_coordination_token(factory, "current"),
    )
    actual = factory.execution.get_verification_decision(current_call.call_id)
    assert actual is not None
    assert actual.is_match is is_match
    assert actual.source_call_id == source_call_id
    assert factory.execution.get_verification_decisions_for_run("current") == [actual]


@pytest.mark.parametrize("count", [1, 10])
@pytest.mark.parametrize("batch_size", [None, 2, 3])
def test_verification_run_read_uses_one_joined_query(count: int, batch_size: int | None) -> None:
    factory, source_operation, current_operation = _two_runs()
    expected_call_ids: list[str] = []
    for _ in range(count):
        source_call = _record(factory, "source", source_operation)
        current_call = _record(factory, "current", current_operation)
        expected_call_ids.append(current_call.call_id)
        factory.execution.record_verification_decision(
            current_run_id="current",
            current_call_id=current_call.call_id,
            source_run_id="source",
            source_call_id=source_call.call_id,
            is_match=True,
            differences_json="{}",
            coordination_token=leader_coordination_token(factory, "current"),
        )
    # Equal timestamps force the call-id keyset tiebreaker across page boundaries.
    with factory._db.write_connection() as conn:
        conn.execute(call_verifications_table.update().values(recorded_at=datetime(2026, 1, 1, tzinfo=UTC)))
    statements: list[str] = []

    def capture_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(factory._db.engine, "before_cursor_execute", capture_statement)
    try:
        if batch_size is None:
            decisions = factory.execution.get_verification_decisions_for_run("current")
        else:
            iterator = factory.execution.iter_verification_decisions_for_run("current", batch_size=batch_size)
            assert statements == []
            first = next(iterator)
            assert sum(statement.startswith("SELECT") for statement in statements) == 1
            decisions = [first, *iterator]
        assert len(decisions) == count
        assert [decision.current_call_id for decision in decisions] == sorted(expected_call_ids)
    finally:
        event.remove(factory._db.engine, "before_cursor_execute", capture_statement)
    expected_queries = 1 if batch_size is None else count // batch_size + 1
    queries = [statement for statement in statements if statement.startswith("SELECT")]
    assert len(queries) == expected_queries
    assert all("LIMIT" in statement for statement in queries)


@pytest.mark.parametrize("batch_size", [True, 0, -1, 1.5])
def test_verification_iterator_rejects_invalid_batch_size(batch_size: int) -> None:
    factory, _, _ = _two_runs()
    with pytest.raises(ValueError, match="positive exact integer"):
        list(factory.execution.iter_verification_decisions_for_run("current", batch_size=batch_size))


def test_repeated_source_loads_bind_transactional_occurrence() -> None:
    factory, source_operation, current_operation = _two_runs()
    first = _record(factory, "source", source_operation)
    second_source_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "source")
    )
    second = _record(factory, "source", second_source_operation.operation_id)
    second_current_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "current")
    )
    assert factory.execution.get_operation(source_operation).occurrence_index == 0
    assert factory.execution.get_operation(second_source_operation.operation_id).occurrence_index == 1
    assert (
        factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=first.request_hash,
            current_state_id=None,
            current_operation_id=current_operation,
            current_call_index=0,
        ).call_id
        == first.call_id
    )
    assert (
        factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=second.request_hash,
            current_state_id=None,
            current_operation_id=second_current_operation.operation_id,
            current_call_index=0,
        ).call_id
        == second.call_id
    )


def test_identical_sink_write_operations_remain_ambiguous() -> None:
    factory, _source_operation, _current_operation = _two_runs()
    for run_id in ("source", "current"):
        register_test_node(factory.data_flow, run_id, "sink-node", node_type=NodeType.SINK, plugin_name="sink")
    source_operations = [
        factory.execution.begin_operation("sink-node", "sink_write", coordination_token=leader_coordination_token(factory, "source"))
        for _ in range(2)
    ]
    source_calls = [_record(factory, "source", operation.operation_id) for operation in source_operations]
    current_operation = factory.execution.begin_operation(
        "sink-node", "sink_write", coordination_token=leader_coordination_token(factory, "current")
    )
    with pytest.raises(AuditIntegrityError, match="ambiguous"):
        factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=source_calls[0].request_hash,
            current_state_id=None,
            current_operation_id=current_operation.operation_id,
            current_call_index=0,
        )


def test_rejects_lineage_from_an_unconfigured_source_run() -> None:
    factory, _source_operation, current_operation = _two_runs()
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="other")
    register_test_node(factory.data_flow, "other", "source-node", node_type=NodeType.SOURCE, plugin_name="source")
    other_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "other")
    )
    other_call = _record(factory, "other", other_operation.operation_id)
    current_call = _record(factory, "current", current_operation)
    with pytest.raises(AuditIntegrityError, match="configured source run"):
        _record(factory, "current", current_operation, source_call_id=other_call.call_id)
    with pytest.raises(AuditIntegrityError, match="configured source run"):
        factory.execution.record_verification_decision(
            current_run_id="current",
            current_call_id=current_call.call_id,
            source_run_id="other",
            source_call_id=other_call.call_id,
            is_match=True,
            differences_json="{}",
            coordination_token=leader_coordination_token(factory, "current"),
        )
    assert factory.execution.get_verification_decisions_for_run("current") == []


def test_state_lookup_binds_node_and_source_row_then_exposes_source_token() -> None:
    factory, source_operation, _current_operation = _two_runs()
    source_operation_call = _record(factory, "source", source_operation)
    source_state_id = "source-state"
    current_state_id = "current-state"
    for run_id, state_id, ingest_sequence in (
        ("source", source_state_id, 3),
        ("current", current_state_id, 9),
    ):
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        _row, token = factory.data_flow.create_row_with_token(
            "source-node",
            0,
            {"value": 1},
            source_row_index=0,
            ingest_sequence=ingest_sequence,
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.execution.begin_node_state(
            token.token_id,
            "transform",
            0,
            {"value": 1},
            state_id=state_id,
            member_token=leader_coordination_token(factory, run_id).membership,
        )
    with factory._db.write_connection() as conn:
        conn.execute(
            calls_table.insert().values(
                call_id="state-source-call",
                state_id=source_state_id,
                operation_id=None,
                call_index=0,
                call_type=CallType.HTTP.value,
                status=CallStatus.SUCCESS.value,
                request_hash=stable_hash({"url": "https://example.test/a"}),
                created_at=datetime.now(UTC),
            )
        )
    calls = factory.execution.list_source_calls_for_current_parent(
        source_run_id="source",
        call_type=CallType.HTTP,
        current_state_id=current_state_id,
        current_operation_id=None,
    )
    assert [call.call_id for call in calls] == ["state-source-call"]
    source_state = factory.execution.get_node_state(calls[0].state_id)
    assert source_state is not None
    assert source_state.state_id == source_state_id
    assert source_state.token_id != factory.execution.get_node_state(current_state_id).token_id
    assert {call.call_id for call in factory.execution.get_all_calls_for_run("source")} == {
        "state-source-call",
        source_operation_call.call_id,
    }


def test_exact_state_call_lookup_loads_only_requested_occurrence(monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _source_operation, _current_operation = _two_runs()
    for run_id in ("source", "current"):
        register_test_node(factory.data_flow, run_id, "transform", plugin_name="transform")
        _row, token = factory.data_flow.create_row_with_token(
            "source-node",
            0,
            {"value": 1},
            source_row_index=0,
            ingest_sequence=0,
            coordination_token=leader_coordination_token(factory, run_id),
        )
        factory.execution.begin_node_state(
            token.token_id,
            "transform",
            0,
            {"value": 1},
            state_id=f"{run_id}-state",
            member_token=leader_coordination_token(factory, run_id).membership,
        )
    count = 16
    request_hash = stable_hash({"url": "https://example.test/a"})
    with factory._db.write_connection() as conn:
        for index in range(count):
            conn.execute(
                calls_table.insert().values(
                    call_id=f"source-call-{index}",
                    state_id="source-state",
                    operation_id=None,
                    call_index=index,
                    call_type=CallType.HTTP.value,
                    status=CallStatus.SUCCESS.value,
                    request_hash=request_hash,
                    created_at=datetime.now(UTC),
                )
            )
    loader = factory.execution.calls._call_loader
    original_load = loader.load
    loaded = 0

    def counted_load(row):
        nonlocal loaded
        loaded += 1
        return original_load(row)

    monkeypatch.setattr(loader, "load", counted_load)
    for index in range(count):
        found = factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=request_hash,
            current_state_id="current-state",
            current_operation_id=None,
            current_call_index=index,
        )
        assert found is not None and found.call_id == f"source-call-{index}"
    assert loaded == count
