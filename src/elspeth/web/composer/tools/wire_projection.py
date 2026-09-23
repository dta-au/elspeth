"""The static per-dialect wire projection W of the composer tool registry.

The flat registry schema S (``_dispatch.get_tool_definitions()``) is the
single authority for what a tool accepts: MCP advertises it, the runtime
Draft 2020-12 gate enforces it, and the byte pins in
``test_tool_declarations.py`` hold it. W is what the web composer *sends* a
provider. It is derived from S once, at import, by one pure walk per
:class:`ToolContractDialect`, and is never hand-written:

* ``none`` is S unchanged, except that the web ``set_pipeline`` arguments
  carry a required ``{"pipeline": <flat document>}`` envelope. LiteLLM's
  Anthropic and Bedrock adapters keep unions nested below a property but
  discard root-level ``oneOf``, so the flat set_pipeline schema would lose
  its source/sources choice on those routes. No tool carries a ``strict``
  key: this is the list every route sent before strict tool contracts.
* ``openai_strict`` projects each tool whose S has no free-form object (32
  of the 42) into an OpenAI strict-capable schema:

  - every S-optional property becomes required and nullable (``[T, "null"]``,
    or ``anyOf: [{enum}, {"type": "null"}]`` for an enum), and the decode
    plan gains a ``StripNull`` node for it, so decode turns the ``null`` a
    grammar-bound model must send back into the omission S expects;
  - keywords outside ``WIRE_KEYWORD_ALLOWLIST`` move to a ledger and are
    rendered as description text from a closed template map;
  - the 6 descriptions that tell the model to *omit* a now-required key are
    rewritten to say "pass null" (``_STRICT_DESCRIPTION_OVERRIDES``).

  The 10 option-bearing tools keep their ``none`` parameters, and the wire
  list stamps them ``strict: false``.

This module owns the projection, the ledger, the limits check and the
faithfulness gate, all of which run at import, so a projection that is not
faithful to S stops web boot rather than reaching a provider.

It also owns the way back. :func:`decode_wire_arguments` classifies the
provider's arguments against the W that was sent (``wire_conformant``),
unwraps the set_pipeline envelope (its one rejection), and on
``openai_strict`` turns a ``null`` at a promoted position back into the
omission S expects. It never admits or rejects on W: S stays the only
contract. :func:`encode_semantic_arguments` is the inverse of the envelope.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, cast

from jsonschema import Draft202012Validator

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tools._dispatch import _TOOL_SCHEMA_BY_NAME, WIRE_KEYWORD_ALLOWLIST, get_tool_definitions
from elspeth.web.composer.tools.strict_profile import StrictViolationKind, check_openai_strict

__all__ = [
    "OPENAI_STRICT_LIMITS",
    "DecodedArguments",
    "EnvelopeUnwrap",
    "LedgerEntry",
    "StripNull",
    "WireLimits",
    "WireLimitsReport",
    "WireProjectionError",
    "WireTool",
    "assert_wire_projection_faithful",
    "build_wire_tool_defs",
    "decode_wire_arguments",
    "encode_semantic_arguments",
    "omission_instruction_matches",
    "project_tool",
    "stamp_planner_terminal",
    "wire_limits_report",
    "wire_tool_definitions",
]


class WireProjectionError(Exception):
    """The wire projection cannot be built faithfully from S.

    Raised at import (so web boot fails), or by a caller that asks for a
    projection that does not exist. Never raised for model-authored input.
    """


# ---------------------------------------------------------------- data model


@dataclass(frozen=True, slots=True)
class EnvelopeUnwrap:
    """Decode node: the wire arguments are ``{key: <semantic arguments>}``."""

    key: str = "pipeline"


@dataclass(frozen=True, slots=True)
class StripNull:
    """Decode node: a ``null`` at this property path means "omitted" in S."""

    path: tuple[str, ...]


DecodeNode = EnvelopeUnwrap | StripNull


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """A keyword removed from W, rendered as description text instead.

    ``path`` is the RFC 6901 pointer of the owning subschema inside the
    tool's ``parameters`` (``""`` is the root).
    """

    path: str
    keyword: str
    value: Any

    def __post_init__(self) -> None:
        freeze_fields(self, "value")


@dataclass(frozen=True, slots=True)
class WireTool:
    """One tool as it is sent under one dialect.

    ``function`` is the deep-frozen ``{name, description, parameters}``;
    :meth:`thawed_function` returns a fresh plain-JSON copy (lists for every
    array, key order kept) for the wire.
    """

    name: str
    dialect: ToolContractDialect
    function: Mapping[str, Any]
    strict_capable: bool
    decode_plan: tuple[DecodeNode, ...]
    ledger: tuple[LedgerEntry, ...]
    promoted_paths: frozenset[tuple[str, ...]]

    def __post_init__(self) -> None:
        freeze_fields(self, "function", "decode_plan", "ledger")

    def thawed_function(self) -> dict[str, Any]:
        return cast(dict[str, Any], deep_thaw(self.function))


@dataclass(frozen=True, slots=True)
class DecodedArguments:
    """Provider arguments decoded back to the flat semantic form S admits.

    ``semantic`` is deep-frozen. ``wire_conformant`` records whether the raw
    arguments conformed to the W they were sent under; it is a
    classification, never an admission decision.
    """

    semantic: Mapping[str, Any]
    wire_conformant: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "semantic")


@dataclass(frozen=True, slots=True)
class WireLimits:
    """Schema-size limits a strict grammar imposes on one request."""

    max_properties: int
    max_depth: int
    max_characters: int
    max_enum_values: int


# OpenAI strict mode: 5000 object properties, 10 levels of nesting, 120,000
# characters of property names, enum values and const values, and 1000 enum
# values (master plan §2.2).
OPENAI_STRICT_LIMITS: Final[WireLimits] = WireLimits(
    max_properties=5000,
    max_depth=10,
    max_characters=120_000,
    max_enum_values=1000,
)


@dataclass(frozen=True, slots=True)
class WireLimitsReport:
    """Measured size of one dialect's whole tool list.

    Counted over every tool in the list, not only the strict-capable ones,
    so the report is an upper bound on what any strict request carries.
    ``max_depth`` counts object and array schema nodes on the deepest path
    (the root object is 1). ``total_characters`` is the length of every
    property name, enum value and const value (non-strings as compact JSON).
    """

    dialect: ToolContractDialect
    total_properties: int
    max_depth: int
    total_characters: int
    total_enum_values: int
    strict_tool_count: int


# ---------------------------------------------------------------- omission prose

# ``(tool, pointer into the function object) -> (exact old text, replacement)``.
# The pointer ``/description`` is the tool description. Each entry is live:
# a missing old text raises (a stale override), and so does an entry for a
# tool that is not strict-capable. ``none`` keeps today's text.
_STRICT_DESCRIPTION_OVERRIDES: Final[Mapping[tuple[str, str], tuple[str, str]]] = MappingProxyType(
    {
        ("get_pipeline_state", "/parameters/properties/component/description"): (
            "Accepted full-state aliases: omit component,",
            "Accepted full-state aliases: pass null for component,",
        ),
        ("get_plugin_assistance", "/description"): (
            "Omit ``issue_code`` (or pass null) to get discovery-time guidance",
            "Pass null for ``issue_code`` to get discovery-time guidance",
        ),
        ("get_plugin_assistance", "/parameters/properties/issue_code/description"): (
            "Omit or pass null for discovery-time guidance.",
            "Pass null for discovery-time guidance.",
        ),
        ("list_models", "/parameters/properties/provider/description"): (
            "Omit to get a provider summary",
            "Pass null to get a provider summary",
        ),
        ("request_interpretation_review", "/parameters/properties/kind/description"): (
            "and OMIT llm_draft — the server computes",
            "and pass null for llm_draft — the server computes",
        ),
        ("request_interpretation_review", "/parameters/properties/llm_draft/description"): (
            "OMIT this when the review site already carries",
            "Pass null for llm_draft when the review site already carries",
        ),
    }
)

# The closed vocabulary of omission instructions the faithfulness gate
# rejects in a strict-capable W. It is a closed list: a phrasing outside it
# (for example "a full-state read (no component ...)") is not caught.
_OMISSION_VOCABULARY: Final[re.Pattern[str]] = re.compile(
    r"\bomit"
    r"|\bleave (it |this )?(out|blank|empty|unset)"
    r"|\bif not (provided|given|set|supplied)"
    r"|\bnot provided\b"
    r"|\b(if|when) absent\b"
    r"|\bdo not (send|pass|include|provide)\b",
    re.IGNORECASE,
)

# Reviewed exceptions: ``(tool, pointer into the function object) -> exact
# text``. The get_pipeline_state sentence is about the set_pipeline
# envelope, not about a promoted position. Each entry must be live.
_OMISSION_VOCABULARY_EXCEPTIONS: Final[Mapping[tuple[str, str], str]] = MappingProxyType(
    {
        ("get_pipeline_state", "/description"): "do not send its source/nodes/edges/outputs fields at the top level",
    }
)


def omission_instruction_matches(text: str) -> bool:
    """Return whether ``text`` contains an instruction to omit a key."""
    return _OMISSION_VOCABULARY.search(text) is not None


# ---------------------------------------------------------------- projection

# The one tool whose web arguments carry a provider envelope.
_ENVELOPE_TOOL: Final[str] = "set_pipeline"
_ENVELOPE_KEY: Final[str] = "pipeline"


def _envelope(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {_ENVELOPE_KEY: parameters},
        "required": [_ENVELOPE_KEY],
        "additionalProperties": False,
    }


def _pointer(parent: str, key: str) -> str:
    return f"{parent}/{key.replace('~', '~0').replace('/', '~1')}"


def _length_bound(tool: str, pointer: str, keyword: str, value: Any) -> int:
    if type(value) is not int:
        raise WireProjectionError(f"{tool}{pointer}: {keyword} must be an integer to render into the ledger")
    return value


def _characters(count: int) -> str:
    return "character" if count == 1 else "characters"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _nullable(tool: str, pointer: str, node: dict[str, Any]) -> dict[str, Any]:
    """Return ``node`` widened to also accept ``null`` (projection rule 3)."""
    if "const" in node:
        raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional const")
    if "enum" in node:
        member: dict[str, Any] = {"type": node["type"]} if "type" in node else {}
        member["enum"] = node["enum"]
        widened: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("type", "enum"):
                if "anyOf" not in widened:
                    widened["anyOf"] = [member, {"type": "null"}]
            elif key == "description":
                widened[key] = value
            else:
                raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional enum carrying {key!r}")
        return widened
    if "type" in node:
        declared = node["type"]
        if type(declared) is str:
            nullable_type: Any = [declared] if declared == "null" else [declared, "null"]
        elif type(declared) is list:
            nullable_type = declared if "null" in declared else [*declared, "null"]
        else:
            raise WireProjectionError(f"{tool}{pointer}: type must be a string or a list")
        return {key: (nullable_type if key == "type" else value) for key, value in node.items()}
    raise WireProjectionError(f"{tool}{pointer}: no nullable rule for an optional subschema without a type")


class _StrictProjection:
    """One ``openai_strict`` walk over one strict-capable tool's flat S."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        self.ledger: list[LedgerEntry] = []
        self.strip: list[StripNull] = []
        self.tool_sentences: list[str] = []

    def _ledger_sentence(
        self,
        node: dict[str, Any],
        pointer: str,
        keyword: str,
        value: Any,
        *,
        promoted: bool,
        is_items: bool,
    ) -> tuple[str | None, str | None]:
        """Return ``(own sentence, sentence for the parent array)`` for one ledgered keyword."""
        has_description = "description" in node
        if keyword == "maxLength":
            bound = _length_bound(self.tool, pointer, keyword, value)
            if has_description:
                return f"At most {bound} {_characters(bound)}.", None
            if is_items:
                return None, f"Each item has at most {bound} {_characters(bound)}."
        elif keyword == "minLength":
            bound = _length_bound(self.tool, pointer, keyword, value)
            if has_description:
                return f"At least {bound} {_characters(bound)}.", None
        elif keyword == "default":
            if promoted and has_description:
                return f"Pass null to use the default ({_canonical_json(value)}).", None
        elif keyword == "examples" and pointer == "":
            if type(value) is not list or not value:
                raise WireProjectionError(f"{self.tool}: root examples must be a non-empty list")
            self.tool_sentences.extend(f"Example arguments: {_canonical_json(example)}." for example in value)
            return None, None
        raise WireProjectionError(f"{self.tool}{pointer}: no wire rule for keyword {keyword!r} on this subschema")

    def project(
        self,
        node: dict[str, Any],
        pointer: str,
        property_path: tuple[str, ...] | None,
        *,
        promoted: bool,
        is_items: bool,
    ) -> tuple[dict[str, Any], list[str]]:
        """Project one subschema; return it and any sentences its parent array owns."""
        if type(node) is not dict:
            raise WireProjectionError(f"{self.tool}{pointer}: subschema is not an object")
        projected: dict[str, Any] = {}
        own_sentences: list[str] = []
        parent_sentences: list[str] = []
        item_sentences: list[str] = []
        promoted_names: list[str] = []
        for key, value in node.items():
            key_pointer = _pointer(pointer, key)
            if key not in WIRE_KEYWORD_ALLOWLIST:
                self.ledger.append(LedgerEntry(path=pointer, keyword=key, value=deepcopy(value)))
                own, for_parent = self._ledger_sentence(node, pointer, key, value, promoted=promoted, is_items=is_items)
                if own is not None:
                    own_sentences.append(own)
                if for_parent is not None:
                    parent_sentences.append(for_parent)
                continue
            if key == "properties":
                if type(value) is not dict:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: properties must be an object")
                required = node["required"] if "required" in node else []
                children: dict[str, Any] = {}
                for name, child in value.items():
                    child_promoted = name not in required
                    child_path = None if property_path is None else (*property_path, name)
                    child_pointer = _pointer(key_pointer, name)
                    projected_child, _ = self.project(child, child_pointer, child_path, promoted=child_promoted, is_items=False)
                    if child_promoted:
                        if child_path is None:
                            raise WireProjectionError(
                                f"{self.tool}{child_pointer}: no decode rule for an optional property inside an array"
                            )
                        projected_child = _nullable(self.tool, child_pointer, projected_child)
                        self.strip.append(StripNull(path=child_path))
                        promoted_names.append(name)
                    children[name] = projected_child
                projected[key] = children
            elif key == "required":
                if type(value) is not list or "properties" not in node:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: required needs a properties map")
                projected[key] = [*value, *(name for name in node["properties"] if name not in value)]
            elif key == "items":
                projected_items, item_sentences = self.project(value, key_pointer, None, promoted=False, is_items=True)
                projected[key] = projected_items
            elif key == "anyOf":
                if type(value) is not list:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: anyOf must be a list")
                projected[key] = [
                    self.project(member, _pointer(key_pointer, str(index)), None, promoted=False, is_items=False)[0]
                    for index, member in enumerate(value)
                ]
            elif key == "additionalProperties":
                if value is not False:
                    raise WireProjectionError(f"{self.tool}{key_pointer}: a strict-capable object must be closed")
                projected[key] = False
            else:
                projected[key] = deepcopy(value)
        if promoted_names and "required" not in node:
            projected["required"] = promoted_names
        own_sentences.extend(item_sentences)
        if own_sentences:
            if "description" not in projected or type(projected["description"]) is not str:
                raise WireProjectionError(f"{self.tool}{pointer}: ledger text has no description to render into")
            projected["description"] = " ".join([projected["description"], *own_sentences])
        return projected, parent_sentences


