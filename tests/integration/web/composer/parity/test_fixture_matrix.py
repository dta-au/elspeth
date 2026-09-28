"""Ten real-path freeform Composer capability fixtures.

Each case drives the production planner, proposal and accept lifecycle, then
compares its committed graph, public YAML and declared capability shape with a
reference graph. The provider response is scripted; validation and commit are real.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers.composer_graphs import assert_isomorphic, public_pipeline_semantics

from .conftest import PARITY_FIXTURES, ParityEnv


def _committed_nodes(state: Any) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in state.to_dict()["nodes"]}


def _assert_semantic_expectations(state: Any, fixture: dict[str, Any]) -> None:
    """Verify the committed graph matches the fixture's declared capability shape.

    This is the "equivalent validation / runtime graph" leg: beyond isomorphism
    to the reference, the committed graph must expose the exact declared
    node/plugin kinds, wiring connections, routes, policies, and failure paths
    the fixture claims — proving the real path derived the intended capability,
    not merely a self-consistent graph.
    """
    expectations = fixture["semantic_expectations"]
    committed = state.to_dict()

    if "source" in expectations:
        sources = committed["sources"]
        assert len(sources) == 1, f"{fixture['class']}: expected a single source, got {list(sources)}"
        source = next(iter(sources.values()))
        for key, value in expectations["source"].items():
            assert source.get(key) == value, f"{fixture['class']}: source.{key} = {source.get(key)!r} != {value!r}"

    if "sources" in expectations:
        by_name = committed["sources"]
        for expected in expectations["sources"]:
            name = expected["name"]
            assert name in by_name, f"{fixture['class']}: missing source {name!r}"
            actual = by_name[name]
            for key in ("plugin", "on_success", "on_validation_failure"):
                if key in expected:
                    assert actual.get(key) == expected[key], f"{fixture['class']}: source[{name}].{key}"

    nodes = _committed_nodes(state)
    for expected in expectations["nodes"]:
        node_id = expected["id"]
        assert node_id in nodes, f"{fixture['class']}: missing node {node_id!r}"
        actual = nodes[node_id]
        for key in (
            "node_type",
            "plugin",
            "input",
            "on_success",
            "condition",
            "policy",
            "merge",
            "output_mode",
            "timeout_seconds",
        ):
            if key in expected:
                assert actual.get(key) == expected[key], (
                    f"{fixture['class']}: node[{node_id}].{key} = {actual.get(key)!r} != {expected[key]!r}"
                )
        if "routes" in expected:
            assert actual.get("routes") == expected["routes"], f"{fixture['class']}: node[{node_id}].routes"
        if "fork_to" in expected:
            assert actual.get("fork_to") == expected["fork_to"], f"{fixture['class']}: node[{node_id}].fork_to"
        if "branches" in expected:
            assert actual.get("branches") == expected["branches"], f"{fixture['class']}: node[{node_id}].branches"
        if "trigger" in expected:
            # The committed trigger carries defaulted keys (condition,
            # timeout_seconds); assert the declared trigger keys are a subset.
            actual_trigger = actual.get("trigger") or {}
            for trigger_key, trigger_value in expected["trigger"].items():
                assert actual_trigger.get(trigger_key) == trigger_value, f"{fixture['class']}: node[{node_id}].trigger.{trigger_key}"

    outputs = {output["name"]: output for output in committed["outputs"]}
    for expected in expectations["outputs"]:
        name = expected["sink_name"]
        assert name in outputs, f"{fixture['class']}: missing output {name!r}"
        actual = outputs[name]
        assert actual["plugin"] == expected["plugin"], f"{fixture['class']}: output[{name}].plugin"
        if "on_write_failure" in expected:
            assert actual["on_write_failure"] == expected["on_write_failure"], f"{fixture['class']}: output[{name}].on_write_failure"


def _assert_row_union_semantics(state: Any) -> None:
    """Keep the correlated N-to-N graph contract exact."""
    committed = state.to_dict()
    row_union = next(node for node in committed["nodes"] if node["node_type"] == "row_union")
    assert row_union["plugin"] is None
    assert row_union["timeout_seconds"] == 12.5
    assert row_union.get("policy") is None
    assert row_union.get("merge") is None
    assert list(row_union["branches"]) == ["control_branch", "treatment_branch"]
    assert list(row_union["branches"].values()) == ["control_scored", "treatment_scored"]
    assert row_union["input"] == "control_scored"
    downstream = next(node for node in committed["nodes"] if node["node_type"] == "aggregation")
    assert downstream["input"] == row_union["on_success"]
    gate = next(node for node in committed["nodes"] if node["node_type"] == "gate")
    assert list(gate["fork_to"]) == list(row_union["branches"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", PARITY_FIXTURES, ids=lambda fixture: fixture["class"])
async def test_freeform_derives_isomorphic_committed_graph(parity_env: ParityEnv, fixture: dict[str, Any]) -> None:
    reference = parity_env.reference_state(fixture)
    committed = await parity_env.drive_freeform(fixture)

    assert_isomorphic(committed, reference, left=f"freeform:{fixture['class']}", right="reference")
    if fixture["class"] == "row_union":
        _assert_row_union_semantics(committed)

    assert public_pipeline_semantics(committed) == public_pipeline_semantics(reference), (
        f"freeform:{fixture['class']}: public pipeline semantics diverged from reference"
    )
    _assert_semantic_expectations(committed, fixture)
