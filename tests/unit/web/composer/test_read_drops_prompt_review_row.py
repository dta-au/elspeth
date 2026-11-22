"""On a step with prompt parts, the model is not handed the prompt-review row.

The prompt-review row's ``draft`` is written once, when the review is staged,
and is never refreshed: after an operator amends a term's definition the row
still carries the earlier wording. The exact-arguments read copied it out, so
the model was offered superseded prompt text beside a placeholder rendering
and sent it back as the step's prompt. On a step with parts the server derives
the prompt text and re-stages the review itself, so the row tells the model
nothing it can use. Every projection the model is handed leaves it out; stored
state keeps it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools.transforms import _normalized_splice_node_projection
from elspeth.web.interpretation_state import (
    InterpretationReviewPending,
    approved_prompt_artifact_hash_from_options,
    materialize_state_for_execution,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
    reconcile_authoritative_reviews,
)
from tests.unit.web.composer.test_prompt_patch_reviews import _amended_approved_state, _approved_state
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

_STALE = "STALE-DRAFT-MARKER wording from before the definitions were amended"
_PROMPT_KIND = "llm_prompt_template"
_NODE_ID = "code_themes"
_AMENDED_TEXT = "Classify support categories for {{ row.text }}. Audience: new users"
_WRITERS = ("set_pipeline", "upsert_node", "patch_node_options")


def _stale_state(*, pending: bool = False) -> CompositionState:
    """A parts step whose stored prompt-review row still carries superseded wording."""
    state = _amended_approved_state(pending_audience=pending)
    options = deep_thaw(state.nodes[0].options)
    _prompt_row(options)["draft"] = _STALE
    state = state.with_node(replace(state.nodes[0], options=options))
    state = reconcile_authoritative_reviews(state, state)
    assert _prompt_row(deep_thaw(state.nodes[0].options))["draft"] == _STALE
    return state


def _rows(options: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = options["interpretation_requirements"]
    return rows


def _prompt_row(options: dict[str, Any]) -> dict[str, Any]:
    [row] = [row for row in _rows(options) if row["kind"] == _PROMPT_KIND]
    return row


def _kinds(options: dict[str, Any]) -> list[str]:
    return [row["kind"] for row in _rows(options)]


def _read(state: CompositionState) -> dict[str, Any]:
    result = execute_tool("get_pipeline_state", {"component": "set_pipeline_arguments"}, state, _mock_catalog())
    assert result.success, result.to_dict()
    data: dict[str, Any] = json.loads(json.dumps(result.to_dict()["data"]))
    return data


def _read_node(state: CompositionState) -> dict[str, Any]:
    [node] = [node for node in _read(state)["nodes"] if node["id"] == _NODE_ID]
    return node


def _write_back(writer: str, state: CompositionState, arguments: dict[str, Any]) -> CompositionState:
    [node] = [node for node in arguments["nodes"] if node["id"] == _NODE_ID]
    if writer == "set_pipeline":
        call = arguments
    elif writer == "upsert_node":
        call = node
    else:
        call = {"node_id": _NODE_ID, "patch": node["options"]}
    result = execute_tool(writer, call, state, _mock_catalog())
    assert result.success, result.to_dict()
    return result.updated_state


def _stored_options(state: CompositionState) -> dict[str, Any]:
    [node] = [node for node in state.nodes if node.id == _NODE_ID]
    options: dict[str, Any] = deep_thaw(node.options)
    return options


def _assert_approved_review_survived(before: CompositionState, after: CompositionState) -> None:
    stored_before = _stored_options(before)
    stored_after = _stored_options(after)
    # Same review, same approval event, nothing reopened.
    assert _prompt_row(stored_after) == _prompt_row(stored_before)
    assert _prompt_row(stored_after)["status"] == "resolved"
    assert _prompt_row(stored_after)["event_id"] == "prompt-event"
    assert {row["id"]: (row["status"], row["event_id"]) for row in _rows(stored_after)} == {
        row["id"]: (row["status"], row["event_id"]) for row in _rows(stored_before)
    }
    assert stored_after["approved_prompt_artifact_hash"] == stored_before["approved_prompt_artifact_hash"]
    # The amended definitions are still in the stored compiled text.
    assert stored_after["prompt_template"] == _AMENDED_TEXT
    assert stored_after["prompt_template_parts"] == stored_before["prompt_template_parts"]
    # No pending card: the state still executes without a review.
    assert isinstance(materialize_state_for_execution(after), CompositionState)


# --- what the model is handed ------------------------------------------------


def test_exact_arguments_read_on_a_parts_step_carries_no_prompt_review_row() -> None:
    state = _stale_state()

    arguments = _read(state)

    options = _read_node(state)["options"]
    assert "prompt_template_parts" in options
    assert _PROMPT_KIND not in _kinds(options)
    assert _STALE not in json.dumps(arguments)
    # The other rows are still offered: the model choice and both term reviews.
    assert _kinds(options) == ["llm_model_choice", "vague_term", "vague_term"]


@pytest.mark.parametrize("writer", ["upsert_node", "patch_node_options"])
def test_applied_component_echo_on_a_parts_step_carries_no_prompt_review_row(writer: str) -> None:
    state = _stale_state()
    node = _read_node(state)
    call = {**node, "on_error": "main"} if writer == "upsert_node" else {"node_id": _NODE_ID, "patch": {"temperature": 0.1}}

    result = execute_tool(writer, call, state, _mock_catalog())

    assert result.success, result.to_dict()
    payload = result.to_dict()
    [echoed] = [echoed for echoed in payload["applied_component"]["nodes"] if echoed["id"] == _NODE_ID]
    assert "prompt_template_parts" in echoed["options"]
    assert _PROMPT_KIND not in _kinds(echoed["options"])
    assert _STALE not in json.dumps(payload)
    # The stored row is untouched by the echo's shape.
    assert _prompt_row(_stored_options(result.updated_state))["draft"] == _STALE


def test_routing_only_rewire_of_a_parts_step_echoes_nothing_and_keeps_the_approved_review() -> None:
    """``routing_only=true`` returns no applied-component echo and never hands back the stored row.

    That form takes no options (``test_upsert_node_routing_only.py`` pins the
    refusal), so the read cannot be written back through it.
    """
    state = _stale_state()
    call = {"id": _NODE_ID, "node_type": "transform", "input": state.nodes[0].input, "on_error": "main", "routing_only": True}

    result = execute_tool("upsert_node", call, state, _mock_catalog())

    assert result.success, result.to_dict()
    assert result.updated_state.nodes[0].on_error == "main" != state.nodes[0].on_error
    payload = result.to_dict()
    assert "applied_component" not in payload
    assert _STALE not in json.dumps(payload)
    _assert_approved_review_survived(state, result.updated_state)


# --- the approval survives the read being written back ------------------------


def _write_back_of_the_read_keeps_the_approved_review(writer: str) -> None:
    state = _stale_state()
    arguments = _read(state)
    assert _PROMPT_KIND not in _kinds(_read_node(state)["options"])

    after = _write_back(writer, state, arguments)

    _assert_approved_review_survived(state, after)


def test_set_pipeline_write_back_of_the_read_keeps_the_approved_prompt_review() -> None:
    _write_back_of_the_read_keeps_the_approved_review("set_pipeline")


def test_upsert_node_write_back_of_the_read_keeps_the_approved_prompt_review() -> None:
    _write_back_of_the_read_keeps_the_approved_review("upsert_node")


def test_patch_node_options_write_back_of_the_read_keeps_the_approved_prompt_review() -> None:
    _write_back_of_the_read_keeps_the_approved_review("patch_node_options")


def _prompt_review_only_state() -> CompositionState:
    """A parts step whose only stored row is the approved prompt review.

    Azure takes ``deployment_name`` and no ``model``, so no model-choice row is
    staged; text-only parts reference no term, so no term row either.
    """
    state = _approved_state(structured=False)
    options = deep_thaw(state.nodes[0].options)
    del options["model"]
    options.update({"provider": "azure", "deployment_name": "gpt-4o", "endpoint": "https://example.openai.azure.com"})
    options["prompt_template"] = "Classify the current row {{ row.text }}."
    options["prompt_template_parts"] = [{"kind": "text", "text": "Classify the current row {{ row.text }}."}]
    options["interpretation_requirements"] = [row for row in _rows(options) if row["kind"] == _PROMPT_KIND]
    row = _prompt_row(options)
    row["draft"] = prompt_review_draft_from_options(options)
    row["accepted_value"] = row["draft"]
    row["event_id"] = "prompt-event"
    row["resolved_prompt_template_hash"] = prompt_review_anchor_hash_from_options(options)
    options["approved_prompt_artifact_hash"] = approved_prompt_artifact_hash_from_options(options)
    state = state.with_node(replace(state.nodes[0], options=options))
    state = reconcile_authoritative_reviews(state, state)
    stored = _stored_options(state)
    assert _kinds(stored) == [_PROMPT_KIND] and _prompt_row(stored)["status"] == "resolved"
    assert isinstance(materialize_state_for_execution(state), CompositionState)
    return state


@pytest.mark.parametrize("writer", _WRITERS)
def test_write_back_of_an_empty_review_list_keeps_the_only_approved_prompt_review(writer: str) -> None:
    """When the prompt review is a parts step's only row, the read offers ``[]``, and ``[]`` written back removes nothing."""
    state = _prompt_review_only_state()
    stored = _stored_options(state)
    arguments = _read(state)
    assert _read_node(state)["options"]["interpretation_requirements"] == []

    after = _write_back(writer, state, arguments)

    stored_after = _stored_options(after)
    assert stored_after == stored
    assert _prompt_row(stored_after)["status"] == "resolved"
    assert _prompt_row(stored_after)["event_id"] == "prompt-event"
    assert stored_after["approved_prompt_artifact_hash"] == stored["approved_prompt_artifact_hash"]
    # No review re-opened: the state still executes without a card.
    assert isinstance(materialize_state_for_execution(after), CompositionState)


