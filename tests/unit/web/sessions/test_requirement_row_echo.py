"""What happens when the composer model sends a review row back with its server-owned fields.

A stored review row has nine fields; the model may author three (``kind``,
``user_term``, ``draft``). The model sees the nine-field form in two places:
``get_pipeline_state`` for a node (and the full state), and the server's replay
of its own earlier call, whose arguments are shown merged with the stored rows.
In a live run the model sent such rows back, so this file pins today's
behaviour for ``set_pipeline``, ``upsert_node`` and ``patch_node_options``:

* a row that equals the stored row field for field is recognised as an echo:
  the call is accepted and the row keeps its state;
* the three-field read form and the state-context form are accepted too, and
  the approval is kept;
* a row with any server-owned field that differs from the stored row is
  refused (``interpretation_requirements_invalid``), so a flipped status, an
  altered event id, value or hash is never honoured;
* a NEW row carrying ``status`` or ``id`` is refused, naming the field;
* only the first bad row of a node is reported.

These describe behaviour that predates the carry change, except
``test_an_exact_echo_sent_with_a_wording_edit_keeps_the_terms``, which is the
carry seen through an echo.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationChoice
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.prompt_review import reviewed_anchor_for
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool
from tests.unit.web.sessions.test_carry_terms_across_wording_edits import (
    AUDIENCE,
    AUDIENCE_AMENDED,
    CATEGORY,
    CATEGORY_DRAFT,
    NODE,
    PROMPT_REVIEW,
    TWO_TERM_ROWS,
    _approved_session,
    _carry_service,
    _errors,
    _options,
    _patch,
    _prompt,
    _read,
    _rows,
    _state_context_node,
    _with_first_text,
)
from tests.unit.web.sessions.test_prompt_file_approval import _pending_file_review


@pytest.fixture
def service() -> SessionServiceImpl:
    return _carry_service()


blob_env = blob_tools.blob_env

TOOLS = ("set_pipeline", "upsert_node", "patch_node_options")
WHOLE_NODE_TOOLS = ("set_pipeline", "upsert_node")
INVALID = "interpretation_requirements_invalid"
TONE_REF = {"kind": "interpretation_ref", "requirement_id": f"tone:{NODE}"}
NINE_FIELDS = {
    "id",
    "kind",
    "user_term",
    "status",
    "draft",
    "event_id",
    "accepted_value",
    "accepted_artifact_hash",
    "resolved_prompt_template_hash",
}


def _stored_rows(state: CompositionState) -> list[dict[str, Any]]:
    return [deep_thaw(row) for row in _options(state)[INTERPRETATION_REQUIREMENTS_KEY]]


def _send(
    tool: str,
    state: CompositionState,
    rows: list[dict[str, Any]],
    *,
    first_text: str | None = None,
    extra_parts: tuple[dict[str, str], ...] = (),
) -> ToolResult:
    """Send ``rows`` for the LLM node through one tool, with the node's parts optionally changed."""
    parts = deep_thaw(_options(state)["prompt_template_parts"])
    if first_text is not None:
        parts = _with_first_text(parts, first_text)
    parts = [*parts, *extra_parts]
    rows = copy.deepcopy(rows)
    if tool == "patch_node_options":
        patch: dict[str, Any] = {INTERPRETATION_REQUIREMENTS_KEY: rows}
        if first_text is not None or extra_parts:
            patch["prompt_template_parts"] = parts
        return _patch(state, patch)
    arguments = _read(state, "set_pipeline_arguments")
    node = arguments["nodes"][0]
    node["options"][INTERPRETATION_REQUIREMENTS_KEY] = rows
    node["options"]["prompt_template_parts"] = parts
    if tool == "set_pipeline":
        return execute_tool("set_pipeline", arguments, state, _mock_catalog())
    return execute_tool("upsert_node", node, state, _mock_catalog())


def _refusals(result: ToolResult) -> list[str]:
    assert not result.success
    return [entry["message"] for entry in _errors(result) if entry["error_code"] == INVALID]