def _resolve_description(function: dict[str, Any], tool: str, pointer: str) -> tuple[dict[str, Any], str]:
    """Return the container and key of the description string at ``pointer``."""
    tokens = pointer.lstrip("/").split("/")
    container: Any = function
    for token in tokens[:-1]:
        if type(container) is not dict or token not in container:
            raise WireProjectionError(f"{tool}{pointer}: override pointer does not resolve")
        container = container[token]
    key = tokens[-1]
    if type(container) is not dict or key not in container or type(container[key]) is not str:
        raise WireProjectionError(f"{tool}{pointer}: override pointer does not name a description")
    return container, key


def _apply_overrides(
    function: dict[str, Any],
    tool: str,
    overrides: Mapping[tuple[str, str], tuple[str, str]],
) -> None:
    for (override_tool, pointer), (old, new) in overrides.items():
        if override_tool != tool:
            continue
        container, key = _resolve_description(function, tool, pointer)
        if container[key].count(old) != 1:
            raise WireProjectionError(f"{tool}{pointer}: stale description override (old text not found exactly once)")
        container[key] = container[key].replace(old, new)


def project_tool(
    definition: Mapping[str, Any],
    dialect: ToolContractDialect,
    *,
    overrides: Mapping[tuple[str, str], tuple[str, str]] = _STRICT_DESCRIPTION_OVERRIDES,
) -> WireTool:
    """Project one flat registry definition onto the wire of ``dialect``."""
    name = definition["name"]
    flat = deepcopy(definition["parameters"])
    if type(flat) is not dict:
        raise WireProjectionError(f"{name}: parameters must be an object")
    decode_plan: tuple[DecodeNode, ...] = (EnvelopeUnwrap(key=_ENVELOPE_KEY),) if name == _ENVELOPE_TOOL else ()
    none_parameters = _envelope(flat) if name == _ENVELOPE_TOOL else flat
    free_form = any(row.kind is StrictViolationKind.FREE_FORM_OBJECT for row in check_openai_strict(flat, tool=name))
    if dialect == ToolContractDialect.NONE or free_form:
        return WireTool(
            name=name,
            dialect=dialect,
            function={"name": name, "description": definition["description"], "parameters": none_parameters},
            strict_capable=False,
            decode_plan=decode_plan,
            ledger=(),
            promoted_paths=frozenset(),
        )
    if name == _ENVELOPE_TOOL:
        raise WireProjectionError(f"{name}: no strict projection rule for an enveloped tool")
    projection = _StrictProjection(name)
    parameters, orphaned = projection.project(flat, "", (), promoted=False, is_items=False)
    if orphaned:
        raise WireProjectionError(f"{name}: ledger text has no description to render into")
    description = " ".join([definition["description"], *projection.tool_sentences])
    function = {"name": name, "description": description, "parameters": parameters}
    _apply_overrides(function, name, overrides)
    rows = check_openai_strict(parameters, tool=name)
    if rows:
        found = ", ".join(f"{row.path or '/'} {row.kind}" for row in rows)
        raise WireProjectionError(f"{name}: projected schema is not strict-capable: {found}")
    return WireTool(
        name=name,
        dialect=dialect,
        function=function,
        strict_capable=True,
        decode_plan=tuple(projection.strip),
        ledger=tuple(projection.ledger),
        promoted_paths=frozenset(node.path for node in projection.strip),
    )


