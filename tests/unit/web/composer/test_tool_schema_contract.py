"""Directional compatibility tests for the planner's canonical schema."""

from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema


def _registered_set_pipeline_schema() -> dict[str, Any]:
    return next(definition["parameters"] for definition in get_tool_definitions() if definition["name"] == "set_pipeline")


_BASE_PIPELINE: dict[str, Any] = {
    "source": {"plugin": "csv", "on_success": "rows"},
    "nodes": [],
    "edges": [],
    "outputs": [],
}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("metadata",), None),
        (("source",), None),
        (("sources",), None),
        (("source", "blob_id"), None),
        (("source", "on_validation_failure"), None),
        (("source", "inline_blob"), None),
        (("nodes",), [{"id": "gate", "node_type": "gate", "input": "rows", "plugin": None}]),
        (
            ("nodes",),
            [
                {
                    "id": "aggregate",
                    "node_type": "aggregation",
                    "input": "rows",
                    "plugin": "batch_stats",
                    "on_success": None,
                    "on_error": None,
                    "condition": None,
                    "routes": None,
                    "fork_to": None,
                    "branches": None,
                    "policy": None,
                    "merge": None,
                    "trigger": {"count": None, "timeout_seconds": None, "condition": None},
                    "output_mode": None,
                    "expected_output_count": None,
                }
            ],
        ),
        (
            ("nodes",),
            [
                {
                    "id": "variant_union",
                    "node_type": "row_union",
                    "input": "control_done",
                    "plugin": None,
                    "on_success": "unioned_rows",
                    "branches": {
                        "control": "control_done",
                        "treatment": "treatment_done",
                    },
                    "timeout_seconds": 30.0,
                }
            ],
        ),
        (
            ("edges",),
            [{"id": "edge", "from_node": "source", "to_node": "sink", "edge_type": "on_success", "label": None}],
        ),
        (("outputs",), [{"sink_name": "rows", "plugin": "json", "on_write_failure": None}]),
        (("metadata",), {"name": None, "description": None}),
    ],
)
def test_registered_schema_advertises_explicit_null_for_omission_equivalent_fields(
    path: tuple[str, ...],
    value: object,
) -> None:
    payload = deepcopy(_BASE_PIPELINE)
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    SetPipelineArgumentsModel.model_validate(payload)

    errors = list(Draft202012Validator(_registered_set_pipeline_schema()).iter_errors(payload))
    assert errors == []


def test_inline_blob_mime_types_share_the_blob_contract_closed_set() -> None:
    schema = _registered_set_pipeline_schema()
    advertised = schema["properties"]["source"]["properties"]["inline_blob"]["properties"]["mime_type"]["enum"]
    assert set(advertised) == ALLOWED_MIME_TYPES

    for mime_type in sorted(ALLOWED_MIME_TYPES):
        payload = deepcopy(_BASE_PIPELINE)
        payload["source"]["inline_blob"] = {
            "filename": "input.txt",
            "mime_type": mime_type,
            "content": "one\n",
        }
        SetPipelineArgumentsModel.model_validate(payload)


def test_provider_schemas_explicitly_describe_row_union_routing_fields() -> None:
    definitions = get_tool_definitions()
    upsert = next(definition["parameters"] for definition in definitions if definition["name"] == "upsert_node")
    set_pipeline = _registered_set_pipeline_schema()
    pipeline_node = set_pipeline["properties"]["nodes"]["items"]

    for properties in (upsert["properties"], pipeline_node["properties"]):
        on_success_description = properties["on_success"]["description"]
        branches_description = properties["branches"]["description"]
        assert "row_union" in on_success_description
        assert "processing connection" in on_success_description
        assert "row_union" in branches_description
        assert "consum" in branches_description

    unsupported = deepcopy(_BASE_PIPELINE)
    unsupported["source"]["inline_blob"] = {
        "filename": "input.xml",
        "mime_type": "application/xml",
        "content": "<one />",
    }
    with pytest.raises(ValueError):
        SetPipelineArgumentsModel.model_validate(unsupported)


