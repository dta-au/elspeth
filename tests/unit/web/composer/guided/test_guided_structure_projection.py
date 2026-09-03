"""The ordinal-label structure binding a completed guided chat is admitted on.

``guided_structure_projection`` is the parity half of the post-commit chat
gate: a completed session's advisory answer describes the frozen CONFIRM_WIRING
record, so the pipeline that is actually in the head state has to still BE that
pipeline. These tests pin the property that makes the gate meaningful — the
projection is the same subset ``build_guided_proposal_projection`` publishes,
derived through the same topology and behavior helpers, not a parallel
reimplementation that could agree today and drift tomorrow.

The compared subset is everything the committed chat context publishes to the
model as SYSTEM authority and derivable from the head state: component
identity, connection endpoints, per-node ``behavior`` (minus the authored
settings a committed build withholds), per-connection ``flow``, a source's
``row_cardinality``, an llm node's ``structured_output_fields`` and an output's
``business_schema``. A TRANSFORM node's ``row_cardinality`` and a connection's
``schema_contract`` are published too but are frozen from the LOWERED
executable state and its validation summary, so they stay outside the
comparison and are time-qualified in the context instead (elspeth-986801d218).

``TestPublishedFactPartition`` at the end of this module is the durable guard
over all of that: it derives BOTH sets from the code and asserts every key the
committed system projection publishes is compared or qualified, never neither
and never both.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import count
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest

import elspeth.web.composer.guided.chat_solver as chat_solver
import elspeth.web.composer.guided.planning as guided_planning
from elspeth.contracts.freeze import deep_freeze
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.guided.chat_solver import _GUIDED_CONFIRMATION_TIME_SUFFIX as _CONFIRMATION_SUFFIX
from elspeth.web.composer.guided.emitters import _node_cardinality, build_step_4_wire_turn
from elspeth.web.composer.guided.planning import (
    GUIDED_COMMITTED_WITHHELD_LITERAL_KEYS,
    GUIDED_UNCOMPARED_BEHAVIOR_KEYS,
    GuidedStructureFacts,
    GuidedStructureProjection,
    GuidedStructureUnprojectable,
    _state_from_proposal,
    build_guided_proposal_projection,
    guided_private_reviewed_facts,
    guided_structure_compared_behavior,
    guided_structure_facts,
    guided_structure_projection,
)
from elspeth.web.composer.guided.protocol import GuidedStep, ProposePipelinePayload, TurnType
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_proposal import PipelineProposal, PlannerSurface, PresentBase
from elspeth.web.composer.state import COMPOSER_NODE_TYPES, CompositionState, NodeSpec, NodeType, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.sessions.protocol import guided_json_payload_id

# The gate's other half. It lives in the route module because that is where the
# frozen record is loaded, but it is the mirror of ``guided_structure_projection``
# and only a test that reads BOTH can prove the two agree.
from elspeth.web.sessions.routes.composer.guided_chat_atomic import _wire_payload_structure

_SOURCE_ID = "00000000-0000-4000-8000-000000000801"
_OUTPUT_ID = "00000000-0000-4000-8000-000000000802"
_PROPOSAL_ID = UUID("00000000-0000-4000-8000-000000000803")
_CHECKPOINT_ID = UUID("00000000-0000-4000-8000-000000000804")
_GATE_ID = "00000000-0000-4000-8000-000000000805"

_CATALOG_PLUGIN_IDS = {
    "source": frozenset({"csv"}),
    "transform": frozenset({"passthrough"}),
    "sink": frozenset({"json"}),
}


def _guided() -> GuidedSession:
    return replace(
        GuidedSession.initial(),
        reviewed_sources={
            _SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                observed_columns=("name", "amount"),
                sample_rows=({"name": "fixture", "amount": 42},),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            _OUTPUT_ID: SinkOutputResolved(
                name="cleaned",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(_SOURCE_ID,),
        output_order=(_OUTPUT_ID,),
        step=GuidedStep.STEP_3_TRANSFORMS,
    )


def _proposal(guided: GuidedSession) -> PipelineProposal:
    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "gate-input",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": [
                {
                    "id": _GATE_ID,
                    "node_type": "gate",
                    "plugin": None,
                    "input": "gate-input",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "row['amount'] > 500",
                    "routes": {"true": "accepted", "false": "cleaned"},
                    "fork_to": [],
                },
                {
                    "id": "copy",
                    "node_type": "transform",
                    "plugin": "passthrough",
                    "input": "accepted",
                    "on_success": "cleaned",
                    "on_error": "discard",
                    "options": {},
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": {"schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=_CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


def _published_projection() -> ProposePipelinePayload:
    guided = _guided()
    allocated = count(900)

    def fixed_uuid4() -> UUID:
        return UUID(f"00000000-0000-4000-8000-{next(allocated):012d}")

    with patch.object(guided_planning, "uuid4", fixed_uuid4):
        return build_guided_proposal_projection(
            proposal_id=_PROPOSAL_ID,
            proposal=_proposal(guided),
            guided=guided,
            catalog_plugin_ids=_CATALOG_PLUGIN_IDS,
        )


def _structure_subset_of(payload: ProposePipelinePayload) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Extract, by hand, the facts the PROPOSAL projection already advertises.

    A proposal payload carries component identity, ``behavior`` and ``flow``;
    the wire-only members the gate also compares (``row_cardinality``,
    ``structured_output_fields``, ``business_schema``) are minted later by
    ``emitters._build_wire_projection`` and are not here. Callers therefore
    assert containment rather than equality, which keeps this helper from
    having to restate which members are wire-only.
    """

    label_by_stable_id = {
        **{source["stable_id"]: source["label"] for source in payload["graph"]["sources"]},
        **{node["stable_id"]: node["label"] for node in payload["nodes"]},
        **{output["stable_id"]: output["label"] for output in payload["outputs"]},
    }
    components = (
        *(
            guided_structure_facts(
                {"kind": "source", "alias": source["label"], "plugin": source["plugin"]["id"]},
                label_by_component_id=label_by_stable_id,
            )
            for source in payload["graph"]["sources"]
        ),
        *(
            guided_structure_facts(
                {
                    "kind": "node",
                    "alias": node["label"],
                    "plugin": node["plugin"]["id"] if node["plugin"] is not None else None,
                    "node_type": node["node_type"],
                    "behavior": guided_structure_compared_behavior(node["behavior"]),
                },
                label_by_component_id=label_by_stable_id,
            )
            for node in payload["nodes"]
        ),
        *(
            guided_structure_facts(
                {"kind": "output", "alias": output["label"], "plugin": output["plugin"]["id"]},
                label_by_component_id=label_by_stable_id,
            )
            for output in payload["outputs"]
        ),
    )
    connections = tuple(
        guided_structure_facts(
            {
                "alias": f"connection-{index + 1}",
                "from_alias": label_by_stable_id[edge["from_endpoint"]["stable_id"]],
                "to_alias": (label_by_stable_id[edge["to_endpoint"]["stable_id"]] if "stable_id" in edge["to_endpoint"] else None),
                "flow": edge["flow"],
            },
            label_by_component_id=label_by_stable_id,
        )
        for index, edge in enumerate(payload["graph"]["edges"])
    )
    return components, connections


def _committed_state() -> CompositionState:
    """The candidate the published projection above was built from."""

    return _state_from_proposal(_proposal(_guided()))


def _endpoint_pairs(connections: tuple[Any, ...]) -> list[tuple[Any, Any]]:
    """The ``(from_alias, to_alias)`` half the gate compared before ST-RT-5."""

    return [(dict(connection)["from_alias"], dict(connection)["to_alias"]) for connection in connections]


def _flows(connections: tuple[Any, ...]) -> list[Any]:
    return [dict(connection)["flow"] for connection in connections]


def _behaviors(components: tuple[Any, ...]) -> list[Any]:
    """Each component's compared behavior facts; sources and outputs carry none."""

    return [dict(component).get("behavior") for component in components]


_IDENTITY_KEYS = ("kind", "alias", "plugin", "node_type")


def _component_identities(components: tuple[Any, ...]) -> list[dict[str, Any]]:
    """The ``(kind, alias, plugin, node_type)`` half the gate compared before ST-RT-5."""

    return [{key: value for key, value in dict(component).items() if key in _IDENTITY_KEYS} for component in components]


def test_projection_carries_every_fact_the_published_proposal_advertises() -> None:
    """The gate's two sides must be the same structure read two ways.

    Containment, not equality: the projection additionally compares the
    wire-only members (``row_cardinality``, ``structured_output_fields``,
    ``business_schema``) a proposal payload has not minted yet. Every fact the
    proposal DOES advertise must appear in the projection unchanged, which is
    what proves the two are one derivation rather than two that agree today.
    """

    published = _published_projection()
    state = _committed_state()

    components, connections = guided_structure_projection(state)
    expected_components, expected_connections = _structure_subset_of(published)

    for actual, expected in zip(components, expected_components, strict=True):
        assert dict(expected).items() <= dict(actual).items()
    for actual, expected in zip(connections, expected_connections, strict=True):
        assert dict(expected).items() <= dict(actual).items()
    # Guard against a vacuous pass: an empty-vs-empty comparison would hold
    # even if the derivation were entirely wrong.
    assert len(components) == 4
    assert len(connections) == len(published["graph"]["edges"]) >= 4
    assert all(dict(component) for component in expected_components)


