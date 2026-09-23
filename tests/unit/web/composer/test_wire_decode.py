"""Wire decode, envelope encode and W-conformance (``tools/wire_projection.py``).

``decode_wire_arguments`` turns the arguments a provider sent under one
dialect's wire schema W back into the flat semantic arguments S admits:

* ``wire_conformant`` classifies the raw arguments against the W that was
  sent. It never rejects: S stays the only thing that admits or rejects;
* the set_pipeline ``{"pipeline": ...}`` envelope is unwrapped, and a
  malformed envelope is the one rejection (``ToolArgumentError``,
  category ``wire_envelope``);
* on ``openai_strict`` only, a ``null`` at one of the 13 promoted positions
  becomes the omission S expects. Nothing else is touched.

``encode_semantic_arguments`` is the inverse of the envelope only in S1; the
"write null for an absent promoted key" branch lands in S2 with its first
reader.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import NodeSpec
from elspeth.web.composer.tools._dispatch import (
    _TOOL_SCHEMA_BY_NAME,
    _validate_tool_arguments,
    require_schema_valid_arguments,
)
from elspeth.web.composer.tools.wire_projection import (
    _WIRE_TOOL_DEFS,
    _WIRE_VALIDATORS,
    DecodedArguments,
    WireProjectionError,
    decode_wire_arguments,
    encode_semantic_arguments,
)
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool

NONE = ToolContractDialect.NONE
STRICT = ToolContractDialect.OPENAI_STRICT

# One minimal S-valid call for each of the 32 strict-capable tools: the
# flat-required keys only. The promoted positions are filled from
# ``_PROMOTED_VALUES`` below.
_SEEDS: dict[str, dict[str, Any]] = {
    "list_blobs": {},
    "list_composer_blobs": {},
    "get_blob_metadata": {"blob_id": "blob-1"},
    "get_blob_content": {"blob_id": "blob-1"},
    "create_blob": {"filename": "rows.csv", "mime_type": "text/csv", "content": "a,b\n1,2\n"},
    "update_blob": {"blob_id": "blob-1", "content": "a,b\n3,4\n"},
    "delete_blob": {"blob_id": "blob-1"},
    "wire_blob_inline_ref": {"field_path": "document", "blob_id": "00000000-0000-4000-8000-000000000000"},
    "list_sources": {},
    "clear_source": {},
    "inspect_source": {"blob_id": "blob-1"},
    "get_pipeline_state": {},
    "get_plugin_schema": {"plugin_type": "source", "name": "csv"},
    "get_expression_grammar": {},
    "explain_validation_error": {"error_text": "unknown node_type"},
    "get_plugin_assistance": {"plugin_type": "transform", "plugin_name": "passthrough"},
    "get_audit_info": {},
    "list_models": {},
    "preview_pipeline": {},
    "diff_pipeline": {},
    "list_transforms": {},
    "list_sinks": {},
    "upsert_edge": {"id": "e1", "from_node": "a", "to_node": "b", "edge_type": "on_success"},
    "remove_node": {"id": "a"},
    "remove_edge": {"id": "e1"},
    "set_metadata": {"patch": {}},
    "remove_output": {"sink_name": "rows_out"},
    "list_secret_refs": {},
    "validate_secret_ref": {"name": "OPENROUTER_API_KEY"},
    "request_advisor_hint": {
        "trigger": "proactive_security_safety",
        "problem_summary": "a secret appears in a source option",
        "recent_errors": [],
        "attempted_actions": [],
    },
    "request_interpretation_review": {"affected_node_id": "classify", "kind": "vague_term", "user_term": "important"},
    "wire_secret_ref": {"name": "OPENROUTER_API_KEY", "target": "node", "option_key": "api_key"},
}

# A valid (non-null) value for each of the 13 promoted positions, valid under
# both S and the strict W.
_PROMOTED_VALUES: dict[tuple[str, tuple[str, ...]], Any] = {
    ("create_blob", ("description",)): "sample rows",
    ("wire_blob_inline_ref", ("encoding",)): "utf-16",
    ("clear_source", ("source_name",)): "source",
    ("get_pipeline_state", ("component",)): "nodes",
    ("get_plugin_assistance", ("issue_code",)): "no_help",
    ("list_models", ("provider",)): "openrouter/",
    ("list_models", ("limit",)): 5,
    ("upsert_edge", ("label",)): "main path",
    ("set_metadata", ("patch", "name")): "Pipeline name",
    ("set_metadata", ("patch", "description")): "Pipeline description",
    ("request_advisor_hint", ("schema_excerpt",)): '{"type": "object"}',
    ("request_interpretation_review", ("llm_draft",)): "important means priority 1",
    ("wire_secret_ref", ("target_id",)): "classify",
}

_S_VALIDATORS: dict[str, Draft202012Validator] = {name: Draft202012Validator(deep_thaw(_TOOL_SCHEMA_BY_NAME[name])) for name in _SEEDS}


def _strict_capable_names() -> set[str]:
    return {name for name, tool in _WIRE_TOOL_DEFS[STRICT].items() if tool.strict_capable}


def _promoted_paths(tool: str) -> list[tuple[str, ...]]:
    return sorted(path for name, path in _PROMOTED_VALUES if name == tool)


def _put(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = document
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _drop(document: dict[str, Any], path: tuple[str, ...]) -> None:
    node = document
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]


def _full_call(tool: str) -> dict[str, Any]:
    """The seed with every promoted position present with a value."""
    call = deepcopy(_SEEDS[tool])
    for path in _promoted_paths(tool):
        _put(call, path, _PROMOTED_VALUES[(tool, path)])
    return call


def _s_valid(tool: str, arguments: Any) -> bool:
    return not any(_S_VALIDATORS[tool].iter_errors(arguments))


def _semantic(decoded: DecodedArguments) -> dict[str, Any]:
    thawed: dict[str, Any] = deep_thaw(decoded.semantic)
    return thawed


# ---------------------------------------------------------------- the seeds


def test_the_seeds_cover_exactly_the_32_strict_capable_tools_and_13_promoted_positions() -> None:
    assert set(_SEEDS) == _strict_capable_names()
    promoted = {(name, path) for name in _SEEDS for path in _WIRE_TOOL_DEFS[STRICT][name].promoted_paths}
    assert set(_PROMOTED_VALUES) == promoted
    assert len(promoted) == 13


@pytest.mark.parametrize("tool", sorted(_SEEDS))
def test_every_seed_and_its_full_call_are_s_valid(tool: str) -> None:
    """A bad seed would make the decode law vacuous, so each is checked first."""
    assert _s_valid(tool, _SEEDS[tool])
    assert _s_valid(tool, _full_call(tool))
    require_schema_valid_arguments(tool, _full_call(tool))


# ---------------------------------------------------------------- STRIP_NULL


@pytest.mark.parametrize(("tool", "path"), sorted(_PROMOTED_VALUES))
def test_a_null_at_a_promoted_position_becomes_absent(tool: str, path: tuple[str, ...]) -> None:
    raw = _full_call(tool)
    _put(raw, path, None)
    expected = _full_call(tool)
    _drop(expected, path)

    decoded = decode_wire_arguments(tool, STRICT, raw)

    assert _semantic(decoded) == expected
    assert decoded.wire_conformant is True
    assert _s_valid(tool, _semantic(decoded))


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        ("upsert_edge", ("from_node",)),
        ("get_plugin_assistance", ("plugin_name",)),
        ("create_blob", ("content",)),
        ("request_interpretation_review", ("user_term",)),
    ],
)
def test_a_null_at_a_non_promoted_position_is_left_for_s(tool: str, path: tuple[str, ...]) -> None:
    raw = _full_call(tool)
    _put(raw, path, None)

    decoded = decode_wire_arguments(tool, STRICT, raw)

    assert _semantic(decoded) == raw
    assert decoded.wire_conformant is False
    assert not _s_valid(tool, _semantic(decoded))


def test_none_dialect_never_strips_a_null() -> None:
    """On ``none`` no promotion was sent, so a ``null`` is the model's own and S decides."""
    for tool, path in sorted(_PROMOTED_VALUES):
        raw = _full_call(tool)
        _put(raw, path, None)
        decoded = decode_wire_arguments(tool, NONE, raw)
        assert _semantic(decoded) == raw


