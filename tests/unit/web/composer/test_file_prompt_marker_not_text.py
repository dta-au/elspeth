"""A file-backed LLM prompt reaches the planner as its marker, never as its text.

An LLM node's ``prompt_template``, ``system_prompt`` or ``queries.<n>.template``
can be bound to a session file: the node options hold an ADR-034
``{"blob_ref", "mode": "inline_content", "sha256"}`` marker and the text lives
in the blob. Every composer turn re-sends the whole composition state to the
planner, so resolving that marker anywhere between the stored state and the
provider request would copy the file's full text into every request.

What this file pins, for each of the three positions, with a real blob whose
text holds a unique sentinel, bound through the real ``create_blob`` and
``wire_blob_inline_ref`` handlers:

1. the per-turn state-context message (``prompts.build_state_context_message``)
   carries the blob id and marker mode and not the sentinel;
2. a planner read of the node through the real ``get_pipeline_state`` handler
   and the tool-result serialiser (one node, the full state, and the exact
   ``set_pipeline`` arguments) carries the marker and not the sentinel;
3. the staged prompt-review requirement draft is the bounded file label, in the
   stored options and in what the state context shows, not the sentinel.

A positive control shows the same instruments do see text: an inline prompt
holding the sentinel appears in the state context and in the tool read.

Reach, stated honestly. This covers the state-context message and tool reads
only. It does NOT cover, and leaves undetermined: the advisor call's
transcript, SSE frames sent to the browser, and the private per-provider-call
request capture. It also does not cover cross-turn chat history: tool rows are
excluded from replay, which ``tests/unit/web/sessions/test_routes.py``
(``test_composer_chat_history_skips_audit_tool_messages`` and its sibling for
LLM-call audit rows) pins for hand-built rows; no fixture in this package
persists a real ``create_blob`` turn to replay. The tool read is serialised
with ``serialize_tool_result``; the compose loop's admitted-discovery envelope
around it is not exercised here. Within the turn that writes or reads a file,
its text is legitimately present in that turn's tool arguments and results.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_cache import serialize_tool_result
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX, build_state_context_message
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer import test_blob_inline_tools as blob_tools

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = blob_tools.blob_env

_NODE_ID = "classify"
# Plain ASCII with no character JSON escapes, so a serialised copy of the text
# would contain it byte for byte.
_SENTINEL = "ZQXSENTINEL7f3a9cPROMPTFILETEXT"
_FILE_TEXT = f"Classify {{{{ row.x }}}} carefully. {_SENTINEL}"
_FILE_LABEL_HEAD = "File-backed prompt pending full-content review:"
_PROMPT_FILE_FIELDS = ("prompt_template", "system_prompt", "queries.q.template")


def _llm_options() -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
        "model": "openai/gpt-4o",
        "prompt_template": "Base {{ row.x }}",
        "system_prompt": "You are careful.",
        "queries": {"q": {"input_fields": {"x": "x"}, "template": "Q {{ row.x }}"}},
        "required_input_fields": [],
        "schema": {"mode": "observed"},
    }


def _state_with_options(options: dict[str, Any]) -> CompositionState:
    state = blob_tools._inline_ref_state()
    return replace(state, nodes=tuple(replace(node, options=options) if node.id == _NODE_ID else node for node in state.nodes))


def _file_backed_state(blob_env: dict[str, Any], field: str) -> tuple[CompositionState, str]:
    """Bind a real sentinel-bearing blob at ``field`` through the real handlers."""
    created = blob_tools._create_blob(blob_env, content=_FILE_TEXT)
    assert created.success is True, [error.message for error in created.validation.errors]
    assert isinstance(created.data, Mapping)
    blob_id = created.data["blob_id"]
    assert isinstance(blob_id, str)

    wired = blob_tools.execute_tool(
        "wire_blob_inline_ref",
        {"field_path": f"node:{_NODE_ID}.options.{field}", "blob_id": blob_id},
        _state_with_options(_llm_options()),
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )
    assert wired.success is True, [error.message for error in wired.validation.errors]
    return wired.updated_state, blob_id


def _state_context(state: CompositionState) -> str:
    """The exact content of the state message the planner is sent each turn."""
    catalog = blob_tools._catalog()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    message = build_state_context_message(
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        schemas_loaded=frozenset(),
    )
    content = message["content"]
    assert isinstance(content, str)
    assert content.startswith(STATE_CONTEXT_PREFIX + "\n")
    return content


def _context_node_options(content: str) -> dict[str, Any]:
    payload = json.loads(content[len(STATE_CONTEXT_PREFIX) + 1 :])
    node = next(node for node in payload["current_state"]["nodes"] if node["id"] == _NODE_ID)
    options = node["options"]
    assert isinstance(options, dict)
    return options


def _option_at(options: Mapping[str, Any], field: str) -> Any:
    value: Any = options
    for key in field.split("."):
        value = value[key]
    return value


def _read_state(state: CompositionState, arguments: dict[str, Any]) -> ToolResult:
    result = blob_tools.execute_tool("get_pipeline_state", arguments, state, blob_tools._catalog())
    assert result.success is True, [error.message for error in result.validation.errors]
    return result


def _prompt_review_draft(options: Mapping[str, Any]) -> str:
    drafts = [row["draft"] for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["kind"] == "llm_prompt_template"]
    assert len(drafts) == 1, drafts
    draft = drafts[0]
    assert isinstance(draft, str)
    return draft


@pytest.mark.parametrize("field", _PROMPT_FILE_FIELDS)
def test_state_context_carries_the_marker_not_the_file_text(blob_env: dict[str, Any], field: str) -> None:
    state, blob_id = _file_backed_state(blob_env, field)

    content = _state_context(state)

    marker = _option_at(_context_node_options(content), field)
    assert isinstance(marker, dict), marker
    assert marker["blob_ref"] == blob_id
    assert marker["mode"] == "inline_content"
    assert blob_id in content
    assert _SENTINEL not in content


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"component": _NODE_ID}, id="one-node"),
        pytest.param({}, id="full-state"),
        pytest.param({"component": "set_pipeline_arguments"}, id="set-pipeline-arguments"),
    ],
)
@pytest.mark.parametrize("field", _PROMPT_FILE_FIELDS)
def test_tool_read_carries_the_marker_not_the_file_text(blob_env: dict[str, Any], field: str, arguments: dict[str, Any]) -> None:
    state, blob_id = _file_backed_state(blob_env, field)

    serialized = serialize_tool_result(_read_state(state, arguments))

    assert blob_id in serialized
    assert '"inline_content"' in serialized
    assert _SENTINEL not in serialized


@pytest.mark.parametrize("field", _PROMPT_FILE_FIELDS)
def test_prompt_review_draft_is_the_file_label_not_the_file_text(blob_env: dict[str, Any], field: str) -> None:
    state, blob_id = _file_backed_state(blob_env, field)
    node = next(node for node in state.nodes if node.id == _NODE_ID)

    stored_draft = _prompt_review_draft(node.options)
    context_draft = _prompt_review_draft(_context_node_options(_state_context(state)))

    for draft in (stored_draft, context_draft):
        assert draft.startswith(_FILE_LABEL_HEAD), draft
        assert f"{field}: file {blob_id}" in draft
        assert _SENTINEL not in draft
    assert context_draft == stored_draft


def test_positive_control_inline_prompt_text_is_visible_to_both_instruments() -> None:
    """The instruments can see prompt text: an inline prompt appears verbatim."""
    options = _llm_options()
    options["prompt_template"] = _FILE_TEXT
    state = _state_with_options(options)

    content = _state_context(state)
    serialized = serialize_tool_result(_read_state(state, {"component": _NODE_ID}))

    assert _context_node_options(content)["prompt_template"] == _FILE_TEXT
    assert _SENTINEL in content
    assert _SENTINEL in serialized
