"""The duplicate-consumer repair suggestion states facts only, and the error names every consumer.

When two or more steps read one connection, validation reports
``duplicate_connection_consumer`` and every tool result carries
``validation.graph_repair_suggestions``. That entry used to hand the model a
server-built fork gate and one whole-node ``upsert_node`` call per consumer:
36 KB at two LLM consumers, 218 KB at 25, on every result while the error
stood. By the maintainer's ruling (form B) the entry now states facts only
(``code``, ``connection``, ``reason``, ``affected_consumers[].id``): no
server-proposed names, no gate, no ready calls, no whole nodes. The plain
error names every consumer, and the model builds the fork itself from the
skill.

The graph is synthetic (``_fork_facts_graph``): LLM steps with long prompts
and small mappers. The dispatcher (``execute_tool``) and the two serializers
the tool batch chooses between are real; the catalog is the unit suite's mock
catalog. The compose-loop request size and the explicit-approval cost live in
``test_fork_repair_facts_compose.py``.

Two earlier tests are deliberately absent: "only the introducing result
carries it" (emit-once) and "a refused narrow mutation carries none" (the
refusal design). Neither was ruled; both stay outside this change.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import ToolResult, _serialize_set_pipeline_node
from elspeth.web.composer.tools._registry import response_contract_for
from tests.unit.web.composer._fork_facts_graph import SHARED, chain, step_id
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

_STEPS = 25
_LLM_COUNT = 11

_REASON = "One connection can feed one processing node; every node listed reads this one. Change only the input bindings; preserve existing node options."
_ENTRY_KEYS = {"code", "connection", "reason", "affected_consumers"}

# Concrete bounds for the facts (entry + error entry, json.dumps default
# separators). The fixed part covers code, connection, the reason, the error's
# prefix and closing sentence; each consumer adds its id three times (twice in
# the message, once in the entry) plus framing. Measured at 6-character ids:
# see the handoff for the exact bytes.
_FACTS_FIXED_BOUND_BYTES = 600
_FACTS_PER_CONSUMER_BOUND_BYTES = 45


def _id(index: int) -> str:
    return step_id(index, llm_count=_LLM_COUNT)


def _graph(shared_consumers: int, *, steps: int = _STEPS) -> CompositionState:
    return chain(steps=steps, shared=shared_consumers, llm_count=_LLM_COUNT)


def _wire(tool_name: str, result: ToolResult) -> dict[str, Any]:
    """The JSON the model is sent for this result: the same branch as the tool batch's serializer choice."""
    if response_contract_for(tool_name) is not None:
        return json.loads(serialize_admitted_discovery_result(admit_discovery_result(tool_name, result)))
    return json.loads(serialize_tool_result(result))


def _bytes(value: object) -> int:
    return len(json.dumps(value).encode("utf-8"))


def _preview(state: CompositionState) -> dict[str, Any]:
    return _wire("preview_pipeline", execute_tool("preview_pipeline", {}, state, _mock_catalog()))


def _duplicate_errors(wire: dict[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in wire["validation"]["errors"] if entry.get("error_code") == "duplicate_connection_consumer"]


def _expected_message(connection: str, ids: list[str]) -> str:
    named = [f"node '{node_id}' ({node_id})" for node_id in ids]
    listed = named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]
    return f"Duplicate consumer for connection '{connection}': {listed}. Use a gate for fan-out."


def test_fixture_has_enclave_shape() -> None:
    state = _graph(1)
    payload_bytes = sum(_bytes(dict(_serialize_set_pipeline_node(node))) for node in state.nodes)
    assert len(state.nodes) == _STEPS
    assert 180_000 <= payload_bytes <= 220_000, payload_bytes


@pytest.mark.parametrize("consumers", [2, 5, 11, 25])
def test_one_result_carries_only_the_facts(consumers: int) -> None:
    wire = _preview(_graph(consumers))
    [error] = _duplicate_errors(wire)
    [entry] = wire["validation"]["graph_repair_suggestions"]

    assert set(entry) == _ENTRY_KEYS
    assert entry == {
        "code": "duplicate_connection_consumer",
        "connection": SHARED,
        "reason": _REASON,
        "affected_consumers": [{"id": _id(index)} for index in range(consumers)],
    }

    facts_bytes = _bytes(entry) + _bytes(error)
    bound = _FACTS_FIXED_BOUND_BYTES + _FACTS_PER_CONSUMER_BOUND_BYTES * consumers
    assert facts_bytes <= bound, f"{consumers} consumers: entry + error = {facts_bytes} bytes, bound {bound}"
    assert facts_bytes < 3_000