@pytest.mark.parametrize("writer", _WRITERS)
def test_a_write_without_the_prompt_review_row_does_not_reopen_an_approved_review(writer: str) -> None:
    """The write side alone: the row is taken out by hand, whatever the read returns."""
    state = _stale_state()
    arguments = _read(state)
    [node] = [node for node in arguments["nodes"] if node["id"] == _NODE_ID]
    node["options"]["interpretation_requirements"] = [row for row in _rows(node["options"]) if row["kind"] != _PROMPT_KIND]

    after = _write_back(writer, state, arguments)

    _assert_approved_review_survived(state, after)


@pytest.mark.parametrize("writer", _WRITERS)
def test_a_write_without_the_prompt_review_row_leaves_the_stored_step_identical(writer: str) -> None:
    """The row goes back where it is stored: writing the read back is not a reorder."""
    state = _stale_state()

    after = _write_back(writer, state, _read(state))

    assert _kinds(_stored_options(after)) == _kinds(_stored_options(state))
    assert _stored_options(after) == _stored_options(state)


# --- controls -----------------------------------------------------------------


def test_stored_state_still_carries_the_prompt_review_row() -> None:
    """Stored records are built from the state itself, never from the model's projection."""
    state = _stale_state()

    [stored_node] = [node for node in state.to_dict()["nodes"] if node["id"] == _NODE_ID]

    stored_row = _prompt_row(deep_thaw(stored_node["options"]))
    assert stored_row["draft"] == _STALE
    assert stored_row["status"] == "resolved" and stored_row["event_id"] == "prompt-event"
    # The single-node inspection read is the diagnostic view of the stored row.
    inspected = execute_tool("get_pipeline_state", {"component": _NODE_ID}, state, _mock_catalog()).to_dict()["data"]["node"]
    assert _prompt_row(deep_thaw(inspected["options"])) == stored_row