def test_provider_schemas_advertise_gate_error_policy_as_node_level_authoring() -> None:
    definitions = get_tool_definitions()
    upsert = next(definition for definition in definitions if definition["name"] == "upsert_node")
    upsert_edge = next(definition for definition in definitions if definition["name"] == "upsert_edge")
    set_pipeline = _registered_set_pipeline_schema()
    pipeline_node = set_pipeline["properties"]["nodes"]["items"]

    for properties in (upsert["parameters"]["properties"], pipeline_node["properties"]):
        description = properties["on_error"]["description"]
        lowered = description.lower()
        assert properties["on_error"]["minLength"] == 1
        assert "gate" in lowered
        assert "node-level" in lowered
        assert "discard" in lowered
        assert "sink" in lowered
        assert "fail-fast" in lowered

    edge_description = upsert_edge["description"]
    assert "transform/aggregation" in edge_description
    assert "gate" in edge_description
    assert "upsert_node" in edge_description


def test_set_pipeline_schema_and_model_reject_empty_gate_on_error() -> None:
    payload = deepcopy(_BASE_PIPELINE)
    payload["nodes"] = [
        {
            "id": "threshold",
            "node_type": "gate",
            "input": "rows",
            "on_error": "",
            "condition": "row['amount'] > 500",
            "routes": {"true": "main", "false": "main"},
        }
    ]

    with pytest.raises(ValueError):
        SetPipelineArgumentsModel.model_validate(payload)

    errors = list(Draft202012Validator(_registered_set_pipeline_schema()).iter_errors(payload))
    assert len(errors) == 1
    assert list(errors[0].absolute_path) == ["nodes", 0, "on_error"]


def test_canonical_schema_accessor_returns_isolated_registered_copies() -> None:
    spec = importlib.util.find_spec("elspeth.web.composer.tools.schema_contract")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    first = module.canonical_set_pipeline_schema()
    second = module.canonical_set_pipeline_schema()
    registered = _registered_set_pipeline_schema()
    # The canonical accessor discloses the runtime naming rules on top of the
    # registered schema (elspeth-2e9df07c69), so it is no longer byte-equal to
    # the raw registration — but repeated calls must agree with each other,
    # and every registered field the disclosure pass does not touch (nothing
    # under "edges", for instance) must survive unchanged.
    assert first == second
    assert first != registered
    assert first["properties"]["edges"] == registered["properties"]["edges"]
    node_id_schema = first["properties"]["nodes"]["items"]["properties"]["id"]
    assert node_id_schema["pattern"] == r"^[a-zA-Z][a-zA-Z0-9_-]*$"
    assert node_id_schema["maxLength"] == 38
    assert node_id_schema["not"] == {"enum": ["continue", "fork", "on_success"]}
    assert "pattern" not in registered["properties"]["nodes"]["items"]["properties"]["id"]

    first["required"].append("attacker_added")
    first["properties"]["source"]["properties"]["plugin"]["type"] = "integer"

    third = module.canonical_set_pipeline_schema()
    assert third == second
    assert "attacker_added" not in third["required"]
    assert third["properties"]["source"]["properties"]["plugin"]["type"] == "string"


def _schema_contract_module() -> Any:
    return importlib.import_module("elspeth.web.composer.tools.schema_contract")


def _node_at(schema: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = schema
    for segment in path:
        node = node[segment]
    assert isinstance(node, dict)
    return node


def test_registered_schema_is_directionally_compatible_with_full_runtime_model() -> None:
    module = _schema_contract_module()
    module.assert_set_pipeline_schema_compatible()


def test_directional_guard_resolves_advertised_schema_references() -> None:
    module = _schema_contract_module()
    advertised = deepcopy(_registered_set_pipeline_schema())
    advertised["$defs"] = {"AdvertisedNode": advertised["properties"]["nodes"]["items"]}
    advertised["properties"]["nodes"]["items"] = {"$ref": "#/$defs/AdvertisedNode"}

    module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)


def test_directional_guard_does_not_replace_an_explicit_empty_schema() -> None:
    module = _schema_contract_module()

    with pytest.raises(RuntimeError, match="typed runtime branch is not explicitly advertised"):
        module.assert_set_pipeline_schema_compatible(advertised_schema={})