def test_the_facts_grow_only_with_the_consumer_ids() -> None:
    """Between 5 and 25 consumers the facts grow by the ids and their framing, nothing else."""
    sizes = {}
    for consumers in (5, 25):
        wire = _preview(_graph(consumers))
        [error] = _duplicate_errors(wire)
        [entry] = wire["validation"]["graph_repair_suggestions"]
        sizes[consumers] = _bytes(entry) + _bytes(error)
    added_ids = [_id(index) for index in range(5, 25)]
    # Per consumer: "node '<id>' (<id>), " in the message and {"id": "<id>"}, in the entry.
    per_consumer_exact = [len(f"node '{node_id}' ({node_id}), ") + len(f'{{"id": "{node_id}"}}, ') for node_id in added_ids]
    assert sizes[25] - sizes[5] == sum(per_consumer_exact)


def test_the_server_names_no_node_connection_or_call() -> None:
    """Composer invariant 1: the server states facts about the saved graph and authors no structure."""
    wire = _preview(_graph(5))
    serialized = json.dumps(wire["validation"])

    # Negative control: the same search finds what the facts must carry.
    for index in range(5):
        assert f'"{_id(index)}"' in serialized
    assert '"shared"' in serialized

    for forbidden in (
        "tool_sequence",
        "new_input",
        "current_input",
        "strategy",
        "insert_fork_gate",
        "upsert_node",
        "preview_pipeline",
        "fork_",
        "_to_",
        "arguments",
    ):
        assert forbidden not in serialized, forbidden


def test_repair_suggestion_restates_no_consumer_options() -> None:
    serialized = json.dumps(_preview(_graph(2))["validation"]["graph_repair_suggestions"])

    assert "prompt_template" not in serialized
    assert "[149] Criterion 0:" not in serialized


def test_no_shared_connection_gives_no_repair_suggestion() -> None:
    wire = _preview(_graph(1))

    assert _duplicate_errors(wire) == []
    assert wire["validation"]["graph_repair_suggestions"] == []


def test_two_consumer_error_is_byte_identical_to_the_previous_message() -> None:
    """At exactly two consumers the message is the one shipped before this change, byte for byte."""
    [error] = _duplicate_errors(_preview(_graph(2)))

    assert error == {
        "component": f"connection:{SHARED}",
        "message": "Duplicate consumer for connection 'shared': node 'llm_00' (llm_00) and node 'llm_01' (llm_01). Use a gate for fan-out.",
        "severity": "high",
        "error_code": "duplicate_connection_consumer",
    }


@pytest.mark.parametrize("consumers", [3, 5, 25])
def test_the_error_names_every_consumer(consumers: int) -> None:
    [error] = _duplicate_errors(_preview(_graph(consumers)))

    assert error["message"] == _expected_message(SHARED, [_id(index) for index in range(consumers)])
    assert error["message"].startswith("Duplicate consumer for connection 'shared': ")
    assert error["message"].endswith(". Use a gate for fan-out.")


def test_more_than_32_consumers_are_all_named_and_one_fork_gate_cannot_carry_them() -> None:
    """A gate takes at most 32 fork branches (``_RUNTIME_GATE_MAPPING_CAP``).

    The facts still name all 33 consumers; the skill's single-gate fork is
    refused by validation at that size. Pinned so the limit is a known
    property, not a surprise.
    """
    consumers = 33
    state = chain(steps=consumers, shared=consumers, llm_count=0)
    wire = _preview(state)
    [error] = _duplicate_errors(wire)
    [entry] = wire["validation"]["graph_repair_suggestions"]
    ids = [step_id(index, llm_count=0) for index in range(consumers)]

    assert error["message"] == _expected_message(SHARED, ids)
    assert [consumer["id"] for consumer in entry["affected_consumers"]] == ids

    gate_args = _taught_gate("fan_out", SHARED, [f"to_{node_id}" for node_id in ids])
    gated = execute_tool("upsert_node", gate_args, state, _mock_catalog())
    codes = {entry.error_code for entry in gated.validation.errors}
    assert "pipeline_collection_cap_exceeded" in codes, gated.to_dict()["validation"]


def test_validation_delta_reports_a_new_consumer_as_one_resolved_and_one_new_error() -> None:
    """The delta keys on (component, message). Naming every consumer changes the message when a third joins.

    Deliberate: adding a third consumer shows the two-consumer error as
    resolved and the three-consumer error as new.
    """
    state = chain(steps=4, shared=2, llm_count=0)
    third = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog()).to_dict()["data"]["nodes"][
        2
    ]
    third = {**third, "input": SHARED}

    result = execute_tool("upsert_node", third, state, _mock_catalog())
    delta = result.to_dict()["validation_delta"]
    ids = [step_id(index, llm_count=0) for index in range(3)]

    resolved = [entry for entry in delta["resolved_errors"] if entry["error_code"] == "duplicate_connection_consumer"]
    new = [entry for entry in delta["new_errors"] if entry["error_code"] == "duplicate_connection_consumer"]
    assert [entry["message"] for entry in resolved] == [_expected_message(SHARED, ids[:2])]
    assert [entry["message"] for entry in new] == [_expected_message(SHARED, ids)]


