"""``patch_node_options`` with a ``queries`` value that leaves out a query the node has.

The patch is a shallow merge: a top-level option in the patch replaces the
stored option whole. A patch naming one complete query entry on a two-query
node used to be accepted, and the other query was gone with nothing in the
result saying so.

Such a patch is now refused, names the queries it would remove, and writes
nothing. Changing one query means restating all of them; removing or renaming
a query on purpose goes through a whole-node writer, where the full node is in
front of the caller. The refusal names ``upsert_node``; ``set_pipeline`` removes
a query too and is tested here, though the text does not name it.

The refusal's sentences are guidance text and are asserted by their stable
parts only (code, node id, query names, the tool it names).
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult, build_set_pipeline_candidate, execute_tool
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _structured_llm_args, _trained_context

_NODE = "classify"
_CODE = "queries_patch_omits_existing"
_ENTRY: dict[str, Any] = {"input_fields": {"text": "text"}}
_CHANGED: dict[str, Any] = {"input_fields": {"text": "text"}, "template": "Summarise {{ row.text }}"}


def _arguments(tmp_path: Path, queries: Any) -> dict[str, Any]:
    arguments = _structured_llm_args(tmp_path)
    if queries is None:
        del arguments["nodes"][0]["options"]["queries"]
    else:
        arguments["nodes"][0]["options"]["queries"] = queries
    return arguments


def _state(tmp_path: Path, queries: Any) -> CompositionState:
    """An llm node with the given ``queries``, admitted by the real set_pipeline door."""
    candidate = build_set_pipeline_candidate(_arguments(tmp_path, queries), _empty_state(), _trained_context(data_dir=tmp_path))
    assert candidate.acceptable, candidate.result.to_dict()
    return candidate.result.updated_state


def _mapping(*names: str) -> dict[str, Any]:
    return {name: deepcopy(_ENTRY) for name in names}


def _listed(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, **deepcopy(_ENTRY)} for name in names]


def _options(state: CompositionState) -> dict[str, Any]:
    node = next(node for node in state.nodes if node.id == _NODE)
    return deep_thaw(node.options)


def _query_names(state: CompositionState) -> list[str]:
    queries = _options(state)["queries"]
    return list(queries) if isinstance(queries, dict) else [entry["name"] for entry in queries]


def _call(tool_name: str, arguments: dict[str, Any], state: CompositionState) -> ToolResult:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return execute_tool(tool_name, arguments, state, PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)


def _patch(state: CompositionState, patch: dict[str, Any]) -> ToolResult:
    return _call("patch_node_options", {"node_id": _NODE, "patch": patch}, state)


def _assert_refused(result: ToolResult, state: CompositionState, *, missing: tuple[str, ...]) -> str:
    assert result.success is False, f"accepted, leaving queries {_query_names(result.updated_state)}"
    # Nothing is written: the caller gets its own state object back.
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == _CODE
    message = result.validation.errors[0].message
    assert _CODE in message
    assert f"node {_NODE!r}" in message
    for name in missing:
        assert repr(name) in message
    assert "Nothing was changed." in message
    # Both repairs: send every query, or send the whole node to remove one.
    assert "EVERY query" in message
    assert "upsert_node" in message
    return message


# --------------------------------------------------------------------------- #
# Refused: the patch would remove a query it does not name
# --------------------------------------------------------------------------- #


def test_a_patch_naming_one_query_is_refused_rather_than_dropping_the_others(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"queries": {"first": deepcopy(_CHANGED)}})

    message = _assert_refused(result, state, missing=("second",))
    assert repr("first") not in message.split("Nothing was changed.")[0]
    assert _query_names(state) == ["first", "second"]


def test_the_refusal_names_every_query_the_patch_leaves_out(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second", "third"))

    result = _patch(state, {"queries": {"second": deepcopy(_CHANGED)}})

    _assert_refused(result, state, missing=("first", "third"))


def test_a_patch_renaming_a_query_is_refused(tmp_path: Path) -> None:
    """Old name left out, new name added: indistinguishable from a dropped query, so it goes to a whole-node writer."""
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"queries": {"first": deepcopy(_ENTRY), "renamed": deepcopy(_ENTRY)}})

    _assert_refused(result, state, missing=("second",))


def test_a_list_form_patch_leaving_out_a_named_query_is_refused(tmp_path: Path) -> None:
    state = _state(tmp_path, _listed("first", "second"))

    result = _patch(state, {"queries": [{"name": "first", **deepcopy(_CHANGED)}]})

    _assert_refused(result, state, missing=("second",))


def test_a_list_form_patch_entry_without_a_name_names_no_query(tmp_path: Path) -> None:
    """An entry with no ``name`` cannot stand for a stored query, so the stored query counts as left out."""
    state = _state(tmp_path, _listed("first", "second"))

    result = _patch(state, {"queries": [{"name": "first", **deepcopy(_ENTRY)}, deepcopy(_ENTRY)]})

    _assert_refused(result, state, missing=("second",))


def test_a_mapping_patch_over_list_form_queries_is_matched_by_name(tmp_path: Path) -> None:
    state = _state(tmp_path, _listed("first", "second"))

    refused = _patch(state, {"queries": {"first": deepcopy(_CHANGED)}})
    accepted = _patch(state, {"queries": {"first": deepcopy(_CHANGED), "second": deepcopy(_ENTRY)}})

    _assert_refused(refused, state, missing=("second",))
    assert accepted.success is True, accepted.to_dict()
    assert _query_names(accepted.updated_state) == ["first", "second"]


def test_the_refusal_code_has_registry_guidance(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))
    message = _assert_refused(_patch(state, {"queries": {"first": deepcopy(_CHANGED)}}), state, missing=("second",))

    for error_text in (_CODE, message):
        explained = _call("explain_validation_error", {"error_text": error_text}, state)
        assert explained.success, explained.to_dict()
        assert explained.data["error_code"] == _CODE
        assert "upsert_node" in explained.data["suggested_fix"]


# --------------------------------------------------------------------------- #
# Accepted: every stored query is restated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("list_form", [False, True])
def test_a_patch_restating_every_query_with_one_changed_is_accepted(tmp_path: Path, list_form: bool) -> None:
    state = _state(tmp_path, _listed("first", "second") if list_form else _mapping("first", "second"))
    patch_queries: Any = (
        [{"name": "first", **deepcopy(_CHANGED)}, {"name": "second", **deepcopy(_ENTRY)}]
        if list_form
        else {"first": deepcopy(_CHANGED), "second": deepcopy(_ENTRY)}
    )

    result = _patch(state, {"queries": patch_queries})

    assert result.success is True, result.to_dict()
    assert _query_names(result.updated_state) == ["first", "second"]
    assert _options(result.updated_state)["queries"] == patch_queries


def test_a_patch_adding_a_query_while_restating_the_others_is_accepted(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"queries": _mapping("first", "second", "third")})

    assert result.success is True, result.to_dict()
    assert _query_names(result.updated_state) == ["first", "second", "third"]


def test_a_patch_without_a_queries_key_leaves_the_queries_alone(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"pool_size": 3})

    assert result.success is True, result.to_dict()
    assert _options(result.updated_state)["pool_size"] == 3
    assert _options(result.updated_state)["queries"] == _mapping("first", "second")


# --------------------------------------------------------------------------- #
# Unchanged by this check: what these patches did before, they still do
# --------------------------------------------------------------------------- #


def test_a_patch_setting_queries_to_null_still_does_what_it_did(tmp_path: Path) -> None:
    """``null`` deletes the option (merge-patch rule). The check does not apply: the caller named the whole option."""
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"queries": None})

    assert _CODE not in [error.error_code for error in result.validation.errors]
    assert result.success is True, result.to_dict()
    assert "queries" not in _options(result.updated_state)
    # The tool description says so in these words.
    description = next(tool["description"] for tool in get_tool_definitions() if tool["name"] == "patch_node_options")
    assert "queries: null removes every query" in description


def test_a_node_left_without_queries_by_a_null_patch_validates_as_it_does_today(tmp_path: Path) -> None:
    """What ``queries: null`` leaves behind on a multi-query llm node, at validation."""
    state = _state(tmp_path, _mapping("first", "second"))

    result = _patch(state, {"queries": None})

    print("NULLV success=", result.success, "is_valid=", result.validation.is_valid)
    print("NULLV errors=", [(e.error_code, e.message[:300]) for e in result.validation.errors])
    print("NULLV warnings=", [(w.error_code, w.message[:300]) for w in result.validation.warnings])
    print("NULLV revalidate=", [(e.error_code, e.message[:300]) for e in result.updated_state.validate().errors])
    assert result.success is True


def test_a_queries_patch_on_a_node_with_no_queries_is_not_this_refusal(tmp_path: Path) -> None:
    state = _state(tmp_path, None)
    assert "queries" not in _options(state)

    result = _patch(state, {"queries": _mapping("first", "second")})

    assert _CODE not in [error.error_code for error in result.validation.errors]
    assert result.success is True, result.to_dict()
    assert _query_names(result.updated_state) == ["first", "second"]


# --------------------------------------------------------------------------- #
# The repair the refusal names for a deliberate removal works
# --------------------------------------------------------------------------- #


def test_upsert_node_removes_a_query(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))
    node = deepcopy(_arguments(tmp_path, _mapping("first"))["nodes"][0])

    result = _call("upsert_node", node, state)

    assert result.success is True, result.to_dict()
    assert _query_names(result.updated_state) == ["first"]


def test_set_pipeline_removes_a_query(tmp_path: Path) -> None:
    state = _state(tmp_path, _mapping("first", "second"))

    candidate = build_set_pipeline_candidate(_arguments(tmp_path, _mapping("first")), state, _trained_context(data_dir=tmp_path))

    assert candidate.acceptable, candidate.result.to_dict()
    assert _query_names(candidate.result.updated_state) == ["first"]
