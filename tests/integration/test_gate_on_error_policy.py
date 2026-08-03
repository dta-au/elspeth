"""Integration coverage for config-gate row-error routing."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts.enums import NodeStateStatus, RoutingMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.types import GateName
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.expression_parser import ExpressionEvaluationError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db
from tests.integration._helpers import (
    _pipeline_from_settings,
    make_settings_yaml_for_test_plugins,
    run_pipeline,
)

_OMIT_POLICY = object()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_real_csv_gate_pipeline(
    tmp_path: Path,
    *,
    on_error: str | object = _OMIT_POLICY,
    condition: str = "row['id'] == '2' or row['amount'] > 500",
):
    input_path = tmp_path / "rows.csv"
    input_path.write_text("id,amount\n1,not-a-number\n2,750.00\n")
    high_path = tmp_path / "high.jsonl"
    standard_path = tmp_path / "standard.jsonl"
    error_path = tmp_path / "gate-errors.jsonl"
    policy_line = "" if on_error is _OMIT_POLICY else f"    on_error: {on_error}\n"
    error_sink = ""
    if on_error == "gate_errors":
        error_sink = f"""
  gate_errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {error_path}
      format: jsonl
      schema:
        mode: observed
"""
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: csv
    on_success: gate_input
    options:
      path: {input_path}
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: threshold
    input: gate_input
{policy_line}    condition: "{condition}"
    routes:
      'true': high_value
      'false': standard
sinks:
  high_value:
    plugin: json
    on_write_failure: discard
    options:
      path: {high_path}
      format: jsonl
      schema:
        mode: observed
  standard:
    plugin: json
    on_write_failure: discard
    options:
      path: {standard_path}
      format: jsonl
      schema:
        mode: observed
{error_sink}
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'real-csv-audit.db'}")
    store = FilesystemPayloadStore(tmp_path / "real-csv-payloads")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=store)
    return result, graph, db, store, high_path, standard_path, error_path


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
    assert recorded_error["exception"] == "gate expression evaluation failed: incompatible runtime types"

    routing_events = recorder.query.get_routing_events(failed_state.state_id)
    assert len(routing_events) == 1
    error_route = routing_events[0]
    assert error_route.mode == RoutingMode.DIVERT
    assert error_route.reason_ref is not None
    recorded_reason = json.loads(store.retrieve(error_route.reason_ref))
    assert recorded_reason == {
        "condition": "row['amount'] > 500",
        "error": "gate expression evaluation failed: incompatible runtime types",
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

    summary = load_discard_summaries_from_db(db, [result.run_id])[result.run_id]
    assert summary.total == 1
    assert summary.gate_errors == 1
    assert summary.validation_errors == 0
    assert summary.transform_errors == 0
    assert summary.sink_discards == 0
    assert [stage.model_dump() for stage in summary.stages] == [
        {
            "stage": "gate_evaluation",
            "node_id": str(gate_node_id),
            "count": 1,
        }
    ]


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


def test_real_csv_string_gate_error_routes_bad_row_and_later_row_continues(tmp_path: Path) -> None:
    result, graph, db, store, high_path, standard_path, error_path = _run_real_csv_gate_pipeline(
        tmp_path,
        on_error="gate_errors",
    )

    assert result.status == RunStatus.COMPLETED_WITH_FAILURES
    assert result.rows_processed == 2
    assert result.rows_succeeded == 1
    assert result.rows_failed == 1
    assert _read_jsonl(error_path) == [{"amount": "not-a-number", "id": "1"}]
    assert _read_jsonl(high_path) == [{"amount": "750.00", "id": "2"}]
    assert _read_jsonl(standard_path) == []

    recorder = RecorderFactory(db, payload_store=store)
    outcomes = recorder.query.get_all_token_outcomes_for_run(result.run_id)
    failure = next(outcome for outcome in outcomes if outcome.path == TerminalPath.ON_ERROR_ROUTED)
    gate_node_id = graph.get_config_gate_id_map()[GateName("threshold")]
    failed_state = next(state for state in recorder.query.get_node_states_for_token(failure.token_id) if state.node_id == gate_node_id)
    assert failed_state.status == NodeStateStatus.FAILED
    assert [event.mode for event in recorder.query.get_routing_events(failed_state.state_id)] == [RoutingMode.DIVERT]


def test_real_csv_exact_numeric_gate_routes_every_string_type_error_without_aborting_iteration(tmp_path: Path) -> None:
    result, _graph, db, _store, high_path, standard_path, error_path = _run_real_csv_gate_pipeline(
        tmp_path,
        on_error="gate_errors",
        condition="row['amount'] > 500",
    )

    assert result.rows_processed == 2
    assert result.rows_succeeded == 0
    assert result.rows_failed == 2
    assert _read_jsonl(error_path) == [
        {"amount": "not-a-number", "id": "1"},
        {"amount": "750.00", "id": "2"},
    ]
    assert _read_jsonl(high_path) == []
    assert _read_jsonl(standard_path) == []
    outcomes = RecorderFactory(db).query.get_all_token_outcomes_for_run(result.run_id)
    assert [outcome.path for outcome in outcomes] == [
        TerminalPath.ON_ERROR_ROUTED,
        TerminalPath.ON_ERROR_ROUTED,
    ]


def test_real_csv_string_gate_error_without_policy_aborts_before_later_row(tmp_path: Path) -> None:
    with pytest.raises(ExpressionEvaluationError, match="cannot compare str and int"):
        _run_real_csv_gate_pipeline(tmp_path)

    assert _read_jsonl(tmp_path / "high.jsonl") == []
    assert _read_jsonl(tmp_path / "standard.jsonl") == []