@pytest.mark.parametrize(
    ("path", "keyword"),
    (
        ((), "type"),
        (("properties", "nodes"), "items"),
        (("properties", "edges", "items", "properties", "edge_type"), "enum"),
        ((), "additionalProperties"),
    ),
)
def test_directional_guard_distinguishes_absent_optional_keyword_from_present_null(
    path: tuple[str, ...],
    keyword: str,
) -> None:
    module = _schema_contract_module()
    advertised = deepcopy(_registered_set_pipeline_schema())
    node = _node_at(advertised, path)
    del node[keyword]
    module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)

    node[keyword] = None
    with pytest.raises(RuntimeError):
        module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)


def test_directional_guard_does_not_skip_a_malformed_advertised_union_branch() -> None:
    module = _schema_contract_module()
    valid = deepcopy(_registered_set_pipeline_schema())
    malformed = deepcopy(valid)
    malformed["properties"] = None

    with pytest.raises(RuntimeError, match="properties"):
        module.assert_set_pipeline_schema_compatible(advertised_schema={"anyOf": [malformed, valid]})


def test_registered_schema_lookup_rejects_missing_internal_definition_name(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _schema_contract_module()
    monkeypatch.setattr(module, "get_tool_definitions", lambda: ({},))

    with pytest.raises(KeyError, match="name"):
        module.canonical_set_pipeline_schema()


@pytest.mark.parametrize(
    ("path", "non_null_type"),
    [
        (("properties", "source", "properties", "inline_blob", "properties", "description"), "string"),
        (("properties", "sources", "additionalProperties", "properties", "on_validation_failure"), "string"),
        (("properties", "nodes", "items", "properties", "trigger", "properties", "timeout_seconds"), "number"),
        (("properties", "nodes", "items", "properties", "timeout_seconds"), "number"),
        (("properties", "edges", "items", "properties", "label"), "string"),
        (("properties", "outputs", "items", "properties", "on_write_failure"), "string"),
    ],
)
def test_directional_guard_rejects_advertised_nullability_narrowing(
    path: tuple[str, ...],
    non_null_type: str,
) -> None:
    module = _schema_contract_module()
    advertised = deepcopy(_registered_set_pipeline_schema())
    _node_at(advertised, path)["type"] = non_null_type

    with pytest.raises(RuntimeError, match="null"):
        module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)


def test_directional_guard_rejects_advertised_requiredness_narrowing() -> None:
    module = _schema_contract_module()
    advertised = deepcopy(_registered_set_pipeline_schema())
    advertised["required"].append("source")

    with pytest.raises(RuntimeError, match="required"):
        module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)


def test_directional_guard_requires_every_typed_branch_to_be_advertised() -> None:
    module = _schema_contract_module()
    advertised = deepcopy(_registered_set_pipeline_schema())
    del advertised["properties"]["nodes"]["items"]["properties"]["trigger"]

    with pytest.raises(RuntimeError, match=r"nodes\[\]\.trigger"):
        module.assert_set_pipeline_schema_compatible(advertised_schema=advertised)


# --------------------------------------------------------------------------- #
# Runtime naming-rule disclosure (elspeth-2e9df07c69): the advertised schema  #
# must carry the same node/connection/sink naming rules core/config.py       #
# enforces at settings_load, at the exact JSON paths that carry each role.   #
# --------------------------------------------------------------------------- #

_NODE_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_-]*$"
_CONNECTION_NAME_PATTERN = r"^[a-zA-Z0-9_][a-zA-Z0-9_-]*$"
_SINK_NAME_PATTERN = r"^[a-z0-9_][a-z0-9_-]*$"
_SOURCE_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
_RESERVED_LABELS = {"continue", "fork", "on_success"}


