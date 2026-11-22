"""The fork constructions the composer skill teaches, against the REAL plugin catalog.

The facts-only duplicate-consumer entry names the consumers and nothing else;
the skill teaches the model how to build the fork. These tests execute each
construction the skill text names, with the real ``field_mapper`` and
``batch_stats`` config models (``create_catalog_service()``, not the mock
catalog), and pin the exact error codes the model sees after every call:

* the relay options ``{"schema": {"mode": "observed"}, "mapping": {}}``;
* an aggregation reader and a coalesce reader, each behind one relay, in
  more than one call order, every in-between error cleared by the last call;
* a coalesce re-pointed with and without resetting its ``input``;
* a row_union reader of its own origin fork, with and without a relay.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.field_mapper import FieldMapperConfig
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from tests.unit.web.composer.test_tools import execute_tool

RELAY_OPTIONS: dict[str, Any] = {"schema": {"mode": "observed"}, "mapping": {}}

_CATALOG = create_catalog_service()


def _node(node_id: str, node_type: str, **fields: Any) -> NodeSpec:
    defaults: dict[str, Any] = {
        "plugin": None,
        "input": "",
        "on_success": None,
        "on_error": None,
        "options": {},
        "condition": None,
        "routes": None,
        "fork_to": None,
        "branches": None,
        "policy": None,
        "merge": None,
    }
    return NodeSpec(id=node_id, node_type=node_type, **{**defaults, **fields})


def _field_mapper(node_id: str, input_connection: str, on_success: str) -> NodeSpec:
    return _node(
        node_id,
        "transform",
        plugin="field_mapper",
        input=input_connection,
        on_success=on_success,
        on_error="discard",
        options=RELAY_OPTIONS,
    )


def _sink(name: str) -> OutputSpec:
    return OutputSpec(
        name=name,
        plugin="json",
        options={
            "path": f"outputs/{name}.jsonl",
            "schema": {"mode": "observed"},
            "format": "jsonl",
            "mode": "write",
            "collision_policy": "auto_increment",
        },
        on_write_failure="discard",
    )


def _state(source_connection: str, nodes: list[NodeSpec], sinks: list[str]) -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success=source_connection,
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "observed"}},
        )
    )
    for node in nodes:
        state = state.with_node(node)
    for sink in sinks:
        state = state.with_output(_sink(sink))
    return state


def _codes(result: Any) -> list[str]:
    return sorted({entry.error_code for entry in result.validation.errors})


def _model_view(state: CompositionState) -> dict[str, dict[str, Any]]:
    """Each node as the model reads it before re-sending it whole."""
    data = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _CATALOG).to_dict()["data"]
    return {node["id"]: dict(node) for node in data["nodes"]}


def _run(state: CompositionState, calls: list[dict[str, Any]]) -> tuple[CompositionState, list[tuple[str, bool, list[str]]]]:
    """Apply each upsert_node in order; record (id, success, codes the model sees) after every call."""
    seen = []
    for arguments in calls:
        result = execute_tool("upsert_node", arguments, state, _CATALOG)
        seen.append((arguments["id"], result.success, _codes(result)))
        if result.success:
            state = result.updated_state
    return state, seen


def _gate(gate_id: str, connection: str, branches: list[str]) -> dict[str, Any]:
    return {
        "id": gate_id,
        "node_type": "gate",
        "input": connection,
        "condition": "True",
        "routes": {"true": "fork", "false": "fork"},
        "fork_to": branches,
    }


def _relay_call(relay_id: str, branch: str, on_success: str) -> dict[str, Any]:
    return {
        "id": relay_id,
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": branch,
        "on_success": on_success,
        "on_error": "discard",
        "options": RELAY_OPTIONS,
    }


# --- 1. The relay's options against the real field_mapper config model ------------------------------------------


@pytest.mark.parametrize(
    ("options", "accepted"),
    [
        ({"schema": {"mode": "observed"}, "mapping": {}}, True),
        ({"schema": {"mode": "observed"}}, True),
        ({"mapping": {}}, False),
    ],
    ids=["schema-and-empty-mapping", "schema-only", "mapping-only"],
)
def test_the_relay_options_against_the_real_field_mapper_config(options: dict[str, Any], accepted: bool) -> None:
    """``mapping`` defaults to empty and an explicit ``{}`` is accepted; ``schema`` is required."""
    if accepted:
        config = FieldMapperConfig.from_dict(options, plugin_name="field_mapper")
        assert config.mapping == {}
        assert config.select_only is False
    else:
        with pytest.raises(PluginConfigError):
            FieldMapperConfig.from_dict(options, plugin_name="field_mapper")

    state = _state("rows", [], ["relayed"])
    result = execute_tool(
        "upsert_node",
        {
            "id": "relay",
            "node_type": "transform",
            "plugin": "field_mapper",
            "input": "rows",
            "on_success": "relayed",
            "on_error": "discard",
            "options": options,
        },
        state,
        _CATALOG,
    )
    assert result.success is accepted, result.to_dict()


# --- 2. Aggregation reader: relay chain in several orders --------------------------------------------------------


def _aggregation_graph() -> CompositionState:
    step = _field_mapper("step_0", "shared", "step_0_out")
    batch = _node(
        "batch_1",
        "aggregation",
        plugin="batch_stats",
        input="shared",
        on_success="batch_1_out",
        on_error="discard",
        options={"schema": {"mode": "observed"}, "value_field": "score"},
        trigger={"count": 2},
    )
    return _state("shared", [step, batch], ["step_0_out", "batch_1_out"])


def _aggregation_calls(state: CompositionState) -> dict[str, dict[str, Any]]:
    nodes = _model_view(state)
    return {
        "gate": _gate("fan_out", "shared", ["shared_for_step_0", "shared_for_batch_1"]),
        "step": {**nodes["step_0"], "input": "shared_for_step_0"},
        "relay": _relay_call("relay_batch_1", "shared_for_batch_1", "batch_1_rows"),
        "reader": {**nodes["batch_1"], "input": "batch_1_rows"},
    }


_AGGREGATION_ORDERS: dict[str, list[str]] = {
    "gate_step_relay_reader": ["gate", "step", "relay", "reader"],
    "relay_reader_step_gate": ["relay", "reader", "step", "gate"],
    "gate_relay_reader_step": ["gate", "relay", "reader", "step"],
}
# Executed: the codes the model sees after each call. Every call is accepted; the last call clears every error.
# In between: the duplicate stays until the last reader moves; a fork branch nothing reads yet gives
# fork_branch_no_destination; a relay whose output nothing reads yet gives transform_on_success_dangling; a node re-pointed at a branch
# the gate has not created yet gives node_input_not_reachable (and the source output, read by nobody, source_on_success_dangling).
_AGGREGATION_SEEN: dict[str, list[tuple[str, bool, list[str]]]] = {
    "gate_relay_reader_step": [
        ("fan_out", True, ["duplicate_connection_consumer", "fork_branch_no_destination"]),
        ("relay_batch_1", True, ["duplicate_connection_consumer", "fork_branch_no_destination", "transform_on_success_dangling"]),
        ("batch_1", True, ["duplicate_connection_consumer", "fork_branch_no_destination"]),
        ("step_0", True, []),
    ],
    "gate_step_relay_reader": [
        ("fan_out", True, ["duplicate_connection_consumer", "fork_branch_no_destination"]),
        ("step_0", True, ["duplicate_connection_consumer", "fork_branch_no_destination"]),
        ("relay_batch_1", True, ["duplicate_connection_consumer", "transform_on_success_dangling"]),
        ("batch_1", True, []),
    ],
    "relay_reader_step_gate": [
        ("relay_batch_1", True, ["duplicate_connection_consumer", "node_input_not_reachable", "transform_on_success_dangling"]),
        ("batch_1", True, ["node_input_not_reachable"]),
        ("step_0", True, ["node_input_not_reachable", "source_on_success_dangling"]),
        ("fan_out", True, []),
    ],
}


@pytest.mark.parametrize("order", sorted(_AGGREGATION_ORDERS))
def test_an_aggregation_reader_behind_one_relay_in_any_order(order: str) -> None:
    state = _aggregation_graph()
    assert _codes(execute_tool("preview_pipeline", {}, state, _CATALOG)) == ["duplicate_connection_consumer"]
    calls = _aggregation_calls(state)

    repaired, seen = _run(state, [calls[name] for name in _AGGREGATION_ORDERS[order]])

    assert seen == _AGGREGATION_SEEN[order], seen
    assert seen[-1] == (seen[-1][0], True, [])
    assert repaired.validate().errors == ()


# --- 3. Coalesce reader: relay chain, with and without resetting its input ----------------------------------------


def _barrier_graph(barrier: str) -> CompositionState:
    """A valid fork → two arms → barrier graph, plus one transform that also reads the control arm's output."""
    gate = _node(
        "fork_rows",
        "gate",
        input="rows",
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=("control_branch", "treatment_branch"),
    )
    arms = [
        _field_mapper("control", "control_branch", "control_done"),
        _field_mapper("treatment", "treatment_branch", "treatment_done"),
    ]
    branches = {"control_branch": "control_done", "treatment_branch": "treatment_done"}
    if barrier == "coalesce":
        merge = _node(
            "merge",
            "coalesce",
            input="control_done",
            on_success="merged",
            branches=branches,
            policy="require_all",
            merge="union",
        )
        nodes = [gate, *arms, merge]
        sinks = ["merged", "audit_out"]
    else:
        union = _node("merge", "row_union", input="control_done", on_success="union_out", branches=branches)
        nodes = [gate, *arms, union, _field_mapper("after_union", "union_out", "merged")]
        sinks = ["merged", "audit_out"]
    return _state("rows", nodes, sinks)


