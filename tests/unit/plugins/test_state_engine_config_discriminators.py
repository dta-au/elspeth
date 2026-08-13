"""Closed product discriminator contracts used by the state-engine matrix."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from elspeth.plugins.infrastructure.azure_auth import AzureAuthConfig
from elspeth.plugins.infrastructure.clients.retrieval import connection
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProviderConfig
from elspeth.plugins.infrastructure.clients.retrieval.chroma import ChromaSearchProviderConfig
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.azure_blob_sink import AzureBlobSinkConfig
from elspeth.plugins.sinks.chroma_sink import ChromaSinkConfig
from elspeth.plugins.sources.azure_blob_source import AzureBlobSourceConfig
from elspeth.plugins.transforms.rag.config import RAGRetrievalConfig

_ACCOUNT_URL = "https://account.blob.core.windows.net"
_SEARCH_ENDPOINT = "https://service.search.windows.net"
_SCHEMA = {"mode": "observed"}


@pytest.mark.parametrize(
    ("auth_fields", "expected_mode"),
    [
        pytest.param({"connection_string": "connection"}, "connection_string", id="connection-string"),
        pytest.param({"sas_token": "sas", "account_url": _ACCOUNT_URL}, "sas_token", id="sas-token"),
        pytest.param({"use_managed_identity": True, "account_url": _ACCOUNT_URL}, "managed_identity", id="managed-identity"),
        pytest.param(
            {
                "tenant_id": "tenant",
                "client_id": "client",
                "client_secret": "secret",
                "account_url": _ACCOUNT_URL,
            },
            "service_principal",
            id="service-principal",
        ),
    ],
)
def test_azure_auth_infers_legacy_shape_and_publishes_closed_mode(
    auth_fields: dict[str, object],
    expected_mode: str,
) -> None:
    config = AzureAuthConfig.model_validate(auth_fields)

    assert config.auth_mode == expected_mode
    assert config.auth_method == expected_mode
    assert AzureAuthConfig.model_json_schema()["properties"]["auth_mode"]["anyOf"][0]["enum"] == [
        "connection_string",
        "sas_token",
        "managed_identity",
        "service_principal",
    ]


def test_azure_auth_rejects_explicit_mode_that_disagrees_with_credentials() -> None:
    with pytest.raises(ValidationError, match=r"auth_mode.*does not match"):
        AzureAuthConfig(auth_mode="managed_identity", connection_string="connection")


def test_azure_auth_method_fails_closed_when_validation_is_bypassed() -> None:
    malformed = AzureAuthConfig.model_construct(auth_mode=None)

    with pytest.raises(RuntimeError, match="auth_mode invariant"):
        _ = malformed.auth_method


@pytest.mark.parametrize(
    ("auth_fields", "expected_mode"),
    [
        pytest.param({"connection_string": "connection"}, "connection_string", id="connection-string"),
        pytest.param({"sas_token": "sas", "account_url": _ACCOUNT_URL}, "sas_token", id="sas-token"),
        pytest.param({"use_managed_identity": True, "account_url": _ACCOUNT_URL}, "managed_identity", id="managed-identity"),
        pytest.param(
            {
                "tenant_id": "tenant",
                "client_id": "client",
                "client_secret": "secret",
                "account_url": _ACCOUNT_URL,
            },
            "service_principal",
            id="service-principal",
        ),
    ],
)
@pytest.mark.parametrize(
    "config_model",
    [AzureBlobSourceConfig, AzureBlobSinkConfig],
    ids=["source", "sink"],
)
def test_flattened_azure_blob_configs_infer_legacy_auth_mode(
    config_model: type[AzureBlobSourceConfig | AzureBlobSinkConfig],
    auth_fields: dict[str, object],
    expected_mode: str,
) -> None:
    common: dict[str, object] = {
        **auth_fields,
        "container": "container",
        "blob_path": "blob.csv",
        "schema": _SCHEMA,
    }
    if config_model is AzureBlobSourceConfig:
        common["on_validation_failure"] = "discard"

    config = config_model.from_dict(common)

    assert config.auth_mode == expected_mode


@pytest.mark.parametrize(
    "config_model",
    [AzureBlobSourceConfig, AzureBlobSinkConfig],
    ids=["source", "sink"],
)
def test_flattened_azure_blob_configs_pass_explicit_auth_mode(config_model: type[AzureBlobSourceConfig | AzureBlobSinkConfig]) -> None:
    common: dict[str, object] = {
        "auth_mode": "connection_string",
        "connection_string": "connection",
        "container": "container",
        "blob_path": "blob.csv",
        "schema": _SCHEMA,
    }
    if config_model is AzureBlobSourceConfig:
        common["on_validation_failure"] = "discard"

    config = config_model.from_dict(common)

    assert config.auth_mode == "connection_string"
    assert config_model.model_json_schema()["properties"]["auth_mode"]["anyOf"][0]["enum"] == [
        "connection_string",
        "sas_token",
        "managed_identity",
        "service_principal",
    ]


@pytest.mark.parametrize(
    "config_model",
    [AzureBlobSourceConfig, AzureBlobSinkConfig],
    ids=["source", "sink"],
)
def test_flattened_azure_blob_configs_reject_explicit_auth_mode_mismatch(
    config_model: type[AzureBlobSourceConfig | AzureBlobSinkConfig],
) -> None:
    common: dict[str, object] = {
        "auth_mode": "managed_identity",
        "connection_string": "connection",
        "container": "container",
        "blob_path": "blob.csv",
        "schema": _SCHEMA,
    }
    if config_model is AzureBlobSourceConfig:
        common["on_validation_failure"] = "discard"

    with pytest.raises(PluginConfigError, match=r"auth_mode.*does not match"):
        config_model.from_dict(common)


@pytest.mark.parametrize(
    ("auth_fields", "expected_mode"),
    [
        pytest.param({"api_key": "key"}, "api_key", id="api-key"),
        pytest.param({"use_managed_identity": True}, "managed_identity", id="managed-identity"),
    ],
)
def test_azure_search_infers_legacy_shape_and_publishes_closed_mode(
    auth_fields: dict[str, object],
    expected_mode: str,
) -> None:
    config = AzureSearchProviderConfig(endpoint=_SEARCH_ENDPOINT, index="index", **auth_fields)

    assert config.auth_mode == expected_mode
    assert AzureSearchProviderConfig.model_json_schema()["properties"]["auth_mode"]["anyOf"][0]["enum"] == [
        "api_key",
        "managed_identity",
    ]


def test_azure_search_rejects_explicit_mode_that_disagrees_with_credentials() -> None:
    with pytest.raises(ValidationError, match=r"auth_mode.*does not match"):
        AzureSearchProviderConfig(
            endpoint=_SEARCH_ENDPOINT,
            index="index",
            auth_mode="managed_identity",
            api_key="key",
        )


def test_rag_provider_schema_is_the_owned_closed_product_vocabulary() -> None:
    assert RAGRetrievalConfig.model_json_schema()["properties"]["provider"]["enum"] == ["azure_search", "chroma"]


def test_chroma_modes_are_central_owned_vocabularies_reused_by_configs() -> None:
    assert get_args(connection.ChromaConnectionMode) == ("persistent", "client")
    assert get_args(connection.ChromaSearchMode) == ("ephemeral", "persistent", "client")
    assert ChromaSearchProviderConfig.model_json_schema()["properties"]["mode"]["enum"] == list(get_args(connection.ChromaSearchMode))
    assert ChromaSinkConfig.model_json_schema()["properties"]["mode"]["enum"] == list(get_args(connection.ChromaConnectionMode))
