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

import pytest

from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.sources.llm.source import LLMSource
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


@pytest.fixture
def llm_source_policy_manager(monkeypatch: pytest.MonkeyPatch) -> PluginManager:
    """Give the coverage authority an isolated canonical plugin registry."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    assert manager.get_source_by_name("llm") is LLMSource
    monkeypatch.setattr("elspeth.web.plugin_policy.coverage.get_shared_plugin_manager", lambda: manager)
    monkeypatch.setattr("elspeth.web.composer.required_controls.get_shared_plugin_manager", lambda: manager)
    return manager


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


def _bare_llm_source_candidate(
    *,
    container: str = "source",
    source_name: str = "source",
    on_validation_failure: object = "discard",
    response_field: str = "briefing",
) -> dict[str, Any]:
    """One source-native prompt routed directly to a JSON sink."""
    source = {
        "plugin": "llm",
        "on_success": "generated",
        "options": {
            "profile": "sonnet",
            "prompt_template": "Write one concise audit briefing.",
            "response_field": response_field,
            "schema": {"mode": "observed"},
        },
        "on_validation_failure": on_validation_failure,
    }
    candidate: dict[str, Any] = {
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "generated",
                "plugin": "json",
                "options": {
                    "path": "outputs/generated.json",
                    "format": "json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Generate briefing", "description": "One authored LLM prompt."},
    }
    if container == "source":
        candidate["source"] = source
    else:
        candidate["sources"] = {source_name: source}
    return candidate


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

    def test_double_shield_disclosure_names_the_scope_mismatch(self, tmp_path: Path) -> None:
        """When an under-scoped shield already dominates the edge (coverage
        populates scanned_fields), the second shield's disclosure must name the
        scanned-vs-protected mismatch so a two-shield graph is explicable."""
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate()
        candidate["nodes"].insert(
            0,
            {
                "id": "narrow_shield",
                "node_type": "transform",
                "plugin": "aws_bedrock_prompt_shield",
                "input": "rows",
                "on_success": "narrow_shielded",
                "on_error": "discard",
                "options": {
                    "profile": "prompt-approved",
                    "fields": ["ticket_id"],
                    "schema": {"mode": "observed"},
                },
            },
        )
        candidate["nodes"][1]["input"] = "narrow_shielded"

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(wired)
        draft = _disclosure_rows(nodes["prompt_shield_auto_1"])[0]["draft"]
        assert "scans only [ticket_id]" in draft
        assert "reads [body, ticket_id]" in draft

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

    def test_alias_backed_selection_still_wires(self, tmp_path: Path) -> None:
        """A REAL selection — operator profile alias — is auto-wired. (The full
        splice-shape assertions live in the first test of this class; this one
        exists as the explicit positive counterpart to the placeholder refusal.)"""
        view, snapshot = _guardrail_profile_view(tmp_path)

        wired = wire_required_controls(_bare_llm_candidate(), snapshot, view)

        nodes = _nodes_by_id(wired)
        assert nodes["prompt_shield_auto_1"]["options"]["profile"] == "prompt-approved"
        assert nodes["content_safety_auto_1"]["options"]["profile"] == "content-approved"

    def test_placeholder_dependent_direct_selection_inserts_nothing(self, tmp_path: Path) -> None:
        """SECURITY (review finding 1): an alias-less selection whose required
        service bindings only exist as placeholder exemplars must NOT be wired —
        a placeholder Azure endpoint (suffix-only validated, third-party
        registrable) baked into real node config would clear the coverage gate
        while pointing a live secret_ref at an unowned resource. The pass
        treats the selection as REQUIRED-but-unselected: no insertion, coverage
        finding preserved."""
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _direct_control_view(tmp_path)
        candidate = _bare_llm_candidate()

        assert wire_required_controls(candidate, snapshot, view) is candidate

        # The finding stays for the author/operator — never a done-looking
        # disclosure over placeholder config.
        context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
        built = build_set_pipeline_candidate(candidate, _empty_state(), context)
        assert built.acceptable is True, (built.result.data or {}).get("error")
        result = view.validate_authored_state(built.result.updated_state)
        coverage = [finding for finding in result.findings if finding.stage == "required_control_coverage"]
        assert coverage, "the required-control coverage finding must be preserved"

    def test_direct_options_deployability_predicate(self, tmp_path: Path) -> None:
        """Placeholder-dependent plugins are refused; secret-only direct
        bindings (real deployment values by construction) are deployable."""
        from types import SimpleNamespace

        from elspeth.web.composer.planner_authoring_aids import (
            _direct_control_options_are_deployable,
            _plugin_summaries,
        )

        view, _snapshot = _direct_control_view(tmp_path)
        summaries = _plugin_summaries(view)
        assert _direct_control_options_are_deployable(summaries, "azure_prompt_shield") is False
        assert _direct_control_options_are_deployable(summaries, "azure_content_safety") is False

        secret_only = {
            "transform": [
                SimpleNamespace(
                    name="secret_only_control",
                    config_fields=[
                        SimpleNamespace(name="api_key", required=True),
                        SimpleNamespace(name="fields", required=True),
                        SimpleNamespace(name="schema", required=True),
                        SimpleNamespace(name="verbose", required=False),
                    ],
                    secret_requirements=[SimpleNamespace(field="api_key", candidates=("CONTROL_KEY",))],
                )
            ]
        }
        assert _direct_control_options_are_deployable(secret_only, "secret_only_control") is True


class TestLLMSourceAutoWireSplicing:
    def test_legacy_singular_source_is_spliced_without_changing_container_provenance(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate()
        original = copy.deepcopy(candidate)

        wired = wire_required_controls(candidate, snapshot, view)

        assert candidate == original, "the persisted proposal must not be mutated"
        assert "source" in wired
        assert "sources" not in wired
        source = wired["source"]
        assert source["on_success"] == "content_safety_auto_1_in"
        nodes = _nodes_by_id(dict(wired))
        assert set(nodes) == {"content_safety_auto_1"}
        safety = nodes["content_safety_auto_1"]
        assert safety["plugin"] == "aws_bedrock_content_safety"
        assert safety["input"] == source["on_success"]
        assert safety["on_success"] == "generated"
        assert safety["options"]["fields"] == ["briefing"]
        assert safety["options"]["source"] == "OUTPUT"
        assert "llm source 'source'" in _disclosure_rows(safety)[0]["draft"]
        assert all(node["plugin"] != "aws_bedrock_prompt_shield" for node in nodes.values())

    @pytest.mark.parametrize("source_name", ["source", "briefing"])
    def test_plural_source_preserves_conventional_and_arbitrary_source_names(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
        source_name: str,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(container="sources", source_name=source_name)

        wired = wire_required_controls(candidate, snapshot, view)

        assert "source" not in wired
        assert set(wired["sources"]) == {source_name}
        assert wired["sources"][source_name]["on_success"] == "content_safety_auto_1_in"
        safety = _nodes_by_id(dict(wired))["content_safety_auto_1"]
        expected_component_id = "source" if source_name == "source" else f"source:{source_name}"
        assert expected_component_id in _disclosure_rows(safety)[0]["draft"]

    def test_dual_source_containers_are_ambiguous_and_returned_unchanged_by_identity(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate()
        candidate["sources"] = {}

        assert wire_required_controls(candidate, snapshot, view) is candidate

    @pytest.mark.parametrize("failure_route", ["quarantine", "", None, 17])
    def test_non_discard_or_malformed_validation_failure_refuses_the_entire_candidate(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
        failure_route: object,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(on_validation_failure=failure_route)

        assert wire_required_controls(candidate, snapshot, view) is candidate

    def test_validation_failure_requires_an_exact_builtin_discard_string(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        class DiscardAlias(str):
            pass

        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(on_validation_failure=DiscardAlias("discard"))

        assert wire_required_controls(candidate, snapshot, view) is candidate

    def test_second_pass_is_identity_preserving_and_does_not_duplicate_disclosure(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)

        wired = wire_required_controls(_bare_llm_source_candidate(), snapshot, view)
        rewired = wire_required_controls(wired, snapshot, view)

        assert rewired is wired
        safety = _nodes_by_id(dict(wired))["content_safety_auto_1"]
        assert len(_disclosure_rows(safety)) == 1

    def test_multiple_named_llm_sources_each_receive_one_content_safety_control(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(container="sources", source_name="alpha", response_field="alpha_text")
        candidate["sources"]["alpha"]["on_success"] = "alpha_rows"
        candidate["sources"]["beta"] = {
            **copy.deepcopy(candidate["sources"]["alpha"]),
            "on_success": "beta_rows",
            "options": {
                **copy.deepcopy(candidate["sources"]["alpha"]["options"]),
                "response_field": "beta_text",
            },
        }
        candidate["sources"]["records"] = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "records.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        }
        candidate["outputs"] = [
            {**copy.deepcopy(candidate["outputs"][0]), "sink_name": name} for name in ("alpha_rows", "beta_rows", "rows")
        ]

        wired = wire_required_controls(candidate, snapshot, view)

        safety_nodes = [node for node in wired["nodes"] if node.get("plugin") == "aws_bedrock_content_safety"]
        assert [node["id"] for node in safety_nodes] == ["content_safety_auto_1", "content_safety_auto_2"]
        assert {tuple(node["options"]["fields"]) for node in safety_nodes} == {("alpha_text",), ("beta_text",)}
        assert wired["sources"]["records"] is candidate["sources"]["records"]
        assert wired["sources"]["records"]["on_success"] == "rows"

    def test_source_and_transform_findings_are_repaired_in_one_bounded_pass(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_candidate()
        candidate["sources"] = {
            "briefing": _bare_llm_source_candidate(container="sources", source_name="briefing")["sources"]["briefing"],
            "records": candidate["source"],
        }
        del candidate["source"]
        candidate["sources"]["briefing"]["on_success"] = "briefing_rows"
        candidate["outputs"].append({**copy.deepcopy(candidate["outputs"][0]), "sink_name": "briefing_rows"})

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(dict(wired))
        assert {node["plugin"] for node in nodes.values()} >= {
            "llm",
            "aws_bedrock_prompt_shield",
            "aws_bedrock_content_safety",
        }
        assert len([node for node in nodes.values() if node["plugin"] == "aws_bedrock_prompt_shield"]) == 1
        assert len([node for node in nodes.values() if node["plugin"] == "aws_bedrock_content_safety"]) == 2

    def test_source_splice_ids_skip_reserved_node_and_stream_collisions(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(container="sources", source_name="briefing")
        candidate["nodes"].append(
            {
                "id": "authored_passthrough",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "unrelated",
                "on_success": "content_safety_auto_1_in",
                "on_error": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        )

        wired = wire_required_controls(candidate, snapshot, view)

        nodes = _nodes_by_id(dict(wired))
        assert "content_safety_auto_2" in nodes
        assert nodes["content_safety_auto_2"]["on_success"] == "generated"

    def test_source_splice_budget_bounds_a_non_converging_coverage_authority(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from elspeth.contracts.plugin_capabilities import ControlRole
        from elspeth.web.composer import required_controls as required_controls_module
        from elspeth.web.plugin_policy.coverage import ControlCoverageFinding

        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate()

        def repeated_finding(_state: CompositionState, capability: PluginCapability):
            if capability is not PluginCapability.CONTENT_SAFETY:
                return ()
            return (
                ControlCoverageFinding(
                    component_id="source",
                    component_type="source",
                    capability=capability,
                    role=ControlRole.OUTPUT,
                    reason="output_not_post_dominated",
                ),
            )

        monkeypatch.setattr(required_controls_module, "control_coverage_findings", repeated_finding)
        monkeypatch.setattr(required_controls_module, "_stream_proves_output_control", lambda *_args, **_kwargs: False)

        wired = wire_required_controls(candidate, snapshot, view)

        assert len([node for node in wired["nodes"] if node["plugin"] == "aws_bedrock_content_safety"]) == 1

    @pytest.mark.parametrize(
        "malformation",
        [
            {"source": [], "nodes": [], "outputs": []},
            {"sources": [], "nodes": [], "outputs": []},
            {"sources": {17: {}}, "nodes": [], "outputs": []},
            {"sources": {"briefing": []}, "nodes": [], "outputs": []},
            {
                "sources": {
                    "briefing": {
                        "plugin": "llm",
                        "on_success": ["generated"],
                        "options": {"response_field": "briefing"},
                        "on_validation_failure": "discard",
                    }
                },
                "nodes": [],
                "outputs": [],
            },
        ],
    )
    def test_malformed_source_candidates_fail_closed_without_raising(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
        malformation: dict[str, Any],
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)

        assert wire_required_controls(malformation, snapshot, view) is malformation

    @pytest.mark.parametrize(
        ("surface", "malformed_value"),
        [
            ("node_id", 17),
            ("route_destination", ["generated"]),
            ("sink_name", 17),
            ("source_name", "Briefing"),
        ],
    )
    def test_malformed_adjacent_graph_shape_prevents_partial_source_certification(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
        surface: str,
        malformed_value: object,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(container="sources", source_name="briefing")
        if surface == "source_name":
            candidate["sources"] = {malformed_value: candidate["sources"]["briefing"]}
        elif surface == "sink_name":
            candidate["outputs"][0]["sink_name"] = malformed_value
        else:
            candidate["nodes"] = [
                {
                    "id": malformed_value if surface == "node_id" else "gate",
                    "node_type": "gate",
                    "plugin": None,
                    "input": "unrelated",
                    "on_success": None,
                    "on_error": None,
                    "options": {},
                    "condition": "true",
                    "routes": {"true": malformed_value if surface == "route_destination" else "generated"},
                }
            ]

        assert wire_required_controls(candidate, snapshot, view) is candidate

    def test_unsupported_source_prevents_later_repairable_source_mutation(
        self,
        tmp_path: Path,
        llm_source_policy_manager: PluginManager,
    ) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)
        candidate = _bare_llm_source_candidate(container="sources", source_name="briefing")
        candidate["sources"] = {
            "unknown": {
                "plugin": "not_registered",
                "on_success": "unknown_rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            **candidate["sources"],
        }

        assert wire_required_controls(candidate, snapshot, view) is candidate


class TestAutoWireIdempotence:
    def test_second_pass_is_a_no_op(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path)

        wired = wire_required_controls(_bare_llm_candidate(), snapshot, view)
        rewired = wire_required_controls(wired, snapshot, view)

        # Identity, not just equality: a no-op finalizer must return the SAME
        # object (the pre-existing contract pinned by
        # test_shared_planner_surfaces — load-bearing for byte-exactness and
        # authority hashing downstream).
        assert rewired is wired

    def test_already_covered_graph_is_returned_unchanged(self, tmp_path: Path) -> None:
        from elspeth.web.composer.planner_authoring_aids import fork_coalesce_exemplar_args

        view, snapshot = _guardrail_profile_view(tmp_path)
        covered = fork_coalesce_exemplar_args(view)
        assert covered is not None

        assert wire_required_controls(covered, snapshot, view) is covered

    def test_every_no_op_path_is_identity_preserving(self, tmp_path: Path) -> None:
        """Regression (fix round 2): the pass returned a NEW equal dict on
        no-op paths, breaking the finalizer identity contract pinned by
        tests/integration/web/composer/guided/test_shared_planner_surfaces.py.
        Every refusal path must return the input object itself — including for
        read-only mapping inputs such as a frozen proposal pipeline."""
        from types import MappingProxyType

        # Recommend mode (no REQUIRED selections).
        view, snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")
        candidate = _bare_llm_candidate()
        assert wire_required_controls(candidate, snapshot, view) is candidate

        # REQUIRED posture, but no LLM node / empty frozen candidate.
        req_view, req_snapshot = _guardrail_profile_view(tmp_path)
        frozen = MappingProxyType({"sources": MappingProxyType({}), "nodes": (), "edges": (), "outputs": ()})
        assert wire_required_controls(frozen, req_snapshot, req_view) is frozen

        # REQUIRED posture through the service finalizer factory (the seam the
        # regression test exercises).
        from elspeth.web.composer.service import _required_controls_candidate_finalizer

        finalize = _required_controls_candidate_finalizer(policy_catalog=req_view, plugin_snapshot=req_snapshot)
        assert finalize(frozen) is frozen


class TestAutoWireRefusals:
    def test_recommend_mode_inserts_nothing(self, tmp_path: Path) -> None:
        view, snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")
        candidate = _bare_llm_candidate()

        assert wire_required_controls(candidate, snapshot, view) is candidate

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

        assert wire_required_controls(candidate, unselected, view) is candidate

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

    def test_uncreditable_control_inserts_nothing_instead_of_churning(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If the selected implementation would not be credited by the coverage
        authority, the pass must insert NOTHING — not chain redundant nodes
        until the splice budget exhausts."""
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        view, snapshot = _guardrail_profile_view(tmp_path)
        shield_cls = get_shared_plugin_manager().get_transform_by_name("aws_bedrock_prompt_shield")
        safety_cls = get_shared_plugin_manager().get_transform_by_name("aws_bedrock_content_safety")
        for cls in (shield_cls, safety_cls):
            monkeypatch.setattr(cls, "is_effective_blocking_control", classmethod(lambda _cls, **_kwargs: False))
        candidate = _bare_llm_candidate()

        assert wire_required_controls(candidate, snapshot, view) is candidate

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
            assert wire_required_controls(malformed, snapshot, view) is malformed

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


