"""Approved prompts remain editable through the ordinary option-patch tool."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import build_context_string, project_server_owned_option_metadata
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_authoring,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
    reconcile_authoritative_reviews,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_tools import _empty_state, _llm_options_with_forged_resolved_reviews, _mock_catalog, execute_tool


def _amended_approved_state(*, pending_audience: bool = False, literal_prefix: str = "") -> CompositionState:
    """Use owned render/hash producers for two amended interpretation references."""
    state = _approved_state(structured=True)
    options = deep_thaw(state.nodes[0].options)
    options["prompt_template_parts"][0]["text"] = literal_prefix + options["prompt_template_parts"][0]["text"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    category["draft"] = "old rejected broad categories"
    category["display_title"] = "Category definition"
    options["prompt_template_parts"].extend(
        [{"kind": "text", "text": " Audience: "}, {"kind": "interpretation_ref", "requirement_id": "audience"}]
    )
    options["interpretation_requirements"].append(
        {
            "id": "audience",
            "kind": "vague_term",
            "user_term": "audience",
            "draft": "everyone",
            "status": "pending" if pending_audience else "resolved",
            "event_id": None if pending_audience else "audience-event",
            "accepted_value": None if pending_audience else "new users",
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": None if pending_audience else stable_hash("new users"),
        }
    )
    state = materialize_state_for_authoring(state.with_node(replace(state.nodes[0], options=options)))
    options = deep_thaw(state.nodes[0].options)
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["status"] = "pending" if pending_audience else "resolved"
    prompt_review["event_id"] = None if pending_audience else "prompt-event"
    prompt_review["accepted_value"] = None if pending_audience else prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = None if pending_audience else prompt_review_anchor_hash_from_options(options)
    if pending_audience:
        del options["approved_prompt_artifact_hash"]
    else:
        options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    state = state.with_node(replace(state.nodes[0], options=options))
    # The real reconciler derives the current artifact even when another
    # required review remains pending; that digest does not admit execution.
    return reconcile_authoritative_reviews(state, state)


@pytest.mark.parametrize("pending_audience", [False, True])
@pytest.mark.parametrize("writer", ["set_pipeline", "upsert_node", "patch_node_options"])
@pytest.mark.parametrize("echo", ["current", "full", "stale-resolved", "tampered-history"])
def test_current_projection_roundtrip_preserves_amendments_and_review_authority(writer: str, pending_audience: bool, echo: str) -> None:
    state = _amended_approved_state(pending_audience=pending_audience)
    before = state.to_dict()
    before_hash = composition_content_hash(state)
    catalog = _mock_catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    policy = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    visible = json.loads(build_context_string(state, policy, plugin_snapshot=snapshot).split("\n", 1)[1])["current_state"]
    node = visible["nodes"][0]
    options = node["options"]
    original = state.nodes[0].options
    assert "prompt_template_parts" in options
    assert options["prompt_template_parts"] == deep_thaw(original["prompt_template_parts"])
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category == {
        "id": "category",
        "kind": "vague_term",
        "user_term": "category",
        "historical_draft": "old rejected broad categories",
        "status": "resolved",
        "display_title": "Category definition",
    }
    assert "support categories" in options["prompt_template"]
    assert "old rejected broad categories" not in options["prompt_template"]
    if echo == "full":
        options["interpretation_requirements"] = deep_thaw(original["interpretation_requirements"])
    elif echo == "stale-resolved":
        category["draft"] = category.pop("historical_draft")
    elif echo == "tampered-history":
        category["historical_draft"] = "PRIVATE-HISTORY-TAMPER"
    if writer == "set_pipeline":
        # A substantive routing-only change, rather than an identical echo.
        node["on_error"] = "main"
        args, error = _serialize_set_pipeline_arguments(state)
        assert error is None and args is not None
        # The read view's output.name is a display field; the canonical
        # authoring exporter supplies sink_name while this node comes from
        # the actual current context, including its exact review projection.
        args["source"] = visible["sources"]["source"]
        args["nodes"] = visible["nodes"]
    elif writer == "upsert_node":
        args = {**node, "on_error": "main"}
    else:
        args = {"node_id": node["id"], "patch": options}
    result = execute_tool(writer, args, state, catalog)
    assert state.to_dict() == before and composition_content_hash(state) == before_hash
    if echo in ("stale-resolved", "tampered-history"):
        assert not result.success and result.updated_state is state
        assert result.validation.errors[0].error_code == "interpretation_requirements_invalid"
        assert "PRIVATE-HISTORY-TAMPER" not in repr(result.to_dict())
        return
    assert result.success, result.validation.errors
    updated = result.updated_state.nodes[0]
    assert updated.on_error == ("discard" if writer == "patch_node_options" else "main")
    assert updated.options == original
    assert state.to_dict() == before and composition_content_hash(state) == before_hash
    if pending_audience:
        assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    else:
        assert isinstance(materialize_state_for_execution(result.updated_state), CompositionState)
        assert updated.options["approved_prompt_artifact_hash"] == original["approved_prompt_artifact_hash"]


@pytest.mark.parametrize("damage", ["stale", "history", "accepted", "reference", "missing-parts", "stored-hash", "approval"])
def test_projected_review_corruption_refuses_atomically(damage: str) -> None:
    state = _amended_approved_state()
    if damage in ("stored-hash", "approval"):
        options = deep_thaw(state.nodes[0].options)
        if damage == "stored-hash":
            next(row for row in options["interpretation_requirements"] if row["id"] == "category")["resolved_prompt_template_hash"] = (
                stable_hash("different")
            )
        else:
            options["approved_prompt_artifact_hash"] = "a" * 64
        state = state.with_node(replace(state.nodes[0], options=options))
    before = state.to_dict()
    visible = project_server_owned_option_metadata(state.to_dict())
    node = visible["nodes"][0]
    options = node["options"]
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    if damage == "stale":
        category["draft"] = category.pop("historical_draft")
    elif damage == "history":
        category["historical_draft"] = "PRIVATE-HISTORY-TAMPER"
    elif damage == "accepted":
        category["accepted_value"] = "PRIVATE-FORGED-ACCEPTANCE"
    elif damage == "reference":
        options["prompt_template_parts"][1] = {"kind": "interpretation_ref", "requirement_id": "unknown"}
    elif damage == "missing-parts":
        del options["prompt_template_parts"]
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    args["source"] = visible["sources"]["source"]
    args["nodes"] = visible["nodes"]
    if damage == "approval":
        # The option-patch owner explicitly verifies the saved approval digest.
        result = execute_tool("patch_node_options", {"node_id": node["id"], "patch": options}, state, _mock_catalog())
    else:
        result = execute_tool("set_pipeline", args, state, _mock_catalog())
    assert not result.success
    assert result.updated_state is state
    assert state.to_dict() == before
    assert "PRIVATE-HISTORY-TAMPER" not in repr(result.to_dict())
    assert "PRIVATE-FORGED-ACCEPTANCE" not in repr(result.to_dict())


@pytest.mark.parametrize("field", ["prompt_template_parts", "system_prompt"])
def test_editing_current_amended_projection_reopens_only_applicable_reviews(field: str) -> None:
    state = _amended_approved_state()
    options = project_server_owned_option_metadata(state.to_dict())["nodes"][0]["options"]
    if field == "prompt_template_parts":
        value = [{"kind": "text", "text": "Assess "}, *options[field][1:]]
    else:
        value = "Explain the classification."
    before = state.to_dict()
    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": {field: value}}, state, _mock_catalog())
    assert result.success, result.validation.errors
    reviews = result.updated_state.nodes[0].options["interpretation_requirements"]
    assert next(row for row in reviews if row["kind"] == "llm_prompt_template")["status"] == "pending"
    vague = [row for row in reviews if row["kind"] == "vague_term"]
    assert [row["status"] for row in vague] == (["pending", "pending"] if field == "prompt_template_parts" else ["resolved", "resolved"])
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    updated = result.updated_state.nodes[0].options
    if field == "prompt_template_parts":
        assert "approved_prompt_artifact_hash" not in updated
    else:
        # Retained vague-term evidence derives an artifact identity. The
        # pending prompt review above still blocks execution independently.
        assert updated["approved_prompt_artifact_hash"] == approved_prompt_artifact_hash_from_options(updated)
        assert updated["approved_prompt_artifact_hash"] != state.nodes[0].options["approved_prompt_artifact_hash"]
    assert state.to_dict() == before


def test_canonical_export_stays_distinct_from_current_amended_projection() -> None:
    state = _amended_approved_state()
    result = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog())
    assert result.success
    options = result.data["nodes"][0]["options"]
    assert "pending interpretation" in options["prompt_template"]
    assert "support categories" not in options["prompt_template"] and "new users" not in options["prompt_template"]
    assert options["prompt_template_parts"] == state.nodes[0].options["prompt_template_parts"]
    assert all(set(row) <= {"kind", "user_term", "draft", "display_title"} for row in options["interpretation_requirements"])
    reapplied = execute_tool("set_pipeline", deep_thaw(result.data), state, _mock_catalog())
    assert reapplied.success, reapplied.validation.errors
    assert reapplied.updated_state.nodes[0].options == state.nodes[0].options


def _approved_state(*, structured: bool = False) -> CompositionState:
    options = _llm_options_with_forged_resolved_reviews({"secret_ref": "OPENROUTER_API_KEY"})
    options["system_prompt"] = "Classify the row using the requested categories."
    options["required_input_fields"] = []
    if structured:
        options["prompt_template"] = "Classify support categories for {{ row.text }}."
        options["prompt_template_parts"] = [
            {"kind": "text", "text": "Classify "},
            {"kind": "interpretation_ref", "requirement_id": "category"},
            {"kind": "text", "text": " for {{ row.text }}."},
        ]
        options["interpretation_requirements"].append(
            {
                "id": "category",
                "kind": "vague_term",
                "user_term": "category",
                "status": "resolved",
                "draft": "support categories",
                "event_id": "category-event",
                "accepted_value": "support categories",
                "accepted_artifact_hash": None,
                "resolved_prompt_template_hash": stable_hash("support categories"),
            }
        )
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["accepted_value"] = prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = prompt_review_anchor_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return (
        _empty_state()
        .with_source(
            SourceSpec(
                plugin="csv",
                on_success="source_out",
                options={"path": "/data/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        .with_node(
            NodeSpec(
                id="code_themes",
                node_type="transform",
                plugin="llm",
                input="source_out",
                on_success="main",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        .with_output(
            OutputSpec(
                name="main", plugin="csv", options={"path": "/data/output.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"
            )
        )
    )


@pytest.mark.parametrize("field", ["prompt_template", "system_prompt"])
def test_changed_approved_prompt_is_accepted_and_requires_fresh_review(field: str) -> None:
    state = _approved_state()
    original_options = state.nodes[0].options

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {field: "Summarize the row instead."}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options[field] == "Summarize the row instead."
    assert "approved_prompt_artifact_hash" not in options
    statuses = {row["kind"]: row["status"] for row in options["interpretation_requirements"]}
    assert statuses == {"llm_prompt_template": "pending", "llm_model_choice": "resolved"}
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)
    assert state.nodes[0].options == original_options


@pytest.mark.parametrize("noop", [False, True])
@pytest.mark.parametrize("structured", [False, True])
def test_unrelated_or_identical_patch_retains_approval(noop: bool, structured: bool) -> None:
    state = _approved_state(structured=structured)
    patch = {"prompt_template": state.nodes[0].options["prompt_template"]} if noop else {"temperature": 0.1}

    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": patch}, state, _mock_catalog())

    assert result.success, result.validation.errors
    assert result.updated_state.nodes[0].options["approved_prompt_artifact_hash"] == state.nodes[0].options["approved_prompt_artifact_hash"]
    assert result.updated_state.nodes[0].options["interpretation_requirements"] == state.nodes[0].options["interpretation_requirements"]


def test_parts_only_patch_validates_the_new_rendered_prompt() -> None:
    state = _approved_state(structured=True)
    parts = [{"kind": "text", "text": "Assess "}, *state.nodes[0].options["prompt_template_parts"][1:]]

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template_parts": parts}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["prompt_template"].startswith("Assess ")
    assert options["prompt_template_parts"][1] == state.nodes[0].options["prompt_template_parts"][1]
    assert next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")["status"] == "pending"
    category = next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    assert category["status"] == "pending"
    assert category["draft"] == "support categories"
    assert category["accepted_value"] is None
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


def test_system_prompt_edit_preserves_unchanged_structured_category_review() -> None:
    state = _approved_state(structured=True)

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"system_prompt": "Explain the classification."}}, state, _mock_catalog()
    )

    assert result.success, result.validation.errors
    options = result.updated_state.nodes[0].options
    assert options["system_prompt"] == "Explain the classification."
    reviews = {row["kind"]: row for row in options["interpretation_requirements"]}
    assert reviews["llm_prompt_template"]["status"] == "pending"
    assert reviews["vague_term"] == next(row for row in state.nodes[0].options["interpretation_requirements"] if row["id"] == "category")
    assert isinstance(materialize_state_for_execution(result.updated_state), InterpretationReviewPending)


def test_invalid_option_with_prompt_edit_rejects_atomically() -> None:
    state = _approved_state()

    result = execute_tool(
        "patch_node_options",
        {"node_id": "code_themes", "patch": {"prompt_template": "New wording.", "temperature": "invalid"}},
        state,
        _mock_catalog(),
    )

    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "plugin_options_invalid"


def test_raw_prompt_edit_of_structured_node_explains_parts_authority() -> None:
    state = _approved_state(structured=True)

    result = execute_tool(
        "patch_node_options", {"node_id": "code_themes", "patch": {"prompt_template": "New wording."}}, state, _mock_catalog()
    )

    assert not result.success
    assert result.updated_state is state
    assert any(error.error_code == "prompt_template_parts_required" for error in result.validation.errors)
    assert "interpretation_ref" in result.validation.errors[0].message


@pytest.mark.parametrize("field", ["temperature", "prompt_template"])
def test_patch_does_not_heal_corrupt_stored_approval(field: str) -> None:
    state = _approved_state()
    state = state.with_node(replace(state.nodes[0], options={**state.nodes[0].options, "approved_prompt_artifact_hash": "a" * 64}))
    value = 0.1 if field == "temperature" else "New wording."

    result = execute_tool("patch_node_options", {"node_id": "code_themes", "patch": {field: value}}, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state


def test_patch_cannot_supply_replacement_approval_digest() -> None:
    state = _approved_state()

    result = execute_tool(
        "patch_node_options",
        {"node_id": "code_themes", "patch": {"prompt_template": "New wording.", "approved_prompt_artifact_hash": "a" * 64}},
        state,
        _mock_catalog(),
    )

    assert not result.success
    assert result.updated_state is state
    assert "runtime-owned" in result.validation.errors[0].message
