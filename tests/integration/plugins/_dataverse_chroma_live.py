"""Shared support for the protected live-Dataverse and live-Chroma lanes (PB-09).

Not a test module: helpers only.  The generic production-boundary assembly
(``run_orchestrated_pipeline``: settings YAML -> ``instantiate_plugins_from_config``
-> ``ExecutionGraph`` -> runtime preflight -> public ``Orchestrator.run`` with a
real Landscape database, plus its Landscape evidence read-back) is reused from
``tests.integration.plugins._azure_live``, which pins those conventions for the
live-Azure lanes.  This module owns only what those lanes do not:

- the closed Dataverse/Chroma live-resource vocabulary
  (``scripts.state_engine_assessment_lib.selectors``: ``DATAVERSE_RESOURCES`` +
  ``CHROMA_RESOURCES`` + ``COMMON_LIVE_RESOURCES``) — reading a name outside
  that vocabulary is a hard error, and a missing value is a fixture-level
  ``pytest.skip``;
- the PB-09 variant inventories, pinned against the production Pydantic
  discriminators (``DataverseAuthConfig.method`` and ``ChromaConnectionMode``
  are the authority; the golden matrix mirrors them);
- Dataverse per-variant auth options.  The ``service_principal`` variant reads
  the azure-identity ambient credential trio (``AZURE_TENANT_ID`` /
  ``AZURE_CLIENT_ID`` / ``AZURE_CLIENT_SECRET``) — azure-identity's
  ``EnvironmentCredential`` contract owns these names, production documents
  them for service-principal config population
  (``src/elspeth/plugins/infrastructure/azure_auth.py`` ``example_use``), and
  the ``live_dataverse`` workflow job provisions the non-secret pair.  No env
  var name is invented here.
"""

from __future__ import annotations

import os
from typing import get_args

import pytest
from scripts.state_engine_assessment_lib.selectors import (
    CHROMA_RESOURCES,
    COMMON_LIVE_RESOURCES,
    DATAVERSE_RESOURCES,
)

from elspeth.plugins.infrastructure.clients.dataverse import DataverseAuthConfig
from elspeth.plugins.infrastructure.clients.retrieval.connection import ChromaConnectionMode

# PB-09 variant inventories, pinned against the production discriminators.
DATAVERSE_AUTH_VARIANTS: tuple[str, ...] = get_args(DataverseAuthConfig.model_fields["method"].annotation)
if DATAVERSE_AUTH_VARIANTS != ("service_principal", "managed_identity"):
    raise AssertionError(f"DataverseAuthConfig.method discriminator drifted: {DATAVERSE_AUTH_VARIANTS}")

CHROMA_SINK_MODE_VARIANTS: tuple[str, ...] = get_args(ChromaConnectionMode)
if CHROMA_SINK_MODE_VARIANTS != ("persistent", "client"):
    raise AssertionError(f"ChromaConnectionMode discriminator drifted: {CHROMA_SINK_MODE_VARIANTS}")

# Closed live-resource vocabulary (selectors.py is the single source of truth).
DATAVERSE_URL_ENV = "ELSPETH_TEST_DATAVERSE_URL"
DATAVERSE_ENTITY_ENV = "ELSPETH_TEST_DATAVERSE_ENTITY"
DATAVERSE_ROW_PREFIX_ENV = "ELSPETH_TEST_DATAVERSE_ROW_PREFIX"
CHROMA_HOST_ENV = "ELSPETH_TEST_CHROMA_HOST"
CHROMA_PORT_ENV = "ELSPETH_TEST_CHROMA_PORT"
CHROMA_SSL_ENV = "ELSPETH_TEST_CHROMA_SSL"
CHROMA_COLLECTION_PREFIX_ENV = "ELSPETH_TEST_CHROMA_COLLECTION_PREFIX"

_CLOSED_RESOURCE_VOCABULARY = frozenset(DATAVERSE_RESOURCES) | frozenset(CHROMA_RESOURCES) | frozenset(COMMON_LIVE_RESOURCES)
for _resource_name in (
    DATAVERSE_URL_ENV,
    DATAVERSE_ENTITY_ENV,
    DATAVERSE_ROW_PREFIX_ENV,
    CHROMA_HOST_ENV,
    CHROMA_PORT_ENV,
    CHROMA_SSL_ENV,
    CHROMA_COLLECTION_PREFIX_ENV,
):
    if _resource_name not in _CLOSED_RESOURCE_VOCABULARY:
        raise AssertionError(f"{_resource_name} is not in the closed live-resource vocabulary")

# azure-identity's EnvironmentCredential contract owns these names; the
# protected workflow supplies the non-secret pair as repository variables.
# They are the ambient credential channel for the service_principal variant,
# not an ELSPETH-invented vocabulary.
_AZURE_SDK_TENANT_ENV = "AZURE_TENANT_ID"
_AZURE_SDK_CLIENT_ENV = "AZURE_CLIENT_ID"
_AZURE_SDK_CLIENT_SECRET_ENV = "AZURE_CLIENT_SECRET"


def require_resource(name: str) -> str:
    """Read one closed-vocabulary resource variable, skipping when unset."""
    if name not in _CLOSED_RESOURCE_VOCABULARY:
        raise AssertionError(f"{name} is not in the closed live-resource vocabulary")
    value = os.environ.get(name)
    if value is None or not value.strip():
        pytest.skip(f"live lane resource {name} is not configured")
    return value.strip()


def dataverse_auth_options(variant: str) -> dict[str, object]:
    """Real config-discriminator fields for one Dataverse auth variant (or skip)."""
    if variant == "managed_identity":
        return {"method": variant}
    if variant == "service_principal":
        tenant_id = os.environ.get(_AZURE_SDK_TENANT_ENV)
        client_id = os.environ.get(_AZURE_SDK_CLIENT_ENV)
        client_secret = os.environ.get(_AZURE_SDK_CLIENT_SECRET_ENV)
        if not tenant_id or not client_id or not client_secret:
            pytest.skip(
                "ambient Azure service-principal credentials are not configured "
                f"({_AZURE_SDK_TENANT_ENV}/{_AZURE_SDK_CLIENT_ENV}/{_AZURE_SDK_CLIENT_SECRET_ENV})"
            )
        return {
            "method": variant,
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    raise AssertionError(f"unsupported Dataverse auth variant {variant!r}")
