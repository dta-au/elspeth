"""Shared support for the protected live-Azure plugin lanes (PB-09).

Not a test module: helpers only.  Every helper keeps the lane discipline:

- Resource environment variable names come from the closed vocabulary in
  ``scripts.state_engine_assessment_lib.selectors`` (``AZURE_RESOURCES`` +
  ``COMMON_LIVE_RESOURCES``); reading a name outside that vocabulary is a
  hard error, and a missing value is a fixture-level ``pytest.skip`` (the
  evidence gate treats skips as non-promotable).
- Secret material for the safety/document transforms is read only through the
  plugin's own production-declared ``discovery_secret_requirements`` names.
- The runner's ambient Azure identity (``DefaultAzureCredential``, plus the
  azure-identity ``EnvironmentCredential`` variables for the service-principal
  variant) is the only other credential channel — the same posture as the AWS
  lanes' default credential chain.  No env var name is invented here.
- Pipelines cross the production boundary: real YAML settings loading,
  ``instantiate_plugins_from_config``, ``ExecutionGraph``, runtime preflight,
  and the public ``Orchestrator.run`` against a real Landscape database
  (the lane's PostgreSQL when configured, a real SQLite file otherwise).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, get_args

import pytest
import yaml
from scripts.state_engine_assessment_lib.selectors import AZURE_RESOURCES, COMMON_LIVE_RESOURCES
from sqlalchemy import func, select

from elspeth.contracts import RunStatus
from elspeth.contracts.config.runtime import RuntimeRateLimitConfig
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    sink_effects_table,
    token_outcomes_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.rate_limit.registry import RateLimitRegistry
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure.azure_auth import AzureAuthMethod
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config

if TYPE_CHECKING:
    from azure.storage.blob import BlobServiceClient

    from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform

# The four azure_blob PB-09 auth variants, pinned against the production
# Pydantic discriminator (the golden matrix mirrors this set; the Literal is
# the authority).
AZURE_BLOB_AUTH_VARIANTS: tuple[str, ...] = get_args(AzureAuthMethod)
if AZURE_BLOB_AUTH_VARIANTS != ("connection_string", "sas_token", "managed_identity", "service_principal"):
    raise AssertionError(f"AzureAuthMethod discriminator drifted: {AZURE_BLOB_AUTH_VARIANTS}")

# Closed live-resource vocabulary (selectors.py is the single source of truth).
STORAGE_ACCOUNT_URL_ENV = "ELSPETH_TEST_AZURE_STORAGE_ACCOUNT_URL"
STORAGE_CONTAINER_ENV = "ELSPETH_TEST_AZURE_STORAGE_CONTAINER"
STORAGE_BLOB_PREFIX_ENV = "ELSPETH_TEST_AZURE_STORAGE_BLOB_PREFIX"
CONTENT_SAFETY_ENDPOINT_ENV = "ELSPETH_TEST_AZURE_CONTENT_SAFETY_ENDPOINT"
DOCUMENT_INTELLIGENCE_ENDPOINT_ENV = "ELSPETH_TEST_AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
DOCUMENT_INTELLIGENCE_MODEL_ENV = "ELSPETH_TEST_AZURE_DOCUMENT_INTELLIGENCE_MODEL"
DOCUMENT_URL_ENV = "ELSPETH_TEST_AZURE_DOCUMENT_URL"
LANDSCAPE_POSTGRES_URL_ENV = "ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL"

_CLOSED_RESOURCE_VOCABULARY = frozenset(AZURE_RESOURCES) | frozenset(COMMON_LIVE_RESOURCES)
for _resource_name in (
    STORAGE_ACCOUNT_URL_ENV,
    STORAGE_CONTAINER_ENV,
    STORAGE_BLOB_PREFIX_ENV,
    CONTENT_SAFETY_ENDPOINT_ENV,
    DOCUMENT_INTELLIGENCE_ENDPOINT_ENV,
    DOCUMENT_INTELLIGENCE_MODEL_ENV,
    DOCUMENT_URL_ENV,
    LANDSCAPE_POSTGRES_URL_ENV,
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


@dataclass(frozen=True, slots=True)
class AzureStorageTarget:
    """Operator-approved Azure Blob Storage location for disposable test blobs."""

    account_url: str
    container: str
    blob_prefix: str

    def unique_blob_path(self, leaf: str) -> str:
        """Uniquely-prefixed blob path for one disposable remote object."""
        return f"{self.blob_prefix}/elspeth-state-engine-live/{uuid.uuid4().hex}/{leaf}"


def require_resource(name: str) -> str:
    """Read one closed-vocabulary resource variable, skipping when unset."""
    if name not in _CLOSED_RESOURCE_VOCABULARY:
        raise AssertionError(f"{name} is not in the closed live-resource vocabulary")
    value = os.environ.get(name)
    if value is None or not value.strip():
        pytest.skip(f"live Azure resource {name} is not configured")
    return value.strip()


def require_storage_target() -> AzureStorageTarget:
    """Resolve the live storage target from the closed vocabulary (or skip)."""
    return AzureStorageTarget(
        account_url=require_resource(STORAGE_ACCOUNT_URL_ENV),
        container=require_resource(STORAGE_CONTAINER_ENV),
        blob_prefix=require_resource(STORAGE_BLOB_PREFIX_ENV).strip("/"),
    )


def require_declared_secret(plugin_type: type[BaseSource] | type[BaseTransform] | type[BaseSink], option: str = "api_key") -> str:
    """Read a secret through the plugin's production-declared env names (or skip)."""
    names = plugin_type.discovery_secret_requirements[option]
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value
    pytest.skip(f"{plugin_type.name} secret {option} is not configured (declared names: {', '.join(names)})")


