"""Positive controls and deliberate corruptions for the live battery oracle."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from scripts.composer_acceptance.checks import check_case

from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.manager import PluginNotFoundError, get_shared_plugin_manager
from tests.fixtures.base_classes import create_observed_contract

FIXTURES = Path(__file__).parents[2] / "fixtures" / "composer_convergence"
CASES = [json.loads(path.read_text()) for path in sorted(FIXTURES.glob("*.json"))]


def _encode(output: dict[str, Any]) -> bytes:
    if output["format"] == "text":
        return output["text"].encode()
    if output["format"] == "jsonl":
        return ("\n".join(json.dumps(row) for row in output["rows"]) + "\n").encode()
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=output["columns"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(output["rows"])
    return stream.getvalue().encode()


def _positive(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    nodes = [
        {"id": f"node_{index}", "node_type": "transform", "plugin": plugin, "options": {}}
        for index, plugin in enumerate(case["expected_plugins"]["transform"])
    ]
    nodes.extend({"id": f"kind_{kind}", "node_type": kind} for kind in case["required_node_kinds"])
    for node in nodes:
        if node.get("plugin") == "llm":
            node["options"]["profile"] = "sonnet"
    if case["id"] == "06_structured_extract_route":
        for node in nodes:
            if node["node_type"] == "gate":
                node["condition"] = "row['urgent'] == True"
            if node.get("plugin") == "llm":
                node["options"] = {
                    "profile": "sonnet",
                    "response_format": "structured",
                    "output_fields": [
                        {"suffix": "order_code", "type": "string"},
                        {"suffix": "units", "type": "integer"},
                        {"suffix": "urgent", "type": "boolean"},
                    ],
                }
    if case["audit"]["shape"] == "fork_join":
        nodes = [node for node in nodes if node["node_type"] == "transform"]
        nodes.extend(
            [
                {"id": "split", "node_type": "gate", "fork_to": ["left", "right"]},
                {"id": "left_calc", "node_type": "transform", "plugin": "value_transform", "input": "left", "on_success": "left_done"},
                {"id": "right_calc", "node_type": "transform", "plugin": "value_transform", "input": "right", "on_success": "right_done"},
                {
                    "id": "join",
                    "node_type": "coalesce",
                    "branches": {"left": "left_done", "right": "right_done"},
                    "policy": "require_all",
                    "merge": "union",
                },
            ]
        )
    state = {
        "id": "composition-final",
        "is_valid": True,
        "sources": {"primary": {"plugin": case["expected_plugins"]["source"][0]}},
        "nodes": nodes,
        "outputs": [{"plugin": plugin} for plugin in case["expected_plugins"]["sink"]],
    }
    audit = case["audit"]
    successes = audit["succeeded_tokens"]
    failed = audit["routed_error_rows"]
    structural = audit["structural_tokens"]
    total = successes + failed + structural
    run = {
        "run_id": "session-run",
        "landscape_run_id": "landscape-run",
        "status": "completed_with_failures" if failed else "completed",
        "accounting": {
            "source": {"rows_read": audit["source_rows"], "rows_processed": audit["source_rows"], "rows_rejected": 0},
            "tokens": {
                "emitted": total,
                "terminal": total,
                "succeeded": successes,
                "failed": failed,
                "structural": structural,
                "pending": 0,
                "abandoned": 0,
            },
            "routing": {"routed_success": 0, "routed_failure": failed, "quarantined": 0, "discarded": 0},
            "integrity": {"closure": "closed", "missing_terminal_outcomes": 0, "duplicate_terminal_outcomes": 0},
        },
    }
    return state, run, {output["filename"]: _encode(output) for output in case["outputs"]}


def _evidence(case: dict[str, Any], state: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    rows = [{"row_id": f"row-{index}", "row_index": index} for index in range(case["audit"]["source_rows"])]
    tokens = [{"token_id": f"token-{index}", "row_id": row["row_id"], "join_group_id": None} for index, row in enumerate(rows)]
    evidence: dict[str, Any] = {
        "session_run_id": run["run_id"],
        "landscape_run_id": run["landscape_run_id"],
        "composition_state_id": state["id"],
        "source_rows": rows,
        "tokens": tokens,
        "runtime_calls": [],
        "expected_runtime_model": "anthropic/claude-sonnet-4.6",
        "nodes": [{"node_id": "llm-runtime", "plugin_name": "llm", "node_type": "transform"}],
        "token_parents": [],
        "lineage_frames": [],
        "token_outcomes": [],
        "input_blobs": [],
    }
    for index, item in enumerate(case["inputs"]):
        digest = hashlib.sha256(item["content"].encode()).hexdigest()
        evidence["input_blobs"].append(
            {
                "id": f"blob-{index}",
                "filename": item["filename"],
                "before_hash": digest,
                "after_hash": digest,
                "physical_after_hash": digest,
                "status": "ready",
                "source_bound": True,
            }
        )
    evidence["source_bindings_by_turn"] = [[blob["id"] for blob in evidence["input_blobs"]] for _ in case["turns"]]
    if "llm" in case["expected_plugins"]["transform"]:
        evidence["runtime_calls"] = [
            {
                "call_id": f"call-{index}",
                "state_id": f"node-state-{index}",
                "token_id": token["token_id"],
                "row_id": token["row_id"],
                "node_id": "llm-runtime",
                "plugin_name": "llm",
                "call_type": "llm",
                "status": "success",
                "model": "anthropic/claude-sonnet-4.6",
            }
            for index, token in enumerate(tokens)
        ]
    if case["audit"]["shape"] == "fork_join":
        for index, row in enumerate(rows):
            base, left, right, merged = (f"{prefix}-{index}" for prefix in ("token", "left", "right", "merged"))
            for token_id in (left, right, merged):
                tokens.append(
                    {"token_id": token_id, "row_id": row["row_id"], "join_group_id": f"join-{index}" if token_id == merged else None}
                )
            for child, parent, ordinal in ((left, base, 0), (right, base, 1), (merged, left, 0), (merged, right, 1)):
                evidence["token_parents"].append({"token_id": child, "parent_token_id": parent, "ordinal": ordinal})
            for token_id, member in ((left, "left"), (right, "right")):
                evidence["lineage_frames"].append({"token_id": token_id, "kind": "fork", "group_id": f"fork-{index}", "member_key": member})
                evidence["token_outcomes"].append(
                    {"token_id": token_id, "outcome": "success", "path": "coalesced", "completed": 1, "sink_name": None}
                )
            evidence["token_outcomes"].append(
                {"token_id": base, "outcome": "transient", "path": "fork_parent", "completed": 1, "sink_name": None}
            )
            evidence["token_outcomes"].append(
                {"token_id": merged, "outcome": "success", "path": "default_flow", "completed": 1, "sink_name": "combined"}
            )
    elif case["audit"]["shape"] == "expansion":
        for index, count in enumerate(case["audit"]["children_per_source_row"]):
            for child_index in range(count):
                child = f"child-{index}-{child_index}"
                tokens.append({"token_id": child, "row_id": rows[index]["row_id"], "join_group_id": None})
                evidence["token_parents"].append({"token_id": child, "parent_token_id": f"token-{index}", "ordinal": 0})
                evidence["lineage_frames"].append({"token_id": child, "kind": "expand", "group_id": f"expand-{index}", "member_key": child})
    else:
        for index in range(len(tokens), run["accounting"]["tokens"]["emitted"]):
            tokens.append({"token_id": f"child-{index}", "row_id": rows[index % len(rows)]["row_id"], "join_group_id": None})
    return evidence


def _check(case: dict[str, Any], *, state: dict[str, Any], run: dict[str, Any], outputs: dict[str, bytes]) -> list[str]:
    return check_case(case, state=state, run=run, outputs=outputs, evidence=_evidence(case, state, run))


def test_fixture_inventory_and_registry_controls() -> None:
    assert len(CASES) == 10
    assert len({case["id"] for case in CASES}) == 10
    manager = get_shared_plugin_manager()
    assert manager.get_transform_by_name("field_mapper").name == "field_mapper"
    with pytest.raises(PluginNotFoundError):
        manager.get_transform_by_name("__battery_missing__")
    for case in CASES:
        for name in case["expected_plugins"]["source"]:
            assert manager.get_source_by_name(name).name == name
        for name in case["expected_plugins"]["transform"]:
            assert manager.get_transform_by_name(name).name == name
        for name in case["expected_plugins"]["sink"]:
            assert manager.get_sink_by_name(name).name == name
        assert 1 <= len(case["turns"]) < case["max_user_turns"]
        assert all("canonical_arguments" not in item for item in case["inputs"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_positive_capture(case: dict[str, Any]) -> None:
    state, run, outputs = _positive(case)
    assert _check(case, state=state, run=run, outputs=outputs) == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "value", "column"])
def test_output_corruption_fails(case: dict[str, Any], mutation: str) -> None:
    state, run, outputs = _positive(case)
    damaged = copy.deepcopy(case["outputs"][0])
    if damaged["format"] == "text":
        lines = damaged["text"].splitlines(keepends=True)
        if mutation == "missing":
            lines.pop()
        elif mutation == "duplicate":
            lines.append(lines[0])
        else:
            lines[0] = "corrupted\n"
        damaged["text"] = "".join(lines)
    elif mutation == "missing":
        damaged["rows"].pop()
    elif mutation == "duplicate":
        damaged["rows"].append(damaged["rows"][0])
    elif mutation == "value":
        damaged["rows"][0][damaged["columns"][0]] = "corrupted"
    else:
        removed = damaged["columns"].pop()
        for row in damaged["rows"]:
            row.pop(removed)
    outputs[damaged["filename"]] = _encode(damaged)
    assert _check(case, state=state, run=run, outputs=outputs)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_empty_capture_and_missing_plugin_fail(case: dict[str, Any]) -> None:
    assert check_case(case, state={}, run={}, outputs={})
    state, run, outputs = _positive(case)
    state["nodes"] = []
    assert _check(case, state=state, run=run, outputs=outputs)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_unresolved_token_fails_even_with_balanced_ledger(case: dict[str, Any]) -> None:
    state, run, outputs = _positive(case)
    accounting = run["accounting"]
    accounting["tokens"]["emitted"] += 1
    accounting["tokens"]["pending"] = 1
    accounting["integrity"]["closure"] = "open"
    accounting["integrity"]["missing_terminal_outcomes"] = 1
    assert _check(case, state=state, run=run, outputs=outputs)


def test_typed_integer_cannot_be_boolean() -> None:
    case = next(case for case in CASES if case["id"] == "06_structured_extract_route")
    state, run, outputs = _positive(case)
    damaged = copy.deepcopy(case["outputs"][1])
    assert damaged["rows"][0]["units"] == 1
    damaged["rows"][0]["units"] = True
    outputs[damaged["filename"]] = _encode(damaged)
    assert _check(case, state=state, run=run, outputs=outputs)


def test_wrong_sink_and_removed_structured_declaration_fail() -> None:
    case = next(case for case in CASES if case["id"] == "06_structured_extract_route")
    state, run, outputs = _positive(case)
    outputs["urgent.jsonl"], outputs["normal.jsonl"] = outputs["normal.jsonl"], outputs["urgent.jsonl"]
    assert _check(case, state=state, run=run, outputs=outputs)
    state, run, outputs = _positive(case)
    for node in state["nodes"]:
        if node.get("plugin") == "llm":
            node["options"] = {"profile": "sonnet"}
    assert _check(case, state=state, run=run, outputs=outputs)


def test_serialized_fork_and_weakened_join_fail() -> None:
    case = next(case for case in CASES if case["audit"]["shape"] == "fork_join")
    for mutation in ("serialize", "join", "disconnected"):
        state, run, outputs = _positive(case)
        for node in state["nodes"]:
            if mutation == "serialize" and node["id"] == "right_calc":
                node["input"] = "left_done"
            if mutation == "join" and node["id"] == "join":
                node["policy"] = "best_effort"
            if mutation == "disconnected" and node["id"] == "right_calc":
                node["on_success"] = "unrelated_output"
        assert _check(case, state=state, run=run, outputs=outputs)


def _row(data: dict[str, Any]) -> PipelineRow:
    return PipelineRow(data, create_observed_contract(data))


@pytest.mark.parametrize("mutation", ["missing_call", "preflight", "wrong_row", "wrong_run", "wrong_state", "wrong_plugin", "wrong_model"])
def test_runtime_call_evidence_cannot_be_replaced_by_preflight(mutation: str) -> None:
    case = next(case for case in CASES if case["id"] == "05_complaint_sla")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    if mutation == "missing_call":
        evidence["runtime_calls"].pop()
    elif mutation == "preflight":
        evidence["runtime_calls"][0]["state_id"] = None
    elif mutation == "wrong_row":
        evidence["runtime_calls"][0]["row_id"] = "foreign-row"
    elif mutation == "wrong_run":
        evidence["landscape_run_id"] = "foreign-run"
    elif mutation == "wrong_state":
        evidence["composition_state_id"] = "different-composition"
    elif mutation == "wrong_plugin":
        evidence["nodes"][0]["plugin_name"] = "passthrough"
    else:
        evidence["runtime_calls"][0]["model"] = "different-runtime-model"
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence)


def test_runtime_retry_is_retained_without_erasing_complete_row_coverage() -> None:
    case = next(case for case in CASES if case["id"] == "05_complaint_sla")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    retry = dict(evidence["runtime_calls"][0], call_id="failed-attempt", status="error")
    evidence["runtime_calls"].append(retry)
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence) == []
    assert len(evidence["runtime_calls"]) == 7


@pytest.mark.parametrize("mutation", ["hash", "physical", "binding", "between_turns"])
def test_source_data_immutability_controls(mutation: str) -> None:
    case = CASES[0]
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    if mutation == "hash":
        evidence["input_blobs"][0]["after_hash"] = "changed"
    elif mutation == "physical":
        evidence["input_blobs"][0]["physical_after_hash"] = "changed"
    elif mutation == "binding":
        evidence["input_blobs"][0]["source_bound"] = False
    else:
        evidence["source_bindings_by_turn"][0] = ["different-blob"]
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence)


@pytest.mark.parametrize("terminal_path", ["default_flow", "coalesced"])
def test_joined_output_identity_survives_downstream_processing(terminal_path: str) -> None:
    case = next(case for case in CASES if case["audit"]["shape"] == "fork_join")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    sink_outcomes = [outcome for outcome in evidence["token_outcomes"] if outcome["sink_name"] is not None]
    assert len(sink_outcomes) == 3
    for outcome in sink_outcomes:
        outcome["path"] = terminal_path
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence) == []


@pytest.mark.parametrize(
    "mutation", ["cross_row", "same_branch", "different_fork", "different_parent", "missing_parent", "duplicate_parent"]
)
def test_fork_parent_pairing_controls(mutation: str) -> None:
    case = next(case for case in CASES if case["audit"]["shape"] == "fork_join")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    if mutation == "cross_row":
        for token in evidence["tokens"]:
            if token["token_id"] == "right-0":
                token["row_id"] = "row-1"
    elif mutation == "same_branch":
        evidence["lineage_frames"][1]["member_key"] = "left"
    elif mutation == "different_fork":
        evidence["lineage_frames"][1]["group_id"] = "foreign-fork"
    elif mutation == "different_parent":
        evidence["token_parents"][1]["parent_token_id"] = "token-1"
    elif mutation == "missing_parent":
        evidence["token_parents"].pop(3)
    else:
        evidence["token_parents"][3]["parent_token_id"] = "left-0"
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence)


@pytest.mark.parametrize("mutation", ["join_identity", "failure", "incomplete", "no_sink", "missing", "duplicate", "wrong_row"])
def test_joined_output_identity_and_row_coverage_controls(mutation: str) -> None:
    case = next(case for case in CASES if case["audit"]["shape"] == "fork_join")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    merged_token = next(token for token in evidence["tokens"] if token["token_id"] == "merged-0")
    outcome = next(item for item in evidence["token_outcomes"] if item["token_id"] == "merged-0")
    if mutation == "join_identity":
        merged_token["join_group_id"] = None
    elif mutation == "failure":
        outcome["outcome"] = "failure"
    elif mutation == "incomplete":
        outcome["completed"] = 0
    elif mutation == "no_sink":
        outcome["sink_name"] = None
    elif mutation == "missing":
        evidence["token_outcomes"].remove(outcome)
    elif mutation == "duplicate":
        evidence["token_outcomes"].append(dict(outcome))
    else:
        merged_token["row_id"] = "row-1"
    expected = (
        "audit: fork outcome identities do not match emitted tokens exactly once"
        if mutation in {"missing", "duplicate"}
        else "audit: joined outputs do not cover each original row exactly once"
    )
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence) == [expected]


def test_missing_audit_evidence_is_not_a_passing_execution() -> None:
    case = CASES[0]
    state, run, outputs = _positive(case)
    assert check_case(case, state=state, run=run, outputs=outputs) == ["audit: run-bound execution and input evidence missing"]


@pytest.mark.parametrize("mutation", ["failed", "incomplete", "unsunk", "unknown", "missing_branch"])
def test_fork_outcome_identity_census_rejects_extra_or_missing_evidence(mutation: str) -> None:
    case = next(case for case in CASES if case["audit"]["shape"] == "fork_join")
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    extra = dict(next(item for item in evidence["token_outcomes"] if item["token_id"] == "merged-0"))
    if mutation == "missing_branch":
        evidence["token_outcomes"].pop(0)
    else:
        if mutation == "failed":
            extra["outcome"] = "failure"
        elif mutation == "incomplete":
            extra["completed"] = 0
        elif mutation == "unsunk":
            extra["sink_name"] = None
        else:
            extra["token_id"] = "unknown-token"
        evidence["token_outcomes"].append(extra)
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence) == [
        "audit: fork outcome identities do not match emitted tokens exactly once"
    ]


@pytest.mark.parametrize("case_id", ["08_json_expansion", "10_multiline_records"])
@pytest.mark.parametrize("mutation", ["foreign_parent", "missing_link", "duplicate_member", "foreign_group"])
def test_expansion_lineage_controls(case_id: str, mutation: str) -> None:
    case = next(case for case in CASES if case["id"] == case_id)
    state, run, outputs = _positive(case)
    evidence = _evidence(case, state, run)
    if mutation == "foreign_parent":
        evidence["token_parents"][0]["parent_token_id"] = "token-1"
    elif mutation == "missing_link":
        evidence["token_parents"].pop()
    elif mutation == "duplicate_member":
        evidence["lineage_frames"][1]["member_key"] = evidence["lineage_frames"][0]["member_key"]
    else:
        evidence["lineage_frames"][1]["group_id"] = "another-parent-group"
    assert check_case(case, state=state, run=run, outputs=outputs, evidence=evidence)


def test_real_plugins_support_requested_values_and_native_summary() -> None:
    manager = get_shared_plugin_manager()
    context = Mock(spec=TransformContext)
    schema = {"mode": "observed"}
    transform = manager.create_transform(
        "value_transform", {"schema": schema, "operations": [{"target": "total", "expression": "row['qty'] * row['price']"}]}
    )
    result = transform.process(_row({"qty": 3, "price": 4}), context)
    assert result.row is not None and result.row["total"] == 12
    truncate = manager.create_transform("truncate", {"schema": schema, "fields": {"note": 3}, "suffix": ""})
    result = truncate.process(_row({"note": "abcdef"}), context)
    assert result.row is not None and result.row["note"] == "abc"
    explode = manager.create_transform("json_explode", {"schema": schema, "array_field": "items", "include_index": True})
    result = explode.process(_row({"id": "j1", "items": ["red", "blue"]}), context)
    assert result.rows is not None
    assert [row.to_dict() for row in result.rows] == [
        {"id": "j1", "item": "red", "item_index": 0},
        {"id": "j1", "item": "blue", "item_index": 1},
    ]
    from elspeth.plugins.transforms.batch_top_k import BatchTopK

    summary = BatchTopK({"schema": schema, "field": "category", "group_by": "region", "k": 2})
    case = next(case for case in CASES if case["id"] == "07_grouped_frequency")
    input_rows = list(csv.DictReader(io.StringIO(case["inputs"][0]["content"])))
    result = summary.process([_row(row) for row in input_rows], context)
    assert result.rows is not None
    assert [row.to_dict() for row in result.rows] == case["outputs"][0]["rows"]