def _with_changes(rows: list[dict[str, Any]], row_id: str, **changes: Any) -> list[dict[str, Any]]:
    changed = copy.deepcopy(rows)
    next(row for row in changed if row["id"] == row_id).update(changes)
    return changed


@pytest.mark.parametrize("form", ["nine_field_rows", "nine_field_term_rows_only", "three_field_read", "state_context"])
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_a_stored_row_sent_back_as_read_is_accepted_and_keeps_its_approval(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
    form: str,
) -> None:
    session = await _approved_session(service, tmp_path)
    stored = _stored_rows(session.state)
    assert all(set(row) == NINE_FIELDS for row in stored)
    if form == "nine_field_rows":
        rows = stored
    elif form == "nine_field_term_rows_only":
        rows = [row for row in stored if row["kind"] == "vague_term"]
    elif form == "three_field_read":
        rows = _read(session.state, "set_pipeline_arguments")["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY]
        assert all(set(row) == {"kind", "user_term", "draft"} for row in rows)
    else:
        rows = _state_context_node(session.state)["options"][INTERPRETATION_REQUIREMENTS_KEY]
        assert all("status" in row and "id" in row and "event_id" not in row for row in rows)

    result = _send(tool, session.state, rows)

    assert result.success, _errors(result)
    assert _rows(result.updated_state) == _rows(session.state)
    assert _prompt(result.updated_state) == f"Classify {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_a_pending_row_sent_back_with_its_id_is_accepted_and_stays_pending(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
) -> None:
    session = await _approved_session(service, tmp_path, leave_pending=("category",))
    stored = _stored_rows(session.state)
    assert _rows(session.state)[CATEGORY]["status"] == "pending"

    result = _send(tool, session.state, stored)

    assert result.success, _errors(result)
    after = _rows(result.updated_state)
    assert after[CATEGORY] == _rows(session.state)[CATEGORY]
    assert after[AUDIENCE] == _rows(session.state)[AUDIENCE]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"status": "pending"},
            "includes resolver-owned field(s): accepted_artifact_hash, accepted_value, event_id, id, resolved_prompt_template_hash, status.",
        ),
        ({"event_id": "00000000-0000-4000-8000-000000000000"}, "includes resolver-owned status 'resolved'."),
        ({"accepted_value": "a value the user never accepted"}, "includes resolver-owned status 'resolved'."),
        ({"resolved_prompt_template_hash": "0" * 64}, "includes resolver-owned status 'resolved'."),
    ],
    ids=["status_flipped", "event_id_altered", "accepted_value_altered", "hash_altered"],
)
@pytest.mark.parametrize(
    ("row_id", "index"), [(CATEGORY, 0), (AUDIENCE, 1), (PROMPT_REVIEW, 2)], ids=["as_drafted", "amended", "prompt_review"]
)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_an_echo_with_one_server_owned_field_changed_is_refused(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
    row_id: str,
    index: int,
    changes: dict[str, Any],
    message: str,
) -> None:
    session = await _approved_session(service, tmp_path)

    result = _send(tool, session.state, _with_changes(_stored_rows(session.state), row_id, **changes))

    refusals = _refusals(result)
    assert len(refusals) == 1
    assert refusals[0].startswith(f"Node '{NODE}': {tool} options.interpretation_requirements[{index}] {message}")
    assert "Composer tool input may" in refusals[0]


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_a_pending_row_sent_back_as_resolved_is_refused(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
) -> None:
    session = await _approved_session(service, tmp_path, leave_pending=("category",))
    claimed = _with_changes(_stored_rows(session.state), CATEGORY, status="resolved", accepted_value=CATEGORY_DRAFT)

    refusals = _refusals(_send(tool, session.state, claimed))

    assert len(refusals) == 1
    assert f"{tool} options.interpretation_requirements[0] includes resolver-owned status 'resolved'." in refusals[0]