def _paged_authoring_document(state: CompositionState, catalog: Any) -> dict[str, Any]:
    """The whole authoring document of a graph too large for one read, joined from its hash-bound pages."""
    arguments: dict[str, Any] = {"component": "set_pipeline_arguments", "field_path": ""}
    content = ""
    while True:
        page = execute_tool("get_pipeline_state", arguments, state, catalog).to_dict()["data"]
        assert page["view"] == "field"
        content += page["content"]
        if page["next_offset_chars"] is None:
            break
        arguments = {**arguments, "offset_chars": page["next_offset_chars"], "expected_sha256": page["sha256"]}
    assert len(content) == page["total_chars"]
    document = json.loads(content)
    assert type(document) is dict
    return document


def test_refused_full_replacement_carries_no_saved_graph_repair_suggestion() -> None:
    """Control: a candidate refusal already describes only the candidate."""
    catalog = _mock_catalog()
    state = _graph(2)
    candidate = _paged_authoring_document(state, catalog)

    result = execute_tool("set_pipeline", candidate, state, catalog, validate_arguments=True)
    wire = _wire("set_pipeline", result)

    assert result.success is False
    assert wire["version"] == state.version
    assert _duplicate_errors(wire) == []
    assert wire["validation"]["graph_repair_suggestions"] == []


# --- The construction the skill teaches is true --------------------------------------------------------------


def _taught_gate(gate_id: str, connection: str, branches: list[str]) -> dict[str, Any]:
    """The gate exactly as the skill block describes it: node_type gate, no plugin, condition 'True', routes to fork."""
    return {
        "id": gate_id,
        "node_type": "gate",
        "input": connection,
        "condition": "True",
        "routes": {"true": "fork", "false": "fork"},
        "fork_to": branches,
    }


def _authoring_nodes(state: CompositionState) -> dict[str, dict[str, Any]]:
    """Each step as the model reads it before re-sending it whole (get_pipeline_state set_pipeline_arguments)."""
    data = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog()).to_dict()["data"]
    return {node["id"]: dict(node) for node in data["nodes"]}


def _apply_taught_fork(state: CompositionState, *, gate_id: str) -> CompositionState:
    """Read the facts, then build the fork the way the skill block teaches, with model-chosen names."""
    [entry] = _preview(state)["validation"]["graph_repair_suggestions"]
    connection = entry["connection"]
    consumer_ids = [consumer["id"] for consumer in entry["affected_consumers"]]
    branches = {node_id: f"{connection}_for_{node_id}" for node_id in consumer_ids}
    nodes = _authoring_nodes(state)

    calls: list[dict[str, Any]] = [_taught_gate(gate_id, connection, list(branches.values()))]
    calls.extend({**nodes[node_id], "input": branches[node_id]} for node_id in consumer_ids)
    for arguments in calls:
        result = execute_tool("upsert_node", arguments, state, _mock_catalog())
        assert result.success is True, result.to_dict()
        state = result.updated_state
    return state


