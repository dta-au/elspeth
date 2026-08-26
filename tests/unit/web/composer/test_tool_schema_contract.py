"""Directional compatibility tests for the planner's canonical schema."""

from __future__ import annotations

import importlib
import importlib.util
from copy import deepcopy
from enum import StrEnum
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES
from elspeth.core.config import OutputMode, TriggerConfig
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.state import (
    _SCOPE_POLICY_VOCABULARY,
    CompositionState,
    NodeSpec,
    NodeType,
    PipelineMetadata,
    _collector_intrinsic_errors,
)
from elspeth.web.composer.tools._common import _validate_aggregation_trigger
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel


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


# --------------------------------------------------------------------------- #
# upsert_node (elspeth-30dc596c79)                                             #
#                                                                              #
# The incremental authoring tool's hand-written node_type enum had drifted     #
# from NodeType — `collector` was missing, so ELSPETH rejected its own          #
# documented collector authoring at `_validate_tool_arguments` for every        #
# provider. Nothing below asserts membership of one named kind: the defect      #
# survived precisely because the wire-schema tests asserted `"queue" in enum`   #
# and `"row_union" in enum` one kind at a time, and collector never got its     #
# line. Every case derives from `NodeType` or from the runtime model, so kind   #
# number eight is covered the day it is declared.                              #
# --------------------------------------------------------------------------- #


def _registered_upsert_node_schema() -> dict[str, Any]:
    return next(definition["parameters"] for definition in get_tool_definitions() if definition["name"] == "upsert_node")


def _advertised_upsert_node_copy() -> dict[str, Any]:
    return deepcopy(_registered_upsert_node_schema())


def _aggregation_node(*, output_mode: str | None) -> NodeSpec:
    return NodeSpec(
        id="agg",
        node_type="aggregation",
        input="rows",
        plugin="batch_plugin",
        on_success="out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        output_mode=output_mode,
    )


