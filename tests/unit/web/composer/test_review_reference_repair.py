"""New candidate review references receive candidate-specific repair guidance."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.tools import execute_tool
from elspeth.web.composer.tools._common import (
    REVIEW_RECONCILIATION_FAILURE_PREFIX,
    _authored_interpretation_requirement_id,
    review_reconciliation_failure_message,
)
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    InterpretationReviewIntegrityError,
    PromptPart,
    UnknownInterpretationRequirementError,
    _render_prompt_parts,
    reconcile_authoritative_reviews,
)
from tests.unit.web.composer.test_authoring_reconciliation import (
    _exact_arguments,
    _node,
    _state,
    _trained_context,
    _two_vague_term_fixtures,
)


def _review_candidate(user_term: str, requirement_id: str) -> dict[str, Any]:
    return {
        "id": "classify_pcs_lh",
        "node_type": "transform",
        "plugin": "llm",
        "input": "in",
        "on_success": "out",
        "on_error": "discard",
        "options": {
            "provider": "bedrock",
            "model": "bedrock/example.model",
            "prompt_template": "Classify {{ row.case_study }}",
            "required_input_fields": ["case_study"],
            "response_field": "category",
            "schema": {"mode": "observed"},
            "interpretation_requirements": [{"kind": "vague_term", "user_term": user_term, "draft": "Synthetic classification definition"}],
            "prompt_template_parts": [
                {"kind": "text", "text": "Classify {{ row.case_study }} using "},
                {"kind": "interpretation_ref", "requirement_id": requirement_id},
            ],
        },
    }


@pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"])
@pytest.mark.parametrize(
    ("user_term", "canonical_term"),
    [("service type", "service type"), (" \tService Type / PCS-LH! \n", "Service Type / PCS-LH!")],
)
def test_new_candidate_unknown_review_ref_keeps_candidate_repair_evidence(tool_name: str, user_term: str, canonical_term: str) -> None:
    # The source destination was already edited, but its new classifier has
    # never been accepted: precisely the draft state at the incident rejection.
    previous = replace(_state(nodes=(_node(plugin="passthrough"),)), nodes=())
    assert previous.nodes == ()
    assert all("interpretation_requirements" not in source.options for source in previous.sources.values())
    candidate = _review_candidate(user_term, "service_type:classify_pcs_lh")
    if tool_name == "set_pipeline":
        arguments = deep_thaw(_exact_arguments(previous).data)
        arguments["nodes"] = [candidate]
    else:
        arguments = candidate
    context = _trained_context()

    result = execute_tool(
        tool_name,
        arguments,
        previous,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        validate_arguments=True,
    )

    assert not result.success
    assert result.updated_state is previous
    envelope = result.to_dict()
    rejection = envelope["validation"]["errors"][0]
    assert rejection["error_code"] == "review_reconciliation_failed"
    guidance = envelope["validation_guidance"]["codes"]["review_reconciliation_failed"]
    assert "was already APPROVED" not in guidance["explanation"]
    assert "service_type:classify_pcs_lh" in rejection["message"]
    valid_id = f"{canonical_term}:classify_pcs_lh"
    assert valid_id in rejection["message"]
    assert "valid requirement IDs" in rejection["message"]
    assert "rejected candidate" in rejection["message"]
    assert "Re-inspect" not in rejection["message"]

    expanded = execute_tool(
        "explain_validation_error",
        {"error_text": rejection["message"]},
        previous,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
    )
    assert expanded.success
    assert expanded.data["explanation"] == guidance["explanation"]
    assert expanded.data["suggested_fix"] == guidance["suggested_fix"]

    candidate["options"]["prompt_template_parts"][1]["requirement_id"] = valid_id
    repaired = execute_tool(
        tool_name,
        arguments,
        previous,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        validate_arguments=True,
    )
    assert repaired.success, repaired.to_dict()
    node = next(node for node in repaired.updated_state.nodes if node.id == "classify_pcs_lh")
    requirement = next(review for review in node.options["interpretation_requirements"] if review["id"] == valid_id)
    assert requirement["user_term"] == user_term
    assert requirement["status"] == "pending"


def test_unknown_review_ref_with_no_requirements_reports_empty_valid_ids() -> None:
    parts = (PromptPart(kind="interpretation_ref", requirement_id="missing:classify"),)

    with pytest.raises(UnknownInterpretationRequirementError) as caught:
        _render_prompt_parts(parts, {}, unresolved_text="pending")

    assert caught.value.requirement_id == "missing:classify"
    assert caught.value.valid_requirement_ids == ()
    message = review_reconciliation_failure_message(caught.value, retry_hint="Re-inspect the pipeline and retry.")
    assert "valid requirement IDs: []" in message
    assert "add the intended authorable requirement shell" in message
    assert "Re-inspect" not in message


@pytest.mark.parametrize(
    ("user_term", "expected"),
    [("  service type  ", "service type"), ("Service Type / PCS-LH!", "Service Type / PCS-LH!"), ("service_type", "service_type")],
)
@pytest.mark.parametrize("source", [False, True])
def test_review_id_projection_only_trims_surrounding_whitespace(user_term: str, expected: str, source: bool) -> None:
    projected = _authored_interpretation_requirement_id(component_id="classify", user_term=user_term, source=source)

    assert projected == (f"source_review:{expected}" if source else f"{expected}:classify")


@pytest.mark.parametrize(
    "reason",
    [
        "resolved interpretation requirement 'model_choice_review:classify' hash drifted",
        "prompt part references unknown interpretation requirement 'spoofed:classify'",
    ],
)
def test_generic_reconciliation_failure_preserves_existing_retry_hint(reason: str) -> None:
    message = review_reconciliation_failure_message(ValueError(reason), retry_hint="Re-inspect the pipeline and retry.")

    assert message == f"{REVIEW_RECONCILIATION_FAILURE_PREFIX}: {reason}. Re-inspect the pipeline and retry."


def test_unknown_ref_in_saved_review_keeps_existing_review_repair_semantics() -> None:
    previous, proposed = _two_vague_term_fixtures(tone_hash=stable_hash("warm"), audience_hash=stable_hash("new users"))
    node = previous.nodes[0]
    options = deep_thaw(node.options)
    # Corrupt the saved authoritative rows, leaving its audience reference.
    # The new candidate still carries both requirements and valid references.
    options[INTERPRETATION_REQUIREMENTS_KEY] = options[INTERPRETATION_REQUIREMENTS_KEY][:1]
    previous = replace(previous, nodes=(replace(node, options=options),))

    with pytest.raises(InterpretationReviewIntegrityError) as caught:
        reconcile_authoritative_reviews(previous, proposed)

    assert caught.value.component_id == node.id
    message = review_reconciliation_failure_message(caught.value, retry_hint="Re-inspect the pipeline and retry.")
    assert "resolved vague-term review" in message
    assert "Re-inspect the pipeline and retry" in message
    assert "Repair the rejected candidate" not in message


def test_set_pipeline_producer_carries_candidate_reference_facts_without_new_wire_fields() -> None:
    previous = replace(_state(nodes=(_node(plugin="passthrough"),)), nodes=())
    arguments = deep_thaw(_exact_arguments(previous).data)
    arguments["nodes"] = [_review_candidate("service type", "service_type:classify_pcs_lh")]

    result = build_set_pipeline_candidate(arguments, previous, _trained_context()).result

    assert not result.success
    assert result.updated_state is previous
    (entry,) = result.validation.errors
    assert entry.error_code == "review_reconciliation_failed"
    assert entry.rejected_component == "node:classify_pcs_lh"
    assert entry.review_reference.unknown_requirement_id == "service_type:classify_pcs_lh"
    assert "service type:classify_pcs_lh" in entry.review_reference.valid_requirement_ids
    assert "review_reference" not in entry.to_dict()