def test_decode_does_not_mutate_the_raw_arguments() -> None:
    raw = _full_call("list_models")
    raw["provider"] = None
    snapshot = deepcopy(raw)

    decode_wire_arguments("list_models", STRICT, raw)

    assert raw == snapshot


# ---------------------------------------------------------------- classification only


@pytest.mark.parametrize("tool", sorted({name for name, _ in _PROMOTED_VALUES}))
def test_an_omitted_wire_required_key_passes_through_and_is_not_conformant(tool: str) -> None:
    """The seed omits every promoted key: S admits it, W does not, decode classifies."""
    raw = deepcopy(_SEEDS[tool])

    decoded = decode_wire_arguments(tool, STRICT, raw)

    assert _semantic(decoded) == raw
    assert decoded.wire_conformant is False
    assert _s_valid(tool, _semantic(decoded))


def test_decode_never_rejects_on_a_w_failure() -> None:
    """A raw that fails W but passes S gives ``wire_conformant=False`` and no exception."""
    raw = {"provider": "openrouter/"}
    assert any(_WIRE_VALIDATORS[STRICT]["list_models"].iter_errors(raw))
    assert _s_valid("list_models", raw)

    decoded = decode_wire_arguments("list_models", STRICT, raw)

    assert decoded.wire_conformant is False
    assert _semantic(decoded) == raw