# ---------------------------------------------------------------- faithfulness


def _is_nullable(node: Any) -> bool:
    if type(node) is not dict:
        return False
    if "type" in node:
        declared = node["type"]
        if declared == "null" or (type(declared) is list and "null" in declared):
            return True
    if "enum" in node and type(node["enum"]) is list and None in node["enum"]:
        return True
    if "anyOf" in node and type(node["anyOf"]) is list:
        return any(_is_nullable(member) for member in node["anyOf"])
    return False


def _requiredness_failures(
    tool: WireTool,
    flat: Any,
    wire: Any,
    path: tuple[str, ...],
) -> Iterator[str]:
    """Yield every property whose wire requiredness is not faithful to S (checks 2 and 3)."""
    if type(flat) is not dict or type(wire) is not dict or "properties" not in flat:
        return
    flat_required = flat["required"] if "required" in flat else []
    wire_required = wire["required"] if "required" in wire else []
    for name, flat_child in flat["properties"].items():
        child_path = (*path, name)
        wire_child = wire["properties"][name] if "properties" in wire and name in wire["properties"] else None
        if wire_child is None:
            yield f"{tool.name}: {'.'.join(child_path)} is missing from W"
            continue
        if name not in wire_required:
            yield f"{tool.name}: {'.'.join(child_path)} is not wire-required"
        if name in flat_required:
            if _is_nullable(wire_child) is not _is_nullable(flat_child):
                yield f"{tool.name}: flat-required {'.'.join(child_path)} changed nullability on the wire"
            if child_path in tool.promoted_paths:
                yield f"{tool.name}: flat-required {'.'.join(child_path)} has a StripNull node"
        else:
            if not _is_nullable(wire_child):
                yield f"{tool.name}: promoted {'.'.join(child_path)} is not nullable on the wire"
            if child_path not in tool.promoted_paths:
                yield f"{tool.name}: promoted {'.'.join(child_path)} has no StripNull node"
        yield from _requiredness_failures(tool, flat_child, wire_child, child_path)


