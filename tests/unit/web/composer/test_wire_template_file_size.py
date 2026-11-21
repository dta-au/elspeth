"""wire_blob_inline_ref refuses a file over the template bound at a TEMPLATE position, from its recorded size.

A template position (``prompt_template``, a query's ``template``) parses the
file as Jinja, and the plugin refuses text over 65536 UTF-8 bytes at every
run. Before the change the tool bound such a file and the user was asked to
approve a review card for a binding that could never run. Now the call is
refused with the save route's sentence, naming the position and the step;
nothing is bound and the state is unchanged. ``system_prompt`` is plain text
and keeps accepting a large file; a non-text file and a non-UTF-8 binding are
refused for their own reasons first, as before.

Real files are created through the tool; expected texts are spelled here so
the file collects on the tree before the change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer.test_wire_ref_review_reconciliation import _multi_query_options, _with_node_options

blob_env = blob_tools.blob_env

LIMIT = 64 * 1024
HEAD = "Rate {{ row.x }}. "
TEMPLATE_POSITIONS = ["prompt_template", "queries.q.template"]


def _text(size: int) -> str:
    return HEAD + "x" * (size - len(HEAD))


def _state() -> CompositionState:
    return _with_node_options(blob_tools._inline_ref_state(), "classify", _multi_query_options())


def _file(blob_env: dict[str, Any], size: int) -> str:
    created = blob_tools._create_blob(blob_env, content=_text(size))
    assert created.success is True, [error.message for error in created.validation.errors]
    assert isinstance(created.data, Mapping)
    blob_id = created.data["blob_id"]
    assert isinstance(blob_id, str)
    return blob_id


def _wire(blob_env: dict[str, Any], state: CompositionState, position: str, blob_id: str, **extra: str) -> ToolResult:
    return blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": f"node:classify.options.{position}", "blob_id": blob_id, **extra},
        state,
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )


def _bound_value(state: CompositionState, position: str) -> object:
    value: object = deep_thaw(state.nodes[0].options)
    for key in position.split("."):
        assert isinstance(value, Mapping)
        value = value[key]
    return value


@pytest.mark.parametrize("position", TEMPLATE_POSITIONS)
def test_a_file_over_the_template_bound_is_refused_naming_position_and_step(blob_env: dict[str, Any], position: str) -> None:
    state = _state()
    size = LIMIT + 1

    result = _wire(blob_env, state, position, _file(blob_env, size))

    assert result.success is False
    first = result.validation.errors[0]
    assert first.error_code == "plugin_options_invalid"
    assert first.message == (
        f"{position} on step classify is {size} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    )
    assert result.updated_state == state
    assert type(_bound_value(result.updated_state, position)) is str


@pytest.mark.parametrize("position", TEMPLATE_POSITIONS)
def test_a_file_at_the_template_bound_binds(blob_env: dict[str, Any], position: str) -> None:
    blob_id = _file(blob_env, LIMIT)

    result = _wire(blob_env, _state(), position, blob_id)

    assert result.success is True, [error.message for error in result.validation.errors]
    marker = _bound_value(result.updated_state, position)
    assert isinstance(marker, Mapping)
    assert marker["blob_ref"] == blob_id


def test_a_large_file_still_binds_at_system_prompt(blob_env: dict[str, Any]) -> None:
    blob_id = _file(blob_env, 70 * 1024)

    result = _wire(blob_env, _state(), "system_prompt", blob_id)

    assert result.success is True, [error.message for error in result.validation.errors]
    marker = _bound_value(result.updated_state, "system_prompt")
    assert isinstance(marker, Mapping)
    assert marker["blob_ref"] == blob_id


def test_a_non_text_file_over_the_bound_is_refused_as_not_text_as_before(blob_env: dict[str, Any]) -> None:
    blob_id = _file(blob_env, 70 * 1024)
    with blob_env["engine"].begin() as conn:
        conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(mime_type="application/pdf"))

    result = _wire(blob_env, _state(), "prompt_template", blob_id)

    assert result.success is False
    assert result.validation.errors[0].error_code == "inline_blob_not_text"


def test_a_non_utf8_binding_over_the_bound_is_refused_for_its_encoding_as_before(blob_env: dict[str, Any]) -> None:
    result = _wire(blob_env, _state(), "prompt_template", _file(blob_env, 70 * 1024), encoding="utf-16")

    assert result.success is False
    first = result.validation.errors[0]
    assert first.error_code == "inline_blob_field_path_invalid"
    assert first.message == "File-backed LLM prompts require UTF-8 text."
