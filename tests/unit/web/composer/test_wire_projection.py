"""The static per-dialect wire projection W (``tools/wire_projection.py``).

S (the flat registry, ``get_tool_definitions()``) stays the single authority.
W is derived from it once, at import, per ``ToolContractDialect``:

* ``none`` is S exactly (plus the set_pipeline ``{"pipeline": ...}``
  envelope), with no ``strict`` key anywhere;
* ``openai_strict`` makes every S-optional property of the 32 strict-capable
  tools required and nullable (decode strips the ``null``), moves keywords
  outside the wire allowlist into a ledger rendered as description text, and
  rewrites the 6 omission instructions into "pass null" wording.

The fail-closed controls call the pure ``build_wire_tool_defs`` with an
edited copy of the definitions (or a lowered copy of the limits); they never
reload the module.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers
from elspeth.web.composer.tools import schema_contract
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.strict_profile import check_openai_strict
from elspeth.web.composer.tools.wire_projection import (
    _OMISSION_VOCABULARY_EXCEPTIONS,
    _STRICT_DESCRIPTION_OVERRIDES,
    _WIRE_TOOL_DEFS,
    OPENAI_STRICT_LIMITS,
    EnvelopeUnwrap,
    LedgerEntry,
    StripNull,
    WireProjectionError,
    build_wire_tool_defs,
    omission_instruction_matches,
    stamp_planner_terminal,
    wire_limits_report,
    wire_tool_definitions,
)

NONE = ToolContractDialect.NONE
STRICT = ToolContractDialect.OPENAI_STRICT

_STRICT_CAPABLE: frozenset[str] = frozenset(
    {
        "list_blobs",
        "list_composer_blobs",
        "get_blob_metadata",
        "get_blob_content",
        "create_blob",
        "update_blob",
        "delete_blob",
        "wire_blob_inline_ref",
        "list_sources",
        "clear_source",
        "inspect_source",
        "get_pipeline_state",
        "get_plugin_schema",
        "get_expression_grammar",
        "explain_validation_error",
        "get_plugin_assistance",
        "get_audit_info",
        "list_models",
        "preview_pipeline",
        "diff_pipeline",
        "list_transforms",
        "list_sinks",
        "upsert_edge",
        "remove_node",
        "remove_edge",
        "set_metadata",
        "remove_output",
        "list_secret_refs",
        "validate_secret_ref",
        "request_advisor_hint",
        "request_interpretation_review",
        "wire_secret_ref",
    }
)

_OPTION_TOOLS: frozenset[str] = frozenset(
    {
        "set_source",
        "patch_source_options",
        "set_source_from_blob",
        "set_source_from_blobs",
        "set_pipeline",
        "upsert_node",
        "splice_transform",
        "patch_node_options",
        "set_output",
        "patch_output_options",
    }
)

# The 13 promoted positions: S-optional properties of the 32.
_PROMOTED: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {
        ("create_blob", ("description",)),
        ("wire_blob_inline_ref", ("encoding",)),
        ("clear_source", ("source_name",)),
        ("get_pipeline_state", ("component",)),
        ("get_plugin_assistance", ("issue_code",)),
        ("list_models", ("provider",)),
        ("list_models", ("limit",)),
        ("upsert_edge", ("label",)),
        ("set_metadata", ("patch", "name")),
        ("set_metadata", ("patch", "description")),
        ("request_advisor_hint", ("schema_excerpt",)),
        ("request_interpretation_review", ("llm_draft",)),
        ("wire_secret_ref", ("target_id",)),
    }
)

# The two promoted positions whose flat S already accepts ``null``.
_ALREADY_NULLABLE: frozenset[tuple[str, tuple[str, ...]]] = frozenset(
    {("get_plugin_assistance", ("issue_code",)), ("upsert_edge", ("label",))}
)

_UPSERT_EDGE_EXAMPLE: dict[str, Any] = {
    "id": "e_judge_layers_error",
    "from_node": "judge_layers",
    "to_node": "llm_failures",
    "edge_type": "on_error",
    "label": "LLM failures",
}

# The 15 ledger entries on the 32: (tool, JSON pointer of the owner node, keyword, value).
_LEDGER: frozenset[tuple[str, str, str, Any]] = frozenset(
    {
        ("clear_source", "/properties/source_name", "minLength", 1),
        ("clear_source", "/properties/source_name", "default", "source"),
        ("wire_blob_inline_ref", "/properties/encoding", "default", "utf-8"),
        ("list_models", "/properties/limit", "default", 50),
        ("upsert_edge", "", "examples", json.dumps([_UPSERT_EDGE_EXAMPLE], sort_keys=True)),
        ("request_advisor_hint", "/properties/problem_summary", "maxLength", 2000),
        ("request_advisor_hint", "/properties/recent_errors/items", "maxLength", 2000),
        ("request_advisor_hint", "/properties/attempted_actions/items", "maxLength", 2000),
        ("request_advisor_hint", "/properties/schema_excerpt", "maxLength", 8000),
        ("request_interpretation_review", "/properties/affected_node_id", "minLength", 1),
        ("request_interpretation_review", "/properties/affected_node_id", "maxLength", 256),
        ("request_interpretation_review", "/properties/user_term", "minLength", 1),
        ("request_interpretation_review", "/properties/user_term", "maxLength", 8192),
        ("request_interpretation_review", "/properties/llm_draft", "minLength", 1),
        ("request_interpretation_review", "/properties/llm_draft", "maxLength", 8192),
    }
)

# (tool, JSON pointer of the description's owner, "" for the tool description) -> sentence.
_LEDGER_SENTENCES: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("clear_source", "/properties/source_name", "At least 1 character."),
        ("clear_source", "/properties/source_name", 'Pass null to use the default ("source").'),
        ("wire_blob_inline_ref", "/properties/encoding", 'Pass null to use the default ("utf-8").'),
        ("list_models", "/properties/limit", "Pass null to use the default (50)."),
        ("request_advisor_hint", "/properties/problem_summary", "At most 2000 characters."),
        ("request_advisor_hint", "/properties/recent_errors", "Each item has at most 2000 characters."),
        ("request_advisor_hint", "/properties/attempted_actions", "Each item has at most 2000 characters."),
        ("request_advisor_hint", "/properties/schema_excerpt", "At most 8000 characters."),
        ("request_interpretation_review", "/properties/affected_node_id", "At least 1 character."),
        ("request_interpretation_review", "/properties/affected_node_id", "At most 256 characters."),
        ("request_interpretation_review", "/properties/user_term", "At least 1 character."),
        ("request_interpretation_review", "/properties/user_term", "At most 8192 characters."),
        ("request_interpretation_review", "/properties/llm_draft", "At least 1 character."),
        ("request_interpretation_review", "/properties/llm_draft", "At most 8192 characters."),
        (
            "upsert_edge",
            "",
            "Example arguments: " + json.dumps(_UPSERT_EDGE_EXAMPLE, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + ".",
        ),
    }
)


# ---------------------------------------------------------------- helpers


def _definitions() -> list[dict[str, Any]]:
    return get_tool_definitions()


def _definition(definitions: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(definition for definition in definitions if definition["name"] == name)


def _functions(dialect: ToolContractDialect) -> dict[str, dict[str, Any]]:
    return {tool["function"]["name"]: tool["function"] for tool in wire_tool_definitions(dialect)}


def _node(schema: Any, pointer: str) -> Any:
    node = schema
    if pointer == "":
        return node
    for token in pointer.lstrip("/").split("/"):
        node = node[token]
    return node


def _property_node(schema: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = schema
    for name in path:
        node = node["properties"][name]
    return node


def _build(definitions: list[dict[str, Any]], **overrides: Any) -> Any:
    return build_wire_tool_defs(definitions, limits=overrides.pop("limits", OPENAI_STRICT_LIMITS), **overrides)


def _walk_json(value: Any, visit: Callable[[Any], None]) -> None:
    visit(value)
    if type(value) is dict:
        for child in value.values():
            _walk_json(child, visit)
    elif type(value) is list:
        for child in value:
            _walk_json(child, visit)


def _null_enum_pointers(schema: Any, pointer: str = "") -> list[str]:
    found: list[str] = []
    if type(schema) is dict:
        if "enum" in schema and None in schema["enum"]:
            found.append(f"{pointer}/enum")
        for key, child in schema.items():
            if key != "enum":
                found.extend(_null_enum_pointers(child, f"{pointer}/{key}"))
    elif type(schema) is list:
        for index, child in enumerate(schema):
            found.extend(_null_enum_pointers(child, f"{pointer}/{index}"))
    return found


# ---------------------------------------------------------------- partition


def test_strict_capable_partition_by_name() -> None:
    strict = _WIRE_TOOL_DEFS[STRICT]
    assert {name for name, tool in strict.items() if tool.strict_capable} == _STRICT_CAPABLE
    assert {name for name, tool in strict.items() if not tool.strict_capable} == _OPTION_TOOLS
    assert not any(tool.strict_capable for tool in _WIRE_TOOL_DEFS[NONE].values())


def test_both_dialects_keep_registry_order_with_wire_secret_ref_last() -> None:
    names = [definition["name"] for definition in _definitions()]
    for dialect in (NONE, STRICT):
        assert [tool["function"]["name"] for tool in wire_tool_definitions(dialect)] == names
        assert list(_WIRE_TOOL_DEFS[dialect]) == names
    assert names[-1] == "wire_secret_ref"


def test_option_tools_keep_their_none_parameters_on_openai_strict() -> None:
    none_functions = _functions(NONE)
    strict_functions = _functions(STRICT)
    for name in _OPTION_TOOLS:
        assert strict_functions[name]["parameters"] == none_functions[name]["parameters"]
        assert strict_functions[name]["description"] == none_functions[name]["description"]
    assert _WIRE_TOOL_DEFS[STRICT]["set_pipeline"].decode_plan == (EnvelopeUnwrap(key="pipeline"),)
    assert _WIRE_TOOL_DEFS[NONE]["set_pipeline"].decode_plan == (EnvelopeUnwrap(key="pipeline"),)


# ---------------------------------------------------------------- promotion


def test_strip_null_paths_are_exactly_the_13_promoted_positions() -> None:
    strict = _WIRE_TOOL_DEFS[STRICT]
    got = {(name, node.path) for name, tool in strict.items() for node in tool.decode_plan if type(node) is StripNull}
    assert got == _PROMOTED
    for name, tool in strict.items():
        assert tool.promoted_paths == frozenset(path for tool_name, path in _PROMOTED if tool_name == name)
    for tool in _WIRE_TOOL_DEFS[NONE].values():
        assert tool.promoted_paths == frozenset()
        assert not any(type(node) is StripNull for node in tool.decode_plan)


def test_flat_s_rejects_null_at_the_11_omission_only_positions() -> None:
    definitions = {definition["name"]: definition["parameters"] for definition in _definitions()}
    for name, path in _PROMOTED:
        flat = _property_node(definitions[name], path)
        admits_null = Draft202012Validator(flat).is_valid(None)
        assert admits_null is ((name, path) in _ALREADY_NULLABLE), (name, path)


def test_promoted_positions_are_wire_required_and_nullable() -> None:
    functions = _functions(STRICT)
    for name, path in _PROMOTED:
        parameters = functions[name]["parameters"]
        parent = _property_node(parameters, path[:-1])
        assert path[-1] in parent["required"], (name, path)
        wire = _property_node(parameters, path)
        assert Draft202012Validator(wire).is_valid(None), (name, path)


def test_promoted_enum_is_an_any_of_with_a_null_member() -> None:
    encoding = _functions(STRICT)["wire_blob_inline_ref"]["parameters"]["properties"]["encoding"]
    assert encoding["anyOf"] == [
        {"type": "string", "enum": ["latin-1", "utf-16", "utf-8", "utf-8-sig"]},
        {"type": "null"},
    ]
    assert "enum" not in encoding
    assert "type" not in encoding


# ---------------------------------------------------------------- enums


def test_no_enum_contains_null_in_any_strict_capable_w() -> None:
    functions = _functions(STRICT)
    for name in _STRICT_CAPABLE:
        assert _null_enum_pointers(functions[name]["parameters"]) == [], name


def test_none_w_null_enums_are_exactly_upsert_nodes_two() -> None:
    found = {(name, pointer) for name, function in _functions(NONE).items() for pointer in _null_enum_pointers(function["parameters"])}
    assert found == {
        ("upsert_node", "/properties/output_mode/enum"),
        ("upsert_node", "/properties/scope_policy/enum"),
    }


def test_planted_null_enum_on_a_strict_candidate_fails_closed() -> None:
    definitions = _definitions()
    _definition(definitions, "get_plugin_assistance")["parameters"]["properties"]["plugin_type"]["enum"].append(None)
    with pytest.raises(WireProjectionError):
        _build(definitions)


# ---------------------------------------------------------------- omission vocabulary


def test_omission_vocabulary_controls() -> None:
    assert omission_instruction_matches("Leave blank to reset")
    assert omission_instruction_matches("Omit x")
    assert omission_instruction_matches("OMIT llm_draft")
    assert not omission_instruction_matches("vomit")
    assert not omission_instruction_matches("Pass null to get a provider summary")


def test_no_omission_instruction_in_any_strict_capable_w() -> None:
    functions = _functions(STRICT)
    exception_text = _OMISSION_VOCABULARY_EXCEPTIONS[("get_pipeline_state", "/description")]
    for name in _STRICT_CAPABLE:
        texts: list[str] = []

        def collect(value: Any, texts: list[str] = texts) -> None:
            if type(value) is dict and "description" in value and type(value["description"]) is str:
                texts.append(value["description"])

        _walk_json(functions[name]["parameters"], collect)
        description = functions[name]["description"]
        if name == "get_pipeline_state":
            assert exception_text in description
            description = description.replace(exception_text, "")
        texts.append(description)
        for text in texts:
            assert not omission_instruction_matches(text), (name, text)


def test_the_six_overrides_say_pass_null() -> None:
    functions = _functions(STRICT)
    assert "Pass null to get a provider summary" in functions["list_models"]["parameters"]["properties"]["provider"]["description"]
    assert "pass null for component" in functions["get_pipeline_state"]["parameters"]["properties"]["component"]["description"]
    assert "Pass null for ``issue_code`` to get discovery-time guidance" in functions["get_plugin_assistance"]["description"]
    assert (
        "Pass null for discovery-time guidance"
        in functions["get_plugin_assistance"]["parameters"]["properties"]["issue_code"]["description"]
    )
    review = functions["request_interpretation_review"]["parameters"]["properties"]
    assert "pass null for llm_draft — the server computes the demanded field set" in review["kind"]["description"]
    assert review["llm_draft"]["description"].startswith("Pass null for llm_draft when the review site already carries")
    assert "if provided it must byte-match the staged draft" in review["llm_draft"]["description"]
    assert len(_STRICT_DESCRIPTION_OVERRIDES) == 6


def test_none_descriptions_are_unchanged() -> None:
    functions = _functions(NONE)
    assert "Omit to get a provider summary" in functions["list_models"]["parameters"]["properties"]["provider"]["description"]
    assert functions["request_interpretation_review"]["parameters"]["properties"]["llm_draft"]["description"].startswith("OMIT this")


def test_planted_omit_on_a_promoted_property_fails_closed() -> None:
    definitions = _definitions()
    _definition(definitions, "create_blob")["parameters"]["properties"]["description"]["description"] += " Omit x."
    with pytest.raises(WireProjectionError):
        _build(definitions)


def test_planted_leave_blank_fails_closed() -> None:
    definitions = _definitions()
    _definition(definitions, "wire_secret_ref")["parameters"]["properties"]["target_id"]["description"] += " Leave blank to reset."
    with pytest.raises(WireProjectionError):
        _build(definitions)


def test_deleting_one_override_fails_closed() -> None:
    overrides = dict(_STRICT_DESCRIPTION_OVERRIDES)
    del overrides[("list_models", "/parameters/properties/provider/description")]
    with pytest.raises(WireProjectionError):
        _build(_definitions(), overrides=overrides)


def test_removing_the_named_exception_fails_closed() -> None:
    with pytest.raises(WireProjectionError):
        _build(_definitions(), omission_exceptions={})


def test_a_stale_override_fails_closed() -> None:
    overrides = dict(_STRICT_DESCRIPTION_OVERRIDES)
    overrides[("list_models", "/parameters/properties/provider/description")] = ("text that is not there", "x")
    with pytest.raises(WireProjectionError):
        _build(_definitions(), overrides=overrides)


def test_a_stale_exception_fails_closed() -> None:
    with pytest.raises(WireProjectionError):
        _build(_definitions(), omission_exceptions={("get_pipeline_state", "/description"): "text that is not there"})


# ---------------------------------------------------------------- keywords and ledger


def test_unknown_keyword_fails_closed() -> None:
    definitions = _definitions()
    _definition(definitions, "list_blobs")["parameters"]["x-foo"] = 1
    with pytest.raises(WireProjectionError):
        _build(definitions)


def test_ledger_is_exactly_the_15_entries() -> None:
    got = set()
    for name, tool in _WIRE_TOOL_DEFS[STRICT].items():
        for entry in tool.ledger:
            assert type(entry) is LedgerEntry
            value = json.dumps(deep_thaw(entry.value), sort_keys=True) if entry.keyword == "examples" else entry.value
            got.add((name, entry.path, entry.keyword, value))
    assert got == _LEDGER
    assert all(tool.ledger == () for tool in _WIRE_TOOL_DEFS[NONE].values())


def test_ledgered_keywords_are_absent_from_w() -> None:
    functions = _functions(STRICT)
    for name, pointer, keyword, _ in _LEDGER:
        assert keyword not in _node(functions[name]["parameters"], pointer), (name, pointer, keyword)


def test_ledger_sentences_render_into_descriptions() -> None:
    functions = _functions(STRICT)
    for name, pointer, sentence in _LEDGER_SENTENCES:
        owner = functions[name] if pointer == "" else _node(functions[name]["parameters"], pointer)
        assert sentence in owner["description"], (name, pointer, sentence)


def test_dropping_a_ledgered_property_description_fails_closed() -> None:
    definitions = _definitions()
    del _definition(definitions, "request_advisor_hint")["parameters"]["properties"]["problem_summary"]["description"]
    with pytest.raises(WireProjectionError):
        _build(definitions)


# ---------------------------------------------------------------- limits


@pytest.mark.parametrize(
    ("limit_field", "report_field"),
    [
        ("max_properties", "total_properties"),
        ("max_depth", "max_depth"),
        ("max_characters", "total_characters"),
        ("max_enum_values", "total_enum_values"),
    ],
)
def test_limits_fail_closed_when_lowered_below_the_measured_value(limit_field: str, report_field: str) -> None:
    report = wire_limits_report(STRICT)
    measured = {
        "total_properties": report.total_properties,
        "max_depth": report.max_depth,
        "total_characters": report.total_characters,
        "total_enum_values": report.total_enum_values,
    }[report_field]
    assert measured > 0
    lowered = replace(OPENAI_STRICT_LIMITS, **{limit_field: measured - 1})
    with pytest.raises(WireProjectionError):
        _build(_definitions(), limits=lowered)
    _build(_definitions(), limits=replace(OPENAI_STRICT_LIMITS, **{limit_field: measured}))


def test_limits_report_counts_the_strict_tools() -> None:
    assert wire_limits_report(STRICT).strict_tool_count == 32
    assert wire_limits_report(NONE).strict_tool_count == 0


# ---------------------------------------------------------------- directional walker


def test_directional_walker_pointed_at_w_reports_a_violation() -> None:
    """Pinned negative control: W cannot feed the S walkers.

    ``list_models`` flat S is the runtime schema and its strict W is the
    advertised one. W makes both S-optional properties required, and the
    walker reports exactly that: an advertised schema may be looser than
    the runtime, never narrower.
    """
    runtime = _definition(_definitions(), "list_models")["parameters"]
    advertised = _functions(STRICT)["list_models"]["parameters"]
    failure = schema_contract._directional_compatibility_failure(runtime, advertised, {}, {}, path="list_models")
    assert failure is not None
    assert "advertised required fields are optional in the runtime model: ['limit', 'provider']" in failure
    none_advertised = _functions(NONE)["list_models"]["parameters"]
    assert schema_contract._directional_compatibility_failure(runtime, none_advertised, {}, {}, path="list_models") is None


# ---------------------------------------------------------------- purity


def test_module_defs_equal_a_fresh_build_from_the_registry() -> None:
    rebuilt = _build(_definitions())
    assert rebuilt == _WIRE_TOOL_DEFS


def test_wire_tool_definitions_returns_fresh_isolated_copies() -> None:
    baseline = wire_tool_definitions(STRICT)
    first = wire_tool_definitions(STRICT)
    for tool in first:
        tool["function"]["parameters"]["properties"].clear()
        tool["function"]["description"] = "tampered"
    assert wire_tool_definitions(STRICT) == baseline


# ---------------------------------------------------------------- stamping


def test_openai_strict_stamps_true_on_the_32_and_false_on_the_10() -> None:
    for tool in wire_tool_definitions(STRICT):
        function = tool["function"]
        assert function["strict"] is (function["name"] in _STRICT_CAPABLE), function["name"]
        assert set(function) == {"name", "description", "parameters", "strict"}


def test_none_carries_no_strict_key() -> None:
    for tool in wire_tool_definitions(NONE):
        assert "strict" not in tool["function"]


def test_cache_markers_keep_function_strict_on_a_stamped_list() -> None:
    """A function test: D8 means no production route sends both."""
    tools = wire_tool_definitions(STRICT)
    _, marked = apply_anthropic_cache_markers([], tools)
    assert marked is not None
    assert marked[-1]["function"]["name"] == "wire_secret_ref"
    assert marked[-1]["function"]["strict"] is True
    assert marked[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in marked[-1]["function"]
    assert [tool["function"]["strict"] for tool in marked] == [tool["function"]["strict"] for tool in tools]


def test_stamp_planner_terminal() -> None:
    terminal = {"type": "function", "function": {"name": "emit_pipeline_proposal", "description": "d", "parameters": {"type": "object"}}}
    before = deepcopy(terminal)
    stamped = stamp_planner_terminal(terminal, STRICT)
    assert stamped["function"]["strict"] is False
    assert stamped["function"]["parameters"] == terminal["function"]["parameters"]
    unchanged = stamp_planner_terminal(terminal, NONE)
    assert unchanged == before
    assert unchanged is not terminal
    assert terminal == before


# ---------------------------------------------------------------- none identity (the durable byte pin)


def _expected_none_list() -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    for definition in _definitions():
        parameters = definition["parameters"]
        if definition["name"] == "set_pipeline":
            parameters = {
                "type": "object",
                "properties": {"pipeline": parameters},
                "required": ["pipeline"],
                "additionalProperties": False,
            }
        expected.append(
            {
                "type": "function",
                "function": {"name": definition["name"], "description": definition["description"], "parameters": parameters},
            }
        )
    return expected


def _assert_plain_json(value: Any) -> None:
    def visit(node: Any) -> None:
        assert type(node) not in (tuple, frozenset), type(node)
        if type(node) is not dict and type(node) is not list:
            assert type(node) in (str, int, float, bool, type(None)), type(node)

    _walk_json(value, visit)


def test_none_w_is_s_plus_the_envelope() -> None:
    tools = wire_tool_definitions(NONE)
    assert tools == _expected_none_list()
    for tool in tools:
        assert set(tool) == {"type", "function"}
        assert set(tool["function"]) == {"name", "description", "parameters"}
    _assert_plain_json(tools)


def test_openai_strict_w_is_plain_json() -> None:
    _assert_plain_json(wire_tool_definitions(STRICT))


def test_strict_capable_w_passes_the_strict_checker() -> None:
    for tool in wire_tool_definitions(STRICT):
        function = tool["function"]
        if function["strict"]:
            assert check_openai_strict(function["parameters"], tool=function["name"]) == ()
