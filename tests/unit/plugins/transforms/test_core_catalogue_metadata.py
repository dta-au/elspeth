"""Reference-content contract for the core and utility transforms."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.core.config import load_bounded_pipeline_yaml
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    discover_builtin_references,
    parse_and_validate_example,
)

EXPECTED_CORE_TAGS = {
    "blob_csv_expand": ("csv", "blob", "tabular", "fan-out"),
    "field_mapper": ("fields", "mapping", "rename", "cleanup"),
    "json_explode": ("json", "array", "fan-out", "deaggregation"),
    "keyword_filter": ("filtering", "regex", "content-screening"),
    "line_explode": ("text", "lines", "fan-out", "deaggregation"),
    "passthrough": ("wiring", "schema", "debugging"),
    "report_assemble": ("report", "aggregation", "batch", "pagination"),
    "truncate": ("text", "truncation", "length-limit"),
    "type_coerce": ("types", "coercion", "normalization"),
    "value_transform": ("expressions", "calculation", "fields"),
}
EXPECTED_CORE_NAMES = set(EXPECTED_CORE_TAGS)
CORE_REFERENCES = tuple(
    reference
    for reference in discover_builtin_references()
    if reference.kind == "transform" and reference.plugin_cls.name in EXPECTED_CORE_NAMES
)
CORE_BY_NAME = {reference.plugin_cls.name: reference for reference in CORE_REFERENCES}

_REFERENCE_FIELDS = ("usage_when_to_use", "usage_when_not_to_use", "example_use", "capability_tags")
_PLACEHOLDER_MARKERS = ("todo", "tbd", "replace-me", "placeholder", "see the technical description")

_REQUIRED_GUIDANCE = {
    "blob_csv_expand": (("payload-store", "csv blob", "rows"), ("file source", "binary parser")),
    "field_mapper": (("rename", "copy", "drop"), ("expression", "type coercion")),
    "json_explode": (("one json array field",), ("object flattening", "batch aggregation")),
    "keyword_filter": (("regex", "on_error"), ("general expression",)),
    "line_explode": (("text field", "rows"), ("file", "csv")),
    "passthrough": (("wiring", "schema", "debug"), ("business transform",)),
    "report_assemble": (("batch", "page", "section"), ("per-row",)),
    "truncate": (("deterministic", "length"), ("token",)),
    "type_coerce": (("explicit", "type", "normal"), ("arbitrary calculation",)),
    "value_transform": (("ordered", "expression", "pass-through"), ("filtering", "routing")),
}

_VALUE_TRANSFORM_AVOID = (
    "Row filtering or routing — value_transform never drops rows; every row passes "
    "through with its computed fields. Use a gate node for conditional row filtering "
    "(or keyword_filter for regex pattern blocking)."
)


def _declaring_node(reference: BuiltinReference) -> Mapping[str, Any]:
    example = reference.plugin_cls.example_use
    assert isinstance(example, str)
    parsed = load_bounded_pipeline_yaml(example)
    section_name = "aggregations" if reference.plugin_cls.name == "report_assemble" else "transform"
    section = parsed[section_name]
    if section_name == "transform":
        return cast(Mapping[str, Any], section)
    assert isinstance(section, (Mapping, list))
    node = next(iter(section.values())) if isinstance(section, Mapping) else section[0]
    return cast(Mapping[str, Any], node)


def _assert_constructor_is_side_effect_free(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    options = cast(Mapping[str, Any], _declaring_node(reference).get("options", {}))
    before = tuple(tmp_path.rglob("*"))
    reference.plugin_cls(dict(options))
    assert tuple(tmp_path.rglob("*")) == before


def test_core_catalogue_discovers_every_and_only_named_core_transform() -> None:
    assert set(CORE_BY_NAME) == EXPECTED_CORE_NAMES


@pytest.mark.parametrize("reference", CORE_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_core_catalogue_reference_content_is_class_owned_specific_valid_and_truthful(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_cls = reference.plugin_cls
    assert all(field_name in plugin_cls.__dict__ for field_name in _REFERENCE_FIELDS)
    assert_reference_text(plugin_cls)
    assert_reference_tags(plugin_cls)
    assert plugin_cls.capability_tags == EXPECTED_CORE_TAGS[plugin_cls.name]
    parse_and_validate_example(reference)
    assert _declaring_node(reference)["plugin"] == plugin_cls.name

    to_use_value = plugin_cls.usage_when_to_use
    not_to_use_value = plugin_cls.usage_when_not_to_use
    assert isinstance(to_use_value, str)
    assert isinstance(not_to_use_value, str)
    to_use = to_use_value.casefold()
    not_to_use = not_to_use_value.casefold()
    required_use, required_avoid = _REQUIRED_GUIDANCE[plugin_cls.name]
    assert all(term in to_use for term in required_use)
    assert all(term in not_to_use for term in required_avoid)

    all_reference_text = " ".join(cast(str, getattr(plugin_cls, field_name)) for field_name in _REFERENCE_FIELDS[:-1]).casefold()
    assert not any(marker in all_reference_text for marker in _PLACEHOLDER_MARKERS)
    _assert_constructor_is_side_effect_free(reference, tmp_path, monkeypatch)


def test_core_catalogue_examples_use_the_required_direct_section() -> None:
    for name, reference in CORE_BY_NAME.items():
        example = reference.plugin_cls.example_use
        assert isinstance(example, str)
        parsed = load_bounded_pipeline_yaml(example)
        expected_section = "aggregations" if name == "report_assemble" else "transform"
        assert set(parsed) == {expected_section}


def test_value_transform_preserves_its_accurate_avoid_warning() -> None:
    assert CORE_BY_NAME["value_transform"].plugin_cls.usage_when_not_to_use == _VALUE_TRANSFORM_AVOID