def _with_audit_reader(state: CompositionState) -> CompositionState:
    return state.with_node(_field_mapper("audit", "control_done", "audit_out"))


def _barrier_calls(state: CompositionState, *, relay: bool, reset_input: bool) -> dict[str, dict[str, Any]]:
    nodes = _model_view(state)
    slot_value = "done_for_merge_rows" if relay else "done_for_merge"
    merge = {**nodes["merge"], "branches": {**nodes["merge"]["branches"], "control_branch": slot_value}}
    if reset_input:
        merge["input"] = slot_value
    calls = {
        "gate": _gate("split_done", "control_done", ["done_for_audit", "done_for_merge"]),
        "audit": {**nodes["audit"], "input": "done_for_audit"},
        "reader": merge,
    }
    if relay:
        calls["relay"] = _relay_call("relay_merge", "done_for_merge", "done_for_merge_rows")
    return calls


def test_the_barrier_graphs_are_valid_before_the_duplicate_reader() -> None:
    for barrier in ("coalesce", "row_union"):
        assert _barrier_graph(barrier).validate().errors == (), barrier
        duplicated = execute_tool("preview_pipeline", {}, _with_audit_reader(_barrier_graph(barrier)), _CATALOG)
        # A second reader inside the fork region that ends at a sink is itself refused (bound_region_sink_inside):
        # that error belongs to the fixture's shape and stands through every repair below.
        assert _codes(duplicated) == ["bound_region_sink_inside", "duplicate_connection_consumer"], barrier
        [entry] = duplicated.to_dict()["validation"]["graph_repair_suggestions"]
        assert entry["affected_consumers"] == [{"id": "audit"}, {"id": "merge"}]


