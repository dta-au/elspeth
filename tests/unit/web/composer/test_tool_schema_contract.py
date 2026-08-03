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
        assert "gate" in lowered
        assert "node-level" in lowered
        assert "discard" in lowered
        assert "sink" in lowered
        assert "fail-fast" in lowered

    edge_description = upsert_edge["description"]
    assert "transform/aggregation" in edge_description
    assert "gate" in edge_description
    assert "upsert_node" in edge_description


def test_canonical_schema_accessor_returns_isolated_registered_copies() -> None:
    spec = importlib.util.find_spec("elspeth.web.composer.tools.schema_contract")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    first = module.canonical_set_pipeline_schema()
    second = module.canonical_set_pipeline_schema()
    registered = _registered_set_pipeline_schema()
    assert first == second == registered

    first["required"].append("attacker_added")
    first["properties"]["source"]["properties"]["plugin"]["type"] = "integer"

    assert module.canonical_set_pipeline_schema() == registered


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
