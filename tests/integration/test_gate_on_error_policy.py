"""Integration coverage for config-gate row-error routing."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from elspeth.contracts.enums import NodeStateStatus, RoutingMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.types import GateName
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.expression_parser import ExpressionEvaluationError
from elspeth.core.landscape.factory import RecorderFactory
from tests.integration._helpers import (
    _pipeline_from_settings,
    make_settings_yaml_for_test_plugins,
    run_pipeline,
)

_OMIT_POLICY = object()


def _gate_yaml(
    rows: list[dict[str, object]],
    *,
    on_error: str | object = _OMIT_POLICY,
) -> str:
    gate: dict[str, object] = {
        "name": "threshold",
        "input": "gate_input",
        "condition": "row['amount'] > 500",
        "routes": {"true": "high_value", "false": "standard"},
    }
    if on_error is not _OMIT_POLICY:
        gate["on_error"] = on_error
    sinks = {
        "high_value": {"plugin": "collect_sink", "config": {"name": "high_value"}},
        "standard": {"plugin": "collect_sink", "config": {"name": "standard"}},
    }
    if on_error == "gate_errors":
        sinks["gate_errors"] = {"plugin": "collect_sink", "config": {"name": "gate_errors"}}
    return make_settings_yaml_for_test_plugins(
        source_plugin="list_source",
        source_config={"rows": rows, "on_success": "gate_input"},
        gates=[gate],
        sinks=sinks,
    )


def test_gate_on_error_routes_only_bad_row_and_continues_source_iteration(
    tmp_path,
    monkeypatch,
) -> None:
    """One row-level expression failure must not abort unaffected rows."""
    yaml_text = _gate_yaml(
        [
            {"id": 1, "amount": "not-a-number"},
            {"id": 2, "amount": 750},
            {"id": 3, "amount": 250},
        ],
        on_error="gate_errors",
    )
    settings = load_settings_from_yaml_string(yaml_text)
    config, graph, db, store = _pipeline_from_settings(settings, tmp_path, monkeypatch)

    result = run_pipeline(config, graph, db, store)

    assert result.status == RunStatus.COMPLETED_WITH_FAILURES
    assert result.rows_processed == 3
    assert result.rows_succeeded == 2
    assert result.rows_failed == 1
    assert result.rows_routed_failure == 1
    assert config.sinks["high_value"].rows_written == [{"id": 2, "amount": 750}]
    assert config.sinks["standard"].rows_written == [{"id": 3, "amount": 250}]
    assert config.sinks["gate_errors"].rows_written == [{"id": 1, "amount": "not-a-number"}]

    recorder = RecorderFactory(db, payload_store=store)
    outcomes = recorder.query.get_all_token_outcomes_for_run(result.run_id)
    assert Counter((outcome.outcome, outcome.path, outcome.sink_name) for outcome in outcomes) == Counter(
        {
            (TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED, "gate_errors"): 1,
            (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED, "high_value"): 1,
            (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED, "standard"): 1,
        }
    )
    gate_error = next(outcome for outcome in outcomes if outcome.path == TerminalPath.ON_ERROR_ROUTED)
    assert gate_error.error_hash is not None

    gate_node_id = graph.get_config_gate_id_map()[GateName("threshold")]
    gate_states = [state for state in recorder.query.get_node_states_for_token(gate_error.token_id) if state.node_id == gate_node_id]
    assert len(gate_states) == 1
    failed_state = gate_states[0]
    assert failed_state.status == NodeStateStatus.FAILED
    assert failed_state.error_json is not None
    recorded_error = json.loads(failed_state.error_json)
    assert recorded_error["type"] == "ExpressionEvaluationError"
    assert "cannot compare str and int" in recorded_error["exception"]

    routing_events = recorder.query.get_routing_events(failed_state.state_id)
    assert len(routing_events) == 1
    error_route = routing_events[0]
    assert error_route.mode == RoutingMode.DIVERT
    assert error_route.reason_ref is not None
    recorded_reason = json.loads(store.retrieve(error_route.reason_ref))
    assert recorded_reason == {
        "condition": "row['amount'] > 500",
        "error": "type error in comparison (Gt): cannot compare str and int",
        "error_type": "ExpressionEvaluationError",
    }


def test_gate_on_error_discard_fails_only_bad_row_and_continues(
    tmp_path,
    monkeypatch,
) -> None:
    """Gate discard is a failed row with gate-owned provenance, not quarantine."""
    settings = load_settings_from_yaml_string(
        _gate_yaml(
            [
                {"id": 1, "amount": "not-a-number"},
                {"id": 2, "amount": 750},
            ],
            on_error="discard",
        )
    )
    config, graph, db, store = _pipeline_from_settings(settings, tmp_path, monkeypatch)

    result = run_pipeline(config, graph, db, store)

    assert result.status == RunStatus.COMPLETED_WITH_FAILURES
    assert result.rows_processed == 2
    assert result.rows_succeeded == 1
    assert result.rows_failed == 1
    assert result.rows_quarantined == 0
    assert result.rows_routed_failure == 0
    assert config.sinks["high_value"].rows_written == [{"id": 2, "amount": 750}]
    assert config.sinks["standard"].rows_written == []
    outcomes = RecorderFactory(db).query.get_all_token_outcomes_for_run(result.run_id)
    assert Counter((outcome.outcome, outcome.path, outcome.sink_name) for outcome in outcomes) == Counter(
        {
            (TerminalOutcome.FAILURE, TerminalPath.GATE_ERROR_DISCARDED, None): 1,
            (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED, "high_value"): 1,
        }
    )
    discarded = next(outcome for outcome in outcomes if outcome.path == TerminalPath.GATE_ERROR_DISCARDED)
    assert discarded.error_hash is not None

    recorder = RecorderFactory(db, payload_store=store)
    gate_node_id = graph.get_config_gate_id_map()[GateName("threshold")]
    gate_states = [state for state in recorder.query.get_node_states_for_token(discarded.token_id) if state.node_id == gate_node_id]
    assert len(gate_states) == 1
    failed_state = gate_states[0]
    assert failed_state.status == NodeStateStatus.FAILED
    assert failed_state.error_json is not None
    assert json.loads(failed_state.error_json)["type"] == "ExpressionEvaluationError"
    assert recorder.query.get_routing_events(failed_state.state_id) == []

    recorded_gate = recorder.data_flow.get_node(gate_node_id, result.run_id)
    assert recorded_gate is not None
    gate_config = json.loads(recorded_gate.config_json)
    assert gate_config["condition"] == "row['amount'] > 500"
    assert gate_config["on_error"] == "discard"


def test_gate_without_on_error_preserves_fail_fast_execution(
    tmp_path,
    monkeypatch,
) -> None:
    settings = load_settings_from_yaml_string(
        _gate_yaml(
            [
                {"id": 1, "amount": "not-a-number"},
                {"id": 2, "amount": 750},
            ]
        )
    )
    config, graph, db, store = _pipeline_from_settings(settings, tmp_path, monkeypatch)

    with pytest.raises(ExpressionEvaluationError, match="cannot compare str and int"):
        run_pipeline(config, graph, db, store)

    assert config.sinks["high_value"].rows_written == []
    assert config.sinks["standard"].rows_written == []