def ambient_blob_service_client(target: AzureStorageTarget) -> BlobServiceClient:
    """Support-only client from the runner's ambient identity (fixture setup/cleanup)."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    return BlobServiceClient(target.account_url, credential=DefaultAzureCredential())


def delete_blob_if_present(service: BlobServiceClient, target: AzureStorageTarget, blob_path: str) -> None:
    """Cleanup helper tolerating a blob the failed test never created."""
    from azure.core.exceptions import ResourceNotFoundError

    with contextlib.suppress(ResourceNotFoundError):
        service.get_container_client(target.container).delete_blob(blob_path)


def mint_container_sas(target: AzureStorageTarget) -> str:
    """Mint a short-lived user-delegation SAS from the runner's ambient identity.

    The SAS (and the connection string built from it) is genuine variant
    credential material: the plugin's ``sas_token`` / ``connection_string``
    discriminator arms consume it through their real client constructors.
    """
    from azure.storage.blob import ContainerSasPermissions, generate_container_sas

    start = datetime.now(UTC) - timedelta(minutes=5)
    expiry = datetime.now(UTC) + timedelta(hours=1)
    with ambient_blob_service_client(target) as service:
        delegation_key = service.get_user_delegation_key(start, expiry)
        account_name = service.account_name
        if type(account_name) is not str or not account_name:
            raise RuntimeError("Azure BlobServiceClient did not expose an account name")
        return generate_container_sas(
            account_name=account_name,
            container_name=target.container,
            user_delegation_key=delegation_key,
            permission=ContainerSasPermissions(read=True, add=True, create=True, write=True, delete=True, list=True),
            start=start,
            expiry=expiry,
        )


def auth_options(variant: str, target: AzureStorageTarget) -> dict[str, object]:
    """Real config-discriminator fields for one azure_blob auth variant (or skip)."""
    if variant == "managed_identity":
        return {"auth_mode": variant, "use_managed_identity": True, "account_url": target.account_url}
    if variant == "sas_token":
        return {"auth_mode": variant, "sas_token": mint_container_sas(target), "account_url": target.account_url}
    if variant == "connection_string":
        connection_string = f"BlobEndpoint={target.account_url};SharedAccessSignature={mint_container_sas(target)}"
        return {"auth_mode": variant, "connection_string": connection_string}
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
            "auth_mode": variant,
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "account_url": target.account_url,
        }
    raise AssertionError(f"unsupported azure_blob auth variant {variant!r}")


def ensure_fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide the audit fingerprint key when the environment carries none.

    Credential-bearing plugin options are fingerprinted into the audit trail;
    without a key the run fails closed.  A CI-provided key always wins.
    """
    if not os.environ.get("ELSPETH_FINGERPRINT_KEY"):
        monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "live-azure-lane-fingerprint-key")


