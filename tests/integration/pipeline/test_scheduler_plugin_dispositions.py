"""Real plugin failures must reach one coherent durable scheduler disposition.

These tests deliberately assemble the production plugin bundle and execution
graph.  The scheduler evidence is asserted together with the plugin, routing,
token-outcome, payload-scrub, and sink-effect evidence so a locally plausible
record in one audit plane cannot conceal disagreement in another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts.enums import NodeStateStatus, NodeType, RoutingMode, TerminalOutcome, TerminalPath
from elspeth.contracts.run_result import RunResult
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkStatus
from elspeth.core.canonical import canonical_json
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    edges_table,
    group_losses_table,
    node_states_table,
    nodes_table,
    operations_table,
    routing_events_table,
    run_workers_table,
    scheduler_events_table,
    sink_effect_members_table,
    sink_effects_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
    transform_errors_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config

_SECRET = "sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"  # secret-scan: allow-this-line
_BOUNDED_GATE_ERROR = "gate expression evaluation failed: missing key"


@dataclass(frozen=True)
class _RunEvidence:
    db: LandscapeDB
    payload_store: FilesystemPayloadStore
    result: RunResult


def _rows(db: LandscapeDB, table: Any, *, run_id: str) -> list[dict[str, Any]]:
    with db.engine.connect() as conn:
        records = conn.execute(select(table).where(table.c.run_id == run_id)).all()
    return [dict(record._mapping) for record in records]


def _run_pipeline(tmp_path: Path, *, processing_yaml: str) -> _RunEvidence:
    input_path = tmp_path / "input.jsonl"
    input_path.write_text(json.dumps({"id": 1, "notes": _SECRET}) + "\n")
    output_path = tmp_path / "output.jsonl"
    error_path = tmp_path / "errors.jsonl"
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: json
    on_success: processing
    options:
      path: {json.dumps(str(input_path))}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
{processing_yaml}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(output_path))}
      format: jsonl
      schema:
        mode: observed
  errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {json.dumps(str(error_path))}
      format: jsonl
      schema:
        mode: observed
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
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
    )
    return _RunEvidence(db=db, payload_store=payload_store, result=result)


def _assert_scheduler_sink_terminalization(
    evidence: _RunEvidence,
    *,
    processing_node: dict[str, Any],
    expected_error_message: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_id = evidence.result.run_id
    tokens = _rows(evidence.db, tokens_table, run_id=run_id)
    assert len(tokens) == 1
    token = tokens[0]

    work_items = _rows(evidence.db, token_work_items_table, run_id=run_id)
    assert len(work_items) == 1
    work_item = work_items[0]
    assert work_item["token_id"] == token["token_id"]
    assert work_item["node_id"] == processing_node["node_id"]
    assert work_item["status"] == TokenWorkStatus.TERMINAL.value
    assert work_item["lease_owner"] is None
    assert work_item["lease_expires_at"] is None
    assert work_item["pending_sink_name"] == "errors"
    assert work_item["pending_outcome"] == TerminalOutcome.FAILURE.value
    assert work_item["pending_path"] == TerminalPath.ON_ERROR_ROUTED.value
    assert work_item["pending_error_message"] == expected_error_message
    assert work_item["pending_error_hash"]

    scrubbed_payload = json.loads(work_item["row_payload_json"])
    assert scrubbed_payload == {
        "payload_hash": scrubbed_payload["payload_hash"],
        "row_payload": "purged",
    }
    assert len(scrubbed_payload["payload_hash"]) == 64
    assert _SECRET not in work_item["row_payload_json"]

    workers = _rows(evidence.db, run_workers_table, run_id=run_id)
    leaders = [worker for worker in workers if worker["role"] == "leader"]
    assert len(leaders) == 1
    leader = leaders[0]

    events = _rows(evidence.db, scheduler_events_table, run_id=run_id)
    assert sorted(event["event_type"] for event in events) == sorted(
        [
            SchedulerEventType.ENQUEUE.value,
            SchedulerEventType.CLAIM_READY.value,
            SchedulerEventType.MARK_PENDING_SINK.value,
            SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value,
        ]
    )
    events_by_type = {event["event_type"]: event for event in events}
    assert {(event["token_id"], event["work_item_id"], event["node_id"]) for event in events} == {
        (token["token_id"], work_item["work_item_id"], processing_node["node_id"])
    }
    assert {
        event_type: (
            event["from_status"],
            event["to_status"],
            event["from_lease_owner"],
            event["to_lease_owner"],
            event["caller_owner"],
            event["from_attempt"],
            event["to_attempt"],
        )
        for event_type, event in events_by_type.items()
    } == {
        SchedulerEventType.ENQUEUE.value: (None, TokenWorkStatus.READY.value, None, None, None, None, 1),
        SchedulerEventType.CLAIM_READY.value: (
            TokenWorkStatus.READY.value,
            TokenWorkStatus.LEASED.value,
            None,
            leader["worker_id"],
            leader["worker_id"],
            1,
            1,
        ),
        SchedulerEventType.MARK_PENDING_SINK.value: (
            TokenWorkStatus.LEASED.value,
            TokenWorkStatus.PENDING_SINK.value,
            leader["worker_id"],
            leader["worker_id"],
            leader["worker_id"],
            1,
            1,
        ),
        SchedulerEventType.MARK_PENDING_SINK_TERMINAL.value: (
            TokenWorkStatus.PENDING_SINK.value,
            TokenWorkStatus.TERMINAL.value,
            leader["worker_id"],
            None,
            leader["worker_id"],
            1,
            1,
        ),
    }

    outcomes = _rows(evidence.db, token_outcomes_table, run_id=run_id)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["token_id"] == token["token_id"]
    assert outcome["completed"] == 1
    assert outcome["outcome"] == TerminalOutcome.FAILURE.value
    assert outcome["path"] == TerminalPath.ON_ERROR_ROUTED.value
    assert outcome["sink_name"] == "errors"
    assert outcome["error_hash"] == work_item["pending_error_hash"]

    assert _rows(evidence.db, group_losses_table, run_id=run_id) == []

    effects = _rows(evidence.db, sink_effects_table, run_id=run_id)
    assert len(effects) == 1
    effect = effects[0]
    assert effect["state"] == "finalized"
    assert effect["lease_owner"] is None
    assert effect["lease_expires_at"] is None
    assert effect["lease_heartbeat_at"] is None
    assert effect["publication_performed"] is True
    assert effect["publication_evidence_kind"] == "returned"
    assert effect["result_descriptor_hash"]
    assert _SECRET not in effect["target_json"]
    assert effect["plan_json"] is not None
    assert _SECRET not in effect["plan_json"]

    members = _rows(evidence.db, sink_effect_members_table, run_id=run_id)
    assert len(members) == 1
    assert members[0]["effect_id"] == effect["effect_id"]
    assert members[0]["token_id"] == token["token_id"]
    assert _SECRET not in members[0]["lineage_json"]

    operations = [
        operation for operation in _rows(evidence.db, operations_table, run_id=run_id) if operation["sink_effect_id"] == effect["effect_id"]
    ]
    assert len(operations) == 1
    assert operations[0]["operation_type"] == "sink_write"
    assert operations[0]["status"] == "completed"
    assert operations[0]["error_message"] is None
    return token, work_item


def _assert_divert_routing(
    evidence: _RunEvidence,
    *,
    state: dict[str, Any],
    expected_reason: dict[str, Any],
) -> None:
    run_id = evidence.result.run_id
    events = _rows(evidence.db, routing_events_table, run_id=run_id)
    assert len(events) == 1
    event = events[0]
    assert event["state_id"] == state["state_id"]
    assert event["mode"] == RoutingMode.DIVERT.value
    assert event["reason_ref"] == event["reason_hash"]
    reason_payload = evidence.payload_store.retrieve(event["reason_ref"])
    assert reason_payload == canonical_json(expected_reason).encode("utf-8")
    assert _SECRET.encode() not in reason_payload

    edges = _rows(evidence.db, edges_table, run_id=run_id)
    selected_edge = next(edge for edge in edges if edge["edge_id"] == event["edge_id"])
    nodes = _rows(evidence.db, nodes_table, run_id=run_id)
    target = next(node for node in nodes if node["node_id"] == selected_edge["to_node_id"])
    assert target["node_type"] == NodeType.SINK.value
    assert target["plugin_name"] == "json"


def _assert_keyword_filter_evidence(evidence: _RunEvidence) -> None:
    run_id = evidence.result.run_id
    nodes = _rows(evidence.db, nodes_table, run_id=run_id)
    transform = next(node for node in nodes if node["node_type"] == NodeType.TRANSFORM.value)
    states = _rows(evidence.db, node_states_table, run_id=run_id)
    state = next(record for record in states if record["node_id"] == transform["node_id"])
    assert state["status"] == NodeStateStatus.FAILED.value

    expected_reason = {
        "reason": "blocked_content",
        "field": "notes",
        "matched_pattern": "<redacted-secret>",
        "match_position": 0,
        "match_length": len(_SECRET),
        "field_length": len(_SECRET),
    }
    assert json.loads(state["error_json"]) == expected_reason
    assert _SECRET not in state["error_json"]

    errors = _rows(evidence.db, transform_errors_table, run_id=run_id)
    assert len(errors) == 1
    assert errors[0]["transform_id"] == transform["node_id"]
    assert errors[0]["destination"] == "errors"
    assert json.loads(errors[0]["error_details_json"]) == expected_reason
    assert _SECRET not in errors[0]["error_details_json"]

    _assert_scheduler_sink_terminalization(
        evidence,
        processing_node=transform,
        expected_error_message=str(expected_reason),
    )
    _assert_divert_routing(evidence, state=state, expected_reason=expected_reason)


def _assert_declarative_gate_evidence(evidence: _RunEvidence) -> None:
    run_id = evidence.result.run_id
    nodes = _rows(evidence.db, nodes_table, run_id=run_id)
    gate = next(node for node in nodes if node["node_type"] == NodeType.GATE.value)
    states = _rows(evidence.db, node_states_table, run_id=run_id)
    state = next(record for record in states if record["node_id"] == gate["node_id"])
    assert state["status"] == NodeStateStatus.FAILED.value
    expected_state_error = {
        "exception": _BOUNDED_GATE_ERROR,
        "type": "ExpressionEvaluationError",
    }
    assert json.loads(state["error_json"]) == expected_state_error
    assert _SECRET not in state["error_json"]

    expected_reason = {
        "condition": "row['missing'] > 0",
        "error": _BOUNDED_GATE_ERROR,
        "error_type": "ExpressionEvaluationError",
    }
    _assert_scheduler_sink_terminalization(
        evidence,
        processing_node=gate,
        expected_error_message=_BOUNDED_GATE_ERROR,
    )
    _assert_divert_routing(evidence, state=state, expected_reason=expected_reason)
    assert _rows(evidence.db, transform_errors_table, run_id=run_id) == []


def test_real_keyword_filter_failure_has_one_composed_durable_disposition(tmp_path: Path) -> None:
    evidence = _run_pipeline(
        tmp_path,
        processing_yaml=f"""
transforms:
  - name: policy_filter
    plugin: keyword_filter
    input: processing
    on_success: output
    on_error: errors
    options:
      fields: [notes]
      blocked_patterns: [{json.dumps(_SECRET)}]
      schema:
        mode: observed
""",
    )
    try:
        _assert_keyword_filter_evidence(evidence)
    finally:
        evidence.db.close()


def test_real_declarative_gate_error_has_one_composed_durable_disposition(tmp_path: Path) -> None:
    evidence = _run_pipeline(
        tmp_path,
        processing_yaml="""
gates:
  - name: required_field_gate
    input: processing
    condition: "row['missing'] > 0"
    routes:
      'true': output
      'false': output
    on_error: errors
""",
    )
    try:
        _assert_declarative_gate_evidence(evidence)
    finally:
        evidence.db.close()
