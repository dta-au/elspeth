"""A carried term is always accompanied by a reopened prompt review.

Reconciliation carries an accepted vague term across a wording edit and leaves
the review of that edit to the node's ``llm_prompt_template`` review. It does
not check that the review reopened; it relies on the anchor. These tests pin
that reliance for the three shapes a structured prompt node can take.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.interpretation_state import (
    approved_prompt_artifact_hash_from_options,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
    reconcile_authoritative_reviews,
)
from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state

SHAPES = ("single_prompt", "single_prompt_with_system_prompt", "multi_query")
RE_SPLIT_PARTS = [
    {"kind": "text", "text": "Classify "},
    {"kind": "interpretation_ref", "requirement_id": "category"},
    {"kind": "text", "text": " for "},
    {"kind": "text", "text": "{{ row.text }}."},
]


def _approved_options(shape: str) -> dict[str, Any]:
    """Options of an approved structured LLM node of one shape, its reviews re-anchored to that shape."""
    options: dict[str, Any] = deep_thaw(_approved_state(structured=True).nodes[0].options)
    if shape == "single_prompt":
        del options["system_prompt"]
    elif shape == "multi_query":
        options["queries"] = {"first": {"input_fields": {"text": "text"}}, "second": {"input_fields": {"text": "text"}}}
    prompt_review = next(row for row in options["interpretation_requirements"] if row["kind"] == "llm_prompt_template")
    prompt_review["draft"] = prompt_review_draft_from_options(options)
    prompt_review["accepted_value"] = prompt_review["draft"]
    prompt_review["resolved_prompt_template_hash"] = prompt_review_anchor_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    return options


@pytest.mark.parametrize("shape", SHAPES)
def test_the_prompt_review_anchor_changes_when_the_parts_skeleton_changes_and_the_text_does_not(shape: str) -> None:
    """A carried term is always accompanied by a reopened prompt review.

    A term is carried only when the parts skeleton changed. The prompt review
    reopens on that same edit because its anchor contains the skeleton hash.
    If the skeleton ever leaves the anchor, terms could carry with no card
    shown: nothing else stops a resolved prompt review from standing beside a
    prompt whose wording moved around an accepted term.
    """
    options = _approved_options(shape)
    re_split = {**options, "prompt_template_parts": RE_SPLIT_PARTS}
    assert "".join(part["text"] for part in RE_SPLIT_PARTS if part["kind"] == "text") == "".join(
        part["text"] for part in options["prompt_template_parts"] if part["kind"] == "text"
    )

    assert prompt_review_anchor_hash_from_options(re_split) != prompt_review_anchor_hash_from_options(options)


@pytest.mark.parametrize("edit", ["wording", "re_split_same_text"])
@pytest.mark.parametrize("shape", SHAPES)
def test_after_a_carried_edit_the_prompt_review_row_is_pending(shape: str, edit: str) -> None:
    state = _approved_state(structured=True)
    options = _approved_options(shape)
    edited_options = deep_thaw(options)
    if edit == "wording":
        edited_options["prompt_template_parts"][0]["text"] = "Assess "
    else:
        edited_options["prompt_template_parts"] = RE_SPLIT_PARTS
    previous = state.with_node(replace(state.nodes[0], options=options))
    proposed = state.with_node(replace(state.nodes[0], options=edited_options))

    reconciled = reconcile_authoritative_reviews(previous, proposed).nodes[0].options

    rows = {row["id"]: row for row in reconciled["interpretation_requirements"]}
    assert rows["category"] == next(row for row in options["interpretation_requirements"] if row["id"] == "category")
    prompt_review = next(row for row in rows.values() if row["kind"] == "llm_prompt_template")
    assert (prompt_review["status"], prompt_review["event_id"], prompt_review["accepted_value"]) == ("pending", None, None)