_COALESCE_ORDERS: dict[str, list[str]] = {
    "gate_audit_relay_reader": ["gate", "audit", "relay", "reader"],
    "relay_reader_audit_gate": ["relay", "reader", "audit", "gate"],
}
_GATE_FIRST = [
    ("split_done", True, ["bound_region_sink_inside", "duplicate_connection_consumer", "fork_branch_no_destination"]),
    ("audit", True, ["bound_region_sink_inside", "duplicate_connection_consumer", "fork_branch_no_destination"]),
    ("relay_merge", True, ["bound_region_sink_inside", "duplicate_connection_consumer", "transform_on_success_dangling"]),
    ("merge", True, ["bound_region_sink_inside"]),
]
_RELAY_FIRST = [
    (
        "relay_merge",
        True,
        ["bound_region_sink_inside", "duplicate_connection_consumer", "node_input_not_reachable", "transform_on_success_dangling"],
    ),
    ("merge", True, ["bound_region_sink_inside", "node_input_not_reachable"]),
    ("audit", True, ["node_input_not_reachable", "transform_on_success_dangling"]),
    ("split_done", True, ["bound_region_sink_inside"]),
]
# Executed: resetting the coalesce's ``input`` to the re-pointed slot changes nothing (same codes either way).
_COALESCE_SEEN: dict[tuple[str, bool], list[tuple[str, bool, list[str]]]] = {
    ("gate_audit_relay_reader", True): _GATE_FIRST,
    ("gate_audit_relay_reader", False): _GATE_FIRST,
    ("relay_reader_audit_gate", True): _RELAY_FIRST,
    ("relay_reader_audit_gate", False): _RELAY_FIRST,
}


@pytest.mark.parametrize("reset_input", [True, False], ids=["input-reset", "input-kept"])
@pytest.mark.parametrize("order", sorted(_COALESCE_ORDERS))
def test_a_coalesce_reader_behind_one_relay_in_any_order(order: str, reset_input: bool) -> None:
    state = _with_audit_reader(_barrier_graph("coalesce"))
    calls = _barrier_calls(state, relay=True, reset_input=reset_input)

    repaired, seen = _run(state, [calls[name] for name in _COALESCE_ORDERS[order]])

    assert seen == _COALESCE_SEEN[(order, reset_input)], seen
    assert [entry.error_code for entry in repaired.validate().errors] == ["bound_region_sink_inside"]
    merge = next(node for node in repaired.nodes if node.id == "merge")
    assert dict(merge.branches or {}) == {"control_branch": "done_for_merge_rows", "treatment_branch": "treatment_done"}


# --- 4. Row union reader of its own origin fork ------------------------------------------------------------------

# Executed: with or without a relay, a fork inside a row_union's branch is refused (row_union_nested_fork_invalid).
_ROW_UNION_AFTER: dict[bool, list[str]] = {
    False: ["bound_region_sink_inside", "fork_branch_no_destination", "row_union_nested_fork_invalid"],
    True: ["bound_region_sink_inside", "row_union_nested_fork_invalid"],
}


@pytest.mark.parametrize("relay", [False, True], ids=["direct", "relay"])
def test_a_row_union_reader_takes_no_fork_in_front_of_it(relay: bool) -> None:
    """The union's branches come from its origin fork; a second fork inside a branch is refused."""
    state = _with_audit_reader(_barrier_graph("row_union"))
    calls = _barrier_calls(state, relay=relay, reset_input=True)
    order = ["gate", "audit", *(["relay"] if relay else []), "reader"]

    repaired, seen = _run(state, [calls[name] for name in order])

    assert all(success for _id, success, _codes in seen), seen
    assert sorted({entry.error_code for entry in repaired.validate().errors}) == _ROW_UNION_AFTER[relay]