class TestServiceFinalizerFactory:
    """The exact finalizer callable all three plan_pipeline sites now pass."""

    def test_finalizer_wires_the_candidate(self, tmp_path: Path) -> None:
        from elspeth.web.composer.service import _required_controls_candidate_finalizer

        view, snapshot = _guardrail_profile_view(tmp_path)
        finalize = _required_controls_candidate_finalizer(policy_catalog=view, plugin_snapshot=snapshot)

        wired = finalize(_bare_llm_candidate())

        assert type(wired) is dict  # the planner's exact-dict finalizer contract
        assert "prompt_shield_auto_1" in _nodes_by_id(dict(wired))

    def test_inner_finalizer_runs_before_the_pass(self, tmp_path: Path) -> None:
        """The guided reviewed-component binder composes BEFORE wiring, so the
        pass always sees the bound candidate."""
        from elspeth.web.composer.service import _required_controls_candidate_finalizer

        view, snapshot = _guardrail_profile_view(tmp_path)
        seen: list[dict[str, Any]] = []

        def inner(candidate: Any) -> Any:
            seen.append(copy.deepcopy(dict(candidate)))
            return candidate

        finalize = _required_controls_candidate_finalizer(policy_catalog=view, plugin_snapshot=snapshot, inner=inner)
        bare = _bare_llm_candidate()

        wired = finalize(bare)

        assert seen == [bare], "inner must receive the pre-wire candidate"
        assert "prompt_shield_auto_1" in _nodes_by_id(dict(wired))


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