def test_projection_uses_ordinal_labels_not_per_projection_stable_ids() -> None:
    """Node stable IDs are minted per projection; labels are what survive."""

    components, connections = guided_structure_projection(_committed_state())

    assert [dict(component)["alias"] for component in components] == ["source-1", "node-1", "node-2", "output-1"]
    endpoints = _endpoint_pairs(connections)
    assert ("source-1", "node-1") in endpoints
    assert ("node-2", None) in endpoints  # copy.on_error == "discard"


def test_projection_changes_when_a_node_is_added() -> None:
    state = _committed_state()
    added = replace(state, nodes=(*state.nodes, replace(state.nodes[1], id="extra")))

    assert guided_structure_projection(added) != guided_structure_projection(state)


def test_removing_a_wired_node_leaves_a_head_that_cannot_be_projected() -> None:
    """Removal is refused too — by the harder of the two failure modes.

    Deleting the transform orphans the gate's ``accepted`` route, so the head
    has no canonical consumer for it and no ordinal structure at all. Both
    outcomes reach the caller as the same refusal, which is the point: a
    removal never quietly compares equal to the confirmed wire record.
    """

    state = _committed_state()
    removed = replace(state, nodes=state.nodes[:1])

    with pytest.raises(GuidedStructureUnprojectable):
        guided_structure_projection(removed)


def test_projection_changes_when_the_graph_is_rewired() -> None:
    """Same components, same plugins, different routing."""

    state = _committed_state()
    gate = state.nodes[0]
    rewired = replace(
        state,
        nodes=(replace(gate, routes={"true": "cleaned", "false": "cleaned"}), *state.nodes[1:]),
    )

    before = guided_structure_projection(state)
    after = guided_structure_projection(rewired)
    assert after[0] == before[0], "component half is unchanged by a pure rewire"
    assert after[1] != before[1]


def test_projection_is_unchanged_by_a_prompt_template_option_patch() -> None:
    """An interpretation Accept patches node options, never structure."""

    state = _committed_state()
    patched = replace(
        state,
        nodes=(
            state.nodes[0],
            replace(state.nodes[1], options={"prompt_template": "Summarise {{ row.name }}"}),
        ),
    )

    assert guided_structure_projection(patched) == guided_structure_projection(state)


def test_projection_refuses_a_state_it_cannot_project() -> None:
    """An unprojectable state is not the state a wire review was built from.

    The typed refusal is what lets the chat route answer 409 (drift) instead of
    letting a derivation failure surface to the client as a server fault.
    """

    orphaned = CompositionState(
        sources={
            "primary": SourceSpec(
                plugin="csv",
                on_success="nothing-consumes-this",
                options={},
                on_validation_failure="discard",
            )
        },
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="cleaned",
                plugin="json",
                options={},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )

    with pytest.raises(GuidedStructureUnprojectable):
        guided_structure_projection(orphaned)


# --------------------------------------------------------------------------
# ST-RT-5: the published behavior and flow facts participate in the gate.
#
# Before this, the gate compared only ``(kind, label, plugin, node_type)`` and
# ``(from_label, to_label)``. The committed chat context publishes far more than
# that at SYSTEM authority and tells the model to use it exactly, so an
# options-only or literal-only freeform edit could leave the gate green while
# the model stated a frozen structural fact as current. Each test below moves
# one published fact and asserts the OLD compared halves are untouched, so a
# regression that narrowed the gate again could not pass it.
# --------------------------------------------------------------------------

_BARRIER_SOURCE_ID = "00000000-0000-4000-8000-000000000811"
_BARRIER_OUTPUT_ID = "00000000-0000-4000-8000-000000000812"
_BARRIER_PROPOSAL_ID = UUID("00000000-0000-4000-8000-000000000813")

_BARRIER_CATALOG_PLUGIN_IDS = {
    "source": frozenset({"csv"}),
    # ``llm`` is here for the structured-output round-trip fixture: an llm node
    # is the ONLY one whose ``structured_output_fields`` is non-empty, and that
    # published fact only exercises the gate when it carries real fields.
    "transform": frozenset({"passthrough", "json_explode", "batch_stats", "llm"}),
    "sink": frozenset({"json"}),
}

# The sink schema every structural fixture authors: an observed-mode block
# whose projected ``business_schema`` is four EMPTY lists. That emptiness is
# why a fixture authoring it cannot exercise the ``business_schema`` half of
# the gate at all (RT-4), so the round-trip corpus also carries the declared
# block below.
_OBSERVED_OUTPUT_OPTIONS: dict[str, Any] = {"schema": {"mode": "observed"}}
_DECLARED_OUTPUT_OPTIONS: dict[str, Any] = {
    "schema": {
        "mode": "declared",
        # BOTH authored field forms in one fixture. ``FieldDefinition.parse``
        # SKIPS a spec it cannot read (``emitters._wire_schema`` catches the
        # ValueError and continues), so a fixture authoring only a partial dict
        # would project ``fields: []`` and prove exactly what the empty
        # observed block already proves.
        "fields": [
            {"name": "name", "type": "str", "required": True, "nullable": False},
            "amount: int",
        ],
        "guaranteed_fields": ["name"],
        "required_fields": ["name"],
    }
}


