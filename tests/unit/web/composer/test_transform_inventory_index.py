"""list_transforms is a selection index: five facts per plugin, never its configuration.

Spec carried from the staging hotfix (``test_transform_inventory_index.py``):
the listing keeps selection and prohibition facts and names the per-plugin
details tool, but no config field, default, hint or usage text reaches the
model through it. Every assertion goes through the public declaration and its
own response contract, and checks a key before indexing it, so a tree still
echoing the full ``PluginSummary`` fails here with an AssertionError.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.composer_planner_audit import ComposerPlannerInformationClass
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, ControlRole, PluginCapability, WebConfigAuthority
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import ConfigFieldSummary, PluginSecretRequirement, PluginSummary
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.pipeline_planner import _planner_information_classes, _tool_information_keys
from elspeth.web.composer.planner_authoring_aids import discovery_digest
from elspeth.web.composer.tools import generation, transforms
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer.test_inventory_inspection_response_contracts import _context, _state

_INDEX_KEYS = {"name", "description", "capability_tags", "web_config_authority", "policy_capabilities"}
_ROOT_KEYS = {"available", "prohibited", "details_tool"}
_LARGE = "synthetic configuration and guidance marker " * 20_000

# Captured from this tree's handler; JSON spacing, escaping and order are part of the witness.
_GOLDEN_INDEX = (
    '{"available": [{"name": "example", "description": "Example \\u00e9", "capability_tags": ["llm"], '
    '"web_config_authority": "user_configurable", "policy_capabilities": [{"capability": "content_safety", '
    '"control_role": "output", "blocks_positive_detection": true}]}], "prohibited": [{"name": "example", '
    '"reason": "plugin_not_allowed_on_web", "explanation": "the plugin is installed and authorized for this '
    "deployment's runtime but prohibited on the web authoring surface by security policy\"}], "
    '"details_tool": "get_plugin_schema"}'
)


def _heavy_summary() -> PluginSummary:
    return PluginSummary(
        name="example",
        description="Example é",
        plugin_type="transform",
        config_fields=[ConfigFieldSummary(name="prompt", type="string", required=True, default=_LARGE)],
        usage_when_to_use=_LARGE,
        usage_when_not_to_use=_LARGE,
        example_use=_LARGE,
        capability_tags=("llm",),
        policy_capabilities=(CapabilityDeclaration(PluginCapability.CONTENT_SAFETY, ControlRole.OUTPUT, True),),
        composer_hints=(_LARGE,),
        secret_requirements=(PluginSecretRequirement(field="api_key", candidates=("OPENAI_API_KEY",)),),
    )


def _listing(summary: PluginSummary) -> tuple[dict[str, object], object]:
    catalog = MagicMock(spec=PolicyCatalogView)
    catalog.list_transforms.return_value = [summary]
    catalog.list_prohibited_transforms.return_value = [summary]
    declaration = transforms._LIST_TRANSFORMS_DECLARATION
    result = declaration.handler({}, _state(), _context(catalog))
    assert result.success
    contract = declaration.response_contract
    assert contract is not None
    admitted = contract.admit(result.data)
    wire = admitted.to_wire()
    assert type(wire) is dict
    return wire, admitted


def test_listing_keeps_selection_and_prohibition_but_never_echoes_configuration() -> None:
    summary = _heavy_summary()
    original = summary.model_dump()

    wire, admitted = _listing(summary)

    assert set(wire) == _ROOT_KEYS
    assert wire["details_tool"] == "get_plugin_schema"
    [entry] = wire["available"]
    assert set(entry) == _INDEX_KEYS
    assert entry == {
        "name": "example",
        "description": "Example é",
        "capability_tags": ["llm"],
        "web_config_authority": WebConfigAuthority.USER_CONFIGURABLE.value,
        "policy_capabilities": [{"capability": "content_safety", "control_role": "output", "blocks_positive_detection": True}],
    }
    [prohibited] = wire["prohibited"]
    assert prohibited["name"] == "example"
    assert prohibited["reason"] == "plugin_not_allowed_on_web"
    rendered = json.dumps(wire)
    assert "synthetic configuration and guidance marker" not in rendered
    assert "OPENAI_API_KEY" not in rendered
    assert len(rendered.encode()) < 2_000
    # The projection reads the catalog's summaries; it never rewrites them.
    assert summary.model_dump() == original
    assert json.dumps(admitted.readmit(transforms._LIST_TRANSFORMS_DECLARATION.response_contract).to_wire()) == rendered


def test_listing_wire_bytes_are_pinned() -> None:
    summary = _heavy_summary().model_copy(
        update={"config_fields": [], "usage_when_to_use": None, "usage_when_not_to_use": None, "example_use": None, "composer_hints": ()}
    )
    wire, _admitted = _listing(summary)
    assert json.dumps(wire) == _GOLDEN_INDEX


@pytest.mark.parametrize(
    "mutation",
    ["extra_root", "extra_entry_key", "missing_details_tool", "other_details_tool", "bad_capability", "bad_authority", "bad_reason"],
)
def test_index_contract_refuses_a_listing_it_does_not_own(mutation: str) -> None:
    contract = transforms._LIST_TRANSFORMS_DECLARATION.response_contract
    assert contract is not None
    raw = json.loads(_GOLDEN_INDEX)
    entry = raw["available"][0]
    if mutation == "extra_root":
        raw["private"] = "secret"
    elif mutation == "extra_entry_key":
        entry["config_fields"] = []
    elif mutation == "missing_details_tool":
        del raw["details_tool"]
    elif mutation == "other_details_tool":
        raw["details_tool"] = "list_transforms"
    elif mutation == "bad_capability":
        entry["policy_capabilities"] = [{"capability": "invented", "control_role": None, "blocks_positive_detection": False}]
    elif mutation == "bad_authority":
        entry["web_config_authority"] = "invented"
    else:
        raw["prohibited"][0]["reason"] = "private invented reason"
    with pytest.raises(FrameworkBugError) as error:
        contract.admit(raw)
    assert "secret" not in str(error.value) and "private" not in str(error.value)


def test_every_builtin_transform_projects_to_the_index() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    service = CatalogServiceImpl(manager)
    summaries = service.list_transforms()
    assert summaries
    catalog = MagicMock(spec=PolicyCatalogView)
    catalog.list_transforms.return_value = summaries
    catalog.list_prohibited_transforms.return_value = []
    result = transforms._LIST_TRANSFORMS_DECLARATION.handler({}, _state(), _context(catalog))
    contract = transforms._LIST_TRANSFORMS_DECLARATION.response_contract
    assert contract is not None
    wire = contract.admit(result.data).to_wire()
    assert type(wire) is dict and set(wire) == _ROOT_KEYS
    assert [entry["name"] for entry in wire["available"]] == [summary.name for summary in summaries]
    assert all(set(entry) == _INDEX_KEYS for entry in wire["available"])


def test_listing_description_names_the_details_tool_not_configuration_metadata() -> None:
    description = transforms._LIST_TRANSFORMS_DECLARATION.description
    assert "`details_tool` (get_plugin_schema)" in description
    assert "get_plugin_assistance" in description
    assert "config_fields" not in description
    assert "composer_hints" not in description


def test_transform_index_does_not_claim_complete_configuration_discovery() -> None:
    keys = _tool_information_keys("list_transforms", {})
    assert keys == ("catalog.selection", "catalog.inventory.transform")
    classes = _planner_information_classes(keys)
    assert classes == (ComposerPlannerInformationClass.CATALOG_SELECTION, ComposerPlannerInformationClass.CATALOG_SELECTION)
    assert ComposerPlannerInformationClass.CATALOG_DETAILS_TRANSFORM not in classes
    # Sources and sinks still return their full summaries and keep their detail keys.
    assert _tool_information_keys("list_sources", {}) == ("catalog.selection", "catalog.details.source")
    assert _tool_information_keys("list_sinks", {}) == ("catalog.selection", "catalog.details.sink")


def test_an_omitted_transform_prohibition_is_returned_whole_by_that_plugins_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """The digest points an oversized transform prohibition at get_plugin_schema, which returns the hashed text.

    The list_transforms index carries ``description`` (the digest's ``purpose``) but not the
    prohibition, so an omitted ``not_for`` must name the per-plugin schema read instead.
    """
    manager = PluginManager()
    manager.register_builtin_plugins()
    prohibition = "Do not use this plugin for synthetic oversized prohibition probes. " * 40
    assert len(prohibition.encode("utf-8")) > 1024
    monkeypatch.setattr(manager.get_transform_by_name("passthrough"), "usage_when_not_to_use", prohibition)
    service = CatalogServiceImpl(manager)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(service)
    view = PolicyCatalogView.for_trained_operator(service, snapshot)

    entry = next(item for item in discovery_digest(view)["transforms"] if item["name"] == "passthrough")
    assert "not_for" not in entry
    assert "not_for_omitted" in entry
    omission = entry["not_for_omitted"]
    assert omission["details_via"] == "get_plugin_schema"

    context = ToolContext(catalog=view, plugin_snapshot=snapshot)
    declaration = generation._GET_PLUGIN_SCHEMA_DECLARATION
    result = declaration.handler({"plugin_type": "transform", "name": "passthrough"}, _state(), context)
    assert result.success
    contract = declaration.response_contract
    assert contract is not None
    wire = contract.admit(result.data).to_wire()
    assert type(wire) is dict
    assert "usage_when_not_to_use" in wire
    assert wire["usage_when_not_to_use"] == prohibition
    assert hashlib.sha256(wire["usage_when_not_to_use"].encode("utf-8")).hexdigest() == omission["sha256"]

    # The index the digest no longer points at does not carry it.
    index = transforms._LIST_TRANSFORMS_DECLARATION.handler({}, _state(), context)
    index_contract = transforms._LIST_TRANSFORMS_DECLARATION.response_contract
    assert index_contract is not None
    assert prohibition not in json.dumps(index_contract.admit(index.data).to_wire())
