"""JSON-schema disclosure of the runtime naming rules on advertised tool fields.

A leaf module: it imports only ``core.config`` and the standard library, never
the tool registry. ``schema_contract`` (which imports the registry) and the
tool declaration modules the registry imports (``transforms``) both apply these
helpers, so they must live below both; importing them from ``schema_contract``
into a declaration module is an import cycle
(schema_contract -> _dispatch -> _registry -> transforms).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from typing import Any, cast

from elspeth.core.config import (
    _MAX_CONNECTION_NAME_LENGTH,
    _MAX_NODE_NAME_LENGTH,
    _RESERVED_EDGE_LABELS,
    _VALID_CONNECTION_NAME_RE,
    _VALID_NODE_NAME_RE,
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