# Node-rule fields need no "__" arm: the pattern's leading-letter class
# already makes a "__" prefix unreachable (see schema_contract.py). Same for
# the source-name rule (also letter-first, also lowercase-only).
_NODE_RULE_NOT = {"enum": sorted(_RESERVED_LABELS)}
# Connection/sink-rule fields admit a leading "_", so the disclosed "not"
# clause carries a dedicated "^__" exclusion alongside the reserved-label
# one. The "^__" arm is itself type-guarded to "string": "pattern" is a
# no-op against a non-string instance, so an unguarded arm would match (and
# "not" would therefore REJECT) every non-string value, null included —
# every field this rule applies to is nullable (elspeth-2e9df07c69 review
# round 1 Critical).
_CONNECTION_RULE_NOT = {"anyOf": [{"enum": sorted(_RESERVED_LABELS)}, {"type": "string", "pattern": "^__"}]}
# Gate route destinations admit the 'fork'/'discard' escapes (never reaching
# the reserved-label check at core/config.py:820-821), so only 'continue'
# (explicit rejection, core/config.py:822-823) and 'on_success' (still
# caught by the general reserved-label membership test) are excluded.
_ROUTE_DESTINATION_RULE_NOT = {"anyOf": [{"enum": ["continue", "on_success"]}, {"type": "string", "pattern": "^__"}]}