def _flat_optional_paths(flat: Any, path: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    if type(flat) is not dict or "properties" not in flat:
        return
    required = flat["required"] if "required" in flat else []
    for name, child in flat["properties"].items():
        if name not in required:
            yield (*path, name)
        yield from _flat_optional_paths(child, (*path, name))


def _description_sites(value: Any, pointer: str) -> Iterator[tuple[str, str]]:
    """Yield ``(pointer, text)`` for every description string below ``value``."""
    if type(value) is dict:
        for key, child in value.items():
            child_pointer = _pointer(pointer, key)
            if key == "description" and type(child) is str:
                yield child_pointer, child
            else:
                yield from _description_sites(child, child_pointer)
    elif type(value) is list:
        for index, child in enumerate(value):
            yield from _description_sites(child, _pointer(pointer, str(index)))


def _null_enum_found(value: Any) -> bool:
    if type(value) is dict:
        if "enum" in value and type(value["enum"]) is list and None in value["enum"]:
            return True
        return any(_null_enum_found(child) for child in value.values())
    if type(value) is list:
        return any(_null_enum_found(child) for child in value)
    return False


def _same_json(left: Any, right: Any) -> bool:
    """Type-sensitive, key-order-sensitive JSON equality."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return list(left) == list(right) and all(_same_json(left[key], right[key]) for key in left)
    if type(left) is list:
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right, strict=True))
    return bool(left == right)


def assert_wire_projection_faithful(
    defs: Mapping[ToolContractDialect, Mapping[str, WireTool]],
    definitions: Sequence[Mapping[str, Any]],
    *,
    omission_exceptions: Mapping[tuple[str, str], str] = _OMISSION_VOCABULARY_EXCEPTIONS,
) -> None:
    """Raise :class:`WireProjectionError` unless every W is faithful to its S."""
    flat_by_name = {definition["name"]: definition for definition in definitions}
    strict_defs = defs[ToolContractDialect.OPENAI_STRICT]
    used_exceptions: set[tuple[str, str]] = set()
    for name, tool in strict_defs.items():
        function = tool.thawed_function()
        flat = flat_by_name[name]["parameters"]
        for entry in tool.ledger:
            # Check 4: a ledgered keyword is only safe when S still enforces it.
            if name not in _TOOL_SCHEMA_BY_NAME:
                raise WireProjectionError(f"{name}: ledgered {entry.keyword!r} sits on a tool the S gate does not cover")
        if not tool.strict_capable:
            continue
        # Check 1: strict-capable W passes the strict checker.
        rows = check_openai_strict(function["parameters"], tool=name)
        if rows:
            raise WireProjectionError(f"{name}: strict-capable W fails the strict checker")
        # Checks 2 and 3: requiredness and nullability follow S.
        failures = list(_requiredness_failures(tool, flat, function["parameters"], ()))
        if failures:
            raise WireProjectionError("; ".join(failures))
        if tool.promoted_paths != frozenset(_flat_optional_paths(flat, ())):
            raise WireProjectionError(f"{name}: StripNull nodes do not match the S-optional properties")
        # Check 5: no omission instruction anywhere in a strict-capable W.
        sites = [("/description", function["description"]), *_description_sites(function["parameters"], "/parameters")]
        for pointer, text in sites:
            exception = omission_exceptions[(name, pointer)] if (name, pointer) in omission_exceptions else None
            if exception is not None:
                if exception not in text:
                    raise WireProjectionError(f"{name}{pointer}: stale omission-vocabulary exception")
                used_exceptions.add((name, pointer))
                text = text.replace(exception, "")
            if omission_instruction_matches(text):
                raise WireProjectionError(f"{name}{pointer}: description instructs the model to omit a wire-required key")
        # Check 6: no enum in a strict-capable W contains null.
        if _null_enum_found(function["parameters"]):
            raise WireProjectionError(f"{name}: an enum in the strict-capable W contains null")
    stale = set(omission_exceptions) - used_exceptions
    if stale:
        raise WireProjectionError(f"stale omission-vocabulary exceptions: {sorted(stale)}")
    # Check 7: the none W, as thawed for the wire, is S plus the envelope.
    none_defs = defs[ToolContractDialect.NONE]
    if list(none_defs) != list(flat_by_name):
        raise WireProjectionError("none W does not list the registry tools in registry order")
    for name, tool in none_defs.items():
        definition = flat_by_name[name]
        parameters = definition["parameters"]
        expected = {
            "name": name,
            "description": definition["description"],
            "parameters": _envelope(parameters) if name == _ENVELOPE_TOOL else parameters,
        }
        if not _same_json(tool.thawed_function(), expected):
            raise WireProjectionError(f"{name}: none W is not S")


# ---------------------------------------------------------------- limits


def _subschemas(node: dict[str, Any]) -> Iterator[Any]:
    for key, value in node.items():
        if key == "properties" and type(value) is dict:
            yield from value.values()
        elif key in ("items", "additionalProperties", "not") and type(value) is dict:
            yield value
        elif key in ("anyOf", "oneOf", "allOf") and type(value) is list:
            yield from value


def _is_container_schema(node: dict[str, Any]) -> bool:
    if "type" not in node:
        return "properties" in node
    declared = node["type"]
    names = declared if type(declared) is list else [declared]
    return "object" in names or "array" in names


def _measure(node: Any, depth: int, totals: dict[str, int]) -> None:
    if type(node) is not dict:
        return
    here = depth + (1 if _is_container_schema(node) else 0)
    totals["max_depth"] = max(totals["max_depth"], here)
    if "properties" in node and type(node["properties"]) is dict:
        totals["total_properties"] += len(node["properties"])
        totals["total_characters"] += sum(len(name) for name in node["properties"])
    for keyword in ("enum", "const"):
        if keyword in node:
            values = node[keyword] if keyword == "enum" and type(node[keyword]) is list else [node[keyword]]
            if keyword == "enum":
                totals["total_enum_values"] += len(values)
            totals["total_characters"] += sum(len(value) if type(value) is str else len(_canonical_json(value)) for value in values)
    for child in _subschemas(node):
        _measure(child, here, totals)


def _limits_report(dialect: ToolContractDialect, tools: Mapping[str, WireTool]) -> WireLimitsReport:
    totals = {"total_properties": 0, "max_depth": 0, "total_characters": 0, "total_enum_values": 0}
    for tool in tools.values():
        _measure(tool.thawed_function()["parameters"], 0, totals)
    return WireLimitsReport(
        dialect=dialect,
        total_properties=totals["total_properties"],
        max_depth=totals["max_depth"],
        total_characters=totals["total_characters"],
        total_enum_values=totals["total_enum_values"],
        strict_tool_count=sum(1 for tool in tools.values() if tool.strict_capable),
    )


def _check_limits(report: WireLimitsReport, limits: WireLimits) -> None:
    exceeded = [
        f"{label} {measured} > {limit}"
        for label, measured, limit in (
            ("properties", report.total_properties, limits.max_properties),
            ("depth", report.max_depth, limits.max_depth),
            ("characters", report.total_characters, limits.max_characters),
            ("enum values", report.total_enum_values, limits.max_enum_values),
        )
        if measured > limit
    ]
    if exceeded:
        raise WireProjectionError(f"{report.dialect} tool list exceeds the strict limits: {', '.join(exceeded)}")


# ---------------------------------------------------------------- build


def build_wire_tool_defs(
    definitions: Sequence[Mapping[str, Any]],
    *,
    limits: WireLimits,
    overrides: Mapping[tuple[str, str], tuple[str, str]] = _STRICT_DESCRIPTION_OVERRIDES,
    omission_exceptions: Mapping[tuple[str, str], str] = _OMISSION_VOCABULARY_EXCEPTIONS,
) -> Mapping[ToolContractDialect, Mapping[str, WireTool]]:
    """Project, check and freeze every dialect's W for ``definitions``.

    Pure: the only inputs are the arguments. Raises
    :class:`WireProjectionError` on any unfaithful projection, stale
    override or exception, or exceeded limit.
    """
    names = [definition["name"] for definition in definitions]
    if len(set(names)) != len(names):
        raise WireProjectionError("tool definitions contain a duplicate name")
    defs: dict[ToolContractDialect, Mapping[str, WireTool]] = {
        dialect: MappingProxyType(
            {definition["name"]: project_tool(definition, dialect, overrides=overrides) for definition in definitions}
        )
        for dialect in ToolContractDialect
    }
    strict_capable = {name for name, tool in defs[ToolContractDialect.OPENAI_STRICT].items() if tool.strict_capable}
    stale = sorted(key for key in overrides if key[0] not in strict_capable)
    if stale:
        raise WireProjectionError(f"description overrides target tools that are not strict-capable: {stale}")
    assert_wire_projection_faithful(defs, definitions, omission_exceptions=omission_exceptions)
    for dialect, tools in defs.items():
        _check_limits(_limits_report(dialect, tools), limits)
    return MappingProxyType(defs)


# Built once, at import, in registry order (``wire_secret_ref`` last).
_WIRE_TOOL_DEFS: Final[Mapping[ToolContractDialect, Mapping[str, WireTool]]] = build_wire_tool_defs(
    get_tool_definitions(),
    limits=OPENAI_STRICT_LIMITS,
)

_WIRE_LIMITS_REPORTS: Final[Mapping[ToolContractDialect, WireLimitsReport]] = MappingProxyType(
    {dialect: _limits_report(dialect, tools) for dialect, tools in _WIRE_TOOL_DEFS.items()}
)


def wire_limits_report(dialect: ToolContractDialect) -> WireLimitsReport:
    """Return the measured size of ``dialect``'s whole tool list."""
    return _WIRE_LIMITS_REPORTS[dialect]


def wire_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, Any]]:
    """Return fresh LiteLLM function dicts for ``dialect``, in registry order.

    ``none``: ``{"type": "function", "function": {name, description,
    parameters}}`` with no ``strict`` key. ``openai_strict``: the same plus
    ``function.strict``, ``True`` on the strict-capable tools and an explicit
    ``False`` on the others.
    """
    tools: list[dict[str, Any]] = []
    for tool in _WIRE_TOOL_DEFS[dialect].values():
        function = tool.thawed_function()
        if dialect == ToolContractDialect.OPENAI_STRICT:
            function["strict"] = tool.strict_capable
        tools.append({"type": "function", "function": function})
    return tools


