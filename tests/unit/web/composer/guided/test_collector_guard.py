"""Guided-lane collector refusal guard + projection typed rejection (elspeth-88bb77953c).

Interim posture until the maintainer rules on guided collector authoring: the
shared candidate binder REFUSES collector-bearing candidates with a repairable
closed-code rejection, and the proposal projection's node-behavior dispatch
fails typed (never a bare assert whose ``python -O`` erasure would
mis-serialize a collector as a gate). Fixture idiom copied file-locally from
``test_bind_reviewed_components.py``.
"""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided import planning as guided_planning
from elspeth.web.composer.guided.planning import (
    GuidedCandidateBindingRejected,
    _node_behavior,
    bind_guided_reviewed_components,
)
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.composer.pipeline_planner import _CANDIDATE_SHAPE_INTEGRITY_PREFIX
from elspeth.web.composer.state import NodeSpec

SOURCE_ID = "11111111-1111-4111-8111-111111111111"
OUTPUT_ID = "33333333-3333-4333-8333-333333333333"


def _guided() -> GuidedSession:
    return GuidedSession(
        step=GuidedStep.STEP_3_TRANSFORMS,
        source_order=(SOURCE_ID,),
        reviewed_sources={
            SOURCE_ID: SourceResolved(
                name="source",
                plugin="csv",
                options={"path": "blob:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                observed_columns=("color_name", "hex"),
                sample_rows=(),
                on_validation_failure="discard",
            )
        },
        output_order=(OUTPUT_ID,),
        reviewed_outputs={
            OUTPUT_ID: SinkOutputResolved(
                name="output",
                plugin="json",
                options={"path": "outputs/colours.json"},
                required_fields=(),
                schema_mode="observed",
                on_write_failure="discard",
            )
        },
    )


def _collector_candidate() -> dict[str, object]:
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
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
                "on_success": "output",
                "options": {},
                "scope_name": "document_pages",
                "scope_opener": "explode",
                "scope_policy": "require_all",
            },
        ],
        "edges": [],
        "outputs": [
            {"sink_name": "output", "plugin": "json", "options": {}, "on_write_failure": "discard"},
        ],
    }


def _collector_node_spec() -> NodeSpec:
    return NodeSpec(
        id="page_stitcher",
        node_type="collector",
        plugin="batch_stats",
        input="pages",
        on_success="output",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        scope_name="document_pages",
        scope_opener="explode",
        scope_policy="require_all",
    )


