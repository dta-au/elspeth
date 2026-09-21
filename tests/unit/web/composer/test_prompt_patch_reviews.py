"""Approved prompts remain editable through the ordinary option-patch tool."""

from dataclasses import replace

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, SourceSpec
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)
from tests.unit.web.composer.test_tools import _empty_state, _llm_options_with_forged_resolved_reviews, _mock_catalog, execute_tool


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