def stamp_planner_terminal(definition: Mapping[str, Any], dialect: ToolContractDialect) -> dict[str, Any]:
    """Return a copy of the planner terminal tool stamped for ``dialect``.

    The terminal is never strict-capable in S1: ``openai_strict`` gets an
    explicit ``strict: false``; ``none`` gets an unchanged copy.
    """
    stamped = deepcopy(dict(definition))
    if dialect == ToolContractDialect.OPENAI_STRICT:
        stamped["function"]["strict"] = False
    return stamped


# ---------------------------------------------------------------- decode / encode

# One compiled validator per sent W, so ``wire_conformant`` holds the raw
# arguments to exactly the schema the provider was given.
_WIRE_VALIDATORS: Final[Mapping[ToolContractDialect, Mapping[str, Draft202012Validator]]] = MappingProxyType(
    {
        dialect: MappingProxyType({name: Draft202012Validator(tool.thawed_function()["parameters"]) for name, tool in tools.items()})
        for dialect, tools in _WIRE_TOOL_DEFS.items()
    }
)


def _sent_wire_tool(tool_name: str, dialect: ToolContractDialect) -> WireTool:
    tools = _WIRE_TOOL_DEFS[dialect]
    if tool_name not in tools:
        # A caller bug, not a model error: callers decode only the names in
        # the list sent on that call. The name is not echoed, because a
        # buggy caller could pass a model-authored one.
        raise WireProjectionError(f"no {dialect} wire projection for the requested tool name")
    return tools[tool_name]


