"""The fork-gate repair skeleton must apply cleanly to a resolved LLM consumer (finding #6).

``_duplicate_consumer_repair_suggestions`` hands the planner a "copyable"
``tool_sequence``. An LLM consumer whose prompt review has been resolved carries
the runtime-owned node-level ``approved_prompt_artifact_hash``; ``upsert_node``
refuses that key. Built from the diagnostic ``_serialize_node`` the first call
failed, the later calls half-applied, and the graph ended worse than it started
(a new dangling fork branch on top of the duplicate consumer). The skeleton now
uses ``_serialize_set_pipeline_node`` — the authoring projection that
``get_pipeline_state(component='set_pipeline_arguments')`` already serves.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, approved_prompt_artifact_hash_from_options
from tests.unit.web.composer.test_tools import _empty_state, _llm_options_with_api_key, _mock_catalog, execute_tool


def _passthrough(node_id: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input="shared",
        on_success=f"out_{node_id}",
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state_with_resolved_llm_consumer() -> CompositionState:
    catalog = _mock_catalog()
    state = _empty_state().with_source(
        SourceSpec(
            plugin="csv",
            on_success="shared",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
            on_validation_failure="quarantine",
        )
    )
    created = execute_tool(
        "upsert_node",
        {
            "id": "a",
            "node_type": "transform",
            "plugin": "llm",
            "input": "shared",
            "on_success": "out_a",
            "on_error": "discard",
            "options": _llm_options_with_api_key({"secret_ref": "OPENROUTER_API_KEY"}),
        },
        state,
        catalog,
    )
    assert created.success, created.to_dict()
    state = created.updated_state
    [llm_node] = state.nodes
    options = deep_thaw(llm_node.options)
    prompt_rows = [row for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "llm_prompt_template"]
    assert prompt_rows, "fixture precondition: the llm node must stage an llm_prompt_template review"
    # The shape pending_interpretation writes when the user resolves the review:
    # the row is resolved AND the node carries the runtime-owned template pin.
    for row in prompt_rows:
        row.update(
            status="resolved",
            event_id="evt-1",
            accepted_value=row["draft"],
            accepted_artifact_hash=None,
            resolved_prompt_template_hash=stable_hash(row["draft"]),
        )
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    state = state.with_node(replace(llm_node, options=options)).with_node(_passthrough("b"))
    for name in ("out_a", "out_b"):
        state = state.with_output(OutputSpec(name=name, plugin="csv", options={"path": f"outputs/{name}.csv"}, on_write_failure="discard"))
    return state


def _node(state: CompositionState, node_id: str) -> NodeSpec:
    [node] = [node for node in state.nodes if node.id == node_id]
    return node


def _prompt_rows(node: NodeSpec) -> list[dict[str, Any]]:
    return [row for row in deep_thaw(node.options)[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "llm_prompt_template"]


def test_every_suggested_call_applies_for_a_consumer_with_a_resolved_prompt_review() -> None:
    catalog = _mock_catalog()
    state = _state_with_resolved_llm_consumer()
    assert "duplicate_connection_consumer" in {entry.error_code for entry in state.validate().errors}
    resolved_event_ids_before = [row["event_id"] for row in _prompt_rows(_node(state, "a"))]

    [repair] = execute_tool("preview_pipeline", {}, state, catalog).to_dict()["validation"]["graph_repair_suggestions"]
    sequence = repair["tool_sequence"]
    assert [call["tool"] for call in sequence] == ["upsert_node", "upsert_node", "upsert_node", "preview_pipeline"]

    for call in sequence:
        result = execute_tool(call["tool"], call["arguments"], state, catalog, validate_arguments=True)
        assert result.success is True, (call["tool"], call["arguments"].get("id"), result.to_dict())
        state = result.updated_state

    consumer = _node(state, "a")
    assert consumer.input == "shared_to_a"
    assert _node(state, "b").input == "shared_to_b"
    final_codes = {entry.error_code for entry in state.validate().errors}
    assert "duplicate_connection_consumer" not in final_codes
    assert "fork_branch_no_destination" not in final_codes
    # The approval must survive the repair: rewiring the input is not a prompt edit.
    rows = _prompt_rows(consumer)
    assert [row["status"] for row in rows] == ["resolved"] * len(rows)
    assert [row["event_id"] for row in rows] == resolved_event_ids_before
    # The skeleton omits the node-level template pin; upsert_node must keep the
    # stored one rather than drop it, or the approval would silently unpin.
    consumer_options = deep_thaw(consumer.options)
    assert consumer_options["approved_prompt_artifact_hash"] == approved_prompt_artifact_hash_from_options(consumer_options)


def test_suggested_consumer_arguments_omit_runtime_owned_llm_keys() -> None:
    state = _state_with_resolved_llm_consumer()

    [repair] = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]["graph_repair_suggestions"]
    [consumer_call] = [call for call in repair["tool_sequence"] if call["arguments"].get("id") == "a"]

    assert "approved_prompt_artifact_hash" not in consumer_call["arguments"]["options"]
