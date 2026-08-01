"""Auto-wiring of deployment-REQUIRED controls with acknowledgeable disclosure.

R2-F10 (elspeth-f99655f540): required-control enforcement was prose-only at
authoring time and blocking only at execution, so the compose loop shipped
uncovered graphs that wedged at the run gate. The operator product decision is
auto-wire + disclose: ``wire_required_controls`` splices the deployment-SELECTED
implementation onto each offending edge reported by ``control_coverage_findings``
(the single coverage authority), stages a pending ``pipeline_decision``
disclosure per inserted node (user_term ``required_control_auto_wired``), and
surfaces a ``policy_control`` implicit-decision entry. An already-covered graph
must pass through untouched (idempotence), and a REQUIRED-but-unselected
capability inserts nothing — that is the operator's problem, not authorable.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.implicit_decisions import build_implicit_decisions_report
from elspeth.web.composer.required_controls import wire_required_controls
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tools import build_set_pipeline_candidate
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.interpretation_state import (
    REGISTERED_PIPELINE_DECISION_USER_TERMS,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    pipeline_decision_artifact_hash,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_planner_authoring_aids import (
    _custody_context,
    _direct_control_view,
    _empty_state,
    _guardrail_profile_view,
)

_INLINE_CONTENT = "ticket_id,body\nT-1001,Cannot log in since the update\n"


def _bare_llm_candidate(**llm_option_overrides: Any) -> dict[str, Any]:
    """csv -> llm -> json with NO controls: both coverage findings fire."""
    options: dict[str, Any] = {
        "profile": "sonnet",
        "prompt_template": "Assess support ticket {{ row.ticket_id }}: {{ row.body }}. Reply briefly.",
        "required_input_fields": ["ticket_id", "body"],
        "response_field": "assessment",
        "schema": {"mode": "observed"},
    }
    options.update(llm_option_overrides)
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "schema": {
                    "mode": "flexible",
                    "fields": ["ticket_id: str", "body: str"],
                    "guaranteed_fields": ["ticket_id", "body"],
                }
            },
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "support_tickets.csv",
                "mime_type": "text/csv",
                "content": _INLINE_CONTENT,
                "description": "Literal rows the user pasted into chat",
            },
        },
        "nodes": [
            {
                "id": "assess_ticket",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "assessed",
                "on_error": "discard",
                "options": options,
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "assessed",
                "plugin": "json",
                "options": {
                    "path": "outputs/assessments.json",
                    "format": "json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Assess tickets", "description": "One llm assessment per ticket."},
    }


def _nodes_by_id(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in candidate["nodes"]}


def _disclosure_rows(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row
        for row in node.get("options", {}).get("interpretation_requirements", [])
        if row.get("user_term") == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
    ]


class TestAutoWireSplicing:
    def test_selected_controls_are_spliced_on_the_offending_edges(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate()
        original = copy.deepcopy(candidate)

        wired = wire_required_controls(candidate, snapshot, view)

        assert candidate == original, "input candidate must never be mutated in place"
        nodes = _nodes_by_id(wired)
        assert set(nodes) == {"assess_ticket", "prompt_shield_auto_1", "content_safety_auto_1"}

        shield = nodes["prompt_shield_auto_1"]
        assert shield["plugin"] == "aws_bedrock_prompt_shield"
        assert shield["node_type"] == "transform"
        assert shield["input"] == "rows"
        assert shield["on_error"] == "discard"
        assert nodes["assess_ticket"]["input"] == shield["on_success"]
        assert shield["options"]["profile"] == "prompt-approved"
        assert shield["options"]["fields"] == ["body", "ticket_id"]

        safety = nodes["content_safety_auto_1"]
        assert safety["plugin"] == "aws_bedrock_content_safety"
        assert safety["on_success"] == "assessed"
        assert safety["on_error"] == "discard"
        assert nodes["assess_ticket"]["on_success"] == safety["input"]
        assert safety["options"]["profile"] == "content-approved"
        assert safety["options"]["fields"] == ["assessment"]
        assert safety["options"]["source"] == "OUTPUT"

        # Everything the pass does not own is untouched.
        assert wired["source"] == original["source"]
        assert wired["outputs"] == original["outputs"]
        assert wired["metadata"] == original["metadata"]

    def test_each_inserted_node_stages_the_disclosure_review(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)

        wired = wire_required_controls(_bare_llm_candidate(), snapshot, view)

        nodes = _nodes_by_id(wired)
        for node_id in ("prompt_shield_auto_1", "content_safety_auto_1"):
            rows = _disclosure_rows(nodes[node_id])
            assert len(rows) == 1, f"{node_id} must stage exactly one disclosure row"
            row = rows[0]
            assert row["kind"] == "pipeline_decision"
            assert row["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
            assert row["draft"].strip()
            assert "assess_ticket" in row["draft"]
        # The llm node itself carries no disclosure row — the decision is the
        # inserted node, and the row rides on it.
        assert _disclosure_rows(nodes["assess_ticket"]) == []

    def test_wired_candidate_builds_and_passes_required_control_coverage(self, tmp_path: Path) -> None:
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _guardrail_profile_view(tmp_path)
        bare = _bare_llm_candidate()
        context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)

        wired = wire_required_controls(bare, snapshot, view)
        candidate = build_set_pipeline_candidate(wired, _empty_state(), context)
        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"auto-wired candidate rejected: {rejection}"

        result = view.validate_authored_state(candidate.result.updated_state)
        coverage = [finding for finding in result.findings if finding.stage == "required_control_coverage"]
        assert coverage == [], [finding.message for finding in coverage]

    def test_direct_config_controls_author_the_exemplar_direct_options(self, tmp_path: Path) -> None:
        """Alias-less (Azure) controls get secret_ref + placeholder service bindings."""
        view, snapshot = _direct_control_view(tmp_path)

        wired = wire_required_controls(_bare_llm_candidate(), snapshot, view)

        nodes = _nodes_by_id(wired)
        shield = nodes["prompt_shield_auto_1"]
        safety = nodes["content_safety_auto_1"]
        assert shield["plugin"] == "azure_prompt_shield"
        assert safety["plugin"] == "azure_content_safety"
        for control in (shield, safety):
            assert "profile" not in control["options"]
            assert control["options"]["api_key"] == {"secret_ref": "AZURE_CONTENT_SAFETY_KEY"}
            assert control["options"]["endpoint"]
        # Effective blocking posture — all-6 thresholds are a coverage no-op.
        assert any(value < 6 for value in safety["options"]["thresholds"].values())


class TestAutoWireIdempotence:
    def test_second_pass_is_a_no_op(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)

        wired = wire_required_controls(_bare_llm_candidate(), snapshot, view)
        rewired = wire_required_controls(wired, snapshot, view)

        assert rewired == wired
        assert len(rewired["nodes"]) == len(wired["nodes"])

    def test_already_covered_graph_is_returned_unchanged(self, tmp_path: Path) -> None:
        from elspeth.web.composer.planner_authoring_aids import fork_coalesce_exemplar_args

        view, snapshot = _guardrail_profile_view(tmp_path)
        covered = fork_coalesce_exemplar_args(view)
        assert covered is not None

        result = wire_required_controls(covered, snapshot, view)

        assert result == covered


class TestAutoWireRefusals:
    def test_recommend_mode_inserts_nothing(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")
        candidate = _bare_llm_candidate()

        assert wire_required_controls(candidate, snapshot, view) == candidate

    def test_required_but_unselected_inserts_nothing(self) -> None:
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

        catalog = create_catalog_service()
        trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        unselected = PluginAvailabilitySnapshot.create(
            policy_hash="autowire-required-unselected",
            principal_scope="local:autowire-required-unselected",
            available=trained.available,
            unavailable=(),
            selected=tuple(
                (capability, plugin)
                for capability, plugin in dict(trained.selected).items()
                if capability not in (PluginCapability.PROMPT_SHIELD, PluginCapability.CONTENT_SAFETY)
            ),
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="autowire-required-unselected",
            control_modes=(
                (PluginCapability.PROMPT_SHIELD, ControlMode.REQUIRED),
                (PluginCapability.CONTENT_SAFETY, ControlMode.REQUIRED),
            ),
        )
        view = PolicyCatalogView(catalog, unselected, MagicMock(spec=OperatorProfileRegistry))
        candidate = _bare_llm_candidate()

        assert wire_required_controls(candidate, unselected, view) == candidate

    def test_unprovable_prompt_fields_leave_the_input_finding_alone(self, tmp_path: Path) -> None:
        """A dynamic row access defeats field-scoped shielding: insert nothing on
        the input edge (the scope repair is the author's), but the output edge is
        independent and still gets its safety control."""
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate(
            prompt_template="Assess {{ row[key] }} for ticket {{ row.ticket_id }}.",
        )

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(wired)
        assert "prompt_shield_auto_1" not in nodes
        assert "content_safety_auto_1" in nodes

    def test_error_route_quarantine_sink_is_not_authored_around(self, tmp_path: Path) -> None:
        """on_error naming a sink is the operator-decision case: the pass wires
        the on_success edge and leaves the error route exactly as authored."""
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate()
        candidate["nodes"][0]["on_error"] = "quarantine"
        candidate["outputs"].append(
            {
                "sink_name": "quarantine",
                "plugin": "json",
                "options": {
                    "path": "outputs/quarantine.json",
                    "format": "json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        )

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(wired)
        assert nodes["assess_ticket"]["on_error"] == "quarantine"
        assert "content_safety_auto_1" in nodes
        # Exactly one safety node — the residual error-route finding must not
        # provoke splice churn.
        safety_nodes = [node for node in nodes.values() if node.get("plugin") == "aws_bedrock_content_safety"]
        assert len(safety_nodes) == 1

    def test_malformed_candidate_is_returned_unchanged_without_raising(self, tmp_path: Path) -> None:
        """The pass runs inside the planner finalizer seam (T1): it must never
        raise on a malformed candidate — downstream validation owns rejection."""
        view, snapshot = _guardrail_profile_view(tmp_path)
        for malformed in (
            {"nodes": "not-a-list", "edges": [], "outputs": []},
            {"nodes": [17], "edges": [], "outputs": []},
            {"nodes": [{"id": "x"}], "edges": [], "outputs": "nope"},
            {},
        ):
            assert wire_required_controls(malformed, snapshot, view) == malformed

    def test_deterministic_ids_skip_authored_collisions(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate()
        candidate["nodes"].insert(
            0,
            {
                "id": "prompt_shield_auto_1",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "rows_passed",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            },
        )
        candidate["nodes"][1]["input"] = "rows_passed"

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(wired)
        assert nodes["prompt_shield_auto_1"]["plugin"] == "passthrough"
        assert nodes["prompt_shield_auto_2"]["plugin"] == "aws_bedrock_prompt_shield"


class TestDisclosureRegistry:
    def test_user_term_is_registered(self) -> None:
        assert REQUIRED_CONTROL_AUTO_WIRED_USER_TERM in REGISTERED_PIPELINE_DECISION_USER_TERMS

    def test_artifact_hash_binds_to_the_inserted_edge(self) -> None:
        def _control(node_id: str, *, input_stream: str) -> NodeSpec:
            return NodeSpec(
                id=node_id,
                node_type="transform",
                plugin="aws_bedrock_prompt_shield",
                input=input_stream,
                on_success=f"{node_id}_out",
                on_error="discard",
                options={"profile": "prompt-approved", "fields": ["body"], "schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )

        node = _control("prompt_shield_auto_1", input_stream="rows")
        same = _control("prompt_shield_auto_1", input_stream="rows")
        moved = _control("prompt_shield_auto_1", input_stream="other_rows")
        assert pipeline_decision_artifact_hash(
            node, (node,), user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        ) == pipeline_decision_artifact_hash(same, (same,), user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)
        assert pipeline_decision_artifact_hash(
            node, (node,), user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        ) != pipeline_decision_artifact_hash(moved, (moved,), user_term=REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)


class TestImplicitDecisionDisclosure:
    def test_auto_wired_node_yields_a_policy_control_entry(self) -> None:
        control = NodeSpec(
            id="prompt_shield_auto_1",
            node_type="transform",
            plugin="aws_bedrock_prompt_shield",
            input="rows",
            on_success="prompt_shield_auto_1_out",
            on_error="discard",
            options={
                "profile": "prompt-approved",
                "fields": ["body", "ticket_id"],
                "schema": {"mode": "observed"},
                "interpretation_requirements": [
                    {
                        "id": f"{REQUIRED_CONTROL_AUTO_WIRED_USER_TERM}:prompt_shield_auto_1",
                        "kind": "pipeline_decision",
                        "user_term": REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
                        "status": "pending",
                        "draft": "auto-wired disclosure",
                        "event_id": None,
                        "accepted_value": None,
                        "accepted_artifact_hash": None,
                        "resolved_prompt_template_hash": None,
                    }
                ],
            },
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(
            nodes=(control,),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        report = build_implicit_decisions_report(state)

        entries = [entry for entry in report["entries"] if entry["category"] == "policy_control"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["path"] == "node.prompt_shield_auto_1.auto_wired_control"
        assert entry["value"] == "aws_bedrock_prompt_shield"
        assert entry["provenance"] == "policy_required"

    def test_hand_authored_control_yields_no_policy_control_entry(self) -> None:
        control = NodeSpec(
            id="my_shield",
            node_type="transform",
            plugin="aws_bedrock_prompt_shield",
            input="rows",
            on_success="shielded",
            on_error="discard",
            options={"profile": "prompt-approved", "fields": ["body"], "schema": {"mode": "observed"}},
            condition=None,
            routes=None,
            fork_to=None,
            branches=None,
            policy=None,
            merge=None,
        )
        state = CompositionState(nodes=(control,), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)

        report = build_implicit_decisions_report(state)

        assert [entry for entry in report["entries"] if entry["category"] == "policy_control"] == []
