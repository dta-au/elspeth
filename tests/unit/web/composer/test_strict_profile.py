"""The OpenAI strict-mode schema checker (``tools/strict_profile.py``).

Every control is an EXACT match on the reported kinds (and, where a control
names positions, on the ``(kind, path)`` pairs). A subset check would let an
extra, wrong row pass unnoticed.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from elspeth.web.composer.advisor_output import advisor_response_format
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.strict_profile import (
    StrictViolation,
    StrictViolationKind,
    check_openai_strict,
)

K = StrictViolationKind

# The 32 loop tools with no free-form object anywhere in their flat schema S.
_STRICT_CANDIDATES: frozenset[str] = frozenset(
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

# The 10 option-bearing loop tools: each carries at least one free-form object.
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

# The 11 strict candidates that are NOT strict-clean as sent (optional
# properties, or keywords outside the wire allowlist). The other 21 are.
_NOT_CLEAN_AS_SENT: frozenset[str] = frozenset(
    {
        "create_blob",
        "wire_blob_inline_ref",
        "clear_source",
        "get_pipeline_state",
        "get_plugin_assistance",
        "list_models",
        "upsert_edge",
        "set_metadata",
        "request_advisor_hint",
        "request_interpretation_review",
        "wire_secret_ref",
    }
)

_ADVISOR_TITLE_PATHS: frozenset[str] = frozenset(
    {
        "/title",
        "/properties/verdict/title",
        "/properties/category/title",
        "/properties/steps/title",
        "/properties/findings/title",
        "/properties/note/title",
    }
)


def _kinds(rows: tuple[StrictViolation, ...]) -> set[StrictViolationKind]:
    return {row.kind for row in rows}


def _advisor_schema() -> dict[str, Any]:
    return deepcopy(advisor_response_format()["json_schema"]["schema"])


def _advisor_schema_without_titles() -> dict[str, Any]:
    schema = _advisor_schema()
    del schema["title"]
    for name in ("verdict", "category", "steps", "findings", "note"):
        del schema["properties"][name]["title"]
    return schema


# ---------------------------------------------------------------- vocabulary


def test_violation_kind_vocabulary_is_closed() -> None:
    assert {kind.value for kind in StrictViolationKind} == {
        "root_not_object",
        "root_union",
        "nested_union",
        "missing_additional_properties_false",
        "free_form_object",
        "optional_property",
        "required_not_in_properties",
        "ref",
        "typeless_subschema",
        "array_without_items",
        "unsupported_keyword",
        "keyword_not_allowlisted",
        "format_not_supported",
        "pattern_not_portable",
        "enum_contains_null",
    }


# ---------------------------------------------------------------- negatives


def test_hand_written_strict_schema_has_no_rows() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": ["string", "null"]},
            "b": {"type": "array", "items": {"type": "integer"}},
            "c": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "d": {"type": "string", "format": "uuid", "pattern": "^[a-z]+$"},
        },
        "required": ["a", "b", "c", "d"],
        "additionalProperties": False,
    }
    assert check_openai_strict(schema, tool="clean") == ()


def test_advisor_schema_without_titles_has_no_rows() -> None:
    assert check_openai_strict(_advisor_schema_without_titles(), tool="advisor") == ()


# ---------------------------------------------------------------- positives


_POSITIVES: dict[str, tuple[dict[str, Any], set[StrictViolationKind]]] = {
    "optional + free_form (options)": (
        {
            "type": "object",
            "properties": {"plugin": {"type": "string"}, "options": {"type": "object"}},
            "required": ["plugin"],
            "additionalProperties": False,
        },
        {K.FREE_FORM_OBJECT, K.OPTIONAL_PROPERTY},
    ),
    "additionalProperties: true": (
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"], "additionalProperties": True},
        {K.FREE_FORM_OBJECT},
    ),
    "additionalProperties: <schema> map": (
        {
            "type": "object",
            "properties": {"m": {"type": "object", "additionalProperties": {"type": "string"}}},
            "required": ["m"],
            "additionalProperties": False,
        },
        {K.FREE_FORM_OBJECT},
    ),
    "missing additionalProperties": (
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {K.MISSING_ADDITIONAL_PROPERTIES_FALSE},
    ),
    "root oneOf + typeless members": (
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
            "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
        },
        {K.ROOT_UNION, K.TYPELESS_SUBSCHEMA, K.OPTIONAL_PROPERTY},
    ),
    "nested oneOf": (
        {
            "type": "object",
            "properties": {
                "p": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "required": ["a"],
                    "additionalProperties": False,
                    "oneOf": [{"required": ["a"]}],
                }
            },
            "required": ["p"],
            "additionalProperties": False,
        },
        {K.NESTED_UNION, K.TYPELESS_SUBSCHEMA},
    ),
    "$ref": (
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "required": ["a"],
            "additionalProperties": False,
            "$defs": {"A": {"type": "string"}},
        },
        {K.REF},
    ),
    "not + pattern + default + minLength + format(uri)": (
        {
            "type": "object",
            "properties": {
                "a": {
                    "type": "string",
                    "not": {"enum": ["x"]},
                    "pattern": "^a",
                    "default": "a",
                    "minLength": 1,
                    "format": "uri",
                }
            },
            "required": ["a"],
            "additionalProperties": False,
        },
        {K.UNSUPPORTED_KEYWORD, K.KEYWORD_NOT_ALLOWLISTED, K.FORMAT_NOT_SUPPORTED},
    ),
    "array without items": (
        {"type": "object", "properties": {"a": {"type": "array"}}, "required": ["a"], "additionalProperties": False},
        {K.ARRAY_WITHOUT_ITEMS},
    ),
    "root not object": ({"type": "string"}, {K.ROOT_NOT_OBJECT}),
    "required names an absent property": (
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "zz"], "additionalProperties": False},
        {K.REQUIRED_NOT_IN_PROPERTIES},
    ),
    "enum contains null": (
        {"type": "object", "properties": {"a": {"enum": ["x", None]}}, "required": ["a"], "additionalProperties": False},
        {K.ENUM_CONTAINS_NULL},
    ),
    "ECMA-only pattern escape": (
        {
            "type": "object",
            "properties": {"a": {"type": "string", "pattern": "\\p{L}"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        {K.PATTERN_NOT_PORTABLE},
    ),
    "pattern Python re cannot compile": (
        {
            "type": "object",
            "properties": {"a": {"type": "string", "pattern": "[a-"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        {K.PATTERN_NOT_PORTABLE},
    ),
}


@pytest.mark.parametrize("label", sorted(_POSITIVES))
def test_positive_control_reports_exactly_the_expected_kinds(label: str) -> None:
    schema, expected = _POSITIVES[label]
    rows = check_openai_strict(schema, tool=label)
    assert _kinds(rows) == expected
    assert all(row.tool == label for row in rows)


def test_not_pattern_default_min_length_format_rows_are_at_their_keywords() -> None:
    schema, _ = _POSITIVES["not + pattern + default + minLength + format(uri)"]
    rows = check_openai_strict(schema, tool="t")
    assert {(row.kind, row.path) for row in rows} == {
        (K.UNSUPPORTED_KEYWORD, "/properties/a/not"),
        (K.KEYWORD_NOT_ALLOWLISTED, "/properties/a/default"),
        (K.KEYWORD_NOT_ALLOWLISTED, "/properties/a/minLength"),
        (K.FORMAT_NOT_SUPPORTED, "/properties/a/format"),
    }


def test_unmodified_advisor_schema_reports_only_its_six_titles() -> None:
    rows = check_openai_strict(_advisor_schema(), tool="advisor")
    assert {(row.kind, row.path) for row in rows} == {(K.KEYWORD_NOT_ALLOWLISTED, path) for path in _ADVISOR_TITLE_PATHS}


def test_enum_contains_null_row_is_at_the_enum() -> None:
    schema, _ = _POSITIVES["enum contains null"]
    rows = check_openai_strict(schema, tool="t")
    assert [(row.kind, row.path) for row in rows] == [(K.ENUM_CONTAINS_NULL, "/properties/a/enum")]


def test_root_any_of_is_a_root_union_but_nested_any_of_is_allowed() -> None:
    root = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
        "anyOf": [{"type": "object"}],
    }
    assert K.ROOT_UNION in _kinds(check_openai_strict(root, tool="t"))
    nested = {
        "type": "object",
        "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert check_openai_strict(nested, tool="t") == ()


def test_checker_does_not_mutate_its_input() -> None:
    schema = _advisor_schema()
    before = deepcopy(schema)
    check_openai_strict(schema, tool="advisor")
    assert schema == before


# ---------------------------------------------------------------- inventory over flat S


def test_inventory_partition_over_flat_s_is_pinned_by_name() -> None:
    definitions = {definition["name"]: definition["parameters"] for definition in get_tool_definitions()}
    assert set(definitions) == _STRICT_CANDIDATES | _OPTION_TOOLS
    assert len(_STRICT_CANDIDATES) == 32
    assert len(_OPTION_TOOLS) == 10

    clean = {name for name in _STRICT_CANDIDATES if check_openai_strict(definitions[name], tool=name) == ()}
    assert clean == _STRICT_CANDIDATES - _NOT_CLEAN_AS_SENT
    assert len(clean) == 21

    for name in _STRICT_CANDIDATES:
        assert K.FREE_FORM_OBJECT not in _kinds(check_openai_strict(definitions[name], tool=name)), name
    for name in _OPTION_TOOLS:
        assert K.FREE_FORM_OBJECT in _kinds(check_openai_strict(definitions[name], tool=name)), name
