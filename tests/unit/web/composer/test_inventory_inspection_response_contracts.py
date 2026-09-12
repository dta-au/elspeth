"""Selected inventory/inspection admission and baseline producer wire witnesses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, JsonValue
from sqlalchemy import Engine

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSummary
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer._response_json import encode_response_json, parse_frozen_response_json, parse_response_json
from elspeth.web.composer.discovery_cache import pydantic_default
from elspeth.web.composer.inventory_response_contracts import PLUGIN_INVENTORY_RESPONSE_CONTRACT
from elspeth.web.composer.source_inspection import SOURCE_INSPECTION_RESPONSE_CONTRACT, facts_to_dict, inspect_blob_content
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import sources, transforms
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

# Actual handler captures from clean a6c58c68fe99c726805937f297a156af8f2cd3ef.
# JSON spacing, escaping, object order and array order are part of this witness.
_GOLDEN_INVENTORY = r"""{"available": [{"name": "example", "description": "Example \u00e9", "plugin_type": "source", "config_fields": [{"name": "options", "type": "object", "required": false, "description": null, "default": {"z": [null, true, 1, 1.5], "a": "\u00e9"}}], "usage_when_to_use": null, "usage_when_not_to_use": null, "example_use": null, "capability_tags": [], "web_config_authority": "user_configurable", "policy_capabilities": [], "audit_characteristics": [], "composer_hints": [], "secret_requirements": []}], "prohibited": [{"name": "example", "reason": "plugin_not_allowed_on_web", "explanation": "the plugin is installed and authorized for this deployment's runtime but prohibited on the web authoring surface by security policy"}]}"""
_GOLDEN_INSPECTION = r"""{"source_kind": "csv", "redacted_identity": {"filename": "data.csv", "mime_type": "text/csv", "byte_size": "11", "blob_id": "00000000-0000-0000-0000-000000000001", "content_hash_prefix": "0528fb34"}, "byte_range_inspected": [0, 11], "sample_row_count": 1, "observed_headers": ["z", "a"], "inferred_types": {"z": "int", "a": "bool"}, "url_candidates": [], "warnings": ["csv_lexical_types_advisory: 2 column(s) have int/float/bool inferred_types \u2014 these are lexical observations of CSV text. At runtime every csv value arrives as str unless the SOURCE schema declares the field's type (declared source fields are coerced at ingestion). Do not copy an inferred type into a downstream node's schema without declaring it on the source or inserting a type_coerce."]}"""


def _state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _context(catalog: MagicMock) -> ToolContext:
    return ToolContext(catalog=catalog, plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot))


@pytest.mark.parametrize("tool,kind", [("list_sources", "source"), ("list_transforms", "transform"), ("list_sinks", "sink")])
def test_inventory_real_handlers_preserve_baseline_bytes(tool: str, kind: str) -> None:
    catalog = MagicMock(spec=PolicyCatalogView)
    mutable_values: list[JsonValue] = [None, True, 1, 1.5]
    summary = PluginSummary.model_validate(
        {
            "name": "example",
            "description": "Example é",
            "plugin_type": kind,
            "config_fields": [ConfigFieldSummary(name="options", type="object", required=False, default={"z": mutable_values, "a": "é"})],
        }
    )
    catalog.list_sources.return_value = catalog.list_transforms.return_value = catalog.list_sinks.return_value = [summary]
    catalog.list_prohibited_sources.return_value = catalog.list_prohibited_transforms.return_value = (
        catalog.list_prohibited_sinks.return_value
    ) = [summary]
    declarations = {
        "list_sources": sources._LIST_SOURCES_DECLARATION,
        "list_transforms": transforms._LIST_TRANSFORMS_DECLARATION,
        "list_sinks": transforms._LIST_SINKS_DECLARATION,
    }
    declaration = declarations[tool]
    assert declaration.response_contract is PLUGIN_INVENTORY_RESPONSE_CONTRACT
    result = declaration.handler({}, _state(), _context(catalog))
    assert result.success
    admitted = PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(result.data)
    expected = _GOLDEN_INVENTORY.replace('"plugin_type": "source"', f'"plugin_type": "{kind}"')
    assert json.dumps(admitted.to_wire()) == expected
    assert json.dumps(admitted.readmit(PLUGIN_INVENTORY_RESPONSE_CONTRACT).to_wire()) == expected
    mutable_values.append("later mutation")
    assert json.dumps(admitted.to_wire()) == expected


def test_inspection_real_handler_preserves_baseline_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "data.csv"
    path.write_bytes(b"z,a\n1,true\n")
    blob_id = "00000000-0000-0000-0000-000000000001"
    blob = {
        "id": blob_id,
        "status": "ready",
        "storage_path": str(path),
        "filename": "data.csv",
        "mime_type": "text/csv",
        "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": 11,
    }
    monkeypatch.setattr(sources, "_sync_get_blob", lambda *args: blob)
    context = ToolContext(
        catalog=MagicMock(spec=PolicyCatalogView),
        plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
        session_engine=MagicMock(spec=Engine),
        session_id="00000000-0000-0000-0000-000000000002",
    )
    result = sources._INSPECT_SOURCE_DECLARATION.handler({"blob_id": blob_id}, _state(), context)
    assert result.success
    assert sources._INSPECT_SOURCE_DECLARATION.response_contract is SOURCE_INSPECTION_RESPONSE_CONTRACT
    admitted = SOURCE_INSPECTION_RESPONSE_CONTRACT.admit(result.data)
    assert json.dumps(admitted.to_wire()) == _GOLDEN_INSPECTION
    assert json.dumps(admitted.readmit(SOURCE_INSPECTION_RESPONSE_CONTRACT).to_wire()) == _GOLDEN_INSPECTION


@pytest.mark.parametrize(
    "mutation", ["extra_root", "extra_config", "bad_required", "bad_default", "bad_capability", "bad_secret", "bad_reason"]
)
def test_inventory_nested_producer_corruption_is_not_an_argument_error(mutation: str) -> None:
    raw = json.loads(_GOLDEN_INVENTORY)
    plugin = raw["available"][0]
    if mutation == "extra_root":
        raw["private"] = "secret"
    elif mutation == "extra_config":
        plugin["config_fields"][0]["private"] = "secret"
    elif mutation == "bad_required":
        plugin["config_fields"][0]["required"] = 1
    elif mutation == "bad_default":
        plugin["config_fields"][0]["default"] = object()
    elif mutation == "bad_capability":
        plugin["policy_capabilities"] = [{"capability": "invented", "control_role": None, "blocks_positive_detection": False}]
    elif mutation == "bad_secret":
        plugin["secret_requirements"] = [{"field": "token", "candidates": [1]}]
    else:
        raw["prohibited"][0]["reason"] = "private invented reason"
    with pytest.raises(FrameworkBugError) as error:
        PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(raw)
    assert "secret" not in str(error.value) and "private" not in str(error.value)


@pytest.mark.parametrize(
    "mutation", ["extra_root", "extra_identity", "missing_identity", "numeric_size", "inferred_type", "bool_range", "warnings_string"]
)
def test_inspection_closed_records_reject_corruption(mutation: str) -> None:
    raw = json.loads(_GOLDEN_INSPECTION)
    if mutation == "extra_root":
        raw["private"] = "secret"
    elif mutation == "extra_identity":
        raw["redacted_identity"]["storage_path"] = "secret"
    elif mutation == "missing_identity":
        del raw["redacted_identity"]["filename"]
    elif mutation == "numeric_size":
        raw["redacted_identity"]["byte_size"] = 11
    elif mutation == "inferred_type":
        raw["inferred_types"]["z"] = "object"
    elif mutation == "bool_range":
        raw["byte_range_inspected"] = [False, 11]
    else:
        raw["warnings"] = "secret"
    with pytest.raises(FrameworkBugError) as error:
        SOURCE_INSPECTION_RESPONSE_CONTRACT.admit(raw)
    assert str(error.value) == "Source inspection producer returned malformed data"


def test_inspection_optional_identity_and_null_collections_preserved() -> None:
    facts = inspect_blob_content(content=b"hello", filename="data.txt", mime_type="text/plain")
    admitted = SOURCE_INSPECTION_RESPONSE_CONTRACT.admit(facts)
    assert admitted.to_wire() == facts_to_dict(facts)
    assert admitted.readmit(SOURCE_INSPECTION_RESPONSE_CONTRACT).to_wire() == facts_to_dict(facts)


def test_inventory_empty_and_frozen_input() -> None:
    value = MappingProxyType({"available": (), "prohibited": ()})
    admitted = PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(value)
    assert admitted.to_wire() == {"available": [], "prohibited": []}


def test_actual_builtin_catalog_summaries_have_closed_inventory_contract() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    catalog = CatalogServiceImpl(manager)
    for summaries in (catalog.list_sources(), catalog.list_transforms(), catalog.list_sinks()):
        assert summaries
        raw = {"available": summaries, "prohibited": []}
        actual = PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(raw).to_wire()
        assert json.dumps(actual) == json.dumps(raw, default=pydantic_default)


def test_corrupt_catalog_item_from_actual_handler_fails_closed() -> None:
    summary = PluginSummary(name="example", description="Example", plugin_type="source", config_fields=[])
    corrupted_fields: dict[str, Any] = {"name": "field", "type": "string", "required": "private"}
    summary.config_fields = [ConfigFieldSummary.model_construct(**corrupted_fields)]
    catalog = MagicMock(spec=PolicyCatalogView)
    catalog.list_sources.return_value = [summary]
    catalog.list_prohibited_sources.return_value = []
    result = sources._handle_list_sources({}, _state(), _context(catalog))
    with pytest.raises(FrameworkBugError, match="Plugin inventory producer returned malformed data"):
        PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(result.data)


def test_nominal_cached_values_are_rechecked() -> None:
    inventory = PLUGIN_INVENTORY_RESPONSE_CONTRACT.parse(json.loads(_GOLDEN_INVENTORY))
    corrupted = replace(inventory, available=(replace(inventory.available[0], plugin_type="invented"),))
    with pytest.raises(FrameworkBugError):
        PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(corrupted)
    facts = inspect_blob_content(content=b"hello", filename="data.txt", mime_type="text/plain")
    corrupted_facts = replace(facts, redacted_identity={**facts.redacted_identity, "storage_path": "private"})
    with pytest.raises(FrameworkBugError, match="Source inspection producer returned malformed data"):
        SOURCE_INSPECTION_RESPONSE_CONTRACT.admit(corrupted_facts)


def test_owned_cache_containers_cannot_be_repaired_from_mutable_shapes() -> None:
    inventory = PLUGIN_INVENTORY_RESPONSE_CONTRACT.parse(json.loads(_GOLDEN_INVENTORY))
    root_corruption: dict[str, Any] = {"available": list(inventory.available)}
    root_item_corruption: dict[str, Any] = {"available": tuple(json.loads(_GOLDEN_INVENTORY)["available"])}
    nested_corruption: dict[str, Any] = {"config_fields": list(inventory.available[0].config_fields)}
    nested_item_corruption: dict[str, Any] = {"config_fields": tuple(json.loads(_GOLDEN_INVENTORY)["available"][0]["config_fields"])}
    default_corruption: dict[str, Any] = {"default": {"nested": []}}
    bad_default = replace(inventory.available[0].config_fields[0], **default_corruption)
    capability_corruption: dict[str, Any] = {"capability": "llm"}
    bad_capability = CapabilityDeclaration(**capability_corruption)
    corruptions = (
        replace(inventory, **root_corruption),
        replace(inventory, **root_item_corruption),
        replace(inventory, available=(replace(inventory.available[0], **nested_corruption),)),
        replace(inventory, available=(replace(inventory.available[0], **nested_item_corruption),)),
        replace(inventory, available=(replace(inventory.available[0], config_fields=(bad_default,)),)),
        replace(inventory, available=(replace(inventory.available[0], policy_capabilities=(bad_capability,)),)),
    )
    for corrupted in corruptions:
        with pytest.raises(FrameworkBugError):
            PLUGIN_INVENTORY_RESPONSE_CONTRACT.admit(corrupted)
    facts = inspect_blob_content(content=b"hello", filename="data.txt", mime_type="text/plain")
    facts_corruption: dict[str, Any] = {"warnings": ["mutable"]}
    with pytest.raises(FrameworkBugError):
        SOURCE_INSPECTION_RESPONSE_CONTRACT.admit(replace(facts, **facts_corruption))


@pytest.mark.parametrize("contract", [PLUGIN_INVENTORY_RESPONSE_CONTRACT, SOURCE_INSPECTION_RESPONSE_CONTRACT])
def test_impostor_model_or_serializer_is_not_admitted(contract) -> None:
    class Impostor(BaseModel):
        available: tuple[str, ...] = ()
        prohibited: tuple[str, ...] = ()

    class Serializer:
        def to_dict(self):
            return {"available": [], "prohibited": []}

    for value in (Impostor(), Serializer()):
        with pytest.raises(FrameworkBugError):
            contract.admit(value)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), b"bytes", {1: "value"}, object()])
def test_dynamic_json_leaf_refuses_non_json(value) -> None:
    with pytest.raises(FrameworkBugError):
        parse_response_json(value)


def test_dynamic_json_alias_isolation_and_fresh_encoding() -> None:
    mutable_values: list[JsonValue] = [None, True, 1, 1.5]
    raw = {"z": mutable_values, "a": "é"}
    parsed = parse_response_json(raw)
    wire = encode_response_json(parsed)
    mutable_values.append("private mutation")
    assert encode_response_json(parsed) == wire
    assert deep_thaw(parsed) == wire


def test_dynamic_json_cycles_fail_with_safe_framework_error() -> None:
    backing: dict[str, Any] = {}
    frozen_cycle = MappingProxyType(backing)
    backing["self"] = frozen_cycle
    for parser in (parse_response_json, parse_frozen_response_json):
        with pytest.raises(FrameworkBugError, match="JSON nesting is invalid"):
            parser(frozen_cycle)
