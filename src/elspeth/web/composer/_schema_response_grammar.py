"""Canonical schema keyword grammar, independent of disclosure and budgets."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer._response_json import FrozenResponseJSON, encode_response_json, parse_frozen_response_json, parse_response_json

_JSON_SCHEMA_PROSE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "$comment",
        "title",
        "description",
        "examples",
        "example",
        "composer_description",
        "composer_placeholder",
        # UI-disclosure hint (elspeth-9cca900d41): presentational, not
        # audit-bearing (mirrors knob_schema._attach_tier's own docstring).
        # The knob_schema projection already treats "tier" as prose
        # (_contract_knob_schema's own prose_keys); this is the raw
        # json_schema side of the same fact, since pydantic bakes
        # json_schema_extra={"composer_tier": ...} onto the property's
        # generated schema the same way it does composer_description.
        "composer_tier",
    }
)
_JSON_SCHEMA_SCALAR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "$vocabulary",
        "type",
        "const",
        "enum",
        "default",
        "pattern",
        "format",
        "contentEncoding",
        "contentMediaType",
        "deprecated",
        "readOnly",
        "writeOnly",
        "nullable",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "minContains",
        "maxContains",
    }
)
_JSON_SCHEMA_MAP_KEYS: Final[frozenset[str]] = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "items",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "additionalProperties",
        "unevaluatedItems",
        "unevaluatedProperties",
        "contentSchema",
    }
)
_JSON_SCHEMA_SCHEMA_LIST_KEYS: Final[frozenset[str]] = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_JSON_SCHEMA_STRING_SCALAR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
        "pattern",
        "format",
        "contentEncoding",
        "contentMediaType",
    }
)
_JSON_SCHEMA_BOOLEAN_SCALAR_KEYS: Final[frozenset[str]] = frozenset({"deprecated", "readOnly", "writeOnly", "nullable", "uniqueItems"})
_JSON_SCHEMA_NUMERIC_SCALAR_KEYS: Final[frozenset[str]] = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
)
_JSON_SCHEMA_NONNEGATIVE_INTEGER_KEYS: Final[frozenset[str]] = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties", "minContains", "maxContains"}
)
_JSON_SCHEMA_TYPES: Final[frozenset[str]] = frozenset({"null", "boolean", "object", "array", "number", "string", "integer"})


class SchemaKeywordInvalid(ValueError):
    """A known schema keyword has a malformed domain value."""


def validate_json_schema_scalar(key: str, value: object) -> None:
    """Validate shared keyword domains without projecting or copying metadata."""
    if key in _JSON_SCHEMA_STRING_SCALAR_KEYS:
        if type(value) is not str:
            raise SchemaKeywordInvalid
    elif key in _JSON_SCHEMA_BOOLEAN_SCALAR_KEYS:
        if type(value) is not bool:
            raise SchemaKeywordInvalid
    elif key in _JSON_SCHEMA_NUMERIC_SCALAR_KEYS:
        if type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)):
            raise SchemaKeywordInvalid
        if key == "multipleOf" and cast(int | float, value) <= 0:
            raise SchemaKeywordInvalid
    elif key in _JSON_SCHEMA_NONNEGATIVE_INTEGER_KEYS:
        if type(value) is not int or value < 0:
            raise SchemaKeywordInvalid
    elif key == "type":
        if type(value) is str:
            if value not in _JSON_SCHEMA_TYPES:
                raise SchemaKeywordInvalid
        elif type(value) is list:
            if not value or any(type(item) is not str or item not in _JSON_SCHEMA_TYPES for item in value) or len(set(value)) != len(value):
                raise SchemaKeywordInvalid
        else:
            raise SchemaKeywordInvalid
    elif key == "enum":
        if type(value) is not list or not value:
            raise SchemaKeywordInvalid
    elif key == "$vocabulary" and (
        type(value) is not dict or any(type(name) is not str or type(required) is not bool for name, required in value.items())
    ):
        raise SchemaKeywordInvalid


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise SchemaKeywordInvalid
    return cast(dict[str, object], value)


def _strings(value: object) -> None:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise SchemaKeywordInvalid


def _predicate(value: object) -> None:
    row = _mapping(value)
    if set(row) != {"field", "equals"} or type(row["field"]) is not str:
        raise SchemaKeywordInvalid


def _schema_node(value: object) -> None:
    if type(value) is bool:
        return
    row = _mapping(value)
    for key, item in row.items():
        if key in _JSON_SCHEMA_PROSE_KEYS:
            if key == "examples":
                if type(item) is not list:
                    raise SchemaKeywordInvalid
            elif key != "example" and type(item) is not str:
                raise SchemaKeywordInvalid
            continue
        if key == "composer_hidden":
            if type(item) is not bool:
                raise SchemaKeywordInvalid
        elif key == "composer_required_when":
            _predicate(item)
        elif key in _JSON_SCHEMA_SCALAR_KEYS:
            validate_json_schema_scalar(key, item)
        elif key in _JSON_SCHEMA_MAP_KEYS:
            for child in _mapping(item).values():
                _schema_node(child)
        elif key in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
            _schema_node(item)
        elif key in _JSON_SCHEMA_SCHEMA_LIST_KEYS:
            if type(item) is not list:
                raise SchemaKeywordInvalid
            for child in item:
                _schema_node(child)
        elif key == "required":
            _strings(item)
        elif key == "dependentRequired":
            for names in _mapping(item).values():
                _strings(names)
        elif key == "discriminator":
            discriminator = _mapping(item)
            if set(discriminator) - {"propertyName", "mapping"}:
                raise SchemaKeywordInvalid
            if "propertyName" in discriminator and type(discriminator["propertyName"]) is not str:
                raise SchemaKeywordInvalid
            if "mapping" in discriminator and any(type(target) is not str for target in _mapping(discriminator["mapping"]).values()):
                raise SchemaKeywordInvalid
        else:
            # JSON Schema permits vocabulary extensions. Their finite JSON
            # bytes belong to the canonical schema, but only the planner's
            # separate closed projector can authorize forwarding semantics.
            # Known keyword domains above never use this extension branch.
            continue


_KNOB_TEXT_KEYS = frozenset({"name", "kind", "type", "label", "description", "placeholder", "tier", "item_kind"})
_KNOB_BOOLEAN_KEYS = frozenset({"required", "nullable"})
_KNOB_KEYS = _KNOB_TEXT_KEYS | _KNOB_BOOLEAN_KEYS | {"default", "enum", "choices", "visible_when", "required_when", "item_schema", "items"}


def _knob_node(value: object) -> None:
    row = _mapping(value)
    if set(row) != {"fields"} or type(row["fields"]) is not list:
        raise SchemaKeywordInvalid
    for value in row["fields"]:
        field = _mapping(value)
        if set(field) - _KNOB_KEYS or not {"name", "required"} <= set(field) or not ({"kind", "type"} & set(field)):
            raise SchemaKeywordInvalid
        for key, item in field.items():
            if key in _KNOB_TEXT_KEYS:
                if type(item) is not str:
                    raise SchemaKeywordInvalid
            elif key in _KNOB_BOOLEAN_KEYS:
                if type(item) is not bool:
                    raise SchemaKeywordInvalid
            elif key in {"enum", "choices"}:
                _strings(item)
            elif key in {"visible_when", "required_when"}:
                _predicate(item)
            elif key == "item_schema":
                _knob_node(item)
            elif key == "items":
                _schema_node(item)
        if "enum" in field and "choices" in field and field["enum"] != field["choices"]:
            raise SchemaKeywordInvalid


@dataclass(frozen=True, slots=True)
class JSONSchemaSnapshot:
    """Keyword-validated schema object; only literal/metadata leaves are JSON."""

    keywords: Mapping[str, FrozenResponseJSON]

    def to_wire(self) -> dict[str, JsonValue]:
        """Encode admitted leaves; cached values must pass readmission first."""
        return {key: encode_response_json(value) for key, value in self.keywords.items()}


@dataclass(frozen=True, slots=True)
class KnobSchemaSnapshot:
    """Closed knob root and recursively validated field/predicate records."""

    fields: tuple[Mapping[str, FrozenResponseJSON], ...]

    def to_wire(self) -> dict[str, JsonValue]:
        """Encode admitted leaves; cached values must pass readmission first."""
        return {"fields": [{key: encode_response_json(value) for key, value in field.items()} for field in self.fields]}


def readmit_json_schema(value: JSONSchemaSnapshot) -> JSONSchemaSnapshot:
    """Check the immutable representation before encoding keyword domains."""
    if type(value) is not JSONSchemaSnapshot:
        raise FrameworkBugError("Cached plugin JSON Schema has the wrong owned type")
    frozen = parse_frozen_response_json(value.keywords)
    if not isinstance(frozen, Mapping):
        raise FrameworkBugError("Cached plugin JSON Schema root must be an immutable object")
    try:
        _schema_node(encode_response_json(frozen))
    except (SchemaKeywordInvalid, RecursionError):
        raise FrameworkBugError("Cached plugin JSON Schema has malformed keyword domains") from None
    return JSONSchemaSnapshot(frozen)


def readmit_knob_schema(value: KnobSchemaSnapshot) -> KnobSchemaSnapshot:
    """Check every owned field and nested literal before any wire conversion."""
    if type(value) is not KnobSchemaSnapshot:
        raise FrameworkBugError("Cached plugin knob schema has the wrong owned type")
    frozen = parse_frozen_response_json(value.fields)
    if type(frozen) is not tuple:
        raise FrameworkBugError("Cached plugin knob fields must be an immutable sequence")
    admitted: list[Mapping[str, FrozenResponseJSON]] = []
    for field in frozen:
        if not isinstance(field, Mapping):
            raise FrameworkBugError("Cached plugin knob field must be an immutable object")
        admitted.append(field)
    try:
        _knob_node({"fields": encode_response_json(frozen)})
    except (SchemaKeywordInvalid, RecursionError):
        raise FrameworkBugError("Cached plugin knob schema has malformed field domains") from None
    return KnobSchemaSnapshot(tuple(admitted))


def parse_json_schema(value: object) -> JSONSchemaSnapshot:
    frozen = parse_response_json(value)
    try:
        _mapping(value)
        _schema_node(value)
    except (SchemaKeywordInvalid, RecursionError):
        raise FrameworkBugError("Plugin schema producer returned malformed JSON Schema") from None
    if not isinstance(frozen, Mapping):
        raise FrameworkBugError("Plugin schema root must be an object")
    return JSONSchemaSnapshot(frozen)


def parse_knob_schema(value: object) -> KnobSchemaSnapshot:
    frozen = parse_response_json(value)
    try:
        _knob_node(value)
    except (SchemaKeywordInvalid, RecursionError):
        raise FrameworkBugError("Plugin schema producer returned malformed knob schema") from None
    if not isinstance(frozen, Mapping):
        raise FrameworkBugError("Plugin knob schema root must be an object")
    fields = frozen["fields"]
    if not isinstance(fields, tuple):
        raise FrameworkBugError("Plugin knob fields must be a sequence")
    admitted: list[Mapping[str, FrozenResponseJSON]] = []
    for field in fields:
        if not isinstance(field, Mapping):
            raise FrameworkBugError("Plugin knob field must be an object")
        admitted.append(field)
    return KnobSchemaSnapshot(tuple(admitted))
