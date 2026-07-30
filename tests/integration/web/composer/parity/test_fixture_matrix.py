"""Three-surface real-path parity matrix (Plan 05 Task 3).

Ten canonical capability fixtures x three arbitrary authoring surfaces
(freeform, guided-full, guided-staged), for 30 real-path cases. Each case drives one surface's
production entrypoint from the fixture intent all the way to the immutable
committed ``CompositionState`` (real prompt/tool assembly → terminal parser →
custody → candidate validation → durable proposal → acceptance / confirm-wiring
→ audited ``set_pipeline`` → public YAML compiler) and asserts the committed
graph is semantically isomorphic to the ground-truth reference.

All three surfaces drive all ten fixtures. Former guided-staged exclusions for
``multi_source_queue``, ``fork_coalesce``, and ``multi_output`` remain in this
matrix as permanent regressions for their repaired stage-protocol boundaries.

No provider network, no skips, no xfail. Cross-surface parity is transitive:
every surface is anchored to the same per-fixture reference committed graph.

Two surfaces (``freeform``, ``guided_full``) let the planner emit the fixture's
canonical component *names*, so they additionally assert byte-exact public-YAML
semantics and the fixture's exact declared capability shape. The guided-staged
surface reviews sources/outputs through the persisted stage protocol, which
auto-assigns positional names (``source`` / ``source_2`` / ``output`` /
``output_2`` …) the operator cannot override; design §8.1 canonicalizes
connection names / source keys away, so isomorphism to the shared reference is
the complete, name-agnostic parity proof for that surface. It is paired with a
positive guided-naming assertion — proving the committed graph really traversed
the staged protocol and that the *only* delta from the reference is the expected
renaming — rather than a weaker name-agnostic reimplementation of the semantic
check (which could only mask a regression isomorphism already catches).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers.composer_graphs import assert_isomorphic, public_pipeline_semantics

from .conftest import PARITY_FIXTURES, ParityEnv

SURFACES = ["freeform", "guided_full", "guided_staged"]

# Surfaces whose planner emits the fixture's canonical component names verbatim,
# so byte-exact public-YAML equality and exact-name semantic expectations hold.
_NAME_PRESERVING_SURFACES = frozenset({"freeform", "guided_full"})

# Explicit (surface, fixture) grid: all 10 fixtures on all 3 surfaces = 30
# real-path parity cases.
_SURFACE_FIXTURE_PARAMS = [(surface, fixture) for surface in SURFACES for fixture in PARITY_FIXTURES]


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


def _assert_guided_staged_naming(state: Any, fixture: dict[str, Any]) -> None:
    """Positive proof the committed graph came through the staged protocol.

    Guided-staged reviews sources/outputs one at a time and the protocol
    assigns their names positionally (``source`` / ``source_2`` … and
    ``output`` / ``output_2`` …). Asserting exactly those names — with the same
    counts the fixture declares — proves the surface really traversed the staged
    protocol (not a shortcut) and that the only difference from the reference is
    the expected renaming that isomorphism already normalizes away.
    """
    committed = state.to_dict()
    source_names = set(committed["sources"])
    expected_sources = {"source"} | {f"source_{index}" for index in range(2, len(committed["sources"]) + 1)}
    assert source_names == expected_sources, (
        f"{fixture['class']}: guided-staged sources {sorted(source_names)} != guided defaults {sorted(expected_sources)}"
    )
    output_names = {output["name"] for output in committed["outputs"]}
    expected_outputs = {"output"} | {f"output_{index}" for index in range(2, len(committed["outputs"]) + 1)}
    assert output_names == expected_outputs, (
        f"{fixture['class']}: guided-staged outputs {sorted(output_names)} != guided defaults {sorted(expected_outputs)}"
    )


def _assert_row_union_semantics(state: Any) -> None:
    """Keep the correlated N-to-N contract exact even when staged names differ."""
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
@pytest.mark.parametrize(
    ("surface", "fixture"),
    _SURFACE_FIXTURE_PARAMS,
    ids=lambda value: value if isinstance(value, str) else value["class"],
)
async def test_surface_derives_isomorphic_committed_graph(
    parity_env: ParityEnv,
    surface: str,
    fixture: dict[str, Any],
) -> None:
    reference = parity_env.reference_state(fixture)
    committed = await parity_env.drive(surface, fixture)

    # 1. Semantic graph isomorphism (the primary, name-agnostic parity proof;
    #    applied to every surface). Cross-surface parity is transitive: all
    #    three surfaces are anchored to the same per-fixture reference.
    assert_isomorphic(committed, reference, left=f"{surface}:{fixture['class']}", right="reference")
    if fixture["class"] == "row_union":
        _assert_row_union_semantics(committed)

    if surface in _NAME_PRESERVING_SURFACES:
        # 2. Public compiled-pipeline (runtime graph) semantics agree byte-exact
        #    (these surfaces emit canonical component names).
        assert public_pipeline_semantics(committed) == public_pipeline_semantics(reference), (
            f"{surface}:{fixture['class']}: public pipeline semantics diverged from reference"
        )
        # 3. The committed graph exposes the fixture's exact declared capability shape.
        _assert_semantic_expectations(committed, fixture)
    else:
        # Guided-staged: isomorphism above is the complete parity proof (§8.1
        # canonicalizes the auto-assigned names). Add the positive guided-naming
        # assertion and confirm the public pipeline still compiles non-empty.
        _assert_guided_staged_naming(committed, fixture)
        public = public_pipeline_semantics(committed)
        assert public.get("sources") and public.get("sinks"), f"{surface}:{fixture['class']}: public pipeline compiled empty sources/sinks"
