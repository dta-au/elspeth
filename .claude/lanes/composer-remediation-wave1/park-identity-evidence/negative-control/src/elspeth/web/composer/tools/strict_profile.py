"""OpenAI strict-mode schema checker for composer tool wire schemas.

``check_openai_strict`` walks one tool's ``parameters`` schema and returns
one :class:`StrictViolation` row per reason the schema could not be sent with
``strict: true`` under OpenAI function calling. It never raises and never
mutates its input.

The schemas it reads are ELSPETH-owned tool definitions (the flat registry S
and the wire projection W built from it), so the walk uses exact
``type(x) is dict`` / ``type(x) is list`` checks: S definitions are plain
dicts and lists at every level. A deep-frozen schema is not a plain dict and
is reported as ``root_not_object``, so callers check a thawed schema.

Allowed keywords are exactly ``WIRE_KEYWORD_ALLOWLIST`` (the S0 constant the
S gate already uses to classify ``schema_shape`` errors). Each keyword yields
at most one kind:

* ``oneOf`` / ``allOf`` (and ``anyOf`` at the root) are ``root_union`` at the
  root and ``nested_union`` below it (``anyOf`` below the root is allowed);
* ``$ref`` / ``$defs`` / ``definitions`` are ``ref``;
* any other keyword in :data:`_UNSUPPORTED` is ``unsupported_keyword``;
* any other keyword outside the allowlist is ``keyword_not_allowlisted``
  (this includes ``title``, ``default``, ``minLength``, ``maxLength`` and
  ``examples``);
* ``format`` outside the OpenAI set is ``format_not_supported``;
* ``pattern`` that Python ``re`` cannot compile, or that carries an
  ECMA-only construct, is ``pattern_not_portable``;
* an ``enum`` that contains ``None`` is ``enum_contains_null`` (a nullable
  enum is spelled ``anyOf: [{enum}, {"type": "null"}]`` on the wire).

Rules ported from the strict-tool-contracts design-lane checker; the kind
names below map one to one onto that checker's categories, except that its
``missing_additionalProperties_false`` is spelled
``missing_additional_properties_false`` here and its ``uncertain_keyword``
does not exist (the allowlist decides).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from elspeth.web.composer.tools._dispatch import WIRE_KEYWORD_ALLOWLIST

__all__ = [
    "StrictViolation",
    "StrictViolationKind",
    "check_openai_strict",
]


class StrictViolationKind(StrEnum):
    """Closed vocabulary of reasons a schema is not OpenAI-strict-capable."""

    ROOT_NOT_OBJECT = "root_not_object"
    ROOT_UNION = "root_union"
    NESTED_UNION = "nested_union"
    MISSING_ADDITIONAL_PROPERTIES_FALSE = "missing_additional_properties_false"
    FREE_FORM_OBJECT = "free_form_object"
    OPTIONAL_PROPERTY = "optional_property"
    REQUIRED_NOT_IN_PROPERTIES = "required_not_in_properties"
    REF = "ref"
    TYPELESS_SUBSCHEMA = "typeless_subschema"
    ARRAY_WITHOUT_ITEMS = "array_without_items"
    UNSUPPORTED_KEYWORD = "unsupported_keyword"
    KEYWORD_NOT_ALLOWLISTED = "keyword_not_allowlisted"
    FORMAT_NOT_SUPPORTED = "format_not_supported"
    PATTERN_NOT_PORTABLE = "pattern_not_portable"
    ENUM_CONTAINS_NULL = "enum_contains_null"


@dataclass(frozen=True, slots=True)
class StrictViolation:
    """One reason one tool schema is not strict-capable.

    ``path`` is an RFC 6901 JSON pointer into the tool's ``parameters``
    schema (``""`` is the root). Keyword rows point at the keyword itself.
    """

    tool: str
    path: str
    kind: StrictViolationKind
    detail: str


# Keywords no strict grammar accepts. ``oneOf``/``allOf`` are listed so they
# are never also reported as ``keyword_not_allowlisted``; they report as
# ``root_union`` / ``nested_union`` instead.
_UNSUPPORTED: Final[frozenset[str]] = frozenset(
    {
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "patternProperties",
        "dependentRequired",
        "dependentSchemas",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contains",
        "minProperties",
        "maxProperties",
        "uniqueItems",
    }
)

_UNION_KEYWORDS: Final[frozenset[str]] = frozenset({"oneOf", "allOf"})

_REF_KEYS: Final[frozenset[str]] = frozenset({"$ref", "$defs", "definitions"})

# ``format`` values OpenAI strict mode documents as supported.
_OPENAI_FORMATS: Final[frozenset[str]] = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "ipv4", "ipv6", "uuid"})

# Regex constructs Python ``re`` and an ECMA grammar do not share.
_ECMA_ONLY_PATTERN_FRAGMENTS: Final[tuple[str, ...]] = ("\\p", "\\P", "(?<")

# Keys whose values are data, not subschemas: the walk does not descend.
_DATA_KEYS: Final[frozenset[str]] = frozenset(
    {"enum", "const", "default", "examples", "required", "description", "title", "type", "pattern", "format"}
)

# Keys whose value is a map of name -> subschema.
_SCHEMA_MAP_KEYS: Final[frozenset[str]] = frozenset({"properties", "$defs", "definitions", "patternProperties"})

# Keys whose value is a single subschema.
_SCHEMA_KEYS: Final[frozenset[str]] = frozenset({"items", "additionalProperties", "not", "if", "then", "else", "contains", "propertyNames"})

# Keys whose value is a list of subschemas.
_SCHEMA_LIST_KEYS: Final[frozenset[str]] = frozenset({"oneOf", "anyOf", "allOf"})

# Keys that give a subschema a type of its own.
_TYPE_MARKERS: Final[frozenset[str]] = frozenset({"type", "enum", "const", "anyOf", "oneOf", "allOf", "$ref"})


def _pointer(parent: str, key: str) -> str:
    """Append one RFC 6901 reference token to ``parent``."""
    return f"{parent}/{key.replace('~', '~0').replace('/', '~1')}"


def _type_names(node: dict[str, Any]) -> tuple[Any, ...]:
    if "type" not in node:
        return ()
    declared = node["type"]
    if type(declared) is list:
        return tuple(declared)
    return (declared,)


def _is_object(node: dict[str, Any]) -> bool:
    if "object" in _type_names(node):
        return True
    return "properties" in node and "type" not in node


def _is_array(node: dict[str, Any]) -> bool:
    return "array" in _type_names(node)


def _pattern_portability_problem(pattern: Any) -> str | None:
    """Return why ``pattern`` is not portable to a strict grammar, or ``None``."""
    if type(pattern) is not str:
        return "pattern is not a string"
    for fragment in _ECMA_ONLY_PATTERN_FRAGMENTS:
        if fragment in pattern:
            return f"ECMA-only construct {fragment!r}"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"Python re cannot compile it: {exc.msg}"
    return None


class _Walker:
    """Collects rows for one tool schema."""

    def __init__(self, tool: str) -> None:
        self._tool = tool
        self.rows: list[StrictViolation] = []

    def add(self, path: str, kind: StrictViolationKind, detail: str = "") -> None:
        self.rows.append(StrictViolation(tool=self._tool, path=path, kind=kind, detail=detail))

    def check_keywords(self, node: dict[str, Any], path: str) -> None:
        for key in node:
            key_path = _pointer(path, key)
            if key in _REF_KEYS:
                self.add(key_path, StrictViolationKind.REF)
            elif key in _UNION_KEYWORDS:
                if path == "":
                    self.add(key_path, StrictViolationKind.ROOT_UNION, f"{key} at the parameters root")
                else:
                    self.add(key_path, StrictViolationKind.NESTED_UNION, f"{key} below the parameters root")
            elif key == "anyOf" and path == "":
                self.add(key_path, StrictViolationKind.ROOT_UNION, "anyOf at the parameters root")
            elif key in _UNSUPPORTED:
                self.add(key_path, StrictViolationKind.UNSUPPORTED_KEYWORD, key)
            elif key not in WIRE_KEYWORD_ALLOWLIST:
                self.add(key_path, StrictViolationKind.KEYWORD_NOT_ALLOWLISTED, key)
            elif key == "format" and node[key] not in _OPENAI_FORMATS:
                self.add(key_path, StrictViolationKind.FORMAT_NOT_SUPPORTED, str(node[key]))
            elif key == "pattern" and (problem := _pattern_portability_problem(node[key])) is not None:
                self.add(key_path, StrictViolationKind.PATTERN_NOT_PORTABLE, problem)
            elif key == "enum" and type(node[key]) is list and None in node[key]:
                self.add(key_path, StrictViolationKind.ENUM_CONTAINS_NULL)

    def check_object(self, node: dict[str, Any], path: str) -> None:
        properties: Any = node["properties"] if "properties" in node else {}
        has_properties = type(properties) is dict and len(properties) > 0
        if "additionalProperties" in node:
            additional = node["additionalProperties"]
            closed = additional is False
        else:
            additional = None
            closed = False
        if not has_properties:
            if not closed:
                self.add(path, StrictViolationKind.FREE_FORM_OBJECT, "object with no properties")
        elif "additionalProperties" not in node:
            self.add(path, StrictViolationKind.MISSING_ADDITIONAL_PROPERTIES_FALSE)
        elif not closed:
            open_detail = "additionalProperties: true" if additional is True else "additionalProperties map"
            self.add(path, StrictViolationKind.FREE_FORM_OBJECT, open_detail)
        required: Any = node["required"] if "required" in node else []
        required_names = frozenset(name for name in required if type(name) is str) if type(required) is list else frozenset()
        if type(properties) is dict:
            for name in properties:
                if name not in required_names:
                    self.add(_pointer(_pointer(path, "properties"), name), StrictViolationKind.OPTIONAL_PROPERTY)
            for name in sorted(required_names - frozenset(properties)):
                self.add(_pointer(path, "required"), StrictViolationKind.REQUIRED_NOT_IN_PROPERTIES, name)

    def walk(self, node: Any, path: str) -> None:
        if type(node) is not dict:
            return
        self.check_keywords(node, path)
        if not (_TYPE_MARKERS & node.keys()) and "properties" not in node:
            self.add(path, StrictViolationKind.TYPELESS_SUBSCHEMA)
        if _is_object(node):
            self.check_object(node, path)
        if _is_array(node) and "items" not in node:
            self.add(path, StrictViolationKind.ARRAY_WITHOUT_ITEMS)
        for key, value in node.items():
            if key in _DATA_KEYS:
                continue
            key_path = _pointer(path, key)
            if key in _SCHEMA_MAP_KEYS and type(value) is dict:
                for name, child in value.items():
                    self.walk(child, _pointer(key_path, name))
            elif key in _SCHEMA_KEYS:
                self.walk(value, key_path)
            elif key in _SCHEMA_LIST_KEYS and type(value) is list:
                for index, member in enumerate(value):
                    self.walk(member, _pointer(key_path, str(index)))


def check_openai_strict(schema: Mapping[str, Any], *, tool: str) -> tuple[StrictViolation, ...]:
    """Return every reason ``schema`` cannot be sent with OpenAI ``strict: true``.

    An empty tuple means the schema is strict-capable as it stands.
    """
    walker = _Walker(tool)
    if type(schema) is not dict or not _is_object(schema):
        walker.add("", StrictViolationKind.ROOT_NOT_OBJECT)
    walker.walk(schema, "")
    return tuple(walker.rows)
