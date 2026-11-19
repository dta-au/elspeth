"""A structured prompt re-sent with its own text keeps its structure and reviews.

The composer model sees a structured node's compiled ``prompt_template`` in its
per-turn context. When it rewrites a node or the whole pipeline to change
something else, it can send that text back without ``prompt_template_parts``
and without the requirement rows. Identical text is not a replacement: the
stored parts, term reviews and approvals must survive the write.
"""

from dataclasses import replace

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import _serialize_set_pipeline_arguments
from elspeth.web.interpretation_state import reconcile_authoritative_reviews
from tests.unit.web.composer.test_prompt_patch_reviews import _amended_approved_state, _approved_state
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

_WHOLE_NODE_WRITERS = ("upsert_node", "set_pipeline")


def _slot_state(slots: str) -> CompositionState:
    if slots == "accepted":
        return _approved_state(structured=True)
    if slots == "two-accepted":
        return _amended_approved_state()
    assert slots == "accepted-and-pending"
    return _amended_approved_state(pending_audience=True)


def _resend(
    writer: str,
    state: CompositionState,
    *,
    prompt: str,
    with_rows: bool,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """The node as the authoring export gives it, with the prompt as plain text and no parts."""
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    node = next(item for item in args["nodes"] if item["id"] == "code_themes")
    dropped = {"prompt_template_parts"} if with_rows else {"prompt_template_parts", "interpretation_requirements"}
    node["options"] = {
        **{key: value for key, value in node["options"].items() if key not in dropped},
        "prompt_template": prompt,
        **(extra or {}),
    }
    return node if writer == "upsert_node" else args


@pytest.mark.parametrize("writer", _WHOLE_NODE_WRITERS)
@pytest.mark.parametrize("slots", ["accepted", "two-accepted", "accepted-and-pending"])
@pytest.mark.parametrize("with_rows", [False, True], ids=["no-rows", "export-rows"])
def test_node_resent_with_its_stored_prompt_text_and_no_parts_keeps_structure_and_reviews(writer: str, slots: str, with_rows: bool) -> None:
    state = _slot_state(slots)
    stored = deep_thaw(state.nodes[0].options)

    result = execute_tool(writer, _resend(writer, state, prompt=stored["prompt_template"], with_rows=with_rows), state, _mock_catalog())

    assert result.success, result.validation.errors
    options = deep_thaw(result.updated_state.nodes[0].options)
    assert options["prompt_template_parts"] == stored["prompt_template_parts"]
    # Whole-row equality: ids, statuses, accepted values, events and hashes.
    assert options["interpretation_requirements"] == stored["interpretation_requirements"]
    assert options == stored
    assert result.data is None or "superseded_review_ids" not in result.data


@pytest.mark.parametrize("writer", _WHOLE_NODE_WRITERS)
def test_the_rest_of_a_resent_node_still_applies(writer: str) -> None:
    """Keeping the prompt does not make the call a no-op: another option in the same write lands."""
    state = _approved_state(structured=True)
    stored = deep_thaw(state.nodes[0].options)
    assert "temperature" not in stored

    result = execute_tool(
        writer,
        _resend(writer, state, prompt=stored["prompt_template"], with_rows=False, extra={"temperature": 0.25}),
        state,
        _mock_catalog(),
    )

    assert result.success, result.validation.errors
    options = deep_thaw(result.updated_state.nodes[0].options)
    assert options["temperature"] == 0.25
    assert options["prompt_template_parts"] == stored["prompt_template_parts"]
    assert [row for row in options["interpretation_requirements"] if row["kind"] == "vague_term"] == [
        row for row in stored["interpretation_requirements"] if row["kind"] == "vague_term"
    ]


@pytest.mark.parametrize("writer", _WHOLE_NODE_WRITERS)
@pytest.mark.parametrize("variant", ["trailing-newline", "crlf", "double-space"])
def test_text_that_differs_only_in_whitespace_is_a_change_and_is_refused_not_swallowed(writer: str, variant: str) -> None:
    """Equal means the same string. A near-copy without parts is refused, so no edit is silently dropped and nothing is flattened."""
    state = _amended_approved_state(literal_prefix="Read the row.\n")
    stored_prompt = state.nodes[0].options["prompt_template"]
    assert "\n" in stored_prompt and "\r" not in stored_prompt and "  " not in stored_prompt
    resent = {
        "trailing-newline": stored_prompt + "\n",
        "crlf": stored_prompt.replace("\n", "\r\n"),
        "double-space": stored_prompt.replace("Classify ", "Classify  ", 1),
    }[variant]
    assert resent != stored_prompt
    before = state.to_dict()

    result = execute_tool(writer, _resend(writer, state, prompt=resent, with_rows=False), state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state and state.to_dict() == before
    assert "prompt_template_parts_required" in [error.error_code for error in result.validation.errors]


@pytest.mark.parametrize("writer", _WHOLE_NODE_WRITERS)
def test_omitting_the_requirement_rows_restores_nothing_when_the_prompt_changed(writer: str) -> None:
    """Stored rows come back only when the prompt text is unchanged; a changed prompt sent without its rows is judged as before."""
    state = _approved_state(structured=True)
    before = state.to_dict()
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    options = next(item for item in args["nodes"] if item["id"] == "code_themes")["options"]
    del options["interpretation_requirements"]
    options["prompt_template_parts"][0]["text"] = "Sort "
    options["prompt_template"] = options["prompt_template"].replace("Classify ", "Sort ", 1)

    result = execute_tool(writer, args["nodes"][0] if writer == "upsert_node" else args, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state and state.to_dict() == before
    assert [error.error_code for error in result.validation.errors] == ["review_reconciliation_failed"]


@pytest.mark.parametrize("writer", ("patch_node_options", *_WHOLE_NODE_WRITERS))
@pytest.mark.parametrize("parts_null", [True, False], ids=["null-parts", "no-parts-key"])
@pytest.mark.parametrize("slots", ["accepted", "two-accepted"])
def test_the_exports_placeholder_prompt_text_sent_back_changes_nothing(writer: str, parts_null: bool, slots: str) -> None:
    """The authoring export renders accepted terms as placeholder words.

    That text is a rendering of the stored prompt, not new text: sent back as a
    plain ``prompt_template`` it must leave the stored prompt byte for byte and
    never write the placeholder words into it.
    """
    state = _slot_state(slots)
    stored = deep_thaw(state.nodes[0].options)
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    exported = args["nodes"][0]["options"]["prompt_template"]
    assert "pending interpretation" in exported and exported != stored["prompt_template"]
    written: dict[str, object] = {"prompt_template": exported, **({"prompt_template_parts": None} if parts_null else {})}
    if writer == "patch_node_options":
        call: dict[str, object] = {"node_id": "code_themes", "patch": written}
    else:
        node = args["nodes"][0]
        node["options"] = {
            **{key: value for key, value in node["options"].items() if key not in ("prompt_template_parts", "interpretation_requirements")},
            **written,
        }
        call = node if writer == "upsert_node" else args

    result = execute_tool(writer, call, state, _mock_catalog())

    assert result.success, result.validation.errors
    options = deep_thaw(result.updated_state.nodes[0].options)
    assert "pending interpretation" not in options["prompt_template"]
    assert options == stored
    assert result.data is None or "superseded_review_ids" not in result.data


_ALL_WRITERS = ("patch_node_options", *_WHOLE_NODE_WRITERS)


def _rendering(state: CompositionState, rendering: str, node_id: str = "code_themes") -> str:
    """One of the two texts the server hands out for a node's stored prompt."""
    if rendering == "stored-text":
        return next(node for node in state.nodes if node.id == node_id).options["prompt_template"]
    assert rendering == "export-text"
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    return next(item for item in args["nodes"] if item["id"] == node_id)["options"]["prompt_template"]


@pytest.mark.parametrize("writer", _ALL_WRITERS)
@pytest.mark.parametrize("rendering", ["stored-text", "export-text"])
def test_step_resent_with_a_server_rendering_and_no_rows_keeps_rows_definitions_and_approvals(writer: str, rendering: str) -> None:
    """A re-sent unchanged step needs no requirement rows: the stored rows, the amended definition and the approvals stay."""
    state = _amended_approved_state()
    stored = deep_thaw(state.nodes[0].options)
    amended = next(row for row in stored["interpretation_requirements"] if row["id"] == "category")
    assert amended["status"] == "resolved" and amended["accepted_value"] != amended["draft"]
    text = _rendering(state, rendering)
    if writer == "patch_node_options":
        call: dict[str, object] = {"node_id": "code_themes", "patch": {"prompt_template": text}}
    else:
        call = _resend(writer, state, prompt=text, with_rows=False)

    result = execute_tool(writer, call, state, _mock_catalog())

    assert result.success, result.validation.errors
    options = deep_thaw(result.updated_state.nodes[0].options)
    assert next(row for row in options["interpretation_requirements"] if row["id"] == "category") == amended
    assert options == stored
    assert result.data is None or "superseded_review_ids" not in result.data


def _two_structured_steps() -> CompositionState:
    """code_themes -> second, each an approved structured prompt with its own accepted term."""
    state = _approved_state(structured=True)
    first = state.nodes[0]
    second_options = deep_thaw(first.options)
    for row in second_options["interpretation_requirements"]:
        row["id"] = row["id"].replace("code_themes", "second")
        row["user_term"] = row["user_term"].replace("code_themes", "second")
    second = replace(first, id="second", input="themes_out", on_success="main", options=second_options)
    state = state.with_node(replace(first, on_success="themes_out")).with_node(second)
    return reconcile_authoritative_reviews(state, state)


def test_set_pipeline_with_rows_omitted_for_one_step_and_sent_for_another_keeps_both() -> None:
    state = _two_structured_steps()
    stored = {node.id: deep_thaw(node.options) for node in state.nodes}
    args, error = _serialize_set_pipeline_arguments(state)
    assert error is None and args is not None
    first, second = args["nodes"]
    # First step: its stored text, no parts, no rows. Second step: exactly as exported, rows included.
    first["options"] = {
        **{key: value for key, value in first["options"].items() if key not in ("prompt_template_parts", "interpretation_requirements")},
        "prompt_template": stored["code_themes"]["prompt_template"],
    }
    assert "interpretation_requirements" in second["options"]

    result = execute_tool("set_pipeline", args, state, _mock_catalog())

    assert result.success, result.validation.errors
    assert {node.id: deep_thaw(node.options) for node in result.updated_state.nodes} == stored


@pytest.mark.parametrize("writer", _ALL_WRITERS)
def test_step_resent_with_its_stored_text_and_an_empty_row_list_is_refused_and_nothing_is_cleared(writer: str) -> None:
    """An empty list is a statement, not an omission: it says the step has no rows while its prompt still cites them."""
    state = _approved_state(structured=True)
    before = state.to_dict()
    text = state.nodes[0].options["prompt_template"]
    if writer == "patch_node_options":
        call: dict[str, object] = {"node_id": "code_themes", "patch": {"prompt_template": text, "interpretation_requirements": []}}
    else:
        call = _resend(writer, state, prompt=text, with_rows=False, extra={"interpretation_requirements": []})

    result = execute_tool(writer, call, state, _mock_catalog())

    assert not result.success
    assert result.updated_state is state and state.to_dict() == before
    assert [error.error_code for error in result.validation.errors] == ["review_reconciliation_failed"]
