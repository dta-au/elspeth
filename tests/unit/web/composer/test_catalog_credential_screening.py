"""Registered plugin documentation remains usable without weakening screening."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.credential_material import find_credential_material
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import execute_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


@pytest.fixture
def llm_schema_text() -> str:
    registry = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(registry)
    catalog = PolicyCatalogView.for_trained_operator(registry, snapshot)
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    result = execute_tool(
        "get_plugin_schema",
        {"plugin_type": "transform", "name": "llm"},
        state,
        catalog,
        plugin_snapshot=snapshot,
    )
    assert result.success
    assert result.updated_state is state
    return serialize_admitted_discovery_result(admit_discovery_result("get_plugin_schema", result))


def test_registered_llm_schema_passes_credential_screening(llm_schema_text: str) -> None:
    assert find_credential_material(llm_schema_text) is None


@pytest.mark.parametrize("config_name", ["AzureOpenAIConfig", "OpenRouterConfig"])
@pytest.mark.parametrize(
    "credential_text",
    [
        pytest.param("sk-ant-" + "A" * 40, id="recognized-key-shape"),
        pytest.param("api_key=" + "synthetic-catalog-control", id="credential-assignment"),
    ],
)
def test_credentials_in_registered_schema_descriptions_remain_rejected(
    llm_schema_text: str, config_name: str, credential_text: str
) -> None:
    assert find_credential_material(llm_schema_text) is None
    payload = json.loads(llm_schema_text)
    config = payload["data"]["json_schema"]["$defs"][config_name]
    config["description"] += "\n- " + credential_text

    finding = find_credential_material(json.dumps(payload))

    assert finding is not None
    assert finding.category == "token_shape"
    assert finding.location == "root"