def test_splice_comparison_projection_still_carries_the_prompt_review_row() -> None:
    """The splice replay compares two nodes through one projection; both sides keep the row."""
    state = _stale_state()

    projection = _normalized_splice_node_projection(state.nodes[0])

    assert _PROMPT_KIND in _kinds(projection["options"])


@pytest.mark.parametrize("writer", _WRITERS)
def test_a_pending_prompt_review_stays_pending_through_a_write_back(writer: str) -> None:
    state = _stale_state(pending=True)
    before = _stored_options(state)
    assert _prompt_row(before)["status"] == "pending"

    after_state = _write_back(writer, state, _read(state))

    after = _stored_options(after_state)
    assert _prompt_row(after)["id"] == _prompt_row(before)["id"]
    assert _prompt_row(after)["status"] == "pending" and _prompt_row(after)["event_id"] is None
    assert isinstance(materialize_state_for_execution(after_state), InterpretationReviewPending)


@pytest.mark.parametrize("writer", _WRITERS)
def test_an_earlier_read_that_still_carries_the_row_is_accepted_unchanged(writer: str) -> None:
    """A model holding a read taken before this change sends the row back; nothing moves."""
    state = _stale_state()
    stored = _stored_options(state)
    arguments = _read(state)
    [node] = [node for node in arguments["nodes"] if node["id"] == _NODE_ID]
    stored_row = _prompt_row(stored)
    earlier_form = {"kind": stored_row["kind"], "user_term": stored_row["user_term"], "draft": stored_row["draft"]}
    position = _kinds(stored).index(_PROMPT_KIND)
    rows = [row for row in _rows(node["options"]) if row["kind"] != _PROMPT_KIND]
    rows.insert(position, earlier_form)
    node["options"]["interpretation_requirements"] = rows

    after = _write_back(writer, state, arguments)

    assert _stored_options(after) == stored
    _assert_approved_review_survived(state, after)


def test_exact_arguments_read_on_a_plain_step_keeps_the_prompt_review_row() -> None:
    """Without parts the model's own text is the prompt, so the read is as it was."""
    state = _approved_state(structured=False)

    options = _read_node(state)["options"]

    assert "prompt_template_parts" not in options
    assert _kinds(options) == [_PROMPT_KIND, "llm_model_choice"]
    assert options["prompt_template"] == "Classify the current row."