def _validation_errors_for(node: NodeSpec) -> tuple[Any, ...]:
    """Run the real authoring-path validator over a state holding one node."""
    state = CompositionState(
        source=None,
        nodes=(node,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    return tuple(state.validate().errors)


def _collector_node(*, scope_policy: str | None) -> NodeSpec:
    return NodeSpec(
        id="closer",
        node_type="collector",
        input="rows",
        plugin="batch_plugin",
        on_success="out",
        on_error=None,
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        scope_name="scope",
        scope_opener="opener",
        scope_policy=scope_policy,
    )


def test_registered_upsert_node_schema_is_directionally_compatible_with_runtime_model() -> None:
    module = _schema_contract_module()
    module.assert_upsert_node_schema_compatible()


def test_advertised_node_type_enum_carries_every_declared_node_kind() -> None:
    """The wire enum is the NodeType membership, not a restatement of it."""
    advertised = _registered_upsert_node_schema()["properties"]["node_type"]["enum"]

    assert set(advertised) == set(get_args(NodeType))


@pytest.mark.parametrize("node_kind", get_args(NodeType))
def test_directional_guard_rejects_a_node_kind_dropped_from_the_advertised_enum(node_kind: str) -> None:
    """Dropping ANY declared kind from the wire enum fails the contract.

    Parametrising over ``NodeType`` is what makes this un-forgettable: a new
    kind adds its own case without anyone remembering to write one.
    """
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    node_type = advertised["properties"]["node_type"]
    node_type["enum"] = [value for value in node_type["enum"] if value != node_kind]

    with pytest.raises(RuntimeError, match="runtime enum values are missing from the advertised schema"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_documented_disclosure_cannot_bury_a_closed_runtime_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing node kind stays a failure even if node_type is disclosed.

    The mutation that matters: the cheapest way to silence this guard for kind
    number eight would be to add ``node_type`` to the disclosure set. The
    disclosure set refuses any property the runtime model pins to a closed
    vocabulary, so that route is closed by construction rather than by anyone
    remembering not to take it.
    """
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    node_type = advertised["properties"]["node_type"]
    node_type["enum"] = [value for value in node_type["enum"] if value != "collector"]
    monkeypatch.setattr(
        module,
        "_UPSERT_NODE_ADVERTISED_DISCLOSURES",
        {**module._UPSERT_NODE_ADVERTISED_DISCLOSURES, "node_type": "state.py NodeType"},
    )

    with pytest.raises(RuntimeError, match="the runtime model pins a closed vocabulary"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_disclosure_guard_refuses_a_vocabulary_hidden_behind_an_untraversed_keyword() -> None:
    """The `prefixItems` bypass, pinned as the shape that defeated the guard.

    Retyping `node_type` to a fixed-length tuple moved its enum under
    `prefixItems`, which the traversal does not descend into. It read as "no
    closed vocabulary", `node_type` qualified for disclosure, was relaxed to a
    bare `{"type": "array"}`, and the contract passed with `collector` dropped.
    The guard now refuses the shape instead of answering False about it.
    """
    module = _schema_contract_module()

    class _TupleTypedNodeType(BaseModel):
        id: str
        node_type: tuple[NodeType, int]
        input: str

        model_config = ConfigDict(extra="forbid")

    runtime = _TupleTypedNodeType.model_json_schema()
    definitions = runtime["$defs"] if "$defs" in runtime else {}

    with pytest.raises(RuntimeError, match="prefixItems"):
        module._carries_closed_enum(runtime["properties"]["node_type"], definitions, path="$.node_type")


def test_disclosure_guard_refuses_a_vocabulary_hidden_behind_a_constrained_mapping_key() -> None:
    """The same bypass through a different door, on real pydantic output.

    `prefixItems` was reached by retyping the sequence; this reaches it by
    retyping the mapping KEY. `dict[NodeType, int]` emits the entire node-kind
    enum inline under `propertyNames` — a keyword the traversal does not
    descend — so before the refusal existed the vocabulary was invisible and
    `node_type` qualified for disclosure.
    """
    module = _schema_contract_module()

    class _MappingKeyedNodeType(BaseModel):
        id: str
        node_type: dict[NodeType, int]
        input: str

        model_config = ConfigDict(extra="forbid")

    runtime = _MappingKeyedNodeType.model_json_schema()
    definitions = runtime["$defs"] if "$defs" in runtime else {}
    emitted = runtime["properties"]["node_type"]
    assert "propertyNames" in emitted, "pydantic no longer emits propertyNames for a constrained mapping key"

    with pytest.raises(RuntimeError, match="propertyNames"):
        module._carries_closed_enum(emitted, definitions, path="$.node_type")


def test_untraversed_keyword_list_is_pinned_against_silent_deletion() -> None:
    """The parametrized test below cannot notice a member LEAVING the constant.

    It parametrizes over the very tuple it guards, so deleting a keyword deletes
    its own case and the suite stays green while the guard goes blind — proven
    by mutation: removing `"contains"` left every keyword test passing while
    `_carries_closed_enum({"contains": {...}})` returned False.

    A hand-written literal normally violates this project's rule that a guard
    must DERIVE from the authority it enforces. The exemption is warranted here
    for the one reason that lifts it: `_UNTRAVERSED_COMPOSITION_KEYWORDS` IS the
    authority. There is no upstream list to derive from — pydantic does not
    enumerate JSON-Schema keywords, and which ones this walk cannot see through
    is a judgement about THIS traversal. With no upstream, "derive it" has no
    referent, so the honest alternative is to make deletion a deliberate
    two-place edit instead of a silent one. Do not delete this pin citing the
    derive rule; read this paragraph first.
    """
    module = _schema_contract_module()

    assert set(module._UNTRAVERSED_COMPOSITION_KEYWORDS) == {
        "allOf",
        "prefixItems",
        "patternProperties",
        "propertyNames",
        "dependentSchemas",
        "unevaluatedProperties",
        "if",
        "contains",
        "not",
    }


@pytest.mark.parametrize("keyword", _schema_contract_module()._UNTRAVERSED_COMPOSITION_KEYWORDS)
def test_disclosure_guard_refuses_every_untraversed_composition_keyword(keyword: str) -> None:
    """`allOf` matters in the other direction: pydantic 2.0-2.8 emitted it for
    `$ref`-with-siblings, so a dependency DOWNGRADE reintroduces the shape."""
    module = _schema_contract_module()
    branch: dict[str, Any] = {"type": "string", keyword: {"type": "string"}}

    with pytest.raises(RuntimeError, match=keyword):
        module._carries_closed_enum(branch, {}, path="$.probe")


def test_untraversed_keyword_refusal_is_a_no_op_on_the_current_runtime_model() -> None:
    """Failing closed costs nothing today — and says so if that ever changes."""
    module = _schema_contract_module()

    def keywords_in(node: object, seen: set[str]) -> set[str]:
        if isinstance(node, dict):
            seen.update(node)
            for value in node.values():
                keywords_in(value, seen)
        elif isinstance(node, list):
            for value in node:
                keywords_in(value, seen)
        return seen

    present = keywords_in(_UpsertNodeArgumentsModel.model_json_schema(), set())
    offenders = sorted(set(module._UNTRAVERSED_COMPOSITION_KEYWORDS) & present)

    assert not offenders, (
        f"_UpsertNodeArgumentsModel now emits {offenders} — the disclosure guard refuses these "
        "shapes, so extend _carries_closed_enum to descend them rather than removing the refusal."
    )


def test_directional_guard_rejects_an_undocumented_advertised_enum_narrowing() -> None:
    """Only the documented disclosures may advertise a closed vocabulary."""
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    advertised["properties"]["policy"]["enum"] = ["require_all", None]

    with pytest.raises(RuntimeError, match="advertised enum is narrower than the runtime model"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_directional_guard_rejects_advertised_upsert_node_requiredness_narrowing() -> None:
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    advertised["required"] = [*advertised["required"], "plugin"]

    with pytest.raises(RuntimeError, match="required"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_directional_guard_rejects_advertised_upsert_node_nullability_narrowing() -> None:
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    advertised["properties"]["scope_name"]["type"] = "string"

    with pytest.raises(RuntimeError, match="null"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_directional_guard_requires_every_upsert_node_runtime_field_to_be_advertised() -> None:
    module = _schema_contract_module()
    advertised = _advertised_upsert_node_copy()
    del advertised["properties"]["scope_opener"]

    with pytest.raises(RuntimeError, match=r"scope_opener: typed runtime branch is not explicitly advertised"):
        module.assert_upsert_node_schema_compatible(advertised_schema=advertised)


def test_a_stale_disclosure_entry_fails_rather_than_passing_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disclosure naming a field the runtime model no longer has is fatal."""
    module = _schema_contract_module()
    monkeypatch.setattr(
        module,
        "_UPSERT_NODE_ADVERTISED_DISCLOSURES",
        {**module._UPSERT_NODE_ADVERTISED_DISCLOSURES, "retired_field": "nowhere"},
    )

    with pytest.raises(RuntimeError, match="disclosed property is absent from the runtime model"):
        module.assert_upsert_node_schema_compatible()


# --------------------------------------------------------------------------- #
# Each disclosure in `_UPSERT_NODE_ADVERTISED_DISCLOSURES` is honest only      #
# while its advertised vocabulary EQUALS what its named enforcer accepts.      #
# The relaxation strips the advertised enum before the walker runs, so the     #
# contract itself cannot see that pairing — these tests are what hold it. A    #
# downstream vocabulary that grows fails here, which is the same drift shape   #
# as the node_type enum this section exists for.                              #
# --------------------------------------------------------------------------- #


def test_disclosed_scope_policy_enum_equals_the_enforced_scope_vocabulary() -> None:
    advertised = _registered_upsert_node_schema()["properties"]["scope_policy"]["enum"]

    assert set(advertised) == {*_SCOPE_POLICY_VOCABULARY, None}


def test_enforced_scope_policy_vocabulary_refuses_a_value_outside_it() -> None:
    """The enforcer really refuses, and with its own message — not a schema bounce."""
    collector = _collector_node(scope_policy="whenever")

    entries = _collector_intrinsic_errors(collector, nodes=(collector,))

    assert [entry.error_code for entry in entries if entry.error_code == "collector_scope_policy_invalid"]


def test_disclosed_output_mode_enum_equals_the_enforced_aggregation_vocabulary() -> None:
    advertised = _registered_upsert_node_schema()["properties"]["output_mode"]["enum"]

    assert set(advertised) == {*(member.value for member in OutputMode), None}


def test_enforced_output_mode_vocabulary_refuses_a_value_outside_it() -> None:
    """A value outside the vocabulary is rejected, and today's members are not.

    A cheap sanity check only. It cannot detect a hand-listed
    `("passthrough", "transform")`, because that tuple and the enum agree
    today — that is
    `test_enforced_output_mode_vocabulary_derives_from_the_enum`'s job.
    """
    entries = _validation_errors_for(_aggregation_node(output_mode="sideways"))

    assert [entry for entry in entries if entry.error_code == "aggregation_output_mode_invalid"]

    for member in OutputMode:
        accepted = _validation_errors_for(_aggregation_node(output_mode=member.value))
        assert not [entry for entry in accepted if entry.error_code == "aggregation_output_mode_invalid"]


def test_enforced_output_mode_vocabulary_derives_from_the_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member the enum GROWS must be accepted without editing the enforcer.

    The equality test above binds the wire enum to `OutputMode`; this binds the
    ENFORCER to it. Asserting only that today's two members are accepted cannot
    fail on a hand-listed `("passthrough", "transform")`, because that tuple and
    the enum agree today — so the mutation has to be a grown enum, not a member
    sweep.
    """
    import elspeth.web.composer.state as state_module

    class _GrownOutputMode(StrEnum):
        PASSTHROUGH = "passthrough"
        TRANSFORM = "transform"
        SIDEWAYS = "sideways"

    monkeypatch.setattr(state_module, "OutputMode", _GrownOutputMode)
    entries = _validation_errors_for(_aggregation_node(output_mode="sideways"))

    assert not [entry for entry in entries if entry.error_code == "aggregation_output_mode_invalid"], (
        "'sideways' is a declared OutputMode member but CompositionState.validate rejects it — "
        "the enforcer is restating the vocabulary instead of deriving it."
    )


def test_disclosed_trigger_object_equals_the_enforced_trigger_vocabulary() -> None:
    trigger = _registered_upsert_node_schema()["properties"]["trigger"]

    assert set(trigger["properties"]) == set(TriggerConfig.model_fields)
    assert trigger["additionalProperties"] is False
    assert TriggerConfig.model_config["extra"] == "forbid"


def test_enforced_trigger_vocabulary_refuses_a_key_outside_it() -> None:
    """`_execute_upsert_node` runs this on every authored aggregation."""
    assert _validate_aggregation_trigger({"condition": "row['batch_count'] >= 2"}) is None
    assert _validate_aggregation_trigger({"nope": 1}) is not None