class TestBinderRefusal:
    def test_collector_candidate_is_refused_with_the_closed_code(self) -> None:
        with pytest.raises(GuidedCandidateBindingRejected) as raised:
            bind_guided_reviewed_components(_collector_candidate(), _guided())

        assert raised.value.error_code == "guided_collector_not_authorable"
        message = str(raised.value)
        # The planner loop reclassifies binder complaints by this prefix; a
        # drifted message becomes a terminal 500 instead of a repair turn.
        assert message.startswith(_CANDIDATE_SHAPE_INTEGRITY_PREFIX)
        # The refusal must be actionable: say what is unavailable and what the
        # guided lane DOES author.
        assert "collector/scope authoring is not yet available in the guided lane" in message
        for kind in ("transform", "gate", "aggregation", "coalesce", "row_union", "queue"):
            assert kind in message
        assert raised.value.connectivity == {
            "component_kind": "nodes",
            "collector_node_ids": ["page_stitcher"],
        }

    def test_refusal_never_rewrites_or_strips_the_collector(self) -> None:
        candidate = _collector_candidate()
        with pytest.raises(GuidedCandidateBindingRejected):
            bind_guided_reviewed_components(candidate, _guided())
        # The rejected candidate is untouched — refusal, not silent repair.
        node_types = [node["node_type"] for node in candidate["nodes"]]  # type: ignore[index]
        assert node_types == ["transform", "collector"]

    def test_the_guard_is_what_produces_the_refusal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mutation-style probe: with the guard disabled, the same candidate
        binds — proving the refusal comes from the guard, not from a later
        rejection that happens to fire on this shape."""
        monkeypatch.setattr(guided_planning, "_reject_collector_candidate_nodes", lambda bound: None)

        bound = bind_guided_reviewed_components(_collector_candidate(), _guided())

        collector_nodes = [node for node in bound["nodes"] if node.get("node_type") == "collector"]
        assert [node["id"] for node in collector_nodes] == ["page_stitcher"]

    def test_collectorless_candidate_still_binds(self) -> None:
        candidate = _collector_candidate()
        transform = dict(candidate["nodes"][0])  # type: ignore[index]
        transform["on_success"] = "output"
        candidate["nodes"] = [transform]
        bound = bind_guided_reviewed_components(candidate, _guided())
        assert [node["node_type"] for node in bound["nodes"]] == ["transform"]


class TestProjectionTypedRejection:
    def test_collector_node_raises_typed_never_gate_misserialization(self) -> None:
        # The reviewer's probe shape: a collector NodeSpec driven straight into
        # the behavior dispatch must raise the module's typed integrity error —
        # under ``python -O`` a bare assert would vanish and mis-serialize the
        # collector AS A GATE.
        with pytest.raises(AuditIntegrityError, match="no behavior arm for node kind 'collector'"):
            _node_behavior(_collector_node_spec(), route_aliases={}, branch_aliases={})


class TestNoRegretsHardening:
    """Panel-authorized no-regrets slice (ruling comment 7878, elspeth-88bb77953c)."""

    def test_step_3_overlay_names_the_binder_refusal(self) -> None:
        # The lane-boundary sentence is BOUND to the binder's rejection: the
        # overlay must name the exact closed code the binder emits, so a
        # future rename of either side reddens here instead of drifting the
        # planner's brief away from the refusal it will actually receive.
        from elspeth.web.composer.guided.prompts import load_step_chat_skill, load_step_planner_skill

        with pytest.raises(GuidedCandidateBindingRejected) as raised:
            bind_guided_reviewed_components(_collector_candidate(), _guided())
        code = raised.value.error_code

        planner_overlay = load_step_planner_skill(GuidedStep.STEP_3_TRANSFORMS)
        chat_overlay = load_step_chat_skill(GuidedStep.STEP_3_TRANSFORMS)
        assert code in planner_overlay
        assert code in chat_overlay
        # The no-substitution posture rides with the boundary sentence.
        assert "substituting an aggregation" in planner_overlay

    def test_wire_cardinality_dispatch_fails_typed_for_a_collector(self) -> None:
        from elspeth.web.composer.guided.emitters import _node_cardinality
        from elspeth.web.composer.guided.errors import InvariantError

        with pytest.raises(InvariantError, match="no row-cardinality arm for node kind 'collector'"):
            _node_cardinality(_collector_node_spec(), executable_node=None)

    def test_protocol_node_flow_vocabularies_cannot_drift_apart(self) -> None:
        # Extending _NODE_TYPES (the WS6 lift scenario) without a flows arm
        # must fail HERE, not as a KeyError inside payload validation.
        from elspeth.web.composer.guided.protocol import _LEGAL_NODE_FLOWS, _NODE_TYPES

        assert set(_NODE_TYPES) == set(_LEGAL_NODE_FLOWS)

    def test_prose_revision_refuses_a_collector_bearing_predecessor(self) -> None:
        from elspeth.web.composer.guided.planning import GuidedRevisionAuthority, bind_guided_prose_revision_candidate
        from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec

        predecessor = CompositionState(
            sources={"source": SourceSpec(plugin="csv", on_success="pages", options={}, on_validation_failure="discard")},
            nodes=(_collector_node_spec(),),
            edges=(),
            outputs=(OutputSpec(name="output", plugin="json", options={}, on_write_failure="discard"),),
            metadata=PipelineMetadata(),
            version=1,
        )
        candidate = _collector_candidate()
        candidate["nodes"] = [candidate["nodes"][0]]  # collector-free candidate

        with pytest.raises(AuditIntegrityError, match="guided predecessor authority carries collector node"):
            bind_guided_prose_revision_candidate(
                candidate,
                _guided(),
                authority=GuidedRevisionAuthority(mode="amend", predecessor=predecessor),
            )

    def test_predecessor_guard_names_the_offending_nodes(self) -> None:
        from elspeth.web.composer.guided.planning import _require_collector_free_predecessor
        from elspeth.web.composer.state import CompositionState, PipelineMetadata

        state = CompositionState(
            source=None,
            nodes=(_collector_node_spec(),),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        with pytest.raises(AuditIntegrityError, match="page_stitcher"):
            _require_collector_free_predecessor(state)