@pytest.mark.parametrize("tool", sorted(_SEEDS))
def test_a_full_call_is_conformant_on_both_dialects(tool: str) -> None:
    for dialect in (NONE, STRICT):
        decoded = decode_wire_arguments(tool, dialect, _full_call(tool))
        assert decoded.wire_conformant is True
        assert _semantic(decoded) == _full_call(tool)


# ---------------------------------------------------------------- patch map


def test_a_null_inside_a_patch_map_is_stripped_only_at_a_declared_promoted_key() -> None:
    """``patch.name`` is promoted; an undeclared ``patch`` key is left for S to reject, as today."""
    raw = {"patch": {"name": None, "description": "kept", "x_undeclared": None}}

    decoded = decode_wire_arguments("set_metadata", STRICT, raw)

    assert _semantic(decoded) == {"patch": {"description": "kept", "x_undeclared": None}}
    assert not _s_valid("set_metadata", _semantic(decoded))
    # Equivalence: the same undeclared key is rejected by S with or without decode.
    assert not _s_valid("set_metadata", {"patch": {"description": "kept", "x_undeclared": None}})


def test_a_null_patch_map_is_left_for_s() -> None:
    raw: dict[str, Any] = {"patch": None}

    decoded = decode_wire_arguments("set_metadata", STRICT, raw)

    assert _semantic(decoded) == {"patch": None}
    assert not _s_valid("set_metadata", _semantic(decoded))


# ---------------------------------------------------------------- decode law

_PRESENCE = st.sampled_from(("value", "null", "absent"))


@given(data=st.data())
@pytest.mark.parametrize("tool", sorted({name for name, _ in _PROMOTED_VALUES}))
def test_decode_law_over_every_promoted_position(tool: str, data: st.DataObject) -> None:
    """On ``openai_strict``: semantic = raw minus null promoted keys, S-valid, conformant iff all present."""
    raw = deepcopy(_SEEDS[tool])
    expected = deepcopy(_SEEDS[tool])
    all_present = True
    for path in _promoted_paths(tool):
        presence = data.draw(_PRESENCE, label=".".join(path))
        if presence == "value":
            _put(raw, path, _PROMOTED_VALUES[(tool, path)])
            _put(expected, path, _PROMOTED_VALUES[(tool, path)])
        elif presence == "null":
            _put(raw, path, None)
        else:
            all_present = False
    snapshot = deepcopy(raw)

    decoded = decode_wire_arguments(tool, STRICT, raw)

    assert _semantic(decoded) == expected
    assert _s_valid(tool, _semantic(decoded))
    assert decoded.wire_conformant is all_present
    assert raw == snapshot
    none_decoded = decode_wire_arguments(tool, NONE, raw)
    assert _semantic(none_decoded) == raw


# ---------------------------------------------------------------- envelope

_PIPELINE: dict[str, Any] = {
    "source": {"plugin": "csv", "on_success": "rows", "options": {"path": "rows.csv"}},
    "nodes": [],
    "edges": [],
    "outputs": [],
}


@pytest.mark.parametrize("dialect", [NONE, STRICT])
def test_envelope_round_trip(dialect: ToolContractDialect) -> None:
    encoded = encode_semantic_arguments("set_pipeline", dialect, _PIPELINE)

    assert encoded == {"pipeline": _PIPELINE}
    assert _semantic(decode_wire_arguments("set_pipeline", dialect, encoded)) == _PIPELINE


@pytest.mark.parametrize("dialect", [NONE, STRICT])
def test_encode_returns_other_tools_unchanged(dialect: ToolContractDialect) -> None:
    for tool in sorted(_SEEDS):
        assert encode_semantic_arguments(tool, dialect, _full_call(tool)) == _full_call(tool)


