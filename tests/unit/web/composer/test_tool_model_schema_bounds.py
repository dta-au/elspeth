"""Whole argument admission agrees with disclosed numeric and string bounds."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.sessions import _RequestInterpretationReviewArgumentsModel
from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel


@pytest.mark.parametrize("field", ["count", "timeout_seconds"])
@pytest.mark.parametrize("value,accepted", [(0, False), (-1, False), (1, True), (1.0, True), (None, True)])
def test_upsert_trigger_bounds_match_whole_model(field: str, value: object, accepted: bool) -> None:
    schema = next(item["parameters"] for item in get_tool_definitions() if item["name"] == "upsert_node")
    validator = Draft202012Validator(schema)
    node = {"id": "n", "node_type": "aggregation", "input": "rows", "trigger": {field: 1}}
    assert validator.is_valid(node)
    _UpsertNodeArgumentsModel.model_validate(node)
    node["trigger"] = {field: value}
    assert validator.is_valid(node) is accepted
    if accepted:
        _UpsertNodeArgumentsModel.model_validate(node)
    else:
        with pytest.raises(ValidationError):
            _UpsertNodeArgumentsModel.model_validate(node)


@pytest.mark.parametrize("field", ["count", "timeout_seconds"])
@pytest.mark.parametrize("value", [0, -1, 1, 1.0, None])
def test_full_state_trigger_shape_admission_is_preserved(field: str, value: object) -> None:
    """Full-state semantic validation remains a separate downstream stage."""
    schema = next(item["parameters"] for item in get_tool_definitions() if item["name"] == "set_pipeline")
    payload = {
        "source": {"plugin": "csv", "on_success": "rows"},
        "nodes": [{"id": "n", "node_type": "aggregation", "input": "rows", "trigger": {field: value}}],
        "edges": [],
        "outputs": [],
    }
    assert Draft202012Validator(schema).is_valid(payload)
    SetPipelineArgumentsModel.model_validate(payload)


@pytest.mark.parametrize("field,maximum", [("affected_node_id", 256), ("user_term", 8192), ("llm_draft", 8192)])
@pytest.mark.parametrize("position", ["empty", "minimum", "maximum", "too_long"])
def test_review_string_bounds_match_whole_model(field: str, maximum: int, position: str) -> None:
    schema = next(item["parameters"] for item in get_tool_definitions() if item["name"] == "request_interpretation_review")
    validator = Draft202012Validator(schema)
    payload = {"affected_node_id": "n", "kind": "vague_term", "user_term": "term", "llm_draft": "draft"}
    assert validator.is_valid(payload)
    _RequestInterpretationReviewArgumentsModel.model_validate(payload)
    size = {"empty": 0, "minimum": 1, "maximum": maximum, "too_long": maximum + 1}[position]
    payload[field] = "x" * size
    accepted = position in ("minimum", "maximum")
    assert validator.is_valid(payload) is accepted
    if accepted:
        _RequestInterpretationReviewArgumentsModel.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            _RequestInterpretationReviewArgumentsModel.model_validate(payload)


def test_review_draft_omission_and_supplied_null_stay_distinct() -> None:
    schema = next(item["parameters"] for item in get_tool_definitions() if item["name"] == "request_interpretation_review")
    validator = Draft202012Validator(schema)
    payload = {"affected_node_id": "n", "kind": "vague_term", "user_term": "term"}
    assert validator.is_valid(payload)
    _RequestInterpretationReviewArgumentsModel.model_validate(payload)
    supplied_null = {**payload, "llm_draft": None}
    assert not validator.is_valid(supplied_null)
    with pytest.raises(ValidationError):
        _RequestInterpretationReviewArgumentsModel.model_validate(supplied_null)
