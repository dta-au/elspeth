"""Public accessors for composer tool schema contracts.

The registered tool declarations are the wire authority.  Consumers must
select from :func:`get_tool_definitions` rather than reaching into the
dispatch registry's private lookup tables.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from copy import deepcopy
from typing import Any, cast

from elspeth.core.config import (
    _MAX_CONNECTION_NAME_LENGTH,
    _MAX_NODE_NAME_LENGTH,
    _RESERVED_EDGE_LABELS,
    _VALID_CONNECTION_NAME_RE,
    _VALID_NODE_NAME_RE,
)
from elspeth.web.composer.tools._dispatch import get_tool_definitions


def _registered_set_pipeline_schema() -> Mapping[str, Any]:
    for definition in get_tool_definitions():
        if definition["name"] == "set_pipeline":
            parameters = definition["parameters"]
            if type(parameters) is not dict:  # pragma: no cover - registry integrity guard
                raise RuntimeError("registered set_pipeline parameters must be a JSON-schema object")
            return parameters
    raise RuntimeError("registered set_pipeline tool definition is missing")


def _schema_mapping(value: object, *, path: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise RuntimeError(f"{path}: schema node must be an object")
    return cast(Mapping[str, Any], value)


def _schema_sequence(value: object, *, path: str) -> Sequence[object]:
    if type(value) not in (list, tuple):
        raise RuntimeError(f"{path}: schema union/required metadata must be an array")
    return cast(Sequence[object], value)


def _resolve_ref(schema: Mapping[str, Any], definitions: Mapping[str, Any], *, path: str) -> Mapping[str, Any]:
    current = schema
    seen: set[str] = set()
    while "$ref" in current:
        ref = current["$ref"]
        if type(ref) is not str or not ref.startswith("#/$defs/"):
            raise RuntimeError(f"{path}: unsupported model schema reference")
        if ref in seen:
            raise RuntimeError(f"{path}: cyclic model schema reference")
        seen.add(ref)
        name = ref.removeprefix("#/$defs/")
        if name not in definitions:
            raise RuntimeError(f"{path}: unresolved model schema reference {name!r}")
        target = _schema_mapping(definitions[name], path=f"$defs.{name}")
        siblings = {key: value for key, value in current.items() if key != "$ref"}
        current = {**target, **siblings}
    return current


def _schema_branches(
    schema: Mapping[str, Any],
    definitions: Mapping[str, Any],
    *,
    path: str,
) -> tuple[Mapping[str, Any], ...]:
    resolved = _resolve_ref(schema, definitions, path=path)
    union_keywords = tuple(keyword for keyword in ("anyOf", "oneOf") if keyword in resolved)
    if len(union_keywords) > 1:
        raise RuntimeError(f"{path}: schema node cannot combine anyOf and oneOf")
    if union_keywords:
        keyword = union_keywords[0]
        raw_branches = _schema_sequence(resolved[keyword], path=f"{path}.{keyword}")
        branches: list[Mapping[str, Any]] = []
        for index, raw_branch in enumerate(raw_branches):
            branches.extend(
                _schema_branches(
                    _schema_mapping(raw_branch, path=f"{path}.{keyword}[{index}]"),
                    definitions,
                    path=path,
                )
            )
        return tuple(branches)

    if "type" not in resolved:
        return (resolved,)

    raw_type = resolved["type"]
    if type(raw_type) is list:
        if not raw_type or any(type(item) is not str for item in raw_type):
            raise RuntimeError(f"{path}: schema type union must contain exact strings")
        return tuple({**resolved, "type": item} for item in raw_type)
    if type(raw_type) is not str:
        raise RuntimeError(f"{path}: schema type must be an exact string or list")
    return (resolved,)


def _required_fields(schema: Mapping[str, Any], *, path: str) -> frozenset[str]:
    raw_required: object = ()
    if "required" in schema:
        raw_required = schema["required"]
    required = _schema_sequence(raw_required, path=f"{path}.required")
    if any(type(item) is not str for item in required):
        raise RuntimeError(f"{path}: required entries must be exact strings")
    return frozenset(cast(Sequence[str], required))


def _branch_type(schema: Mapping[str, Any], *, path: str) -> str | None:
    if "type" not in schema:
        return None
    raw_type = schema["type"]
    if type(raw_type) is not str:
        raise RuntimeError(f"{path}: normalized schema branch must have one exact type")
    return raw_type


def _types_compatible(runtime_type: str | None, advertised_type: str | None) -> bool:
    if advertised_type is None:
        return True
    if runtime_type == advertised_type:
        return True
    return runtime_type == "integer" and advertised_type == "number"


def _additional_properties(schema: Mapping[str, Any], *, path: str) -> bool | Mapping[str, Any]:
    if "additionalProperties" not in schema:
        return True
    value = schema["additionalProperties"]
    if type(value) is bool:
        return value
    return _schema_mapping(value, path=f"{path}.additionalProperties")


def _branch_compatibility_failure(
    runtime: Mapping[str, Any],
    advertised: Mapping[str, Any],
    runtime_definitions: Mapping[str, Any],
    advertised_definitions: Mapping[str, Any],
    *,
    path: str,
) -> str | None:
    runtime_type = _branch_type(runtime, path=path)
    advertised_type = _branch_type(advertised, path=path)
    if not _types_compatible(runtime_type, advertised_type):
        return f"{path}: runtime type {runtime_type!r} is not advertised as {advertised_type!r}"

    runtime_values: frozenset[object] | None = None
    if "enum" in runtime:
        runtime_values = frozenset(_schema_sequence(runtime["enum"], path=f"{path}.enum"))
    advertised_values: frozenset[object] | None = None
    if "enum" in advertised:
        advertised_values = frozenset(_schema_sequence(advertised["enum"], path=f"{path}.enum"))
    if advertised_values is not None:
        if runtime_values is None:
            return f"{path}: advertised enum is narrower than the runtime model"
        if not runtime_values <= advertised_values:
            return f"{path}: runtime enum values are missing from the advertised schema"

    if runtime_type == "array":
        runtime_items: Mapping[str, Any] | None = None
        if "items" in runtime:
            runtime_items = _schema_mapping(runtime["items"], path=f"{path}[]")
        advertised_items: Mapping[str, Any] | None = None
        if "items" in advertised:
            advertised_items = _schema_mapping(advertised["items"], path=f"{path}[]")
        if runtime_items is not None and advertised_items is None:
            return None
        if runtime_items is None and advertised_items is not None:
            return f"{path}[]: advertised item schema is narrower than the runtime model"
        if runtime_items is not None and advertised_items is not None:
            return _directional_compatibility_failure(
                runtime_items,
                advertised_items,
                runtime_definitions,
                advertised_definitions,
                path=f"{path}[]",
            )

    if runtime_type != "object":
        return None

    runtime_required = _required_fields(runtime, path=path)
    advertised_required = _required_fields(advertised, path=path)
    extra_advertised_required = advertised_required - runtime_required
    if extra_advertised_required:
        return f"{path}: advertised required fields are optional in the runtime model: {sorted(extra_advertised_required)!r}"

    runtime_properties: Mapping[str, Any] = {}
    if "properties" in runtime:
        runtime_properties = _schema_mapping(runtime["properties"], path=f"{path}.properties")
    advertised_properties: Mapping[str, Any] = {}
    if "properties" in advertised:
        advertised_properties = _schema_mapping(advertised["properties"], path=f"{path}.properties")
    for name, runtime_property in runtime_properties.items():
        property_path = f"{path}.{name}"
        if name not in advertised_properties:
            return f"{property_path}: typed runtime branch is not explicitly advertised"
        failure = _directional_compatibility_failure(
            _schema_mapping(runtime_property, path=property_path),
            _schema_mapping(advertised_properties[name], path=property_path),
            runtime_definitions,
            advertised_definitions,
            path=property_path,
        )
        if failure is not None:
            return failure

    runtime_additional = _additional_properties(runtime, path=path)
    advertised_additional = _additional_properties(advertised, path=path)
    if runtime_additional is False:
        return None
    if advertised_additional is False:
        return f"{path}.*: advertised schema rejects properties accepted by the runtime model"
    if runtime_additional is True:
        if advertised_additional is not True:
            return f"{path}.*: advertised value schema is narrower than the runtime model"
        return None
    if advertised_additional is True:
        return None
    return _directional_compatibility_failure(
        runtime_additional,
        advertised_additional,
        runtime_definitions,
        advertised_definitions,
        path=f"{path}.*",
    )


def _directional_compatibility_failure(
    runtime_schema: Mapping[str, Any],
    advertised_schema: Mapping[str, Any],
    runtime_definitions: Mapping[str, Any],
    advertised_definitions: Mapping[str, Any],
    *,
    path: str,
) -> str | None:
    runtime_branches = _schema_branches(runtime_schema, runtime_definitions, path=path)
    advertised_branches = _schema_branches(advertised_schema, advertised_definitions, path=path)
    for runtime_branch in runtime_branches:
        failures: list[str] = []
        for advertised_branch in advertised_branches:
            failure = _branch_compatibility_failure(
                runtime_branch,
                advertised_branch,
                runtime_definitions,
                advertised_definitions,
                path=path,
            )
            if failure is None:
                break
            failures.append(failure)
        else:
            runtime_type = _branch_type(runtime_branch, path=path)
            detail = failures[0] if failures else "no advertised branches"
            return f"{path}: runtime type {runtime_type!r} is not compatibly advertised ({detail})"
    return None


def _assert_directional_compatibility(
    runtime_schema: Mapping[str, Any],
    advertised_schema: Mapping[str, Any],
    runtime_definitions: Mapping[str, Any],
    advertised_definitions: Mapping[str, Any],
    *,
    path: str,
) -> None:
    failure = _directional_compatibility_failure(
        runtime_schema,
        advertised_schema,
        runtime_definitions,
        advertised_definitions,
        path=path,
    )
    if failure is not None:
        raise RuntimeError(failure)


def assert_set_pipeline_schema_compatible(*, advertised_schema: Mapping[str, Any] | None = None) -> None:
    """Fail if a runtime-valid typed branch is absent from the tool schema.

    The direction is deliberate: the advertised schema may be looser about
    unknown properties, while Pydantic remains the stricter runtime boundary.
    It must never be narrower in requiredness, nullability, enum membership,
    or any typed source/node/edge/output branch.
    """
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel

    runtime_schema = SetPipelineArgumentsModel.model_json_schema()
    runtime_definitions: Mapping[str, Any] = {}
    if "$defs" in runtime_schema:
        runtime_definitions = _schema_mapping(runtime_schema["$defs"], path="$defs")
    advertised = _registered_set_pipeline_schema() if advertised_schema is None else advertised_schema
    advertised_definitions: Mapping[str, Any] = {}
    if "$defs" in advertised:
        advertised_definitions = _schema_mapping(advertised["$defs"], path="$defs")
    _assert_directional_compatibility(
        runtime_schema,
        advertised,
        runtime_definitions,
        advertised_definitions,
        path="$",
    )


# core/config.py enforces these at settings_load, well after a composer
# proposal has already been accepted; nothing on the advertised terminal
# schema told the planner beforehand, so a violation costs a whole repair
# round-trip (elspeth-2e9df07c69). Every constraint added below restates a
# rule the runtime already enforces on the same field — disclosure, not a
# new or relaxed acceptance path (Stage 1 keeps enforcing everything it
# enforced before; see `_composer_node_id_validation_message` /
# `_routing_label_errors` in web/composer/state.py, which mirror the same
# core/config.py functions at proposal time).
_RESERVED_EDGE_LABEL_EXCLUSION: Mapping[str, Any] = {"enum": sorted(_RESERVED_EDGE_LABELS)}

# The node pattern's first character class (a letter) already makes a "__"
# prefix unreachable, so `validate_runtime_node_name`'s separate
# `startswith("__")` check (core/config.py:229) needs no extra "not" arm
# here. The connection charset's first character class admits "_", so
# `_validate_connection_or_sink_name`'s identical check (core/config.py:253)
# — and `validate_sink_name`'s (core/config.py:267) — DOES need a dedicated
# exclusion; a bare enum exclusion would silently admit "__anything".
#
# The "^__" arm carries an explicit "type": "string": JSON Schema's
# "pattern" keyword is a no-op (vacuously satisfied) against a non-string
# instance, so a bare {"pattern": "^__"} arm would itself match — and
# therefore its enclosing "not" would REJECT — every non-string instance,
# null included. Three of the fields this clause applies to are declared
# nullable (["string", "null"]) with `SetPipelineArgumentsModel` accepting
# null on each (the rest are plain strings or object keys, where the guard
# is a no-op), so an unguarded arm here would advertise a phantom rejection
# of legal omitted-as-null values (caught by review; see
# `test_canonical_schema_accepts_null_on_nullable_disclosed_fields`).
_CONNECTION_NAME_NOT_CLAUSE: Mapping[str, Any] = {"anyOf": [dict(_RESERVED_EDGE_LABEL_EXCLUSION), {"type": "string", "pattern": "^__"}]}

# validate_sink_name (core/config.py:258-269) requires the connection
# charset (core/config.py:192) PLUS `value == value.lower()`. JSON Schema has
# no case-insensitivity keyword; restricting the character class to
# lowercase is the exact regex equivalent, since a string containing no
# uppercase letter trivially equals its own lowercased form.
_SINK_NAME_PATTERN = r"^[a-z0-9_][a-z0-9_-]*$"

# validate_sources_not_empty_and_named's FIRST check (core/config.py:2114-2117,
# mirrored at state.py:454) is lowercase enforcement — source names are
# node-shaped (charset/length/reserved-label, core/config.py:2118-2124) PLUS
# lowercase, unlike a plain node name. Restricting the node charset's first
# character class to lowercase letters is the case-insensitivity equivalent,
# same reasoning as `_SINK_NAME_PATTERN` above.
_SOURCE_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"

# Gate route DESTINATIONS (core/config.py:813-830, mirrored at
# state.py:288-299): 'fork'/'discard' are literal escapes admitted before
# any naming check runs, so they never reach the reserved-label exclusion —
# only 'continue' (explicitly rejected with its own message) and
# 'on_success' (rejected by the same `_RESERVED_EDGE_LABELS` membership test
# every other connection-rule field uses) are actually excluded here.
# 'fork' and 'discard' both already satisfy the plain connection
# charset/length, so admitting them needs no separate branch — only a
# narrower "not" than `_CONNECTION_NAME_NOT_CLAUSE`.
_ROUTE_DESTINATION_NOT_CLAUSE: Mapping[str, Any] = {"anyOf": [{"enum": ["continue", "on_success"]}, {"type": "string", "pattern": "^__"}]}


def _disclose_node_name_constraints(schema: MutableMapping[str, Any]) -> None:
    """Advertise the runtime processing-node identifier rule (core/config.py:187-231)."""
    schema["pattern"] = _VALID_NODE_NAME_RE.pattern
    schema["maxLength"] = _MAX_NODE_NAME_LENGTH
    schema["not"] = dict(_RESERVED_EDGE_LABEL_EXCLUSION)


def _disclose_connection_name_constraints(schema: MutableMapping[str, Any]) -> None:
    """Advertise the runtime connection/route-label rule (core/config.py:189-255).

    Applies unchanged to fields whose only other admitted value is a literal
    escape hatch ('discard'): 'discard' itself satisfies the connection
    charset, length cap, and reserved-label exclusion, so disclosing this
    rule never advertises the escape value as invalid.
    """
    schema["pattern"] = _VALID_CONNECTION_NAME_RE.pattern
    schema["maxLength"] = _MAX_CONNECTION_NAME_LENGTH
    schema["not"] = deepcopy(cast(dict[str, Any], _CONNECTION_NAME_NOT_CLAUSE))


def _disclose_sink_name_constraints(schema: MutableMapping[str, Any]) -> None:
    """Advertise the runtime sink-name rule (core/config.py:258-269).

    Used for every field the runtime documents as sink-only — `nodes[].id`
    is NOT one of these (a processing node, never a sink), but
    `nodes[].on_error` and `outputs[].on_write_failure` are: both are
    documented as "'discard' or a declared sink name" (tools/sessions.py),
    and a declared sink's own key is always sink-shaped
    (`ElspethSettings.validate_sink_names_lowercase` -> `validate_sink_name`),
    so no legally-resolvable value for either field can ever be wider than
    this rule even where a field's own syntax-only validator (transform/gate
    `on_error`) independently permits more.
    """
    schema["pattern"] = _SINK_NAME_PATTERN
    schema["maxLength"] = _MAX_NODE_NAME_LENGTH
    schema["not"] = deepcopy(cast(dict[str, Any], _CONNECTION_NAME_NOT_CLAUSE))


def _disclose_property_name_source_rule(schema: MutableMapping[str, Any]) -> None:
    """Advertise the source-name rule (core/config.py:2110-2125) against an object's KEYS."""
    schema["propertyNames"] = {
        "pattern": _SOURCE_NAME_PATTERN,
        "maxLength": _MAX_NODE_NAME_LENGTH,
        "not": dict(_RESERVED_EDGE_LABEL_EXCLUSION),
    }