@pytest.mark.parametrize("form", ["nine_field_rows", "three_field_read"])
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_an_exact_echo_sent_with_a_wording_edit_keeps_the_terms(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
    form: str,
) -> None:
    session = await _approved_session(service, tmp_path)
    before = _rows(session.state)
    if form == "nine_field_rows":
        rows = _stored_rows(session.state)
    else:
        rows = _read(session.state, "set_pipeline_arguments")["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY]

    result = _send(tool, session.state, rows, first_text="Group {{ row.text }} into ")

    assert result.success, _errors(result)
    after = _rows(result.updated_state)
    assert after[CATEGORY] == before[CATEGORY]
    assert after[AUDIENCE] == before[AUDIENCE]
    assert after[PROMPT_REVIEW]["status"] == "pending"
    assert _prompt(result.updated_state) == f"Group {{{{ row.text }}}} into {CATEGORY_DRAFT} for {AUDIENCE_AMENDED}."


@pytest.mark.parametrize(
    ("extra_fields", "message"),
    [
        ({"status": "pending"}, "includes resolver-owned field(s): status."),
        ({"id": f"tone:{NODE}"}, "includes resolver-owned field(s): id."),
        ({"status": "resolved", "accepted_value": "formal"}, "includes resolver-owned status 'resolved'."),
    ],
    ids=["status_pending", "id", "status_resolved"],
)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_a_new_row_carrying_a_server_owned_field_is_refused(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
    extra_fields: dict[str, str],
    message: str,
) -> None:
    """The live refusal: a row the model authored itself, sent with ``status``."""
    session = await _approved_session(service, tmp_path)
    new_row = {"kind": "vague_term", "user_term": "tone", "draft": "formal", **extra_fields}

    result = _send(tool, session.state, [*_stored_rows(session.state), new_row], extra_parts=(TONE_REF,))

    refusals = _refusals(result)
    assert len(refusals) == 1
    assert refusals[0].startswith(f"Node '{NODE}': {tool} options.interpretation_requirements[4] {message}")

    # The same row without the server-owned field is accepted beside the exact echoes.
    clean = {"kind": "vague_term", "user_term": "tone", "draft": "formal"}
    accepted = _send(tool, session.state, [*_stored_rows(session.state), clean], extra_parts=(TONE_REF,))
    assert accepted.success, _errors(accepted)
    assert _rows(accepted.updated_state)[f"tone:{NODE}"]["status"] == "pending"


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.asyncio
async def test_only_the_first_bad_row_of_a_node_is_reported(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
) -> None:
    session = await _approved_session(service, tmp_path)
    rows = [
        *_with_changes(_stored_rows(session.state), AUDIENCE, accepted_value="a value the user never accepted"),
        {"kind": "vague_term", "user_term": "tone", "draft": "formal", "status": "pending"},
    ]

    refusals = _refusals(_send(tool, session.state, rows, extra_parts=(TONE_REF,)))

    # Rows [1] and [4] are both bad; the refusal names row [1] alone.
    assert len(refusals) == 1
    assert "interpretation_requirements[1] includes resolver-owned status 'resolved'." in refusals[0]
    assert "interpretation_requirements[4]" not in refusals[0]


async def _session_with_titled_terms(service: SessionServiceImpl, tmp_path: Path) -> Any:
    """Terms authored with ``display_title``: the export then shows four-field term rows."""
    rows = [{**row, "display_title": f"What {row['user_term']} means"} for row in TWO_TERM_ROWS]
    return await _approved_session(service, tmp_path, rows=rows)


@pytest.mark.parametrize("tool", WHOLE_NODE_TOOLS)
@pytest.mark.asyncio
async def test_the_whole_node_sent_back_as_exported_changes_nothing(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
) -> None:
    """``get_pipeline_state(component="set_pipeline_arguments")`` sent back as it came.

    The export shows the model's ORIGINAL drafts and no accepted value, and a
    model-choice row. On this step with prompt parts the prompt review is not
    offered at all (test_read_drops_prompt_review_row.py). Sending all of it
    back keeps every stored option, the user's amended definition and the
    prompt review included.
    """
    session = await _session_with_titled_terms(service, tmp_path)
    stored_options = deep_thaw(_options(session.state))
    export = _read(session.state, "set_pipeline_arguments")
    node = export["nodes"][0]
    exported_rows = node["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert [set(row) for row in exported_rows] == [
        {"kind", "user_term", "draft", "display_title"},
        {"kind", "user_term", "draft", "display_title"},
        {"kind", "user_term", "draft"},
    ]
    assert [row["kind"] for row in exported_rows] == ["vague_term", "vague_term", "llm_model_choice"]
    assert any(row["kind"] == "llm_prompt_template" for row in stored_options[INTERPRETATION_REQUIREMENTS_KEY])
    assert AUDIENCE_AMENDED not in str(export)

    result = execute_tool(tool, export if tool == "set_pipeline" else node, session.state, _mock_catalog())

    assert result.success, _errors(result)
    assert deep_thaw(_options(result.updated_state)) == stored_options
    assert stored_options["prompt_template"].endswith(f"for {AUDIENCE_AMENDED}.")


@pytest.mark.parametrize("tool", WHOLE_NODE_TOOLS)
@pytest.mark.asyncio
async def test_the_exported_node_with_only_its_input_changed_keeps_prompt_and_approvals(
    service: SessionServiceImpl,
    tmp_path: Path,
    tool: str,
) -> None:
    """A routing change, as when a step is inserted beside the node, touches no review."""
    session = await _session_with_titled_terms(service, tmp_path)
    stored_options = deep_thaw(_options(session.state))
    export = _read(session.state, "set_pipeline_arguments")
    node = export["nodes"][0]
    node["input"] = "rows_rerouted"
    export["source"]["on_success"] = "rows_rerouted"

    result = execute_tool(tool, export if tool == "set_pipeline" else node, session.state, _mock_catalog())

    assert result.success, _errors(result)
    rerouted = next(candidate for candidate in result.updated_state.nodes if candidate.id == NODE)
    assert rerouted.input == "rows_rerouted"
    assert deep_thaw(rerouted.options) == stored_options


@pytest.mark.asyncio
async def test_a_file_held_prompt_node_sent_back_as_exported_keeps_its_binding_and_approval(blob_env: dict[str, Any]) -> None:
    """``upsert_node`` only: this fixture's source and sink paths are outside the session's
    permitted locations, so ``set_pipeline`` refuses the whole pipeline for that reason."""
    sessions, session_id, event_id, _blob_id = await _pending_file_review(blob_env)
    _resolved, record = await sessions.resolve_interpretation_event(
        session_id=session_id,
        event_id=event_id,
        choice=InterpretationChoice.ACCEPTED_AS_DRAFTED,
        amended_value=None,
        actor="user:test-user",
        reviewed_anchor=await reviewed_anchor_for(sessions, session_id, event_id),
        session_operation_context=blob_env["operation"],
    )
    state = state_from_record(record)
    stored_options = deep_thaw(state.nodes[0].options)
    stored_review = next(row for row in stored_options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "llm_prompt_template")
    assert stored_review["status"] == "resolved"
    tool_context = {
        "data_dir": blob_env["data_dir"],
        "session_engine": blob_env["engine"],
        "session_id": blob_env["session_id"],
        "session_operation_context": blob_env["operation"],
        "session_operation_authority": blob_env["authority"],
    }
    export = deep_thaw(
        blob_tools.execute_tool(
            "get_pipeline_state", {"component": "set_pipeline_arguments"}, state, blob_tools._catalog(), **tool_context
        ).data
    )
    node = export["nodes"][0]
    assert node["options"]["prompt_template"] == stored_options["prompt_template"]

    result = blob_tools.execute_tool("upsert_node", node, state, blob_tools._catalog(), **tool_context)

    assert result.success, _errors(result)
    after = deep_thaw(result.updated_state.nodes[0].options)
    assert after["prompt_template"] == stored_options["prompt_template"]
    assert next(row for row in after[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "llm_prompt_template") == stored_review
