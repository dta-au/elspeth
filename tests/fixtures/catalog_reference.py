"""Reusable assertions for built-in plugin catalogue reference content."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
)
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields

BuiltinPluginClass = type[BaseSource] | type[BaseTransform] | type[BaseSink]


_GENERIC_REFERENCE_TEXT = frozenset(
    {
        "see documentation",
        "see the documentation",
        "see technical description",
        "see the technical description",
    }
)
_GENERIC_TAGS = frozenset({"general", "generic", "plugin", "utility"})
_TAG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SECRET_VALIDATION_SENTINEL = "elspeth-catalogue-reference-secret-placeholder"


@dataclass(frozen=True, slots=True)
class BuiltinReference:
    """A registered built-in plugin and its catalogue kind."""

    kind: Literal["source", "transform", "sink"]
    plugin_cls: BuiltinPluginClass


def discover_builtin_references() -> tuple[BuiltinReference, ...]:
    """Discover built-ins from a fresh manager and return stable records."""
    manager = PluginManager()
    manager.register_builtin_plugins()

    references = [
        *(BuiltinReference("source", cast(BuiltinPluginClass, plugin_cls)) for plugin_cls in manager.get_sources()),
        *(BuiltinReference("transform", cast(BuiltinPluginClass, plugin_cls)) for plugin_cls in manager.get_transforms()),
        *(BuiltinReference("sink", cast(BuiltinPluginClass, plugin_cls)) for plugin_cls in manager.get_sinks()),
    ]
    return tuple(sorted(references, key=lambda reference: (reference.kind, reference.plugin_cls.name)))


def _normalize_reference_text(value: str) -> str:
    normalized = " ".join(value.split()).casefold()
    return normalized.rstrip(".!")


def _reference_text_fields(plugin_cls: BuiltinPluginClass) -> tuple[tuple[str, object], ...]:
    """Return the reference-prose contract as an explicit (name, value) table.

    ``BaseSource``/``BaseTransform``/``BaseSink`` each declare all three fields,
    so every entry is a direct attribute read: a base class that dropped one
    fails here with ``AttributeError`` instead of silently reading a default.
    """
    return (
        ("usage_when_to_use", plugin_cls.usage_when_to_use),
        ("usage_when_not_to_use", plugin_cls.usage_when_not_to_use),
        ("example_use", plugin_cls.example_use),
    )


def assert_reference_text(plugin_cls: BuiltinPluginClass) -> None:
    """Assert that all reference prose is present, specific, and distinct."""
    normalized_values: list[str] = []
    for field_name, value in _reference_text_fields(plugin_cls):
        assert isinstance(value, str), f"{plugin_cls.name}.{field_name} must be a nonblank string"
        assert value.strip(), f"{plugin_cls.name}.{field_name} must be nonblank"
        normalized = _normalize_reference_text(value)
        assert normalized not in _GENERIC_REFERENCE_TEXT, f"{plugin_cls.name}.{field_name} must contain plugin-specific content"
        normalized_values.append(normalized)

    assert len(set(normalized_values)) == len(normalized_values), f"{plugin_cls.name} reference prose fields must be distinct"


def assert_reference_tags(plugin_cls: BuiltinPluginClass) -> None:
    """Assert that discovery tags are bounded, unique lowercase kebab-case."""
    tags = plugin_cls.capability_tags
    assert type(tags) is tuple, f"{plugin_cls.name}.capability_tags must be a tuple"
    assert 2 <= len(tags) <= 6, f"{plugin_cls.name}.capability_tags must contain between 2 and 6 tags"
    assert all(isinstance(tag, str) for tag in tags), f"{plugin_cls.name}.capability_tags must contain only strings"
    assert len(set(tags)) == len(tags), f"{plugin_cls.name}.capability_tags must not contain duplicates"
    for tag in tags:
        assert 1 <= len(tag) <= 32, f"{plugin_cls.name} capability tag {tag!r} must be 1-32 characters"
        assert _TAG_PATTERN.fullmatch(tag), f"{plugin_cls.name} capability tag {tag!r} must be lowercase kebab-case"
    assert not set(tags).issubset(_GENERIC_TAGS), f"{plugin_cls.name}.capability_tags must include a plugin-specific tag"


def _find_declaring_plugin_nodes(
    value: object,
    *,
    plugin_name: str,
) -> list[Mapping[str, Any]]:
    nodes: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if value.get("plugin") == plugin_name:
            nodes.append(value)
        for child in value.values():
            nodes.extend(_find_declaring_plugin_nodes(child, plugin_name=plugin_name))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_find_declaring_plugin_nodes(child, plugin_name=plugin_name))
    return nodes


def _contains_direct_collection_item(value: object, target: Mapping[str, Any]) -> bool:
    if isinstance(value, Mapping):
        return any(child is target for child in value.values())
    if isinstance(value, list):
        return any(child is target for child in value)
    return False


def _replace_secret_refs_for_validation(value: object) -> object:
    if isinstance(value, Mapping):
        if set(value) == {"secret_ref"}:
            secret_name = value["secret_ref"]
            if isinstance(secret_name, str) and secret_name.strip():
                return _SECRET_VALIDATION_SENTINEL
        return {key: _replace_secret_refs_for_validation(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_secret_refs_for_validation(child) for child in value]
    return value


def _expected_section(reference: BuiltinReference) -> str:
    if reference.kind == "source":
        return "sources"
    if reference.kind == "sink":
        return "sinks"
    plugin_cls = reference.plugin_cls
    # kind == "transform" here, so the class is nominally a BaseTransform —
    # the only base that declares ``is_batch_aware``. Assert the owned type
    # rather than probing for the flag: a source/sink misfiled as a transform
    # must fail loudly, not silently fall through to the row-transform section.
    assert issubclass(plugin_cls, BaseTransform), f"{plugin_cls.name} is registered as a transform but is not a BaseTransform"
    if plugin_cls.is_batch_aware:
        return "aggregations"
    return "transform"


def parse_and_validate_example(reference: BuiltinReference) -> None:
    """Parse and config-validate one bounded catalogue component fragment."""
    plugin_cls = reference.plugin_cls
    example = plugin_cls.example_use
    assert isinstance(example, str) and example.strip(), f"{plugin_cls.name}.example_use must be a nonblank YAML fragment"

    parsed = load_bounded_pipeline_yaml(example)
    assert isinstance(parsed, Mapping), f"{plugin_cls.name}.example_use must parse to a top-level mapping"

    declaring_nodes = _find_declaring_plugin_nodes(
        parsed,
        plugin_name=plugin_cls.name,
    )
    assert len(declaring_nodes) == 1, (
        f"{plugin_cls.name}.example_use must declare plugin {plugin_cls.name!r} exactly once; found {len(declaring_nodes)}"
    )
    declaring_node = declaring_nodes[0]

    section_name = _expected_section(reference)
    assert set(parsed) == {section_name}, f"{plugin_cls.name}.example_use must be one bounded {section_name!r} fragment"
    section = parsed[section_name]
    if section_name == "transform":
        assert isinstance(section, Mapping) and section is declaring_node, (
            f"{plugin_cls.name}.example_use must declare the plugin directly under top-level 'transform'"
        )
    else:
        assert isinstance(section, (Mapping, list)), f"{plugin_cls.name}.example_use {section_name!r} must be a mapping or list"
        assert _contains_direct_collection_item(section, declaring_node), (
            f"{plugin_cls.name}.example_use must declare the plugin as a direct mapping value or list item under top-level {section_name!r}"
        )

    options = declaring_node.get("options", {})
    assert isinstance(options, Mapping), f"{plugin_cls.name}.example_use options must be a mapping"

    plugin_specific_credential_fields = allowed_secret_ref_fields(
        reference.kind,
        plugin_cls.name,
    )
    credential_violations = collect_credential_field_violations(
        options,
        additional_credential_fields=plugin_specific_credential_fields,
    )
    assert not credential_violations, (
        f"{plugin_cls.name}.example_use contains literal credential fields: {sorted(set(credential_violations))!r}"
    )
    secret_ref_violations = collect_disallowed_secret_ref_markers(
        options,
        additional_allowed_fields=plugin_specific_credential_fields,
    )
    assert not secret_ref_violations, (
        f"{plugin_cls.name}.example_use contains secret refs in disallowed fields: "
        f"{sorted({violation.field_path for violation in secret_ref_violations})!r}"
    )

    resolved = _replace_secret_refs_for_validation(options)
    assert isinstance(resolved, dict)
    config_model = plugin_cls.get_config_model(resolved)
    if config_model is None:
        assert not resolved, f"{plugin_cls.name}.example_use supplies options but has no config model"
        return
    config_model.from_dict(resolved, plugin_name=plugin_cls.name)
