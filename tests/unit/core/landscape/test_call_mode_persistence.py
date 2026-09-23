"""K056: call lineage and verification decisions remain durable and run bound."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.row_data import CallDataState
from elspeth.core.landscape.schema import call_verifications_table, calls_table
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run, register_test_node


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
        )
    assert factory.execution.get_verification_decision(current_call.call_id) is None
    assert [call.call_id for call in factory.execution.get_operation_calls(current_operation)] == [current_call.call_id]
    with factory._db.engine.connect() as conn:
        assert conn.execute(select(calls_table.c.call_id).where(calls_table.c.source_call_id == source_call.call_id)).all() == []


def test_duplicate_operation_candidates_are_ambiguous_even_with_equal_timestamps() -> None:
    factory, source_operation, current_operation = _two_runs()
    first = _record(factory, "source", source_operation)
    duplicate_operation = factory.execution.begin_operation(
        "source-node", "source_load", coordination_token=leader_coordination_token(factory, "source")
    )
    _record(factory, "source", duplicate_operation.operation_id)
    with pytest.raises(AuditIntegrityError, match="ambiguous"):
        factory.execution.find_call_for_current_parent(
            source_run_id="source",
            call_type=CallType.HTTP,
            request_hash=first.request_hash,
            current_state_id=None,
            current_operation_id=current_operation,
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
        )
    assert factory.execution.get_verification_decisions_for_run("current") == []