def _barrier_guided() -> GuidedSession:
    """The reviewed authority the richer fixtures below are confirmed against."""

    return replace(
        GuidedSession.initial(),
        reviewed_sources={
            _BARRIER_SOURCE_ID: SourceResolved(
                name="primary",
                plugin="csv",
                options={"schema": {"mode": "observed"}},
                observed_columns=("name", "amount"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        reviewed_outputs={
            _BARRIER_OUTPUT_ID: SinkOutputResolved(
                name="cleaned",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                required_fields=("name",),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
        source_order=(_BARRIER_SOURCE_ID,),
        output_order=(_BARRIER_OUTPUT_ID,),
        step=GuidedStep.STEP_3_TRANSFORMS,
    )


def _pipeline(guided: GuidedSession, nodes: list[dict[str, Any]], *, output_options: dict[str, Any] | None = None) -> PipelineProposal:
    """One source into *nodes* into one sink, sharing the barrier authority.

    *output_options* authors the sink's options and defaults to the observed
    block every structural fixture uses. Only the sink PLUGIN and NAME are
    checked against the reviewed authority (``planning._build_projection``), so
    a fixture may author a richer schema without a second guided session.
    """

    return PipelineProposal.create(
        pipeline={
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "rows",
                    "options": {"schema": {"mode": "observed"}},
                    "on_validation_failure": "discard",
                }
            },
            "nodes": nodes,
            "edges": [],
            "outputs": [
                {
                    "name": "cleaned",
                    "plugin": "json",
                    "options": output_options if output_options is not None else _OBSERVED_OUTPUT_OPTIONS,
                    "on_write_failure": "discard",
                }
            ],
        },
        base=PresentBase(state_id=_CHECKPOINT_ID, composition_content_hash="a" * 64),
        reviewed_facts=guided_private_reviewed_facts(guided),
        surface=PlannerSurface.GUIDED_STAGED,
        repair_count=0,
        skill_hash=stable_hash("guided planner skill"),
        covered_deferred_intent_ids=(),
        supersedes_draft_hash=None,
    )


# Every NON-TRANSFORM partition fixture authors these options so they differ
# from ``_minimal_node_spec``'s ``options={}``. Without that difference the
# derivation guard below is blind to a ``_node_cardinality`` arm that starts
# reading ``node.options`` — and an options-only rewrite is exactly the
# post-commit write the design REQUIRES be admitted, so a cardinality that
# moved with it would be published stale behind a green gate (RT5-1).
_NON_DEFAULT_NODE_OPTIONS = {"schema": {"mode": "observed"}}


def _fork_coalesce_nodes(
    *,
    policy: str = "require_all",
    merge: str = "union",
    branches: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """A fork gate feeding two legs into one coalesce, then a sink transform."""

    return [
        {
            "id": "fork_gate",
            "node_type": "gate",
            "plugin": None,
            "input": "rows",
            "on_success": None,
            "on_error": None,
            "options": _NON_DEFAULT_NODE_OPTIONS,
            "condition": "row['amount'] > 0",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": ["a_rows", "b_rows"],
        },
        {
            "id": "leg_a",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "a_rows",
            "on_success": "a_out",
            "on_error": "discard",
            "options": {},
        },
        {
            "id": "leg_b",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "b_rows",
            "on_success": "b_out",
            "on_error": "discard",
            "options": {},
        },
        {
            "id": "reconcile",
            "node_type": "coalesce",
            "plugin": None,
            "input": "a_out",
            "on_success": None,
            "on_error": None,
            "options": _NON_DEFAULT_NODE_OPTIONS,
            "branches": branches if branches is not None else {"a_rows": "a_out", "b_rows": "b_out"},
            "policy": policy,
            "merge": merge,
            "timeout_seconds": None,
        },
        {
            "id": "finish",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "reconcile",
            "on_success": "cleaned",
            "on_error": "discard",
            "options": {},
        },
    ]


def _fork_row_union_nodes() -> list[dict[str, Any]]:
    """The same fork shape closed by a row_union rather than a coalesce.

    Worth its own round-trip case: a row_union is the ONE node kind whose
    incoming connection order is post-processed — ``_guided_projection_topology``
    re-sorts its incoming edge specs into the authored ``branches`` release
    order — and its behavior ``branch_aliases`` is then derived from that
    post-sort order. Two interacting ordering derivations feed both halves of
    the gate, so an asymmetry here would refuse every unchanged row_union
    pipeline.
    """

    return [
        *_fork_coalesce_nodes()[:3],
        {
            "id": "reconcile",
            "node_type": "row_union",
            "plugin": None,
            "input": "a_out",
            "on_success": "merged",
            "on_error": None,
            "options": _NON_DEFAULT_NODE_OPTIONS,
            "branches": {"a_rows": "a_out", "b_rows": "b_out"},
            "policy": None,
            "merge": None,
            "timeout_seconds": 12.5,
        },
        {
            "id": "finish",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "merged",
            "on_success": "cleaned",
            "on_error": "discard",
            "options": {},
        },
    ]


def _queue_nodes() -> list[dict[str, Any]]:
    """A queue fan-in, whose connection name is its own id.

    ``queue_node_contract_error`` enforces ``input == id``, and the queue
    republishes under that same name — so its one downstream consumer reads the
    queue's id, not a separate success connection.
    """

    return [
        {
            "id": "rows",
            "node_type": "queue",
            "plugin": None,
            "input": "rows",
            "on_success": None,
            "on_error": None,
            "options": _NON_DEFAULT_NODE_OPTIONS,
        },
        {
            "id": "finish",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "rows",
            "on_success": "cleaned",
            "on_error": "discard",
            "options": {},
        },
    ]


def _aggregation_nodes(**overrides: Any) -> list[dict[str, Any]]:
    node: dict[str, Any] = {
        "id": "rollup",
        "node_type": "aggregation",
        "plugin": "passthrough",
        "input": "rows",
        "on_success": "cleaned",
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
        "trigger": {},
        "output_mode": "transform",
    }
    node.update(overrides)
    return [node]


def _collector_nodes(*, scope_policy: str = "require_all") -> list[dict[str, Any]]:
    return [
        {
            "id": "explode",
            "node_type": "transform",
            "plugin": "json_explode",
            "input": "rows",
            "on_success": "pages",
            "on_error": "discard",
            "options": {},
        },
        {
            "id": "page_stitcher",
            "node_type": "collector",
            "plugin": "batch_stats",
            "input": "pages",
            "on_success": "cleaned",
            "on_error": "discard",
            "options": _NON_DEFAULT_NODE_OPTIONS,
            "scope_name": "document_pages",
            "scope_opener": "explode",
            "scope_policy": scope_policy,
        },
    ]


# The llm node options the structured-output cases author. Defined here rather
# than beside their RT-2 section below because the round-trip corpus — the
# first reader — is built at module import time, above that section.
_LLM_QUERIES_SCORE = {"queries": {"q1": {"output_fields": [{"suffix": "score", "type": "integer"}]}}}
_LLM_QUERIES_VERDICT = {"queries": {"q1": {"output_fields": [{"suffix": "verdict", "type": "string", "values": ["yes", "no"]}]}}}


def _llm_leg_nodes() -> list[dict[str, Any]]:
    """The fork fixture with its first leg plugged ``llm`` and really querying.

    ``structured_output_fields`` is published — and, since RT-2, compared — for
    an llm node only; every other fixture projects the empty list, which
    compares equal under any asymmetry. This is the fixture that carries a real
    field name, type and enum values through BOTH halves of the gate.
    """

    return [
        {**node, "plugin": "llm", "options": _LLM_QUERIES_VERDICT} if node["id"] == "leg_a" else node for node in _fork_coalesce_nodes()
    ]


def _barrier_state(nodes: list[dict[str, Any]], *, output_options: dict[str, Any] | None = None) -> CompositionState:
    return _state_from_proposal(_pipeline(_barrier_guided(), nodes, output_options=output_options))


def _wire_payload_for(nodes: list[dict[str, Any]], *, output_options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the real CONFIRM_WIRING record a state would have been confirmed from.

    Goes through ``build_step_4_wire_turn`` rather than a hand-written fixture:
    the round-trip property under test is that the gate's mirror half reads back
    exactly what the emitter froze, which a hand-built payload could not prove.
    The ``json`` round trip is not decoration — the record reaches the gate out
    of the payload store, so a projection holding tuples where the stored record
    holds lists would 409 every unchanged pipeline in production while passing
    an in-memory comparison here.
    """

    guided = _barrier_guided()
    proposal = _pipeline(guided, nodes, output_options=output_options)
    allocated = count(900)

    def fixed_uuid4() -> UUID:
        return UUID(f"00000000-0000-4000-8000-{next(allocated):012d}")

    with patch.object(guided_planning, "uuid4", fixed_uuid4):
        projection = build_guided_proposal_projection(
            proposal_id=_BARRIER_PROPOSAL_ID,
            proposal=proposal,
            guided=guided,
            catalog_plugin_ids=_BARRIER_CATALOG_PLUGIN_IDS,
        )
    turn = build_step_4_wire_turn(
        _state_from_proposal(proposal),
        proposal_projection=projection,
        guided=guided,
    )
    return json.loads(json.dumps(turn["payload"]))


def _non_empty_field_families(components: tuple[Any, ...]) -> set[str]:
    """Which of the two field-list families this projection actually carries.

    Both were added to the gate by RT-2 and both are EMPTY on every structural
    fixture — an observed-mode sink projects four empty lists and a non-llm node
    projects no structured fields at all. An empty list compares equal under any
    projection/mirror asymmetry, so the round-trip corpus has to say out loud
    which cases carry a real value and which do not (RT-4).
    """

    families: set[str] = set()
    for component in components:
        facts = dict(component)
        if "business_schema" in facts and dict(facts["business_schema"])["fields"]:
            families.add("business_schema")
        if "structured_output_fields" in facts and facts["structured_output_fields"]:
            families.add("structured_output_fields")
    return families


@pytest.mark.parametrize(
    ("nodes", "output_options", "non_empty_fields"),
    [
        pytest.param(_fork_coalesce_nodes(), None, frozenset(), id="fork-coalesce"),
        pytest.param(_fork_row_union_nodes(), None, frozenset(), id="fork-row-union"),
        pytest.param(_queue_nodes(), None, frozenset(), id="queue"),
        pytest.param(_aggregation_nodes(), None, frozenset(), id="aggregation"),
        pytest.param(_collector_nodes(), None, frozenset(), id="collector"),
        pytest.param(_fork_coalesce_nodes(), _DECLARED_OUTPUT_OPTIONS, frozenset({"business_schema"}), id="declared-output-schema"),
        pytest.param(_llm_leg_nodes(), None, frozenset({"structured_output_fields"}), id="llm-structured-output"),
    ],
)
def test_projection_equals_the_mirror_read_of_its_own_wire_payload(
    nodes: list[dict[str, Any]],
    output_options: dict[str, Any] | None,
    non_empty_fields: frozenset[str],
) -> None:
    """The property the whole gate rests on, proved end to end.

    ``guided_structure_projection`` derives the structure from a live head and
    ``_wire_payload_structure`` reads it back out of the frozen CONFIRM_WIRING
    record. For an UNCHANGED pipeline the two must be equal, or the gate refuses
    every completed-session chat. The collector case is the sharp one: a
    collector's ``opener_stable_id`` is an ordinal label on the projection side
    and a per-projection UUID in the frozen record, so this is what proves the
    single reconciliation in ``guided_structure_facts`` actually reconciles.

    The last two cases exist because equality over an EMPTY value proves
    nothing. ``business_schema`` and ``structured_output_fields`` joined the
    gate with every fixture authoring an observed-mode sink and no llm node, so
    a projection/mirror asymmetry on either would have refused every
    completed-session chat for a whole class of pipelines with this test green
    (RT-4). ``non_empty_fields`` is asserted EQUAL to what the fixture carries,
    so a case cannot decay back into the all-empty shape it replaced.
    """

    state = _barrier_state(nodes, output_options=output_options)
    payload = _wire_payload_for(nodes, output_options=output_options)

    projection = guided_structure_projection(state)

    assert projection == _wire_payload_structure(payload)
    # And in the representation PRODUCTION hands the gate: a
    # ``PreparedGuidedJsonPayload`` runs ``freeze_fields`` over its payload, so
    # the durable record arrives as mapping proxies holding tuples, not the
    # dicts and lists of a fresh ``json.loads``. Both must canonicalise the same
    # or the gate refuses every unchanged pipeline in the deployed path while a
    # JSON-only test stays green.
    assert projection == _wire_payload_structure(deep_freeze(payload))
    # Not vacuous: at least one component actually carries behavior facts.
    assert any(_behaviors(projection[0]))
    assert all(_flows(projection[1]))
    assert _non_empty_field_families(projection[0]) == non_empty_fields


def test_projection_changes_when_a_coalesce_policy_flips() -> None:
    """A behavior-only edit is drift: the model is told the frozen policy exactly.

    ``policy`` is a ``NodeSpec`` field, not an option, so a freeform
    ``upsert_node`` can flip require_all to best_effort without touching any
    component identity or any endpoint pair — the exact hole ST-RT-5 named.
    """

    base = guided_structure_projection(_barrier_state(_fork_coalesce_nodes()))
    flipped = guided_structure_projection(_barrier_state(_fork_coalesce_nodes(policy="best_effort")))

    assert _component_identities(flipped[0]) == _component_identities(base[0])
    assert _endpoint_pairs(flipped[1]) == _endpoint_pairs(base[1])
    assert flipped != base


def test_projection_changes_when_a_coalesce_merge_strategy_changes() -> None:
    """``merge`` is published beside ``policy`` and moves the same way."""

    base = guided_structure_projection(_barrier_state(_fork_coalesce_nodes()))
    merged = guided_structure_projection(_barrier_state(_fork_coalesce_nodes(merge="prefer_first")))

    assert _component_identities(merged[0]) == _component_identities(base[0])
    assert _endpoint_pairs(merged[1]) == _endpoint_pairs(base[1])
    assert merged != base


def test_projection_changes_when_an_aggregation_expected_output_count_appears() -> None:
    """The finding's second named exploit: None to 3 with identical topology.

    ``expected_output_count`` also determines the aggregation's published
    ``row_cardinality`` (``emitters._node_cardinality`` returns
    ``output='expected_count'`` exactly when it is set), so covering the
    behavior field covers that cardinality claim too.
    """

    base = guided_structure_projection(_barrier_state(_aggregation_nodes()))
    counted = guided_structure_projection(_barrier_state(_aggregation_nodes(expected_output_count=3)))

    assert _component_identities(counted[0]) == _component_identities(base[0])
    assert _endpoint_pairs(counted[1]) == _endpoint_pairs(base[1])
    assert counted != base


def test_projection_changes_when_an_aggregation_output_mode_changes() -> None:
    """``output_mode`` is published at system authority and is a NodeSpec field."""

    base = guided_structure_projection(_barrier_state(_aggregation_nodes()))
    passthrough = guided_structure_projection(_barrier_state(_aggregation_nodes(output_mode="passthrough")))

    assert _component_identities(passthrough[0]) == _component_identities(base[0])
    assert _endpoint_pairs(passthrough[1]) == _endpoint_pairs(base[1])
    assert passthrough != base


def test_projection_changes_when_a_collector_arrival_policy_flips() -> None:
    """A collector's ``scope_policy`` is published as its behavior ``policy``."""

    base = guided_structure_projection(_barrier_state(_collector_nodes()))
    relaxed = guided_structure_projection(_barrier_state(_collector_nodes(scope_policy="best_effort")))

    assert _component_identities(relaxed[0]) == _component_identities(base[0])
    assert _endpoint_pairs(relaxed[1]) == _endpoint_pairs(base[1])
    assert relaxed != base


def test_projection_changes_when_a_branch_alias_moves_between_identical_endpoints() -> None:
    """A flow-only edit: same endpoints, same order, different branch aliases.

    Repointing the coalesce's branch VALUES swaps which physical branch each
    leg's rows arrive on. Every ``(from_label, to_label)`` pair is unchanged and
    in the same order, so only the per-connection ``flow`` — which the committed
    chat context publishes and tells the model to use exactly — records that the
    graph now behaves differently.
    """

    base = guided_structure_projection(_barrier_state(_fork_coalesce_nodes()))
    swapped = guided_structure_projection(_barrier_state(_fork_coalesce_nodes(branches={"a_rows": "b_out", "b_rows": "a_out"})))

    assert _endpoint_pairs(swapped[1]) == _endpoint_pairs(base[1])
    assert _flows(swapped[1]) != _flows(base[1])
    assert swapped != base


def test_behavior_facts_are_unchanged_by_a_prompt_template_option_patch() -> None:
    """The admission the design requires, argued structurally rather than by luck.

    An interpretation Accept rewrites ``prompt_template`` and writes
    ``resolved_prompt_template_hash`` — both live in ``NodeSpec.options``.
    ``_node_behavior`` reads no option at all (transform and queue project only
    their kind; collector reads ``scope_policy``/``scope_opener``; aggregation
    reads ``trigger``/``output_mode``/``expected_output_count``; the barriers
    read ``branches``/``policy``/``merge``/``timeout_seconds``; the gate reads
    ``condition``/``routes``/``fork_to``), so widening the gate onto behavior
    cannot make an option patch look like drift. This asserts the behavior half
    specifically, so a future behavior arm that started reading options would
    fail here rather than silently start refusing post-Accept chat.
    """

    state = _committed_state()
    patched = replace(
        state,
        nodes=(
            state.nodes[0],
            replace(
                state.nodes[1],
                options={
                    "prompt_template": "Summarise {{ row.name }}",
                    "resolved_prompt_template_hash": "b" * 64,
                },
            ),
        ),
    )

    before = guided_structure_projection(state)
    after = guided_structure_projection(patched)
    assert _behaviors(after[0]) == _behaviors(before[0])
    assert after == before


def test_mirror_read_ignores_the_schema_contract_the_gate_cannot_re_derive() -> None:
    """Tripwire for the ONE published fact family deliberately left uncompared.

    A connection's ``schema_contract`` is published at system authority
    (present/satisfied plus three counts) but is frozen from
    ``validation.edge_contracts`` over the LOWERED executable state, not the raw
    head. Re-deriving it here would differ from the frozen value with no drift
    at all and refuse every completed-session chat, so it is excluded on
    purpose. This test exists to make that exclusion visible and deliberate: if
    someone widens the gate onto the contract, this fails and they must first
    solve the lowering gap (elspeth-986801d218). It is NOT an assertion that
    ignoring the contract is safe.
    """

    payload = _wire_payload_for(_fork_coalesce_nodes())
    contract = {
        "from": "node:leg_a",
        "to": "node:reconcile",
        "producer_guarantees": ["name"],
        "consumer_requires": ["name"],
        "missing_fields": [],
        "satisfied": True,
    }
    satisfied = json.loads(json.dumps(payload))
    satisfied["connections"][0]["schema_contract"] = contract
    unsatisfied = json.loads(json.dumps(payload))
    unsatisfied["connections"][0]["schema_contract"] = {**contract, "satisfied": False, "missing_fields": ["name"]}

    assert _wire_payload_structure(satisfied) == _wire_payload_structure(unsatisfied)


def test_mirror_read_reacts_to_a_behavior_edit_in_the_frozen_record() -> None:
    """The mirror half is not vacuous: it really does read the frozen behavior.

    The equality tests above would still pass if BOTH halves returned a constant
    fact tuple. Editing the frozen record alone must move the mirror.
    """

    payload = _wire_payload_for(_fork_coalesce_nodes())
    tampered = json.loads(json.dumps(payload))
    coalesce = next(node for node in tampered["nodes"] if node["node_type"] == "coalesce")
    coalesce["behavior"]["policy"] = "best_effort"

    assert _wire_payload_structure(tampered) != _wire_payload_structure(payload)
    assert _wire_payload_structure(tampered) != guided_structure_projection(_barrier_state(_fork_coalesce_nodes()))


def test_structure_facts_refuse_a_behavior_naming_an_unknown_component() -> None:
    """An identity the graph does not contain is refused, never compared unequal.

    ``opener_stable_id`` is the one component identity inside a behavior, and it
    means different things on the two halves (ordinal label vs per-projection
    stable ID). An unresolvable one leaves the halves incomparable, which the
    route turns into the same 409 as ordinary drift rather than a 500.
    """

    with pytest.raises(GuidedStructureUnprojectable):
        guided_structure_facts(
            {"kind": "collector", "opener_stable_id": "node-9", "policy": "require_all"},
            label_by_component_id={"node-1": "node-1"},
        )


def test_structure_facts_survive_a_json_round_trip_and_key_reordering() -> None:
    """Lists, tuples and key order must not change the comparison.

    The projection builds behaviors in memory with ``list`` members; the frozen
    record arrives as JSON, or as a frozen mapping with ``tuple`` members. All
    three must canonicalise identically, and absence must stay absence rather
    than collapsing onto a ``None`` default.
    """

    labels = {"node-1": "node-1"}
    live = {"kind": "gate", "route_aliases": ["route-1", "route-2"], "routes": [{"alias": "route-1", "key": "true"}]}
    frozen = {"routes": ({"key": "true", "alias": "route-1"},), "route_aliases": ("route-1", "route-2"), "kind": "gate"}

    assert guided_structure_facts(live, label_by_component_id=labels) == guided_structure_facts(
        json.loads(json.dumps(live)), label_by_component_id=labels
    )
    assert guided_structure_facts(live, label_by_component_id=labels) == guided_structure_facts(frozen, label_by_component_id=labels)
    # A member that is ABSENT is not the same as a member that is present and
    # null: ``validate_payload`` and the review surfaces read both by membership.
    assert guided_structure_facts({"kind": "node_error"}, label_by_component_id=labels) != guided_structure_facts(
        {"kind": "node_error", "branch": None}, label_by_component_id=labels
    )


# --------------------------------------------------------------------------
# RT2-F2: the gate must not compare MORE than the committed build publishes.
#
# The first widening compared the WHOLE behavior mapping. That made a gate's
# authored ``condition``, a gate's route alias/key pairs, an aggregation's
# trigger ``count`` and a barrier's ``timeout_seconds`` gate-sensitive — yet a
# committed build publishes none of them and the opener tells the model outright
# that those settings "can be rewritten after confirmation without changing the
# structure above". A legitimate post-commit edit of one therefore refused the
# chat permanently. Each test below moves ONE withheld literal and asserts the
# projection is unchanged; the pair of refusal tests after them is what keeps
# the exclusion from becoming a hole.
# --------------------------------------------------------------------------


def _with_node_fields(nodes: list[dict[str, Any]], index: int, **fields: Any) -> CompositionState:
    """The barrier state for *nodes*, with one ``NodeSpec`` field rewritten.

    Rewrites the SPEC rather than the proposal dict so the edit is exactly the
    shape a freeform ``upsert_node`` produces against a committed build — the
    write the gate has to judge — instead of a different proposal that happens
    to lower the same way.
    """

    state = _barrier_state(nodes)
    return replace(state, nodes=(*state.nodes[:index], replace(state.nodes[index], **fields), *state.nodes[index + 1 :]))


def test_a_gate_predicate_rewrite_is_admitted() -> None:
    """``condition`` is withheld from a committed build, so it cannot be drift.

    The frozen record's predicate is never republished to the model on a
    settled build (``_guided_advisory_safe_behavior`` omits it and
    ``_guided_committed_authored_records`` strips it), so comparing it could
    only refuse a chat the design promises to admit.
    """

    base = guided_structure_projection(_barrier_state(_fork_coalesce_nodes()))
    rewritten = guided_structure_projection(_with_node_fields(_fork_coalesce_nodes(), 0, condition="row['amount'] > 999"))

    assert rewritten == base


def test_a_barrier_timeout_rewrite_is_admitted() -> None:
    """``timeout_seconds`` is withheld on both barrier kinds."""

    coalesce = _fork_coalesce_nodes()
    row_union = _fork_row_union_nodes()

    assert guided_structure_projection(_with_node_fields(coalesce, 3, timeout_seconds=45.0)) == guided_structure_projection(
        _barrier_state(coalesce)
    )
    # The row_union fixture authors 12.5, so this moves a real value rather
    # than filling an absent one.
    assert guided_structure_projection(_with_node_fields(row_union, 3, timeout_seconds=30.0)) == guided_structure_projection(
        _barrier_state(row_union)
    )


def test_an_aggregation_trigger_count_rewrite_is_admitted() -> None:
    """A trigger count VALUE change is withheld; its presence is not.

    Deliberately 5 to 10 rather than None to 5: the published ``trigger_kinds``
    list is derived from WHICH trigger keys are set, so introducing a count adds
    "count" to a compared fact and is correctly refused. Only the value behind
    an already-configured trigger is withheld.
    """

    base = _barrier_state(_aggregation_nodes(trigger={"count": 5}))
    rewritten = _barrier_state(_aggregation_nodes(trigger={"count": 10}))

    assert guided_structure_projection(rewritten) == guided_structure_projection(base)


def test_configuring_a_trigger_that_was_absent_still_refuses() -> None:
    """The other half of the pair above: presence IS published, so it is drift."""

    base = guided_structure_projection(_barrier_state(_aggregation_nodes()))
    triggered = guided_structure_projection(_barrier_state(_aggregation_nodes(trigger={"count": 5})))

    assert _component_identities(triggered[0]) == _component_identities(base[0])
    assert triggered != base


def test_expected_output_count_stays_compared_despite_being_withheld() -> None:
    """The one withheld key the gate keeps, because it is published elsewhere.

    ``expected_output_count`` is in the withheld-literal set, but it rides at
    SYSTEM authority inside an aggregation's ``row_cardinality``
    (``emitters._node_cardinality`` returns ``output='expected_count'`` exactly
    when it is set). Excluding it would let the model state a current row
    cardinality that is false, so the exclusion set subtracts it back out.
    """

    assert "expected_output_count" in GUIDED_COMMITTED_WITHHELD_LITERAL_KEYS
    assert "expected_output_count" not in GUIDED_UNCOMPARED_BEHAVIOR_KEYS

    base = guided_structure_projection(_barrier_state(_aggregation_nodes()))
    counted = guided_structure_projection(_barrier_state(_aggregation_nodes(expected_output_count=3)))

    assert _component_identities(counted[0]) == _component_identities(base[0])
    assert _endpoint_pairs(counted[1]) == _endpoint_pairs(base[1])
    assert counted != base


def test_the_exclusion_is_derived_from_the_withheld_literal_authority() -> None:
    """No second enumeration: the exclusion is the authority minus two rules.

    Re-listing the published vocabulary inside ``planning`` is what let ST-RT-5
    happen — a future behavior arm would fall silently outside the gate. This
    pins the derivation instead, including the two documented name mappings:
    the record's ``predicate`` is behavior ``condition``, and its
    ``option_summaries`` has no behavior counterpart at all.

    The derivation runs ONE WAY — record name to behavior name — so pinning it
    alone is not enough. A withheld literal whose behavior spelling differs and
    is left unmapped mints an exclusion matching no behavior key at all: a
    silent no-op that leaves the literal compared, so a legitimate post-commit
    edit of it refuses the chat permanently. That is RT2-F2 recurring through
    the very mechanism installed to prevent it, which is why the coverage
    assertion below lives in the same test as the derivation rather than in one
    a reader could add an exclusion without seeing.
    """

    assert {"condition", "count", "routes", "timeout_seconds"} == GUIDED_UNCOMPARED_BEHAVIOR_KEYS
    assert "predicate" in GUIDED_COMMITTED_WITHHELD_LITERAL_KEYS
    assert "predicate" not in GUIDED_UNCOMPARED_BEHAVIOR_KEYS
    assert "option_summaries" in GUIDED_COMMITTED_WITHHELD_LITERAL_KEYS
    assert "option_summaries" not in GUIDED_UNCOMPARED_BEHAVIOR_KEYS

    emitted = _corpus_behavior_key_names()
    assert emitted >= GUIDED_UNCOMPARED_BEHAVIOR_KEYS, (
        "every exclusion must name a key some ``_node_behavior`` arm actually emits; these match nothing and are "
        f"dead: {sorted(GUIDED_UNCOMPARED_BEHAVIOR_KEYS - emitted)}. Map the withheld record name onto its behavior "
        "spelling in ``_GUIDED_WITHHELD_RECORD_KEY_BEHAVIOR_NAMES``, or to None if it has no behavior counterpart."
    )


def test_the_exclusion_is_scoped_to_behavior_and_never_reaches_a_flow() -> None:
    """``routes`` names two different things, and only one of them is withheld.

    A gate BEHAVIOR's ``routes`` is the authored alias/key table, withheld from a
    committed build. A gate_fork FLOW's ``routes`` is the published list of route
    aliases the fork fires on (``_guided_advisory_safe_flow``). A blanket
    key-name exclusion would blind the gate to the second, which is exactly the
    published-but-uncompared hole this round exists to close.
    """

    labels = {"node-1": "node-1"}
    behavior = {"kind": "gate", "condition": "row['a']", "route_aliases": ["route-1"], "routes": [{"alias": "route-1", "key": "true"}]}
    assert guided_structure_compared_behavior(behavior) == {"kind": "gate", "route_aliases": ["route-1"]}

    fork_flow = {"kind": "gate_fork", "routes": ["route-1", "route-2"], "branch": "branch-1"}
    narrowed = {**fork_flow, "routes": ["route-2"]}
    assert guided_structure_facts(fork_flow, label_by_component_id=labels) != guided_structure_facts(narrowed, label_by_component_id=labels)


def test_mirror_read_ignores_a_withheld_behavior_literal_in_the_frozen_record() -> None:
    """End-to-end half of the exclusion, read off a real frozen record."""

    payload = _wire_payload_for(_fork_coalesce_nodes())
    edited = json.loads(json.dumps(payload))
    gate = next(node for node in edited["nodes"] if node["node_type"] == "gate")
    gate["behavior"]["condition"] = "row['amount'] > 999"

    assert _wire_payload_structure(edited) == _wire_payload_structure(payload)


def test_mirror_read_reacts_to_a_fork_flow_route_edit_in_the_frozen_record() -> None:
    """And the collision case: the SAME key name on a flow is still compared."""

    payload = _wire_payload_for(_fork_coalesce_nodes())
    edited = json.loads(json.dumps(payload))
    fork = next(connection for connection in edited["connections"] if connection["flow"]["kind"] == "gate_fork")
    fork["flow"]["routes"] = fork["flow"]["routes"][:1]

    assert _wire_payload_structure(edited) != _wire_payload_structure(payload)


# --------------------------------------------------------------------------
# RT-2: the gate must not compare LESS than the committed build publishes.
#
# ``structured_output_fields`` (llm nodes) and an output's ``business_schema``
# survive the committed authored-record trim deliberately — their docstring says
# they "describe the schema contract the frozen graph was validated against" —
# and both are PURE functions of head options with no lowering dependency
# (emitters :800 and :815 read ``node.options`` / ``output.options`` and nothing
# from ``executable_state`` or ``validation.edge_contracts``). A
# ``patch_node_options`` / ``patch_output_options`` write therefore moved a
# published fact with the gate green.
# --------------------------------------------------------------------------


def _llm_leg_state(options: dict[str, Any]) -> CompositionState:
    """The fork fixture with its first leg authored as an llm node."""

    state = _barrier_state(_fork_coalesce_nodes())
    return replace(state, nodes=(state.nodes[0], replace(state.nodes[1], plugin="llm", options=options), *state.nodes[2:]))


def test_an_options_only_structured_output_rewrite_refuses() -> None:
    """The published field name, type and enum values all move; nothing else does."""

    base = guided_structure_projection(_llm_leg_state(_LLM_QUERIES_SCORE))
    rewritten = guided_structure_projection(_llm_leg_state(_LLM_QUERIES_VERDICT))

    assert _component_identities(rewritten[0]) == _component_identities(base[0])
    assert _endpoint_pairs(rewritten[1]) == _endpoint_pairs(base[1])
    assert _behaviors(rewritten[0]) == _behaviors(base[0]), "behavior is untouched: this is an options-only write"
    assert rewritten != base


def test_a_non_llm_node_publishes_no_structured_output_fields() -> None:
    """The live half reads the options only for llm nodes, exactly as emitters do."""

    state = _barrier_state(_fork_coalesce_nodes())
    with_options = replace(state, nodes=(state.nodes[0], replace(state.nodes[1], options=_LLM_QUERIES_VERDICT), *state.nodes[2:]))

    assert guided_structure_projection(with_options) == guided_structure_projection(state)
    assert dict(guided_structure_projection(state)[0][1])["structured_output_fields"] == ()


def test_an_options_only_business_schema_rewrite_refuses() -> None:
    """``mode`` observed to declared, ``required_fields`` [] to ["name"]."""

    state = _barrier_state(_fork_coalesce_nodes())
    declared = replace(
        state,
        outputs=(replace(state.outputs[0], options={"schema": {"mode": "declared", "required_fields": ["name"]}}),),
    )

    base = guided_structure_projection(state)
    rewritten = guided_structure_projection(declared)
    assert _component_identities(rewritten[0]) == _component_identities(base[0])
    assert _endpoint_pairs(rewritten[1]) == _endpoint_pairs(base[1])
    assert rewritten != base


def test_a_source_row_cardinality_change_refuses() -> None:
    """A source's cardinality is a total function of its plugin, so it is compared."""

    state = _barrier_state(_queue_nodes())
    llm_source = replace(state, sources={name: replace(source, plugin="llm") for name, source in state.sources.items()})

    assert (
        dict(guided_structure_projection(state)[0][0])["row_cardinality"]
        != (dict(guided_structure_projection(llm_source)[0][0])["row_cardinality"])
    )


# --------------------------------------------------------------------------
# The durable guard. RT2-F2, RT-2 and RT2-F3 are three faces of ONE missing
# invariant, so this pins the invariant rather than its three violations.
# --------------------------------------------------------------------------

# A schema contract no fixture produces on its own: every fixture's connections
# lower to ``schema_contract: null``, and a family that never appears cannot be
# partitioned. Shaped exactly as ``protocol._validate_wire_payload``'s
# ``contract_keys`` proves it, so the authority below still validates.
_INJECTED_SCHEMA_CONTRACT = {
    "from": "node:leg_a",
    "to": "node:reconcile",
    "producer_guarantees": ["name"],
    "consumer_requires": ["name"],
    "missing_fields": [],
    "satisfied": True,
}


def _direct_gate_nodes() -> list[dict[str, Any]]:
    """A gate whose routes go straight to consumers rather than forking.

    In the partition corpus for one reason: a direct route publishes a
    ``gate_route`` flow carrying the singular ``route`` member, which no fork
    fixture produces. Without it that published key would never be observed and
    the registry could not be asserted equal to what the projection emits.
    """

    return [
        {
            "id": "sift",
            "node_type": "gate",
            "plugin": None,
            "input": "rows",
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "row['amount'] > 0",
            "routes": {"true": "kept", "false": "cleaned"},
            "fork_to": [],
        },
        {
            "id": "finish",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "kept",
            "on_success": "cleaned",
            "on_error": "discard",
            "options": {},
        },
    ]


_PARTITION_FIXTURES = (
    pytest.param(_fork_coalesce_nodes(), False, id="fork-coalesce"),
    pytest.param(_direct_gate_nodes(), False, id="direct-gate"),
    pytest.param(_fork_row_union_nodes(), False, id="fork-row-union"),
    pytest.param(_queue_nodes(), False, id="queue"),
    pytest.param(_aggregation_nodes(), False, id="aggregation"),
    pytest.param(_aggregation_nodes(expected_output_count=3), False, id="aggregation-counted"),
    # A CONFIGURED trigger, so the corpus carries a non-empty value for the one
    # withheld member a cardinality arm sits next to. Without it every
    # aggregation here authors ``trigger={}``, which is indistinguishable from
    # the unset default and would let an arm read the trigger unnoticed.
    pytest.param(_aggregation_nodes(trigger={"count": 5}), False, id="aggregation-triggered"),
    pytest.param(_collector_nodes(), False, id="collector"),
    pytest.param(_fork_coalesce_nodes(), True, id="fork-coalesce-with-contract"),
)


def _partition_corpus() -> list[tuple[list[dict[str, Any]], bool]]:
    """The ``(nodes, contracted)`` pairs behind :data:`_PARTITION_FIXTURES`.

    One reading of the corpus for every guard that has to sweep it whole rather
    than one case at a time — the vocabulary span, the exclusion-coverage
    check, and the two partition directions. Sharing the reading is the point:
    a case added to the parametrized guard joins the sweeping ones by
    construction instead of by remembering to widen a second list.
    """

    return [(cast(list[dict[str, Any]], param.values[0]), bool(param.values[1])) for param in _PARTITION_FIXTURES]


def _corpus_behavior_key_names() -> frozenset[str]:
    """Every behavior key name the corpus's FROZEN wire records carry.

    Read off the durable record rather than off the compared subset: the
    compared subset is the raw behavior minus the exclusions, so measuring the
    exclusions against it could only ever agree with itself. Because the corpus
    spans every node kind — ``test_the_partition_corpus_spans_every_node_kind``
    pins that — this is the whole behavior vocabulary ``_node_behavior`` emits,
    not the part one fixture happens to reach.
    """

    names: set[str] = set()
    for nodes, _contracted in _partition_corpus():
        for node in _wire_payload_for(nodes)["nodes"]:
            names.update(node["behavior"])
    return frozenset(names)


# Verdicts. ``_DERIVED`` is a FORM of compared coverage, not a third category:
# the fact is a function of other facts the gate does compare, and
# ``test_a_non_transform_row_cardinality_is_a_function_of_compared_facts``
# below proves that dependency rather than asserting it.
_COMPARED = "compared"
_DERIVED = "derived-from-compared-facts"
_QUALIFIED = "recorded-at-confirmation"

# Top-level system-projection members that carry no pipeline fact: the schema
# and turn discriminators, the four component containers, the fixed omission
# prose, and the covered-intent list (empty by construction on a committed
# build — ``_guided_committed_graph_authority`` refuses otherwise, and the test
# below re-asserts it so it cannot quietly become a fact).
_STRUCTURAL_TOP_LEVEL_KEYS = frozenset(
    {"schema", "turn_type", "sources", "nodes", "outputs", "connections", "omitted", "covered_deferred_intent_ids"}
)

# THE DECISION RECORD. Every ``(component kind, published key)`` the committed
# system projection can emit needs a verdict here, and the observed set must
# EQUAL this set — so a new projection arm fails as unregistered rather than
# silently joining the uncovered set. This is a test-side registry on purpose:
# the same list inside ``planning`` would be the parallel definition that let
# ST-RT-5 happen, because production code cannot fail loudly at review time.
#
# ``node.behavior`` and ``connection.flow`` are their own namespaces because
# their MEMBERS are what the model reads: a behavior key dropped from the
# compared set while still published is exactly the ST-RT-5 shape, and a
# key-name-only partition one level up would not see it.
_PUBLISHED_KEY_VERDICTS: dict[tuple[str, str], str] = {
    ("", "review_status_at_confirmation"): _QUALIFIED,
    ("source", "alias"): _COMPARED,
    ("source", "kind"): _COMPARED,
    ("source", "plugin"): _COMPARED,
    ("source", "row_cardinality"): _COMPARED,
    ("node", "alias"): _COMPARED,
    ("node", "kind"): _COMPARED,
    ("node", "plugin"): _COMPARED,
    ("node", "node_type"): _COMPARED,
    ("node", "behavior"): _COMPARED,
    # Non-transform nodes only; the transform arm is the qualified key below.
    ("node", "row_cardinality"): _DERIVED,
    ("node", "row_cardinality_at_confirmation"): _QUALIFIED,
    ("output", "alias"): _COMPARED,
    ("output", "kind"): _COMPARED,
    ("output", "plugin"): _COMPARED,
    ("connection", "alias"): _COMPARED,
    ("connection", "from_alias"): _COMPARED,
    ("connection", "to_alias"): _COMPARED,
    ("connection", "flow"): _COMPARED,
    ("connection", "schema_contract_at_confirmation"): _QUALIFIED,
    ("node.behavior", "kind"): _COMPARED,
    ("node.behavior", "route_aliases"): _COMPARED,
    ("node.behavior", "fork_branches"): _COMPARED,
    ("node.behavior", "trigger_kinds"): _COMPARED,
    ("node.behavior", "output_mode"): _COMPARED,
    ("node.behavior", "branch_aliases"): _COMPARED,
    ("node.behavior", "policy"): _COMPARED,
    ("node.behavior", "merge"): _COMPARED,
    ("connection.flow", "kind"): _COMPARED,
    ("connection.flow", "route"): _COMPARED,
    ("connection.flow", "routes"): _COMPARED,
    ("connection.flow", "branch"): _COMPARED,
}

# THE OTHER DIRECTION. The registry above pins "published implies compared or
# qualified"; this one pins the converse — every ``(namespace, key)`` the gate
# COMPARES that the committed system projection does not publish, with the
# reason it is compared anyway.
#
# The direction is not free. A compared-but-unpublished fact cannot make the
# model state a stale fact as current: nothing is published, so nothing can go
# stale. It CAN refuse the chat on a pipeline that never drifted, and a
# completed-session chat is the only channel a settled build has — the RT2-F2
# blocker exactly. The first widening compared the whole behavior mapping and
# so put ``condition``, ``routes``, ``count`` and ``timeout_seconds`` in here
# unregistered; with this assertion it would have failed outright instead of
# shipping. Reasons are per member because that is the decision being recorded:
# "strictly safe" as a class is what made the omission look defensible.
_COMPARED_BUT_UNPUBLISHED: dict[tuple[str, str], str] = {
    ("node", "structured_output_fields"): (
        "published in the delimited USER-role literal block rather than the system block, and a pure function of "
        "authored head options — so a post-commit patch_node_options rewrite moves a list the model is told "
        "describes the pipeline as it stands (RT-2)"
    ),
    ("output", "business_schema"): (
        "same authority and same reason: published in the user-role literal block, derived from output options "
        "alone, and rewritable after confirmation by patch_output_options (RT-2)"
    ),
    ("node.behavior", "expected_output_count"): (
        "published TRANSITIVELY at system authority inside an aggregation's row_cardinality "
        "(emitters._node_cardinality returns output='expected_count' exactly when it is set), so a chat admitted "
        "while it moved would state a current row cardinality that is false"
    ),
    ("node.behavior", "opener_stable_id"): (
        "never published — the safe behavior projects a collector's policy alone — but it names WHICH component "
        "opens the buffered scope, which is graph structure rather than an authored setting: repointing it makes "
        "the endpoint relations the model is told to use exactly describe a different pipeline"
    ),
}

# The two published members that are themselves fact mappings, mapped onto the
# namespace their members are registered under.
_NESTED_FACT_KEYS = {("node", "behavior"): "node.behavior", ("connection", "flow"): "connection.flow"}


def _committed_system_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """The SYSTEM block a completed session's chat context actually publishes.

    Built through the production path — the real advisory projection followed by
    the real committed-build time qualification — so a key added to either is
    observed here without an edit.
    """

    authority = chat_solver.GuidedAdvisoryGraphAuthority(
        turn_type=TurnType.CONFIRM_WIRING,
        payload_id=guided_json_payload_id("turn", payload),
        proposal_id=payload["proposal_id"],
        draft_hash=payload["draft_hash"],
        covered_deferred_intent_ids=(),
        payload=payload,
    )
    system, _literals = chat_solver._guided_advisory_graph_projection(authority)
    # Qualifies IN PLACE and returns nothing, so the caller cannot mistake the
    # result for a copy; `system` is freshly built per call and nothing else
    # holds it.
    chat_solver._guided_committed_time_qualified(system)
    return system


def _committed_pair(nodes: list[dict[str, Any]], contracted: bool) -> tuple[dict[str, Any], GuidedStructureProjection]:
    """The published system projection and the compared projection, same state."""

    payload = _wire_payload_for(nodes)
    if contracted:
        payload["connections"][0]["schema_contract"] = dict(_INJECTED_SCHEMA_CONTRACT)
    return _committed_system_projection(payload), guided_structure_projection(_barrier_state(nodes))


def _published_keys(system: dict[str, Any]) -> set[tuple[str, str]]:
    """Every ``(kind, key)`` this system projection actually publishes."""

    observed = {("", key) for key in system if key not in _STRUCTURAL_TOP_LEVEL_KEYS}
    for kind, container in (("source", "sources"), ("node", "nodes"), ("output", "outputs"), ("connection", "connections")):
        for item in system[container]:
            observed.update((kind, key) for key in item)
            for (nested_kind, nested_key), namespace in _NESTED_FACT_KEYS.items():
                if nested_kind == kind and nested_key in item:
                    observed.update((namespace, member) for member in item[nested_key])
    return observed


@dataclass(frozen=True)
class _LoweredNodeStandIn:
    """A stand-in for the LOWERED executable node the gate's live half lacks.

    Two of these are handed to every non-transform arm of
    ``emitters._node_cardinality``, differing in a plugin whose transform
    cardinality differs: ``passthrough`` is one-in-one-out, ``json_explode``
    creates tokens. An arm that started reading the lowered node returns two
    different answers — or raises on a member this stand-in deliberately does
    not carry, which fails the test just as loudly. Only the two members the
    transform arm actually reads are here; nothing is invented to keep a future
    reader quiet.
    """

    plugin: str
    options: Mapping[str, Any]


_LOWERED_STAND_INS = (
    _LoweredNodeStandIn(plugin="passthrough", options={}),
    # Differs from the first in BOTH members: a stand-in pair that varied only
    # by plugin proved nothing about an arm reading the lowered node's options
    # (ST-6). Neither value is cardinality-relevant, so a passing comparison
    # still means "this arm ignored the lowered node".
    _LoweredNodeStandIn(plugin="json_explode", options={"burst": True, "schema": {"mode": "declared"}}),
)


def _minimal_node_spec(node_type: NodeType, *, expected_output_count: int | None) -> NodeSpec:
    """A node carrying ONLY the two facts the ``derived`` verdict says are read.

    Every other member is the unset default, so an arm that reaches for one
    gets None where the authored node had a value and the comparison against
    the authored node's answer fails. (``NodeSpec.__post_init__`` defaults a
    coalesce's ``policy`` and ``merge``; no cardinality arm reads either, and
    the aggregation arm is the only one that reads the count.)
    """

    return NodeSpec(
        id="derivation-probe",
        node_type=node_type,
        plugin=None,
        input="rows",
        on_success=None,
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        expected_output_count=expected_output_count,
    )


class TestPublishedFactPartition:
    """Published as CURRENT ⟺ compared; everything else time-qualified.

    THE RULE, once: a fact reaches the model as a current fact only if the
    post-commit drift gate compares it against the live head, and every other
    published fact carries the ``_at_confirmation`` suffix the committed opener
    explains. Nothing may be in neither category (the model would state a stale
    fact as current) and nothing in both (the qualification would be a lie about
    a fact that IS current).

    Scoped to the SYSTEM projection, which is the block the opener tells the
    model to "use exactly". The delimited user-role literal block is a separate
    authority whose currency the opener describes per list.

    The partition is pinned in BOTH directions, because both directions have a
    defect. ``_PUBLISHED_KEY_VERDICTS`` covers the published side: a published
    key with no verdict, or one whose verdict does not hold, is the stale-fact
    failure. ``_COMPARED_BUT_UNPUBLISHED`` covers the compared side: comparing
    a fact the system block never publishes cannot make the model state a stale
    fact — nothing is published, so nothing can go stale — but it CAN refuse a
    chat on a pipeline that never drifted, which is RT2-F2 and costs a settled
    build its only channel. So it is registered with a per-member reason rather
    than waved through as a class, and the corpus the whole guard observes is
    itself tied to the live node-type vocabulary
    (``test_the_partition_corpus_spans_every_node_kind``) so that a new node
    kind cannot pass either direction by simply never being looked at.

    ACCEPTED LIMITATION, stated so nobody reads more into a green run than it
    earns (ST-5): the corpus is tied to the live vocabulary on the NODE-TYPE
    axis only. A published key that appears solely under some other axis — a
    plugin, an option shape, a connection kind, a validation outcome no fixture
    authors — is still unobserved, and this guard passes without ruling on it.
    Every non-transform fixture therefore authors ``_NON_DEFAULT_NODE_OPTIONS``
    so at least the options axis is discriminating rather than uniform. Closing
    the rest would mean enumerating the projection's key space from the code
    instead of from the corpus; the node-type axis was closed first because a
    new node kind is the parity sweep AGENTS.md actually names.
    """

    @staticmethod
    def _assert_partition(namespace: str, *, published: Mapping[str, Any], compared: frozenset[str]) -> None:
        """Assert one published fact mapping partitions cleanly.

        Every key is registered, and its verdict holds: a ``qualified`` key
        carries the suffix and its BASE name is absent from the compared set
        (else it is in both), a ``compared`` key carries no suffix and is
        present (else it is in neither), and a ``derived`` key is neither
        suffixed nor compared directly.
        """

        for key in published:
            verdict = _PUBLISHED_KEY_VERDICTS.get((namespace, key))
            assert verdict is not None, f"{namespace}.{key} is published with no compare-or-qualify decision"
            if verdict == _QUALIFIED:
                assert key.endswith(_CONFIRMATION_SUFFIX), f"{namespace}.{key} is qualified but unmarked"
                assert key.removesuffix(_CONFIRMATION_SUFFIX) not in compared, f"{namespace}.{key} is qualified AND compared"
            else:
                assert not key.endswith(_CONFIRMATION_SUFFIX), f"{namespace}.{key} is marked qualified but registered {verdict}"
                if verdict == _COMPARED:
                    assert key in compared, f"{namespace}.{key} is published as current but the gate does not compare it"
                else:
                    assert key not in compared, f"{namespace}.{key} is compared directly; register it as {_COMPARED}"

    @pytest.mark.parametrize(("nodes", "contracted"), _PARTITION_FIXTURES)
    def test_every_published_key_is_compared_or_qualified(self, nodes: list[dict[str, Any]], contracted: bool) -> None:
        system, (components, connections) = _committed_pair(nodes, contracted)
        compared_by_alias = {dict(item)["alias"]: dict(item) for item in (*components, *connections)}
        assert system["covered_deferred_intent_ids"] == [], "a committed build has no pending intent to attribute"

        for kind, container in (("source", "sources"), ("node", "nodes"), ("output", "outputs"), ("connection", "connections")):
            for item in system[container]:
                compared = compared_by_alias[item["alias"]]
                self._assert_partition(kind, published=item, compared=frozenset(compared))
                for (nested_kind, nested_key), namespace in _NESTED_FACT_KEYS.items():
                    if nested_kind != kind or nested_key not in item:
                        continue
                    # A nested fact mapping is compared MEMBER BY MEMBER, so
                    # the partition has to descend: a behavior key dropped from
                    # the compared set while still published would otherwise
                    # hide behind its unchanged parent key name.
                    self._assert_partition(
                        namespace,
                        published=item[nested_key],
                        compared=frozenset(dict(compared[nested_key])),
                    )
        for key in system:
            if key in _STRUCTURAL_TOP_LEVEL_KEYS:
                continue
            assert _PUBLISHED_KEY_VERDICTS.get(("", key)) == _QUALIFIED, f"top-level {key} is published with no decision"
            assert key.endswith(_CONFIRMATION_SUFFIX)

    def test_the_partition_corpus_spans_every_node_kind(self) -> None:
        """The corpus is bound to the live vocabulary, not to what it happens to hold.

        Every other assertion in this class is an OBSERVATION over
        ``_PARTITION_FIXTURES``: a key no fixture emits is neither observed nor
        registered, so the guard passes on it vacuously. A new node kind with a
        new behavior arm — the parity sweep AGENTS.md names across exactly these
        surfaces, and the case this guard exists to catch — is precisely that
        shape, and nothing tied the corpus to the vocabulary.

        ``COMPOSER_NODE_TYPES`` is the authority because it is the one
        ``NodeSpec`` itself validates against (composer/state.py), so it is what
        a live head can carry — and the gate compares against a live head. A new
        kind therefore fails HERE, before it can pass the partition unlooked at,
        until someone adds a fixture and makes the compare-or-qualify decision.
        The same span is what makes the exclusion-coverage check and both
        partition directions complete: ``opener_stable_id`` needs the collector
        case, ``expected_output_count`` the aggregation one.
        """

        represented = {node.node_type for nodes, _contracted in _partition_corpus() for node in _barrier_state(nodes).nodes}

        assert represented == COMPOSER_NODE_TYPES, (
            f"the partition corpus no longer spans every composer node kind; missing "
            f"{sorted(COMPOSER_NODE_TYPES - represented)}, obsolete {sorted(represented - COMPOSER_NODE_TYPES)}"
        )

    def test_the_registry_names_exactly_what_the_projection_publishes(self) -> None:
        """The half that kills a NEW arm: unregistered on either side fails.

        A key added to the committed system projection without a verdict shows
        up as unregistered; a verdict for a key the projection no longer emits
        shows up as unobserved. Either way a reviewer has to make the compare-or-
        qualify decision explicitly.
        """

        observed: set[tuple[str, str]] = set()
        for nodes, contracted in _partition_corpus():
            system, _compared = _committed_pair(nodes, contracted)
            observed |= _published_keys(system)

        assert observed == set(_PUBLISHED_KEY_VERDICTS)

    def test_every_compared_key_is_published_or_registered_as_withheld(self) -> None:
        """The converse direction, which the first widening would have failed.

        A fact the gate compares but the system block does not publish is not
        automatically safe: it cannot produce a stale claim, but it can refuse a
        completed-session chat on a pipeline that never drifted. Asserting the
        observed set EQUAL to ``_COMPARED_BUT_UNPUBLISHED`` makes both mistakes
        loud — widening the gate onto an unpublished fact fails as unregistered
        (as ``condition``, ``routes``, ``count`` and ``timeout_seconds`` would
        have), and narrowing the published projection while leaving the reason
        behind fails as unobserved.
        """

        observed: set[tuple[str, str]] = set()
        for nodes, contracted in _partition_corpus():
            system, (components, connections) = _committed_pair(nodes, contracted)
            published_by_alias: dict[str, tuple[str, Mapping[str, Any]]] = {
                item["alias"]: (kind, item)
                for kind, container in (("source", "sources"), ("node", "nodes"), ("output", "outputs"), ("connection", "connections"))
                for item in system[container]
            }
            for item in (*components, *connections):
                facts = dict(item)
                kind, published = published_by_alias[cast(str, facts["alias"])]
                observed.update((kind, key) for key in facts if key not in published)
                for (nested_kind, nested_key), namespace in _NESTED_FACT_KEYS.items():
                    if nested_kind != kind or nested_key not in facts:
                        continue
                    # Descends for the same reason the published direction
                    # does: a behavior member compared while unpublished hides
                    # behind a parent key that IS published.
                    nested_published = published[nested_key] if nested_key in published else {}
                    nested_facts = dict(cast(GuidedStructureFacts, facts[nested_key]))
                    observed.update((namespace, member) for member in nested_facts if member not in nested_published)

        assert observed == set(_COMPARED_BUT_UNPUBLISHED)

    def test_a_non_transform_row_cardinality_is_a_function_of_compared_facts(self) -> None:
        """The proof behind the one ``derived`` verdict, taken from the arms.

        ``emitters._node_cardinality`` returns a non-transform node's cardinality
        from ``node_type`` plus ``expected_output_count``, both of which the gate
        compares — so the published claim cannot go stale while the gate is
        green. That dependency is proved DIRECTLY here, one call per non-
        transform node in the corpus, in the two legs the claim actually needs:

        * the LOWERED node is ignored — the same call with two stand-ins that
          would lower to different cardinalities returns one answer, and an arm
          that started reading the lowered node either disagrees or raises on a
          member the stand-in deliberately does not carry;
        * no OTHER authored value on the ``NodeSpec`` is read — a minimal spec
          carrying only the kind and the expected output count answers the same
          as the real authored node, so an arm reaching for ``trigger``,
          ``options`` or ``policy`` fails here. The leg sees a member only
          where the corpus authors a value the minimal spec leaves at its
          default, which is exactly why the ``aggregation-triggered`` fixture
          exists: every other aggregation here authors ``trigger={}``, which is
          indistinguishable from the unset default and would have hidden a
          trigger read.

        A corpus property was not enough: each kind enters the corpus with one
        options shape, so a collision-keyed property could not fail whatever the
        arms read. What stays corpus-shaped is the LIVE dependency at the end —
        the published cardinality really does move when the compared
        ``expected_output_count`` moves, through the whole emitter-to-context
        path rather than through this test's own call.

        The production side is deliberately left alone. Re-deriving the
        cardinality inside ``guided_structure_projection`` would mean handing
        the emitter an ``executable_node`` that half does not have, and a future
        arm that began reading one would compare a plausible-but-wrong value
        against the real frozen one and refuse every unchanged pipeline of that
        kind. This test is what fails in that world instead.
        """

        covered: set[str] = set()
        cardinality_by_compared_facts: dict[tuple[str, Any], Any] = {}
        for nodes, contracted in _partition_corpus():
            system, (components, _connections) = _committed_pair(nodes, contracted)
            for node in _barrier_state(nodes).nodes:
                if node.node_type == "transform":
                    continue
                covered.add(node.node_type)
                lowered = [_node_cardinality(node, stand_in) for stand_in in _LOWERED_STAND_INS]
                assert lowered[0] == lowered[1], (
                    f"the {node.node_type} row-cardinality arm reads the LOWERED node, which the gate's live half "
                    "cannot supply; re-deriving it there would refuse every unchanged pipeline of this kind"
                )
                minimal = _minimal_node_spec(node.node_type, expected_output_count=node.expected_output_count)
                assert _node_cardinality(minimal, _LOWERED_STAND_INS[0]) == lowered[0], (
                    f"the {node.node_type} row-cardinality arm reads a NodeSpec member beyond node_type and "
                    "expected_output_count, so it is no longer a function of the facts the gate compares"
                )
            # Keyed off the COMPARED facts, not the published ones: the system
            # projection's ``behavior`` is the safe subset and does not carry
            # ``expected_output_count`` at all, which is precisely why the
            # cardinality is the only place a stale count could surface.
            compared_by_alias = {dict(component)["alias"]: dict(component) for component in components}
            for node in system["nodes"]:
                if "row_cardinality" not in node:
                    continue
                compared = compared_by_alias[node["alias"]]
                behavior = dict(compared["behavior"])
                key = (compared["node_type"], behavior["expected_output_count"] if "expected_output_count" in behavior else None)
                published = json.dumps(node["row_cardinality"], sort_keys=True)
                assert cardinality_by_compared_facts.setdefault(key, published) == published, (
                    f"{key} publishes two different row cardinalities, so it is not a function of the compared facts"
                )
        # Every arm was actually exercised, and the floor is the vocabulary
        # rather than a number: a new non-transform kind is unproved until its
        # fixture exists. This is a FLOOR, not a second definition of the
        # corpus's obligation — ``test_the_partition_corpus_spans_every_node_kind``
        # is the authority, and its message is the one to act on when a missing
        # fixture reds both.
        assert covered == COMPOSER_NODE_TYPES - {"transform"}
        # And the dependency is live, not vacuous: moving the compared
        # ``expected_output_count`` moves the published cardinality.
        assert cardinality_by_compared_facts[("aggregation", None)] != cardinality_by_compared_facts[("aggregation", "3")]

    def test_a_transform_cardinality_is_published_only_under_the_qualified_name(self) -> None:
        """The rename is real, and it lands on transforms alone."""

        system, _compared = _committed_pair(_fork_coalesce_nodes(), False)
        by_type = {node["node_type"]: node for node in system["nodes"]}

        assert f"row_cardinality{_CONFIRMATION_SUFFIX}" in by_type["transform"]
        assert "row_cardinality" not in by_type["transform"]
        assert "row_cardinality" in by_type["gate"]
        assert f"row_cardinality{_CONFIRMATION_SUFFIX}" not in by_type["gate"]
        # A source's cardinality IS current — it is a total function of the
        # plugin the gate compares — so it must not be qualified.
        assert "row_cardinality" in system["sources"][0]
