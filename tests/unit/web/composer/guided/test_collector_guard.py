"""Guided-lane collector PROJECTION pin (WS6 lift of the interim guard, ruling 7878).

INVERTED from the refusal pin (elspeth-88bb77953c): the interim binder guard
(`_reject_collector_candidate_nodes`) and the predecessor guard
(`_require_collector_free_predecessor`) are REMOVED — the guided lane now
authors collectors and the proposal projection carries a live collector
behavior arm. These tests pin the projection (authorable ⇒ reviewable, Q8-1):
the binder accepts collector-bearing candidates, `_node_behavior` projects the
scope binding by opener identity + closed policy, the wire cardinality arm is
live, the protocol vocabularies carry the collector, and the retired
`guided_collector_not_authorable` code no longer exists anywhere in the
catalogue. Fixture idiom copied file-locally from
``test_bind_reviewed_components.py`` / ``test_propose_pipeline_protocol.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.emitters import _node_cardinality
from elspeth.web.composer.guided.planning import (
    GuidedRevisionAuthority,
    _node_behavior,
    bind_guided_prose_revision_candidate,
    bind_guided_reviewed_components,
)
from elspeth.web.composer.guided.protocol import (
    _LEGAL_NODE_FLOWS,
    _NODE_TYPES,
    PROPOSAL_RATIONALE_TEMPLATE,
    PROPOSAL_SUMMARY_TEMPLATE,
    GuidedStep,
    TurnType,
    _validate_node_behavior,
    proposal_component_label,
    validate_payload,
)
from elspeth.web.composer.guided.resolved import SinkOutputResolved, SourceResolved
from elspeth.web.composer.guided.state_machine import GuidedSession
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


def _collector_node_spec(*, scope_policy: str = "require_all") -> NodeSpec:
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
        scope_policy=scope_policy,
    )


class TestBinderAcceptance:
    """The lift itself: the binder no longer refuses collector candidates."""

    def test_collector_candidate_binds_with_the_collector_intact(self) -> None:
        bound = bind_guided_reviewed_components(_collector_candidate(), _guided())

        collector_nodes = [node for node in bound["nodes"] if node.get("node_type") == "collector"]
        assert [node["id"] for node in collector_nodes] == ["page_stitcher"]
        # The binder binds, never rewrites: the scope binding the planner
        # authored survives byte-identical into the bound candidate.
        (collector,) = collector_nodes
        assert collector["scope_name"] == "document_pages"
        assert collector["scope_opener"] == "explode"
        assert collector["scope_policy"] == "require_all"

    def test_prose_revision_accepts_a_collector_bearing_predecessor(self) -> None:
        # INVERSION of the old predecessor-guard pin: a collector in the
        # sealed predecessor is legitimate authored structure now, and amend
        # reconstruction restores it server-side like any other node — never
        # an AuditIntegrityError.
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
        candidate["nodes"] = [candidate["nodes"][0]]  # type: ignore[index]  # collector-free candidate

        result = bind_guided_prose_revision_candidate(
            candidate,
            _guided(),
            authority=GuidedRevisionAuthority(mode="amend", predecessor=predecessor),
        )

        # The dropped collector is a repairable amend-contract disposition
        # (the reconstruction restores predecessor authority), not a refusal.
        assert result.rejection_code == "guided_amend_contract_violation"
        restored = [node for node in result.pipeline["nodes"] if node.get("node_type") == "collector"]
        assert [node["id"] for node in restored] == ["page_stitcher"]


class TestProjectionArm:
    """Q8-1 blocking definition of done: authorable ⇒ reviewable."""

    def test_collector_behavior_projects_opener_identity_and_policy(self) -> None:
        behavior = _node_behavior(
            _collector_node_spec(),
            route_aliases={},
            branch_aliases={},
            collector_opener_stable_id="00000000-0000-4000-8000-000000000404",
        )
        assert behavior == {
            "kind": "collector",
            "opener_stable_id": "00000000-0000-4000-8000-000000000404",
            "policy": "require_all",
        }

    def test_collector_behavior_carries_the_authored_policy_not_a_default(self) -> None:
        behavior = _node_behavior(
            _collector_node_spec(scope_policy="best_effort"),
            route_aliases={},
            branch_aliases={},
            collector_opener_stable_id="00000000-0000-4000-8000-000000000404",
        )
        assert behavior["policy"] == "best_effort"

    def test_unresolvable_opener_fails_typed(self) -> None:
        with pytest.raises(AuditIntegrityError, match="could not resolve the scope opener"):
            _node_behavior(_collector_node_spec(), route_aliases={}, branch_aliases={})

    def test_wire_cardinality_arm_is_live(self) -> None:
        # The typed refusal is gone: a collector renders the batch-in shape
        # (the opener's whole EXPAND group) with an unconstrained output count.
        assert _node_cardinality(_collector_node_spec(), executable_node=None) == {
            "input": "batch",
            "output": "zero_or_many",
            "expected_output_count": None,
        }


class TestProtocolVocabularies:
    def test_collector_is_in_the_node_and_flow_vocabularies(self) -> None:
        assert "collector" in _NODE_TYPES
        assert _LEGAL_NODE_FLOWS["collector"] == frozenset({"node_success", "node_error"})

    def test_node_flow_vocabularies_cannot_drift_apart(self) -> None:
        assert set(_NODE_TYPES) == set(_LEGAL_NODE_FLOWS)

    def test_collector_behavior_validates_exactly(self) -> None:
        valid = {
            "kind": "collector",
            "opener_stable_id": "00000000-0000-4000-8000-000000000404",
            "policy": "require_all",
        }
        assert _validate_node_behavior("collector", valid, "payload.nodes[1]") is None
        assert _validate_node_behavior("collector", {**valid, "policy": "quorum"}, "n") is not None
        assert _validate_node_behavior("collector", {**valid, "policy": None}, "n") is not None
        assert _validate_node_behavior("collector", {**valid, "opener_stable_id": "explode"}, "n") is not None
        assert _validate_node_behavior("collector", {**valid, "scope_name": "document_pages"}, "n") is not None
        missing = {"kind": "collector", "policy": "require_all"}
        assert _validate_node_behavior("collector", missing, "n") is not None


PROPOSAL_ID = "00000000-0000-4000-8000-000000000401"
P_SOURCE_ID = "00000000-0000-4000-8000-000000000402"
EXPLODE_ID = "00000000-0000-4000-8000-000000000404"
COLLECTOR_ID = "00000000-0000-4000-8000-000000000407"
P_OUTPUT_ID = "00000000-0000-4000-8000-000000000405"
EDGE_IDS = [f"00000000-0000-4000-8000-00000000041{index}" for index in range(6)]
DRAFT_HASH = "d" * 64


def _collector_propose_payload() -> dict[str, Any]:
    return {
        "proposal_id": PROPOSAL_ID,
        "draft_hash": DRAFT_HASH,
        "supersedes_draft_hash": None,
        "summary": PROPOSAL_SUMMARY_TEMPLATE,
        "rationale": PROPOSAL_RATIONALE_TEMPLATE,
        "component_counts": {"sources": 1, "nodes": 2, "edges": 6, "outputs": 1},
        "blockers": [],
        "graph": {
            "sources": [
                {
                    "stable_id": P_SOURCE_ID,
                    "label": proposal_component_label("source", 0),
                    "plugin": {"kind": "source", "id": "csv"},
                }
            ],
            "edges": [
                {
                    "stable_id": EDGE_IDS[0],
                    "from_endpoint": {"kind": "source", "stable_id": P_SOURCE_ID},
                    "to_endpoint": {"kind": "node", "stable_id": EXPLODE_ID},
                    "flow": {"kind": "source_success", "branch": None},
                },
                {
                    "stable_id": EDGE_IDS[1],
                    "from_endpoint": {"kind": "source", "stable_id": P_SOURCE_ID},
                    "to_endpoint": {"kind": "discard"},
                    "flow": {"kind": "source_validation_failure"},
                },
                {
                    "stable_id": EDGE_IDS[2],
                    "from_endpoint": {"kind": "node", "stable_id": EXPLODE_ID},
                    "to_endpoint": {"kind": "node", "stable_id": COLLECTOR_ID},
                    "flow": {"kind": "node_success", "branch": None},
                },
                {
                    "stable_id": EDGE_IDS[3],
                    "from_endpoint": {"kind": "node", "stable_id": EXPLODE_ID},
                    "to_endpoint": {"kind": "discard"},
                    "flow": {"kind": "node_error"},
                },
                {
                    "stable_id": EDGE_IDS[4],
                    "from_endpoint": {"kind": "node", "stable_id": COLLECTOR_ID},
                    "to_endpoint": {"kind": "output", "stable_id": P_OUTPUT_ID},
                    "flow": {"kind": "node_success", "branch": None},
                },
                {
                    "stable_id": EDGE_IDS[5],
                    "from_endpoint": {"kind": "output", "stable_id": P_OUTPUT_ID},
                    "to_endpoint": {"kind": "discard"},
                    "flow": {"kind": "output_write_failure"},
                },
            ],
        },
        "nodes": [
            {
                "stable_id": EXPLODE_ID,
                "label": proposal_component_label("node", 0),
                "node_type": "transform",
                "plugin": {"kind": "transform", "id": "json_explode"},
                "behavior": {"kind": "transform"},
                "node_options_summary": [],
            },
            {
                "stable_id": COLLECTOR_ID,
                "label": proposal_component_label("node", 1),
                "node_type": "collector",
                "plugin": {"kind": "transform", "id": "batch_stats"},
                "behavior": {
                    "kind": "collector",
                    "opener_stable_id": EXPLODE_ID,
                    "policy": "require_all",
                },
                "node_options_summary": [],
            },
        ],
        "outputs": [
            {
                "stable_id": P_OUTPUT_ID,
                "label": proposal_component_label("output", 0),
                "plugin": {"kind": "sink", "id": "json"},
            }
        ],
        "edit_targets": [
            {"kind": "source", "stable_id": P_SOURCE_ID},
            {"kind": "node", "stable_id": EXPLODE_ID},
            {"kind": "node", "stable_id": COLLECTOR_ID},
            {"kind": "output", "stable_id": P_OUTPUT_ID},
        ],
    }


class TestProposePayloadContract:
    """The wire contract: a collector-bearing proposal is a valid projection."""

    def test_collector_proposal_validates(self) -> None:
        assert validate_payload(TurnType.PROPOSE_PIPELINE, _collector_propose_payload()) is None

    def test_collector_on_error_flow_is_optional_but_bounded(self) -> None:
        # No node_error flow on the collector above: valid (the scope's group
        # machinery owns the failure route, ADR-042 §6). A second success flow
        # is not.
        payload = _collector_propose_payload()
        payload["graph"]["edges"].append(
            {
                "stable_id": "00000000-0000-4000-8000-000000000420",
                "from_endpoint": {"kind": "node", "stable_id": COLLECTOR_ID},
                "to_endpoint": {"kind": "output", "stable_id": P_OUTPUT_ID},
                "flow": {"kind": "node_success", "branch": None},
            }
        )
        payload["component_counts"]["edges"] = 7
        assert validate_payload(TurnType.PROPOSE_PIPELINE, payload) is not None

    def test_collector_opener_must_resolve_to_a_payload_node(self) -> None:
        payload = _collector_propose_payload()
        payload["nodes"][1]["behavior"]["opener_stable_id"] = "00000000-0000-4000-8000-0000000004ff"
        error = validate_payload(TurnType.PROPOSE_PIPELINE, payload)
        assert error is not None and "opener" in error

    def test_collector_requires_a_transform_plugin_ref(self) -> None:
        payload = _collector_propose_payload()
        payload["nodes"][1]["plugin"] = None
        assert validate_payload(TurnType.PROPOSE_PIPELINE, payload) is not None


class TestRetiredRefusal:
    """Q6: the closed code retired WITH its catalogue and teaching surfaces."""

    def test_the_error_code_is_retired_from_the_closed_catalogue(self) -> None:
        from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES, _VALIDATION_ERROR_PATTERNS

        assert "guided_collector_not_authorable" not in _CLOSED_VALIDATION_ERROR_CODES
        assert not any("guided_collector_not_authorable" in pattern for pattern, _explanation, _fix in _VALIDATION_ERROR_PATTERNS)

    def test_the_binder_no_longer_carries_a_collector_guard(self) -> None:
        from elspeth.web.composer.guided import planning as guided_planning

        assert not hasattr(guided_planning, "_reject_collector_candidate_nodes")
        assert not hasattr(guided_planning, "_require_collector_free_predecessor")

    def test_step_3_overlay_teaches_the_collector_instead_of_the_exclusion(self) -> None:
        from elspeth.web.composer.guided.prompts import load_step_chat_skill, load_step_planner_skill

        planner_overlay = load_step_planner_skill(GuidedStep.STEP_3_TRANSFORMS)
        chat_overlay = load_step_chat_skill(GuidedStep.STEP_3_TRANSFORMS)
        for overlay in (planner_overlay, chat_overlay):
            assert "guided_collector_not_authorable" not in overlay
            assert "scope_opener" in overlay
            assert "scope_policy" in overlay
        # The anti-substitution posture survives the inversion: the planner
        # authors the collector rather than degrading the intent.
        assert "substituting an aggregation" in planner_overlay