def _fan_out_graph(consumers: list[NodeSpec]) -> CompositionState:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success=SHARED,
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
        )
    )
    for node in consumers:
        state = state.with_node(node)
    for output in sorted({node.on_success for node in consumers if node.on_success is not None} | {"main"}):
        state = state.with_output(
            OutputSpec(
                name=output,
                plugin="json",
                options={"path": f"outputs/{output}.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
    return state


def _passthrough(node_id: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input=SHARED,
        on_success=f"{node_id}_out",
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _aggregation(node_id: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="aggregation",
        plugin="batch_stats",
        input=SHARED,
        on_success=f"{node_id}_out",
        on_error="main",
        options={"schema": {"mode": "observed"}, "value_field": "score"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        trigger={"count": 2},
    )


@pytest.mark.parametrize("consumers", [2, 3, 5])
def test_the_taught_fork_repairs_transform_consumers(consumers: int) -> None:
    state = _fan_out_graph([_passthrough(f"step_{index}") for index in range(consumers)])
    before = state.validate()
    assert {entry.error_code for entry in before.errors} == {"duplicate_connection_consumer"}, before.errors

    repaired = _apply_taught_fork(state, gate_id="fan_out")

    after = repaired.validate()
    assert after.errors == (), after.errors
    assert _preview(repaired)["validation"]["graph_repair_suggestions"] == []


def _aggregation_alternative(name: str, nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls the model could make for one transform and one aggregation sharing ``SHARED`` (model-chosen names)."""
    step = nodes["step_0"]
    batch = nodes["batch_1"]
    branches = [f"{SHARED}_for_step_0", f"{SHARED}_for_batch_1"]
    fork = [_taught_gate("fan_out", SHARED, branches), {**step, "input": branches[0]}]
    relay_options = {
        "passthrough_relay": {"plugin": "passthrough", "options": {"schema": {"mode": "observed"}}},
        "field_mapper_relay": {"plugin": "field_mapper", "options": {"schema": {"mode": "observed"}, "mapping": {}}},
    }
    if name == "aggregation_reads_its_branch":
        return [*fork, {**batch, "input": branches[1]}]
    if name in relay_options:
        relay = {
            "id": "relay_batch_1",
            "node_type": "transform",
            "input": branches[1],
            "on_success": "batch_1_rows",
            "on_error": "discard",
            **relay_options[name],
        }
        return [*fork, relay, {**batch, "input": "batch_1_rows"}]
    if name == "gate_relay":
        relay = {
            "id": "relay_batch_1",
            "node_type": "gate",
            "input": branches[1],
            "condition": "True",
            "routes": {"true": "batch_1_rows", "false": "batch_1_rows"},
        }
        return [*fork, relay, {**batch, "input": "batch_1_rows"}]
    if name == "aggregation_stays_on_the_original_connection":
        return [_taught_gate("fan_out", SHARED, branches[:1]), {**step, "input": branches[0]}]
    if name == "aggregation_behind_the_transform":
        return [{**step, "on_success": "step_0_rows"}, {**batch, "input": "step_0_rows"}]
    raise AssertionError(name)


# Executed (aggregation-alternatives): validation error codes after each alternative, nothing changed in the validator.
# A fork branch has a destination only when a transform or gate reads it, a barrier names it, or it is a sink
# (state.py fork rule), so an aggregation reading its branch directly is refused: the limitation is pinned here.
# One relay node on the aggregation's branch (a passthrough or identity field_mapper transform, or a gate with
# both routes to one connection) gives the branch a reader, and the fork validates.
_AGGREGATION_ALTERNATIVES: dict[str, set[str]] = {
    "aggregation_reads_its_branch": {"fork_branch_no_destination"},
    "passthrough_relay": set(),
    "field_mapper_relay": set(),
    "gate_relay": set(),
    "aggregation_stays_on_the_original_connection": {"duplicate_connection_consumer"},
    # Validates, but step_0's rows no longer reach its own output: a different pipeline, not a repair.
    "aggregation_behind_the_transform": set(),
}


@pytest.mark.parametrize("alternative", sorted(_AGGREGATION_ALTERNATIVES))
def test_the_fork_with_an_aggregation_consumer(alternative: str) -> None:
    """One transform and one aggregation share a connection; what validation says after each construction."""
    state = _fan_out_graph([_passthrough("step_0"), _aggregation("batch_1")])
    before = state.validate()
    assert {entry.error_code for entry in before.errors} == {"duplicate_connection_consumer"}, before.errors

    refused = []
    for arguments in _aggregation_alternative(alternative, _authoring_nodes(state)):
        result = execute_tool("upsert_node", arguments, state, _mock_catalog())
        if result.success is not True:
            refused.append((arguments["id"], [entry.error_code for entry in result.validation.errors]))
            continue
        state = result.updated_state

    codes = {entry.error_code for entry in state.validate().errors}
    assert refused == []
    assert codes == _AGGREGATION_ALTERNATIVES.get(alternative), (alternative, codes)


# --- One authority: the entry and the error name the same consumers --------------------------------------------


def _barrier(node_id: str, node_type: str, branches: dict[str, str]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type=node_type,
        plugin=None,
        input=next(iter(branches.values())),
        on_success=f"{node_id}_out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=branches,
        policy="require_all" if node_type == "coalesce" else None,
        merge="union" if node_type == "coalesce" else None,
    )


@pytest.mark.parametrize("barrier", [None, "coalesce", "row_union"])
def test_the_entry_and_the_error_name_the_same_consumers(barrier: str | None) -> None:
    """Both come from one claim list, so a mapped barrier branch is named in both or in neither."""
    consumers = [_passthrough("step_0"), _passthrough("step_1")]
    if barrier is not None:
        consumers.append(_barrier("merge", barrier, {"left": SHARED, "right": "other_rows"}))
    wire = _preview(_fan_out_graph(consumers))

    [error] = _duplicate_errors(wire)
    [entry] = wire["validation"]["graph_repair_suggestions"]
    named_in_error = list(dict.fromkeys(re.findall(r"\(([^()]+)\)", error["message"].split(": ", 1)[1])))
    expected = ["step_0", "step_1"] if barrier is None else ["step_0", "step_1", "merge"]
    assert [consumer["id"] for consumer in entry["affected_consumers"]] == named_in_error == expected
    if barrier is not None:
        assert f"{barrier} 'merge' mapped branch (merge)" in error["message"]
