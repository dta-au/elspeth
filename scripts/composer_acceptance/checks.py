"""Check executed Composer workflows against independent fixture expectations.

The caller supplies captured API bodies and downloaded output bytes. No check
can mutate a session, resolve a review, or author a pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from elspeth.core.expression_parser import ExpressionParser
from elspeth.web.execution.schemas import RunAccounting


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return value


def _records(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return [_record(item, label) for item in value]


def _equal(expected: object, actual: object, tolerance: float) -> bool:
    if type(expected) is float:
        return (
            isinstance(actual, (float, int))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and math.isclose(expected, actual, rel_tol=0, abs_tol=tolerance)
        )
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict) and isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(_equal(value, actual[key], tolerance) for key, value in expected.items())
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(_equal(left, right, tolerance) for left, right in zip(expected, actual, strict=True))
    return expected == actual


def _check_output(expected: dict[str, Any], content: bytes) -> list[str]:
    filename = expected["filename"]
    text = content.decode("utf-8")
    if expected["format"] == "text":
        return [] if text == expected["text"] else [f"{filename}: text bytes differ"]
    if expected["format"] == "csv":
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if reader.fieldnames != expected["columns"]:
            return [f"{filename}: CSV columns or order differ"]
        actual_rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in actual_rows):
            return [f"{filename}: malformed CSV record width"]
    elif expected["format"] == "jsonl":
        actual_rows = [_record(json.loads(line), filename) for line in text.splitlines() if line.strip()]
        if any(set(row) != set(expected["columns"]) for row in actual_rows):
            return [f"{filename}: JSON fields differ"]
    else:
        raise ValueError(f"{filename}: unsupported fixture output format")
    expected_rows = _records(expected["rows"], "expected rows")
    if len(actual_rows) != len(expected_rows):
        return [f"{filename}: expected {len(expected_rows)} rows, received {len(actual_rows)}"]
    tolerance = expected.get("float_tolerance", 0.0)
    remaining = list(actual_rows)
    for row in expected_rows:
        match = next((index for index, actual in enumerate(remaining) if _equal(row, actual, tolerance)), None)
        if match is None:
            return [f"{filename}: row values, multiplicities or types differ"]
        remaining.pop(match)
    return []


def _branch_transforms(connection: str, nodes: list[dict[str, Any]], stop: set[str]) -> tuple[set[str], set[str]]:
    """Follow authored connections only until the join boundary."""
    pending = [connection]
    seen: set[str] = set()
    transforms: set[str] = set()
    reached: set[str] = set()
    while pending:
        current = pending.pop()
        if current in stop:
            reached.add(current)
            continue
        if current in seen:
            continue
        seen.add(current)
        for node in nodes:
            if node.get("input") != current:
                continue
            if node.get("plugin") == "value_transform":
                transforms.add(node["id"])
            next_connection = node.get("on_success")
            if isinstance(next_connection, str):
                pending.append(next_connection)
    return transforms, reached


def _check_fork(nodes: list[dict[str, Any]]) -> list[str]:
    joins = [node for node in nodes if node["node_type"] == "coalesce" and node.get("policy") == "require_all"]
    forks = [node for node in nodes if node["node_type"] == "gate" and isinstance(node.get("fork_to"), list)]
    for join in joins:
        branches = _record(join.get("branches"), "coalesce branches")
        if len(branches) != 2 or join.get("merge") != "union":
            continue
        stop = set(branches.values())
        for fork in forks:
            connections = fork["fork_to"]
            if len(connections) != 2 or any(not isinstance(value, str) for value in connections):
                continue
            (left, left_ends), (right, right_ends) = (_branch_transforms(value, nodes, stop) for value in connections)
            if left and right and left.isdisjoint(right) and left_ends and right_ends and left_ends.isdisjoint(right_ends):
                return []
    return ["fork_join: require two independent value_transform branches and a require-all union coalesce"]


def _check_state(scenario: dict[str, Any], state: dict[str, Any]) -> list[str]:
    failures = []
    if state.get("is_valid") is not True:
        failures.append("state: final state is not valid")
    nodes = _records(state["nodes"], "nodes")
    sources = _record(state["sources"], "sources")
    outputs = _records(state["outputs"], "outputs")
    actual = {
        "source": {_record(source, "source")["plugin"] for source in sources.values()},
        "transform": {node["plugin"] for node in nodes if node.get("plugin") is not None},
        "sink": {output["plugin"] for output in outputs},
    }
    for category, expected in scenario["expected_plugins"].items():
        missing = set(expected) - actual[category]
        if missing:
            failures.append(f"state: missing {category} plugins {sorted(missing)}")
    kinds = {node["node_type"] for node in nodes}
    if set(scenario["required_node_kinds"]) - kinds:
        failures.append("state: required node kinds missing")
    if scenario["audit"]["shape"] == "fork_join":
        failures.extend(_check_fork(nodes))
    if scenario["id"] == "06_structured_extract_route":
        structured = False
        for node in nodes:
            if node.get("plugin") != "llm":
                continue
            options = _record(node["options"], "LLM options")
            declarations = [options]
            if isinstance(options.get("queries"), dict):
                declarations.extend(_record(query, "query") for query in options["queries"].values())
            for declaration in declarations:
                if declaration.get("response_format") != "structured":
                    continue
                fields = _records(declaration.get("output_fields"), "structured fields")
                types = {field["type"] for field in fields}
                structured |= {"string", "integer", "boolean"} <= types
        if not structured:
            failures.append("state: structured LLM string/integer/boolean declaration missing")
        gate_reads = set().union(
            *(ExpressionParser(node["condition"]).static_field_reads().fields for node in nodes if node["node_type"] == "gate")
        )
        if not any(field == "urgent" or field.endswith("_urgent") for field in gate_reads):
            failures.append("state: gate does not consume the extracted urgent field")
    return failures


def _check_accounting(expected: dict[str, Any], run: dict[str, Any]) -> list[str]:
    accounting = RunAccounting.model_validate(run["accounting"], strict=True)
    failures = []
    status = "completed_with_failures" if expected["routed_error_rows"] else "completed"
    if run["status"] != status:
        failures.append(f"run: expected status {status}")
    checks = {
        "source rows read": (accounting.source.rows_read, expected["source_rows"]),
        "source rows processed": (accounting.source.rows_processed, expected["source_rows"]),
        "source rows rejected": (accounting.source.rows_rejected, 0),
        "succeeded tokens": (accounting.tokens.succeeded, expected["succeeded_tokens"]),
        "structural tokens": (accounting.tokens.structural, expected["structural_tokens"]),
        "failed tokens": (accounting.tokens.failed, expected["routed_error_rows"]),
        "routed failure tokens": (accounting.routing.routed_failure, expected["routed_error_rows"]),
        "discarded tokens": (accounting.routing.discarded, expected["discarded_rows"]),
        "source quarantines": (accounting.routing.quarantined, 0),
        "pending tokens": (accounting.tokens.pending, 0),
        "abandoned tokens": (accounting.tokens.abandoned, 0),
        "failed collector groups": (accounting.collector_groups_failed, 0),
        "missing outcomes": (accounting.integrity.missing_terminal_outcomes, 0),
        "duplicate outcomes": (accounting.integrity.duplicate_terminal_outcomes, 0),
        "closure": (accounting.integrity.closure, "closed"),
    }
    for label, (actual, wanted) in checks.items():
        if actual != wanted:
            failures.append(f"run: {label}: expected {wanted}, received {actual}")
    return failures


def _check_evidence(scenario: dict[str, Any], state: dict[str, Any], run: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for field, expected in (
        ("session_run_id", run["run_id"]),
        ("landscape_run_id", run["landscape_run_id"]),
        ("composition_state_id", state["id"]),
    ):
        if evidence[field] != expected:
            failures.append(f"audit: {field} does not bind the executed state/run")
    blobs = _records(evidence["input_blobs"], "input blobs")
    expected_inputs = {item["filename"]: item for item in scenario["inputs"]}
    if {blob["filename"] for blob in blobs} != set(expected_inputs) or len(blobs) != len(expected_inputs):
        failures.append("audit: input blob inventory differs")
    for blob in blobs:
        expected_hash = hashlib.sha256(expected_inputs[blob["filename"]]["content"].encode()).hexdigest()
        if any(blob[field] != expected_hash for field in ("before_hash", "after_hash", "physical_after_hash")):
            failures.append("audit: original input bytes changed")
        if blob["status"] != "ready" or blob["source_bound"] is not True:
            failures.append("audit: uploaded input is not ready and bound to the executed source")
    if len(scenario["turns"]) > 1:
        bindings = evidence["source_bindings_by_turn"]
        if not isinstance(bindings, list) or len(bindings) < len(scenario["turns"]):
            failures.append("audit: missing source bindings across planned edits")
        elif any(not {blob["id"] for blob in blobs} <= set(turn) for turn in bindings):
            failures.append("audit: source blob changed between turns")
    rows = _records(evidence["source_rows"], "source rows")
    row_ids = {row["row_id"] for row in rows}
    if len(rows) != scenario["audit"]["source_rows"] or len(row_ids) != len(rows):
        failures.append("audit: source row identity census differs")
    token_records = _records(evidence["tokens"], "tokens")
    tokens = {token["token_id"]: token for token in token_records}
    if len(tokens) != len(token_records) or len(tokens) != run["accounting"]["tokens"]["emitted"]:
        failures.append("audit: token identity census differs")
    if "llm" in scenario["expected_plugins"]["transform"]:
        if any(node["options"].get("profile") != "sonnet" for node in state["nodes"] if node.get("plugin") == "llm"):
            failures.append("audit: authored LLM does not retain the approved sonnet profile")
        expected_model = evidence["expected_runtime_model"]
        if not isinstance(expected_model, str) or not expected_model:
            failures.append("audit: configured runtime model identity missing")
        nodes = {node["node_id"]: node for node in _records(evidence["nodes"], "audit nodes")}
        calls = _records(evidence["runtime_calls"], "runtime calls")
        if len({call["call_id"] for call in calls}) != len(calls):
            failures.append("audit: duplicated runtime call identities")
        success_rows = set()
        for call in calls:
            token = tokens[call["token_id"]]
            node = nodes[call["node_id"]]
            if (
                call["call_type"] != "llm"
                or node["plugin_name"] != "llm"
                or call["plugin_name"] != "llm"
                or not call["state_id"]
                or call["row_id"] != token["row_id"]
                or call["row_id"] not in row_ids
                or call["model"] != expected_model
            ):
                failures.append("audit: call is not associated with an executed per-row LLM node state")
            elif call["status"] == "success":
                success_rows.add(call["row_id"])
        if success_rows != row_ids:
            failures.append("audit: successful runtime LLM calls do not cover every source row")
    if scenario["audit"]["shape"] == "fork_join":
        failures.extend(_check_fork_lineage(evidence, tokens, row_ids))
    if scenario["audit"]["shape"] == "expansion":
        failures.extend(_check_expansion_lineage(scenario, evidence, tokens, rows))
    return failures


def _check_expansion_lineage(
    scenario: dict[str, Any], evidence: dict[str, Any], tokens: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> list[str]:
    parents: dict[str, list[str]] = {}
    for link in _records(evidence["token_parents"], "token parents"):
        parents.setdefault(link["token_id"], []).append(link["parent_token_id"])
    frames: dict[str, list[dict[str, Any]]] = {}
    for frame in _records(evidence["lineage_frames"], "lineage frames"):
        frames.setdefault(frame["token_id"], []).append(frame)
    row_indexes = {row["row_id"]: row["row_index"] for row in rows}
    expected_counts = scenario["audit"]["children_per_source_row"]
    children: dict[str, list[str]] = {}
    for token_id, parent_ids in parents.items():
        if len(parent_ids) != 1 or parent_ids[0] in parents:
            return ["audit: expansion child does not have exactly one original parent"]
        parent = parent_ids[0]
        if tokens[token_id]["row_id"] != tokens[parent]["row_id"]:
            return ["audit: expanded child belongs to another source row"]
        children.setdefault(parent, []).append(token_id)
    if len(children) != len(rows):
        return ["audit: expansion parent inventory differs"]
    for parent, child_ids in children.items():
        row_index = row_indexes[tokens[parent]["row_id"]]
        if (
            not isinstance(row_index, int)
            or row_index < 0
            or row_index >= len(expected_counts)
            or len(child_ids) != expected_counts[row_index]
        ):
            return ["audit: per-source expansion child count differs"]
        expand_frames = [[frame for frame in frames.get(child, []) if frame["kind"] == "expand"] for child in child_ids]
        if any(len(items) != 1 for items in expand_frames):
            return ["audit: expansion children lack their expansion lineage"]
        if len({items[0]["group_id"] for items in expand_frames}) != 1 or len({items[0]["member_key"] for items in expand_frames}) != len(
            child_ids
        ):
            return ["audit: expansion children are not distinct members of one parent group"]
    return []


def _check_fork_lineage(evidence: dict[str, Any], tokens: dict[str, dict[str, Any]], row_ids: set[str]) -> list[str]:
    parents: dict[str, list[str]] = {}
    for link in _records(evidence["token_parents"], "token parents"):
        parents.setdefault(link["token_id"], []).append(link["parent_token_id"])
    frames: dict[str, list[dict[str, Any]]] = {}
    for frame in _records(evidence["lineage_frames"], "lineage frames"):
        frames.setdefault(frame["token_id"], []).append(frame)
    outcomes = _records(evidence["token_outcomes"], "token outcomes")
    merged = [outcome for outcome in outcomes if outcome["path"] == "coalesced" and outcome["sink_name"] is not None]
    if len(merged) != len(row_ids) or {tokens[outcome["token_id"]]["row_id"] for outcome in merged} != row_ids:
        return ["audit: joined outputs do not cover each original row exactly once"]
    for outcome in merged:
        merged_id = outcome["token_id"]
        row_id = tokens[merged_id]["row_id"]
        parent_ids = parents.get(merged_id, [])
        if len(parent_ids) != 2 or len(set(parent_ids)) != 2 or any(tokens[parent]["row_id"] != row_id for parent in parent_ids):
            return ["audit: joined row does not pair two branches of the same original row"]
        branch_frames = [[frame for frame in frames.get(parent, []) if frame["kind"] == "fork"] for parent in parent_ids]
        if any(len(items) != 1 for items in branch_frames):
            return ["audit: joined parents lack their fork lineage"]
        left, right = (items[0] for items in branch_frames)
        if left["group_id"] != right["group_id"] or left["member_key"] == right["member_key"]:
            return ["audit: joined parents are not distinct siblings from the same fork"]
        grandparents = [parents.get(parent, []) for parent in parent_ids]
        if any(len(items) != 1 for items in grandparents) or grandparents[0] != grandparents[1]:
            return ["audit: joined branches do not share one original parent token"]
    return []


def check_case(
    scenario: dict[str, Any],
    *,
    state: dict[str, Any],
    run: dict[str, Any],
    outputs: Mapping[str, bytes],
    evidence: dict[str, Any] | None = None,
) -> list[str]:
    """Return failures; empty output means every captured result met the oracle.

    Missing or malformed captures always fail. This is an artifact checker,
    not proof that a live model has run; the runner owns capture provenance.
    """
    failures: list[str] = []
    try:
        expected_outputs = _records(scenario["outputs"], "scenario outputs")
        if not expected_outputs:
            raise ValueError("scenario has no expected outputs")
        expected_names = {output["filename"] for output in expected_outputs}
        if set(outputs) != expected_names:
            failures.append("outputs: missing or unexpected artifact filenames")
        for expected in expected_outputs:
            filename = expected["filename"]
            if filename in outputs:
                failures.extend(_check_output(expected, outputs[filename]))
        failures.extend(_check_state(scenario, state))
        failures.extend(_check_accounting(scenario["audit"], run))
        if evidence is None:
            failures.append("audit: run-bound execution and input evidence missing")
        else:
            failures.extend(_check_evidence(scenario, state, run, evidence))
    except (KeyError, TypeError, ValueError, UnicodeError, csv.Error, ValidationError) as error:
        failures.append(f"capture: malformed or incomplete evidence ({type(error).__name__})")
    return failures