def local_jsonl_sink_options(path: Path) -> dict[str, object]:
    """Local JSON sink options used to observe subject-source/transform output."""
    return {
        "path": str(path),
        "format": "jsonl",
        "mode": "write",
        "collision_policy": "auto_increment",
        "schema": {"mode": "observed"},
    }


def jsonl_source_options(path: Path) -> dict[str, object]:
    """Local JSON source options used to feed a subject transform or sink."""
    return {
        "path": str(path),
        "format": "jsonl",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass(frozen=True, slots=True)
class OrchestratedRunEvidence:
    """Landscape-backed evidence of one production-boundary run."""

    status: RunStatus
    run_id: str
    node_state_count: int
    token_outcome_count: int
    sink_effect_states: tuple[str, ...]
    artifact_count: int

    def assert_completed_lifecycle(self) -> None:
        if self.status is not RunStatus.COMPLETED:
            raise AssertionError(f"live run {self.run_id} finished {self.status!r}, not COMPLETED")
        if self.node_state_count <= 0 or self.token_outcome_count <= 0:
            raise AssertionError(f"live run {self.run_id} recorded no node states or token outcomes")
        if not self.sink_effect_states or set(self.sink_effect_states) != {"finalized"}:
            raise AssertionError(f"live run {self.run_id} sink effects were {self.sink_effect_states!r}, not all finalized")
        if self.artifact_count <= 0:
            raise AssertionError(f"live run {self.run_id} recorded no artifacts")


def landscape_url(tmp_path: Path) -> str:
    """The lane's PostgreSQL Landscape when configured; a real SQLite file otherwise."""
    live_url = os.environ.get(LANDSCAPE_POSTGRES_URL_ENV)
    if live_url is not None and live_url.strip():
        return live_url.strip()
    return f"sqlite:///{tmp_path / 'audit.db'}"


def run_orchestrated_pipeline(pipeline: dict[str, object], tmp_path: Path) -> OrchestratedRunEvidence:
    """Run one pipeline through the full production boundary and read back evidence.

    YAML settings loading -> instantiate_plugins_from_config -> ExecutionGraph ->
    runtime preflight -> public Orchestrator.run with a real Landscape database.
    """
    store = FilesystemPayloadStore(tmp_path / "payloads")
    document = dict(pipeline)
    document["landscape"] = {"url": landscape_url(tmp_path)}
    document["payload_store"] = {"backend": "filesystem", "base_path": str(store.base_path)}

    settings = load_settings_from_yaml_string(yaml.safe_dump(document, sort_keys=False))
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=bundle.sink_effect_modes,
    )

    database_url = settings.landscape.url
    db = LandscapeDB.from_url(database_url, create_tables=database_url.startswith("sqlite:"))
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        result = Orchestrator(db, rate_limit_registry=rate_limits).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=store,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
        with db.engine.connect() as connection:
            node_state_count = connection.scalar(
                select(func.count()).select_from(node_states_table).where(node_states_table.c.run_id == result.run_id)
            )
            token_outcome_count = connection.scalar(
                select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)
            )
            sink_effect_states = tuple(
                connection.execute(select(sink_effects_table.c.state).where(sink_effects_table.c.run_id == result.run_id)).scalars()
            )
            artifact_count = connection.scalar(
                select(func.count()).select_from(artifacts_table).where(artifacts_table.c.run_id == result.run_id)
            )
        return OrchestratedRunEvidence(
            status=result.status,
            run_id=result.run_id,
            node_state_count=int(node_state_count) if node_state_count is not None else 0,
            token_outcome_count=int(token_outcome_count) if token_outcome_count is not None else 0,
            sink_effect_states=sink_effect_states,
            artifact_count=int(artifact_count) if artifact_count is not None else 0,
        )
    finally:
        rate_limits.close()
        db.close()