def _strip_null(arguments: dict[str, Any], path: tuple[str, ...]) -> None:
    """Remove the key at ``path`` when it is present and ``null``; touch nothing else."""
    node: Any = arguments
    for key in path[:-1]:
        if type(node) is not dict or key not in node:
            return
        node = node[key]
    leaf = path[-1]
    if type(node) is dict and leaf in node and node[leaf] is None:
        del node[leaf]


@trust_boundary(
    tier=3,
    source="the JSON-decoded tool-call arguments a composer provider sent under one dialect's wire schema W",
    source_param="raw",
    suppresses=("R1", "R5"),
    invariant=(
        "raises ToolArgumentError (category wire_envelope) before use when set_pipeline arguments are not exactly "
        "one 'pipeline' object field; never rejects on W, which is classified as wire_conformant only; removes a "
        "null only at a promoted position on openai_strict and never inserts, coerces or recurses into an "
        "undeclared key"
    ),
    test_ref="tests/unit/web/composer/test_wire_decode.py::test_decode_rejects_a_malformed_set_pipeline_envelope",
    test_fingerprint="47b22a3ef9296ebe6e0f2070c36202350754f4a371ce89d1faab81c2c9913c1b",
)
def decode_wire_arguments(tool_name: str, dialect: ToolContractDialect, raw: dict[str, Any]) -> DecodedArguments:
    """Decode provider arguments sent under ``dialect`` into S's semantic form.

    1. ``wire_conformant``: whether ``raw`` validates against the W that was
       sent. Classification only.
    2. ``EnvelopeUnwrap``: set_pipeline arguments must be exactly
       ``{"pipeline": <object>}``, or :class:`ToolArgumentError` (category
       ``wire_envelope``) is raised. This is decode's only rejection.
    3. ``StripNull`` (``openai_strict`` only): a ``null`` at a promoted
       position becomes an omitted key.

    Callers decode only a tool name that was in the list sent on that call;
    any other name raises :class:`WireProjectionError` (a caller bug).
    """
    tool = _sent_wire_tool(tool_name, dialect)
    if type(raw) is not dict:
        raise WireProjectionError("decode takes the JSON object the caller already decoded")
    wire_conformant = next(iter(_WIRE_VALIDATORS[dialect][tool_name].iter_errors(raw)), None) is None
    semantic: dict[str, Any] = deepcopy(raw)
    for node in tool.decode_plan:
        if type(node) is EnvelopeUnwrap:
            if set(semantic) != {node.key} or type(semantic[node.key]) is not dict:
                raise ToolArgumentError(
                    argument=f"{tool_name} arguments",
                    expected="an object conforming to the declared argument schema",
                    actual_type="invalid_schema",
                    category=ToolArgumentErrorCategory.WIRE_ENVELOPE,
                )
            semantic = semantic[node.key]
    if dialect == ToolContractDialect.OPENAI_STRICT:
        for node in tool.decode_plan:
            if type(node) is StripNull:
                _strip_null(semantic, node.path)
    return DecodedArguments(semantic=semantic, wire_conformant=wire_conformant)


def encode_semantic_arguments(tool_name: str, dialect: ToolContractDialect, semantic: Mapping[str, Any]) -> dict[str, Any]:
    """Return the wire form of semantic arguments: the inverse of the envelope only.

    set_pipeline becomes ``{"pipeline": semantic}``; every other tool is
    returned unchanged (as a fresh plain-JSON copy) on both dialects.
    """
    tool = _sent_wire_tool(tool_name, dialect)
    arguments = cast(dict[str, Any], deep_thaw(semantic))
    for node in tool.decode_plan:
        if type(node) is EnvelopeUnwrap:
            return {node.key: arguments}
    return arguments