def _disclose_property_name_connection_rule(schema: MutableMapping[str, Any]) -> None:
    """Advertise the connection-name rule against an object's KEYS via propertyNames."""
    schema["propertyNames"] = {
        "pattern": _VALID_CONNECTION_NAME_RE.pattern,
        "maxLength": _MAX_CONNECTION_NAME_LENGTH,
        "not": deepcopy(cast(dict[str, Any], _CONNECTION_NAME_NOT_CLAUSE)),
    }


def _disclose_route_destination_constraints(schema: MutableMapping[str, Any]) -> None:
    """Advertise the gate route-destination rule (core/config.py:813-830) on VALUES.

    Deliberately not `_disclose_connection_name_constraints`: 'fork' is one
    of `_RESERVED_EDGE_LABELS` but a legitimate destination here, so this
    uses the narrower `_ROUTE_DESTINATION_NOT_CLAUSE`, which excludes only
    'continue' and 'on_success'.
    """
    schema["type"] = "string"
    schema["pattern"] = _VALID_CONNECTION_NAME_RE.pattern
    schema["maxLength"] = _MAX_CONNECTION_NAME_LENGTH
    schema["not"] = deepcopy(cast(dict[str, Any], _ROUTE_DESTINATION_NOT_CLAUSE))


def _disclose_runtime_naming_constraints(schema: MutableMapping[str, Any]) -> None:
    """Mutate an isolated set_pipeline schema copy to disclose runtime naming rules.

    Gate `routes` destination VALUES get the narrower
    `_disclose_route_destination_constraints` rule, not the plain connection
    rule: 'fork' is one of the reserved edge labels but a legitimate
    destination here (`_FORK_ROUTE_TARGET` in state.py; `discard` likewise),
    so excluding every reserved label would advertise a valid destination as
    forbidden. Route LABELS (the mapping's keys) carry no such escape and
    get the plain connection-name rule via `propertyNames`. Queue nodes'
    additional lowercase-only requirement (state.py
    `_LOWERCASE_ONLY_NODE_TYPES`) is conditional on `node_type` and not
    encoded here — a flat per-item schema cannot express a value-dependent
    rule without `if`/`then` composition, which this seam does not add.
    """
    properties = schema["properties"]

    source_properties = properties["source"]["properties"]
    _disclose_connection_name_constraints(source_properties["on_success"])

    named_source = properties["sources"]["additionalProperties"]
    _disclose_connection_name_constraints(named_source["properties"]["on_success"])
    _disclose_property_name_source_rule(properties["sources"])

    node_properties = properties["nodes"]["items"]["properties"]
    _disclose_node_name_constraints(node_properties["id"])
    _disclose_connection_name_constraints(node_properties["input"])
    _disclose_connection_name_constraints(node_properties["on_success"])
    _disclose_sink_name_constraints(node_properties["on_error"])
    _disclose_connection_name_constraints(node_properties["fork_to"]["items"])
    _disclose_connection_name_constraints(node_properties["branches"]["items"])
    _disclose_connection_name_constraints(node_properties["branches"]["additionalProperties"])
    _disclose_property_name_connection_rule(node_properties["branches"])
    _disclose_property_name_connection_rule(node_properties["routes"])
    node_properties["routes"]["additionalProperties"] = {}
    _disclose_route_destination_constraints(node_properties["routes"]["additionalProperties"])

    output_properties = properties["outputs"]["items"]["properties"]
    _disclose_sink_name_constraints(output_properties["sink_name"])
    _disclose_sink_name_constraints(output_properties["on_write_failure"])


def canonical_set_pipeline_schema() -> dict[str, Any]:
    """Return an isolated, runtime-compatible registered schema copy.

    The copy also discloses the runtime node/connection/sink naming rules
    (charset, length, reserved edge labels, sink lowercasing) that
    core/config.py enforces at settings_load — see
    `_disclose_runtime_naming_constraints`.
    """
    registered = _registered_set_pipeline_schema()
    augmented: dict[str, Any] = deepcopy(cast(dict[str, Any], registered))
    _disclose_runtime_naming_constraints(augmented)
    assert_set_pipeline_schema_compatible(advertised_schema=augmented)
    return augmented


__all__ = ["assert_set_pipeline_schema_compatible", "canonical_set_pipeline_schema"]