@pytest.mark.parametrize(
    ("path", "pattern", "max_length", "not_clause"),
    [
        (("properties", "nodes", "items", "properties", "id"), _NODE_NAME_PATTERN, 38, _NODE_RULE_NOT),
        (("properties", "source", "properties", "on_success"), _CONNECTION_NAME_PATTERN, 64, _CONNECTION_RULE_NOT),
        (
            ("properties", "sources", "additionalProperties", "properties", "on_success"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        (("properties", "nodes", "items", "properties", "input"), _CONNECTION_NAME_PATTERN, 64, _CONNECTION_RULE_NOT),
        (
            ("properties", "nodes", "items", "properties", "on_success"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        # on_error/on_write_failure are documented sink-only fields ("'discard'
        # or a declared sink name" — tools/sessions.py); a declared sink's own
        # key is always sink-shaped (validate_sink_names_lowercase ->
        # validate_sink_name), so these get the tighter sink rule even though
        # transform/gate on_error's own Pydantic validator independently
        # permits the wider connection shape.
        (("properties", "nodes", "items", "properties", "on_error"), _SINK_NAME_PATTERN, 38, _CONNECTION_RULE_NOT),
        (
            ("properties", "nodes", "items", "properties", "fork_to", "items"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        (
            ("properties", "nodes", "items", "properties", "branches", "items"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        (
            ("properties", "nodes", "items", "properties", "branches", "additionalProperties"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        (
            ("properties", "outputs", "items", "properties", "on_write_failure"),
            _SINK_NAME_PATTERN,
            38,
            _CONNECTION_RULE_NOT,
        ),
        (("properties", "outputs", "items", "properties", "sink_name"), _SINK_NAME_PATTERN, 38, _CONNECTION_RULE_NOT),
    ],
)
def test_canonical_schema_discloses_naming_rule_at_exact_path(
    path: tuple[str, ...],
    pattern: str,
    max_length: int,
    not_clause: dict[str, Any],
) -> None:
    schema = canonical_set_pipeline_schema()
    node = _node_at(schema, path)

    assert node["pattern"] == pattern
    assert node["maxLength"] == max_length
    assert node["not"] == not_clause
    # The registered (pre-disclosure) schema never carried these keywords —
    # proves the disclosure pass, not pre-existing authoring, put them there.
    registered_node = _node_at(_registered_set_pipeline_schema(), path)
    assert "pattern" not in registered_node
    assert "maxLength" not in registered_node
    assert "not" not in registered_node


@pytest.mark.parametrize(
    ("path", "pattern", "max_length", "not_clause"),
    [
        (
            (
                "properties",
                "sources",
            ),
            _SOURCE_NAME_PATTERN,
            38,
            _NODE_RULE_NOT,
        ),
        (
            ("properties", "nodes", "items", "properties", "branches"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
        (
            ("properties", "nodes", "items", "properties", "routes"),
            _CONNECTION_NAME_PATTERN,
            64,
            _CONNECTION_RULE_NOT,
        ),
    ],
)
def test_canonical_schema_discloses_naming_rule_on_object_keys(
    path: tuple[str, ...],
    pattern: str,
    max_length: int,
    not_clause: dict[str, Any],
) -> None:
    schema = canonical_set_pipeline_schema()
    node = _node_at(schema, path)
    property_names = node["propertyNames"]

    assert property_names["pattern"] == pattern
    assert property_names["maxLength"] == max_length
    assert property_names["not"] == not_clause


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("properties", "nodes", "items", "properties", "input"), "__internal"),
        (("properties", "nodes", "items", "properties", "on_success"), "__internal"),
        (("properties", "outputs", "items", "properties", "sink_name"), "__internal"),
        (("properties", "nodes", "items", "properties", "on_error"), "__internal"),
    ],
)
def test_canonical_schema_rejects_dunder_prefixed_connection_and_sink_names(
    path: tuple[str, ...],
    value: str,
) -> None:
    # The connection/sink charset's first-character class admits "_", unlike
    # the node charset — the dedicated "^__" "not" arm is what actually closes
    # this (core/config.py's `startswith("__")` check on every one of these
    # validators), not the reserved-label enum alone.
    schema = canonical_set_pipeline_schema()
    leaf = _node_at(schema, path)
    errors = list(Draft202012Validator(leaf).iter_errors(value))
    assert errors, f"expected {value!r} to be rejected at {'.'.join(path)}"


def test_canonical_schema_property_name_rules_reject_dunder_prefixed_keys() -> None:
    schema = canonical_set_pipeline_schema()
    payload_source_key = {
        "sources": {"__hidden": {"plugin": "csv", "on_success": "rows"}},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    errors = list(Draft202012Validator(schema).iter_errors(payload_source_key))
    assert errors, "expected a '__'-prefixed source name to be rejected"


def test_canonical_schema_property_name_rules_reject_uppercase_source_names() -> None:
    # validate_sources_not_empty_and_named's FIRST check (core/config.py:2114-2117)
    # is lowercase enforcement — a source name is node-shaped PLUS lowercase,
    # not a plain node name (review round 1 Important: the initial
    # implementation missed this and disclosed the wider node rule).
    schema = canonical_set_pipeline_schema()
    payload_mixed_case_source = {
        "sources": {"MySource": {"plugin": "csv", "on_success": "rows"}},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    errors = list(Draft202012Validator(schema).iter_errors(payload_mixed_case_source))
    assert errors, "expected a mixed-case source name to be rejected"

    payload_lowercase_source = {
        "sources": {"mysource": {"plugin": "csv", "on_success": "rows"}},
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    assert list(Draft202012Validator(schema).iter_errors(payload_lowercase_source)) == []


@pytest.mark.parametrize(
    "path",
    [
        ("properties", "nodes", "items", "properties", "on_success"),
        ("properties", "nodes", "items", "properties", "on_error"),
        ("properties", "outputs", "items", "properties", "on_write_failure"),
    ],
)
def test_canonical_schema_accepts_null_on_nullable_disclosed_fields(path: tuple[str, ...]) -> None:
    # Review round 1 Critical: a bare {"pattern": "^__"} "not" arm is a no-op
    # against a non-string instance (JSON Schema's "pattern" keyword only
    # constrains strings), so it VACUOUSLY MATCHES null — and the enclosing
    # "not" then REJECTS null, even though every one of these three fields is
    # declared `["string", "null"]` and `SetPipelineArgumentsModel` accepts
    # null on all of them. The fix adds an explicit "type": "string" guard to
    # that arm. This test fails red against the unguarded clause.
    schema = canonical_set_pipeline_schema()
    leaf = _node_at(schema, path)
    assert list(Draft202012Validator(leaf).iter_errors(None)) == []
    # The fix must not reopen the "__" gap it was guarding.
    assert list(Draft202012Validator(leaf).iter_errors("__internal"))


def test_canonical_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(canonical_set_pipeline_schema())


def test_canonical_schema_still_validates_a_representative_valid_pipeline() -> None:
    payload = {
        "source": {"plugin": "csv", "on_success": "rows"},
        "nodes": [
            {
                "id": "route_amount",
                "node_type": "gate",
                "input": "rows",
                "condition": "row['amount'] > 500",
                "routes": {"high": "flagged", "low": "main"},
                # Explicit null (not "discard") — review round 1 Important:
                # proves the disclosed rule round-trips null on a nullable
                # sink-rule field, not merely omission of the key.
                "on_error": None,
                "fork_to": None,
            },
            {
                "id": "enrich",
                "node_type": "transform",
                "plugin": "noop",
                "input": "main",
                "on_success": "enriched",
                "on_error": "discard",
            },
            {
                "id": "split",
                "node_type": "gate",
                "input": "enriched",
                "condition": "true",
                "routes": {"a": "fork"},
                "fork_to": ["branch_a", "branch_b"],
                # Explicit null on_success — same null-round-trip proof, on
                # the connection-rule (not sink-rule) field.
                "on_success": None,
            },
            {
                "id": "rejoin",
                "node_type": "row_union",
                "input": "branch_a",
                "on_success": "unioned",
                "branches": {"branch_a": "branch_a", "branch_b": "branch_b"},
            },
        ],
        "edges": [],
        "outputs": [
            # Explicit null on_write_failure — third and last nullable
            # sink-rule field.
            {"sink_name": "flagged", "plugin": "json", "on_write_failure": None},
            {"sink_name": "unioned", "plugin": "json"},
        ],
    }

    errors = list(Draft202012Validator(canonical_set_pipeline_schema()).iter_errors(payload))
    assert errors == []
    # The runtime model must accept it too — disclosure never narrows Stage 1.
    SetPipelineArgumentsModel.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("properties", "nodes", "items", "properties", "fork_to", "items"), "fork"),  # the ticket's named trap
        (("properties", "nodes", "items", "properties", "id"), "continue"),
        (("properties", "nodes", "items", "properties", "id"), "x" * 39),
        (("properties", "nodes", "items", "properties", "id"), "1leading_digit"),
        (("properties", "outputs", "items", "properties", "sink_name"), "Results"),
        (("properties", "nodes", "items", "properties", "on_success"), "on_success"),
    ],
)
def test_canonical_schema_rejects_a_runtime_invalid_name(path: tuple[str, ...], value: str) -> None:
    schema = canonical_set_pipeline_schema()
    leaf = _node_at(schema, path)
    errors = list(Draft202012Validator(leaf).iter_errors(value))
    assert errors, f"expected {value!r} to be rejected at {'.'.join(path)}"


def test_canonical_schema_admits_fork_and_discard_route_destination_escapes() -> None:
    # 'fork' is a reserved edge label AND a legitimate routes destination
    # (state.py's _FORK_ROUTE_TARGET, core/config.py:818-821): destination
    # values get the narrower `_ROUTE_DESTINATION_RULE_NOT`, which excludes
    # only 'continue'/'on_success' — not the full reserved-label set — so
    # 'fork' and 'discard' stay admitted.
    payload = {
        "source": {"plugin": "csv", "on_success": "rows"},
        "nodes": [
            {
                "id": "router",
                "node_type": "gate",
                "input": "rows",
                "condition": "true",
                "routes": {"a": "fork", "b": "discard"},
            }
        ],
        "edges": [],
        "outputs": [],
    }
    errors = list(Draft202012Validator(canonical_set_pipeline_schema()).iter_errors(payload))
    assert errors == []
    SetPipelineArgumentsModel.model_validate(payload)


@pytest.mark.parametrize("destination", ["continue", "on_success", "__hidden", "bad name"])
def test_canonical_schema_rejects_invalid_route_destination_values(destination: str) -> None:
    # 'continue' is explicitly removed as a route destination
    # (core/config.py:822-823); 'on_success' is still caught by the general
    # reserved-label membership test every other connection-rule field uses
    # once it falls through the fork/discard escape; 'bad name' exercises the
    # ordinary connection charset, unrelated to the escapes.
    routes_values = canonical_set_pipeline_schema()["properties"]["nodes"]["items"]["properties"]["routes"]["additionalProperties"]
    errors = list(Draft202012Validator(routes_values).iter_errors(destination))
    assert errors, f"expected {destination!r} to be rejected as a route destination"


def test_canonical_schema_discloses_reserved_label_escape_values_remain_admitted() -> None:
    # 'discard' is a legitimate on_error/on_write_failure escape value; it
    # also happens to satisfy the connection charset/length/reserved-label
    # rule, so disclosing that rule must not reject it.
    on_error_schema = canonical_set_pipeline_schema()["properties"]["nodes"]["items"]["properties"]["on_error"]
    errors = list(Draft202012Validator(on_error_schema).iter_errors("discard"))
    assert errors == []
