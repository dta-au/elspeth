"""Canonical capability guidance shared by the compose loop's system prompt."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final, cast

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.skills import load_skill_with_hash

# HAND-WRITTEN ON PURPOSE — the keys are the operand the capability-skill
# identity pin CHECKS against ``COMPOSER_NODE_TYPES`` (derived from the
# ``NodeType`` Literal); the values are guidance prose that cannot be derived
# without inventing it. Derive the keys and the pin is ``x == x``
# (elspeth-b3117ec3ac, comment 7980).
CAPABILITY_CORE_NODE_GUIDANCE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "aggregation": "[capability-node:aggregation]",
        "coalesce": "[capability-node:coalesce]",
        "collector": "[capability-node:collector]",
        "gate": "[capability-node:gate]",
        "queue": "[capability-node:queue]",
        "row_union": "[capability-node:row_union]",
        "transform": "[capability-node:transform]",
    }
)

_FIELD_INVENTORY_START: Final[str] = "<!-- canonical-field-inventory:start -->"
_FIELD_INVENTORY_END: Final[str] = "<!-- canonical-field-inventory:end -->"
_FIELD_ROW_RE: Final[re.Pattern[str]] = re.compile(r"^\| ([a-z_]+) \| (`[a-z_]+`(?:, `[a-z_]+`)*) \|$")


def documented_capability_fields(text: str) -> Mapping[str, frozenset[str]]:
    """Parse and validate the public canonical-field inventory.

    The inventory is the human-visible authority.  Python derives its field
    mapping from those exact bytes, then the compose-loop tool builder compares
    it with the advertised set_pipeline schema on every call. There is deliberately no second list of
    field names in code that can drift in lockstep with the schema.
    """
    if type(text) is not str:
        raise TypeError("text must be an exact string")
    if text.count(_FIELD_INVENTORY_START) != 1 or text.count(_FIELD_INVENTORY_END) != 1:
        raise AuditIntegrityError("canonical capability field inventory anchors must occur exactly once")
    remainder = text.split(_FIELD_INVENTORY_START, 1)[1]
    block = remainder.split(_FIELD_INVENTORY_END, 1)[0]
    lines = block.strip().splitlines()
    if lines[:2] != ["| Family | Fields |", "| --- | --- |"]:
        raise AuditIntegrityError("canonical capability field inventory header is malformed")
    extracted: dict[str, frozenset[str]] = {}
    for line in lines[2:]:
        match = _FIELD_ROW_RE.fullmatch(line)
        if match is None:
            raise AuditIntegrityError("canonical capability field inventory row is malformed")
        family, rendered_fields = match.groups()
        if family in extracted:
            raise AuditIntegrityError("canonical capability field inventory repeats a family")
        extracted[family] = frozenset(item.removeprefix("`").removesuffix("`") for item in rendered_fields.split(", "))
    return MappingProxyType(extracted)


def _schema_mapping(value: object, *, path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise AuditIntegrityError(f"canonical capability schema node {path} must be an object")
    return cast(Mapping[str, Any], value)


def _property_names(schema: Mapping[str, Any], *, path: str) -> frozenset[str]:
    properties = _schema_mapping(schema["properties"], path=f"{path}.properties")
    if any(type(name) is not str for name in properties):
        raise AuditIntegrityError(f"canonical capability schema property names at {path} must be exact strings")
    return frozenset(properties)


def canonical_capability_fields(schema: Mapping[str, Any]) -> Mapping[str, frozenset[str]]:
    """Extract every authoring-structural field from the terminal schema."""
    root = _schema_mapping(schema, path="$pipeline")
    properties = _schema_mapping(root["properties"], path="$pipeline.properties")
    source = _schema_mapping(properties["source"], path="$pipeline.properties.source")
    sources = _schema_mapping(properties["sources"], path="$pipeline.properties.sources")
    named_source = _schema_mapping(sources["additionalProperties"], path="$pipeline.properties.sources.*")
    nodes = _schema_mapping(properties["nodes"], path="$pipeline.properties.nodes")
    node = _schema_mapping(nodes["items"], path="$pipeline.properties.nodes.items")
    node_properties = _schema_mapping(node["properties"], path="$pipeline.properties.nodes.items.properties")
    trigger = _schema_mapping(node_properties["trigger"], path="$pipeline.properties.nodes.items.properties.trigger")
    edges = _schema_mapping(properties["edges"], path="$pipeline.properties.edges")
    edge = _schema_mapping(edges["items"], path="$pipeline.properties.edges.items")
    outputs = _schema_mapping(properties["outputs"], path="$pipeline.properties.outputs")
    output = _schema_mapping(outputs["items"], path="$pipeline.properties.outputs.items")
    metadata = _schema_mapping(properties["metadata"], path="$pipeline.properties.metadata")
    source_properties = _schema_mapping(source["properties"], path="$pipeline.properties.source.properties")
    inline_blob = _schema_mapping(source_properties["inline_blob"], path="$pipeline.properties.source.properties.inline_blob")
    extracted = {
        "pipeline": frozenset(properties),
        "source": _property_names(source, path="$pipeline.source"),
        "named_source": _property_names(named_source, path="$pipeline.sources.*"),
        "inline_blob": _property_names(inline_blob, path="$pipeline.source.inline_blob"),
        "node": frozenset(node_properties),
        "trigger": _property_names(trigger, path="$pipeline.nodes[].trigger"),
        "edge": _property_names(edge, path="$pipeline.edges[]"),
        "output": _property_names(output, path="$pipeline.outputs[]"),
        "metadata": _property_names(metadata, path="$pipeline.metadata"),
    }
    return MappingProxyType(extracted)


def validate_capability_field_contract(schema: Mapping[str, Any], documented_text: str) -> None:
    """Fail when the visible field table and canonical schema do not agree."""
    if canonical_capability_fields(schema) != documented_capability_fields(documented_text):
        raise AuditIntegrityError("canonical pipeline schema and documented capability fields drifted")


def load_pipeline_capability_core() -> str:
    """Return the one static public capability core as exact UTF-8 text."""
    text, _digest = load_skill_with_hash("pipeline_capabilities")
    return text


# Derived from the public, visible inventory rather than duplicated in code.
CANONICAL_CAPABILITY_FIELDS: Final[Mapping[str, frozenset[str]]] = documented_capability_fields(load_pipeline_capability_core())


def render_with_pipeline_capabilities(interaction_skill: str) -> str:
    """Prepend the exact capability-core bytes to one interaction overlay."""
    if type(interaction_skill) is not str or not interaction_skill.strip():
        raise ValueError("interaction_skill must be a non-empty exact string")
    return f"{load_pipeline_capability_core()}\n{interaction_skill.rstrip()}\n"


__all__ = [
    "CANONICAL_CAPABILITY_FIELDS",
    "CAPABILITY_CORE_NODE_GUIDANCE",
    "canonical_capability_fields",
    "documented_capability_fields",
    "load_pipeline_capability_core",
    "render_with_pipeline_capabilities",
    "validate_capability_field_contract",
]