@pytest.mark.parametrize("dialect", [NONE, STRICT])
def test_a_valid_enveloped_set_pipeline_is_conformant(dialect: ToolContractDialect) -> None:
    decoded = decode_wire_arguments("set_pipeline", dialect, {"pipeline": deepcopy(_PIPELINE)})

    assert decoded.wire_conformant is True
    assert _semantic(decoded) == _PIPELINE
    # Control: the unwrapped form, held to the same W, is not conformant.
    assert any(_WIRE_VALIDATORS[dialect]["set_pipeline"].iter_errors(deepcopy(_PIPELINE)))


@pytest.mark.parametrize("dialect", [NONE, STRICT])
@pytest.mark.parametrize(
    "raw",
    [{}, {"pipeline": 1}, {"pipeline": {"nodes": [], "edges": [], "outputs": []}, "x": 1}],
    ids=["empty", "pipeline-not-object", "extra-key"],
)
def test_decode_rejects_a_malformed_set_pipeline_envelope(dialect: ToolContractDialect, raw: dict[str, Any]) -> None:
    with pytest.raises(ToolArgumentError) as info:
        decode_wire_arguments("set_pipeline", dialect, raw)
    assert info.value.category is ToolArgumentErrorCategory.WIRE_ENVELOPE


def test_the_envelope_rejection_keeps_todays_argument_text() -> None:
    """The fields match the compose loop's pre-dispatch envelope rejection (``tool_batch``)."""
    from elspeth.web.composer.tool_batch import _pre_dispatch_argument_error

    with pytest.raises(ToolArgumentError) as info:
        decode_wire_arguments("set_pipeline", NONE, {})
    today = _pre_dispatch_argument_error("set_pipeline", ToolArgumentErrorCategory.WIRE_ENVELOPE)

    assert str(info.value) == str(today)
    assert info.value.category is today.category


# ---------------------------------------------------------------- names (D17)


@pytest.mark.parametrize("dialect", [NONE, STRICT])
def test_an_unknown_tool_name_is_a_caller_bug(dialect: ToolContractDialect) -> None:
    with pytest.raises(WireProjectionError):
        decode_wire_arguments("not_a_tool", dialect, {})
    with pytest.raises(WireProjectionError):
        encode_semantic_arguments("not_a_tool", dialect, {})


# ---------------------------------------------------------------- presence semantics


@pytest.mark.parametrize("dialect", [NONE, STRICT])
def test_set_pipeline_null_sources_is_admitted_through_the_web_wire_iff_admitted_flat(
    dialect: ToolContractDialect,
) -> None:
    """In S1 set_pipeline is non-strict, so decode leaves ``sources: null`` alone.

    S then decides exactly as it does for MCP's flat call, and today it rejects
    (``sources`` is an object with no ``null``). The master plan's "accepted via
    the web wire" case belongs to S2, where ``sources`` becomes a promoted
    position.
    """
    flat = {**deepcopy(_PIPELINE), "sources": None}
    decoded = decode_wire_arguments("set_pipeline", dialect, {"pipeline": deepcopy(flat)})
    assert _semantic(decoded) == flat

    def web_admitted() -> bool:
        try:
            require_schema_valid_arguments("set_pipeline", _semantic(decoded))
        except ToolArgumentError:
            return False
        return True

    def mcp_admitted() -> bool:
        try:
            _validate_tool_arguments("set_pipeline", flat, _empty_state(), raise_on_error=True)
        except ToolArgumentError:
            return False
        return True

    assert web_admitted() is mcp_admitted()
    assert web_admitted() is False


# ---------------------------------------------------------------- tri-state premise


def _node(node_id: str, input_name: str, on_success: str) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="passthrough",
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def test_null_and_absent_have_the_same_handler_result_at_the_already_nullable_positions() -> None:
    """Master plan §3.4 premise: ``null`` has no distinct S meaning at a promoted position."""
    state = _empty_state().with_node(_node("a", "rows", "mid")).with_node(_node("b", "mid", "out"))
    catalog = _mock_catalog()
    edge = {"id": "e1", "from_node": "a", "to_node": "b", "edge_type": "on_success"}

    with_null = execute_tool("upsert_edge", {**edge, "label": None}, state, catalog)
    absent = execute_tool("upsert_edge", dict(edge), state, catalog)
    assert with_null.success is True
    assert with_null.updated_state == absent.updated_state
    assert with_null.to_dict() == absent.to_dict()

    assistance = {"plugin_type": "transform", "plugin_name": "passthrough"}
    null_code = execute_tool("get_plugin_assistance", {**assistance, "issue_code": None}, state, catalog)
    no_code = execute_tool("get_plugin_assistance", dict(assistance), state, catalog)
    assert null_code.success is True
    assert null_code.to_dict() == no_code.to_dict()
