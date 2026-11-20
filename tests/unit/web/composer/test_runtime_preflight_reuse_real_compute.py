"""Round-scoped preflight reuse against the real runtime preflight.

Every test here runs the real ``validate_pipeline`` (real plugin catalog,
real path allowlist, real secret phase) on a blob-bearing state. The secret
inventory comes from the real ``WebSecretService`` over a SQLite user store,
written through ``set_user_secret`` and ``delete_user_secret``, the calls the
``/api/secrets`` routes make. Each test changes one input between two
preflights and checks the rule the reuse must keep: what the round cache
hands back equals what a fresh compute returns or raises at that moment.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobIntegrityError, BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer import composer_preflight as preflight_module
from elspeth.web.composer.discovery_cache import RuntimePreflightCache
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.paths import NESTED_LOCAL_PATH_OPTION_KEYS, SINK_LOCAL_PATH_OPTION_KEYS, SOURCE_LOCAL_PATH_OPTION_KEYS
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.plugin_policy.profiles import (
    LoweredPluginConfig,
    OperatorProfileRegistry,
    OperatorProfileResolver,
    RuntimeWebPluginConfig,
)
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity

from ._helpers import _make_settings
from .test_runtime_preflight_blob_reuse import _BlobRows

pytestmark = pytest.mark.anyio

_USER = "user-1"


class _CountingValidatePipeline:
    """Delegates to the real ``validate_pipeline`` and counts computes."""

    def __init__(self, real: Callable[..., ValidationResult]) -> None:
        self.calls = 0
        self._real = real

    def __call__(self, *args: Any, **kwargs: Any) -> ValidationResult:
        self.calls += 1
        return self._real(*args, **kwargs)


@dataclass
class _RealRig:
    service: ComposerServiceImpl
    secrets: WebSecretService
    view: PolicyCatalogView
    snapshot: PluginAvailabilitySnapshot
    cache: RuntimePreflightCache
    context: SessionOperationContext
    session_id: UUID
    blobs_dir: Path
    outputs_dir: Path
    outside_dir: Path
    table: Path
    state: CompositionState
    computes: _CountingValidatePipeline
    blobs: _BlobRows

    async def cached(self, state: CompositionState | None = None) -> ValidationResult:
        target = self.state if state is None else state
        return await self.service._preflight.cached_runtime_preflight(
            target,
            user_id=_USER,
            session_id=str(self.session_id),
            cache=self.cache,
            initial_version=target.version,
            session_scope="session:real",
            plugin_snapshot=self.snapshot,
            session_operation_context=self.context,
        )

    def fresh(self, state: CompositionState | None = None) -> ValidationResult:
        """One uncached compute, as the base path runs it, not counted."""
        before = self.computes.calls
        try:
            return self.service._preflight.runtime_preflight(
                self.state if state is None else state,
                _USER,
                str(self.session_id),
                self.snapshot,
                session_operation_context=self.context,
            )
        finally:
            self.computes.calls = before

    def with_sink_options(self, **options: object) -> CompositionState:
        output = self.state.outputs[0]
        return replace(self.state, outputs=(replace(output, options={**dict(output.options), **options}),))

    def create_secret(self, name: str) -> None:
        self.secrets.set_user_secret(_USER, name, "synthetic-secret-value", auth_provider_type="local")

    def delete_secret(self, name: str) -> None:
        assert self.secrets.delete_user_secret(_USER, name, auth_provider_type="local") is True


@pytest.fixture
def real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RealRig:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "real-compute-reuse-test-key")
    session_id = uuid4()
    blob_id = uuid4()
    data_dir = tmp_path / "data"
    blobs_dir = data_dir / "blobs" / str(session_id)
    outputs_dir = data_dir / "outputs" / str(session_id)
    outside_dir = tmp_path / "outside"
    for directory in (blobs_dir, outputs_dir, outside_dir):
        directory.mkdir(parents=True)
    (blobs_dir / "input.csv").write_text("product\nhats\n", encoding="utf-8")
    body = b"sku,description\nhats,A fine hat\n"
    table = blobs_dir / "table.csv"
    table.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    record = BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="table.csv",
        mime_type="text/csv",
        size_bytes=len(body),
        content_hash=digest,
        storage_path=str(table),
        created_at=datetime(2026, 11, 20, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )
    # A file database: the preflight reads the inventory from worker threads.
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id=_USER)
    secrets = WebSecretService(
        user_store=UserSecretStore(engine=engine, master_key="real-compute-master-key-32-chars-min"),
        server_store=ServerSecretStore(allowlist=()),
    )
    blobs = _BlobRows(records={blob_id: record})
    service = ComposerServiceImpl.for_trained_operator(
        catalog=CatalogServiceImpl(get_shared_plugin_manager()),
        settings=_make_settings(data_dir=data_dir),
        blob_service=blobs,
        secret_service=ScopedSecretResolver(secrets, auth_provider_type="local"),
    )
    computes = _CountingValidatePipeline(preflight_module.validate_pipeline)
    monkeypatch.setattr(preflight_module, "validate_pipeline", computes)
    snapshot, view = service._policy_context.build(_USER)
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="enrich",
                node_type="transform",
                plugin="reference_join",
                input="rows",
                on_success="results",
                on_error="discard",
                options={
                    "reference_content": {"blob_ref": str(blob_id), "mode": "inline_content", "sha256": digest},
                    "reference_format": "csv",
                    "key_field": "product",
                    "reference_key_name": "sku",
                    "output": {"description": "ref['description']"},
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": str(outputs_dir / "results.jsonl"),
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "collision_policy": "fail_if_exists",
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name="real-compute-reuse"),
        version=1,
    )
    return _RealRig(
        service=service,
        secrets=secrets,
        view=view,
        snapshot=snapshot,
        cache=service._preflight.new_cache(mutation_epoch=view.mutation_epoch),
        context=SessionOperationContext(
            fence=SessionOperationFence(str(session_id), "operation-1", "lease-1", 1),
            operation_kind=SessionOperationKind.COMPOSE,
        ),
        session_id=session_id,
        blobs_dir=blobs_dir,
        outputs_dir=outputs_dir,
        outside_dir=outside_dir,
        table=table,
        state=state,
        computes=computes,
        blobs=blobs,
    )


def _error_messages(result: ValidationResult) -> list[str]:
    return [error.message for error in result.errors]


async def test_the_rig_state_is_valid_and_its_verdict_is_reused(real: _RealRig) -> None:
    """Positive control for every test below: the real compute runs and a repeat is reused."""
    first = await real.cached()
    second = await real.cached()

    assert first.is_valid is True, _error_messages(first)
    assert real.computes.calls == 1
    assert second == first == real.fresh()


# F1: a secret created or deleted over the secrets API between two preflights.


async def test_a_secret_created_between_two_preflights_is_seen(real: _RealRig) -> None:
    state = real.with_sink_options(api_key="${SINK_KEY}")
    first = await real.cached(state)
    assert first.is_valid is False
    assert any("literal value" in message for message in _error_messages(first))  # the fabricated-credential verdict

    real.create_secret("SINK_KEY")
    second = await real.cached(state)

    assert real.computes.calls == 2
    assert second == real.fresh(state)
    assert second != first


async def test_a_secret_deleted_between_two_preflights_is_seen(real: _RealRig) -> None:
    state = real.with_sink_options(api_key="${SINK_KEY}")
    real.create_secret("SINK_KEY")
    first = await real.cached(state)

    real.delete_secret("SINK_KEY")
    second = await real.cached(state)

    assert real.computes.calls == 2
    assert second == real.fresh(state)
    assert second != first


async def test_an_unrelated_secret_created_between_two_preflights_recomputes(real: _RealRig) -> None:
    """The inventory is compared whole, so any change misses; the verdict itself does not move."""
    first = await real.cached()

    real.create_secret("UNRELATED_KEY")
    second = await real.cached()

    assert real.computes.calls == 2
    assert second == real.fresh() == first


# F5: filesystem state outside blob storage.


async def test_a_symlink_created_at_the_sink_path_is_seen(real: _RealRig) -> None:
    first = await real.cached()

    (real.outputs_dir / "results.jsonl").symlink_to(real.outside_dir / "results.jsonl")
    second = await real.cached()

    assert real.computes.calls == 2
    assert second == real.fresh()
    assert first.is_valid is True and second.is_valid is False


async def test_a_symlink_swapped_in_at_the_source_path_is_seen(real: _RealRig) -> None:
    first = await real.cached()

    source = real.blobs_dir / "input.csv"
    os.replace(source, real.outside_dir / "input.csv")
    source.symlink_to(real.outside_dir / "input.csv")
    second = await real.cached()

    assert real.computes.calls == 2
    assert second == real.fresh()
    assert first.is_valid is True and second.is_valid is False


async def test_a_symlink_at_the_session_scoped_sink_path_is_seen(real: _RealRig) -> None:
    """The authoring form ``outputs/<name>`` resolves under the session's own output folder."""
    state = real.with_sink_options(path="outputs/results.jsonl")
    first = await real.cached(state)

    (real.outputs_dir / "results.jsonl").symlink_to(real.outside_dir / "results.jsonl")
    second = await real.cached(state)

    assert real.computes.calls == 2
    assert second == real.fresh(state)
    assert first.is_valid is True and second.is_valid is False


async def test_a_symlink_created_at_a_provider_config_path_is_seen(real: _RealRig) -> None:
    node = real.state.nodes[0]
    persist_directory = real.outputs_dir / "index"
    state = replace(
        real.state,
        nodes=(replace(node, options={**dict(node.options), "provider_config": {"persist_directory": str(persist_directory)}}),),
    )
    first = await real.cached(state)

    persist_directory.symlink_to(real.outside_dir, target_is_directory=True)
    second = await real.cached(state)

    assert real.computes.calls == 2
    assert second == real.fresh(state)
    assert second != first
    assert any("persist_directory" in message for message in _error_messages(second))


async def test_a_plain_file_appearing_at_the_sink_path_does_not_change_the_verdict(real: _RealRig) -> None:
    """The preflight builds plugins in preflight mode and does not look for an existing output file.

    The sink is ``collision_policy: fail_if_exists``, the case most likely to
    depend on the file, and the fresh compute agrees with the reuse.
    """
    first = await real.cached()

    (real.outputs_dir / "results.jsonl").write_text('{"existing": true}\n', encoding="utf-8")
    second = await real.cached()

    assert real.computes.calls == 1
    assert second == real.fresh() == first


# F2 with the real content read: stored bytes rewritten in place, mtime restored.


async def test_stored_bytes_rewritten_with_the_mtime_restored_raise_as_a_fresh_compute_does(real: _RealRig) -> None:
    await real.cached()
    stat = real.table.stat()
    body = real.table.read_bytes()
    with real.table.open("r+b") as handle:
        handle.write(body.upper())
    os.utime(real.table, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    with pytest.raises(ComposerRuntimePreflightError) as reused:
        await real.cached()
    with pytest.raises(BlobIntegrityError):
        real.fresh()

    assert type(reused.value.original_exc) is BlobIntegrityError
    assert real.computes.calls == 2


async def test_a_row_change_seen_only_by_the_content_read_is_not_stored(real: _RealRig) -> None:
    """The row the content read returns is compute evidence too.

    Reads in one compute: the evidence before-read, the compute's metadata
    read, its content read, the after-read. Only the content read sees a
    changed row, which then reverts, so both bracket reads agree.
    """
    blob_id = next(iter(real.blobs.records))
    changed = replace(real.blobs.records[blob_id], mime_type="text/plain")
    real.blobs.transient = {real.blobs.reads + 3: changed}
    first = await real.cached()
    assert real.blobs.reads == 4
    assert first.is_valid is False
    assert any("changed during validation" in message for message in _error_messages(first))

    second = await real.cached()

    assert real.computes.calls == 2
    assert second == real.fresh()
    assert second.is_valid is True


# The policy-view memo on the same inputs.


async def test_the_policy_view_verdict_does_not_depend_on_sink_paths_or_secrets(real: _RealRig) -> None:
    catalog = CatalogServiceImpl(get_shared_plugin_manager())
    memo = PolicyCatalogView.for_trained_operator(catalog, real.snapshot, validation_memo_enabled=True)
    state = real.with_sink_options(api_key="${SINK_KEY}")
    first = memo.validate_composition_state(state)

    (real.outputs_dir / "results.jsonl").symlink_to(real.outside_dir / "results.jsonl")
    real.create_secret("SINK_KEY")

    fresh = PolicyCatalogView.for_trained_operator(catalog, real.snapshot).validate_composition_state(state)
    assert memo.validate_composition_state(state) is first
    assert fresh == first


# A5: the path evidence walks the AUTHORED state, the preflight's path phase walks the
# profile-LOWERED one. They agree only while no operator-profile lowering adds or rewrites a
# local path option, so that is pinned here against the live profile registry.

_PATH_PIN_SENTINEL = "outputs/path-pin"

_PATH_PIN_INPUTS: dict[PluginId, tuple[str, dict[str, object]]] = {
    PluginId("source", "llm"): ("pin-llm", {"prompt_template": "{{ row.text }}", "schema": {"mode": "observed"}}),
    PluginId("transform", "llm"): ("pin-llm", {"prompt_template": "{{ row.text }}", "schema": {"mode": "observed"}}),
    PluginId("transform", "aws_bedrock_prompt_shield"): ("pin-prompt-shield", {"fields": ["prompt"], "schema": {"mode": "observed"}}),
    PluginId("transform", "aws_bedrock_content_safety"): ("pin-content-safety", {"fields": ["prompt"], "schema": {"mode": "observed"}}),
    PluginId("source", "aws_s3"): ("pin-s3", {"key": "incoming/rows.csv", "format": "csv", "schema": {"mode": "observed"}}),
    PluginId("transform", "aws_textract_document_analysis"): (
        "pin-textract",
        {"key_field": "document_key", "feature_types": ["FORMS"], "schema": {"mode": "observed"}},
    ),
    PluginId("transform", "azure_ai_search"): (
        "pin-search",
        {"output_prefix": "policy", "query_field": "question", "index": "approved-documents", "schema": {"mode": "observed"}},
    ),
}


def _every_kind_profile_registry() -> OperatorProfileRegistry:
    """A registry with a profile of every kind the settings can carry."""
    runtime = RuntimeWebPluginConfig.from_settings(
        _make_settings(
            deployment_aws_region="ap-southeast-1",
            llm_profiles={"pin-llm": {"provider": "bedrock", "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0"}},
            default_llm_profile="pin-llm",
            bedrock_guardrail_profiles=[
                {
                    "alias": "pin-prompt-shield",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "pinguardrail",
                    "guardrail_version": "1",
                    "region": "ap-southeast-1",
                },
                {
                    "alias": "pin-content-safety",
                    "plugin": "aws_bedrock_content_safety",
                    "guardrail_identifier": "pinguardrail",
                    "guardrail_version": "1",
                    "region": "ap-southeast-1",
                },
            ],
            aws_s3_source_profiles=[{"alias": "pin-s3", "bucket": "pin-bucket", "prefix": "pin"}],
            aws_textract_profiles=[{"alias": "pin-textract", "bucket": "pin-bucket", "key_prefix": "pin"}],
            azure_search_profiles=[
                {
                    "alias": "pin-search",
                    "endpoint": "https://pin.search.windows.net",
                    "auth": "managed_identity",
                    "client_id": "11111111-2222-3333-4444-555555555555",
                    "indexes": ["approved-documents"],
                }
            ],
        )
    )
    return OperatorProfileRegistry(
        policy=compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime),
        settings=runtime,
    )


def _local_path_options(options: Mapping[str, object]) -> dict[str, object]:
    """The options the evidence walk reads, under the keys it reads them by."""
    found = {key: options[key] for key in (*SOURCE_LOCAL_PATH_OPTION_KEYS, *SINK_LOCAL_PATH_OPTION_KEYS) if key in options}
    provider_config = options["provider_config"] if "provider_config" in options else None
    if isinstance(provider_config, Mapping):
        found.update({f"provider_config.{key}": provider_config[key] for key in NESTED_LOCAL_PATH_OPTION_KEYS if key in provider_config})
    return found


def _profile_path_drift(registry: OperatorProfileRegistry) -> list[str]:
    """Every profile kind whose lowering adds or rewrites a local path option, named with the key."""
    drift: list[str] = []
    for plugin_id in sorted(registry._resolvers, key=lambda pid: f"{pid.kind}:{pid.name}"):
        kind = f"{plugin_id.kind}:{plugin_id.name}"
        if plugin_id not in _PATH_PIN_INPUTS:
            drift.append(f"{kind}: new profile kind; give this pin an input for it")
            continue
        alias, safe_options = _PATH_PIN_INPUTS[plugin_id]
        # The bare input must lower, or this kind would be checked by nothing.
        authored_variants = [dict(safe_options)]
        authored_variants += [
            {**safe_options, key: _PATH_PIN_SENTINEL} for key in (*SOURCE_LOCAL_PATH_OPTION_KEYS, *SINK_LOCAL_PATH_OPTION_KEYS)
        ]
        authored_variants += [{**safe_options, "provider_config": {key: _PATH_PIN_SENTINEL}} for key in NESTED_LOCAL_PATH_OPTION_KEYS]
        for index, authored in enumerate(authored_variants):
            try:
                lowered = registry.lower_options(plugin_id, alias=alias, safe_options=dict(authored))
            except ValueError:
                if index == 0:
                    raise
                continue  # the kind refuses this option, so no author can place it on a profile-bound node
            before = _local_path_options(authored)
            after = _local_path_options(deep_thaw(lowered.executable_options))
            for key in sorted(set(before) | set(after)):
                if (key in before) != (key in after) or (key in before and before[key] != after[key]):
                    drift.append(f"{kind}: profile lowering adds or rewrites local path option '{key}'")
    return drift


def test_no_operator_profile_lowering_adds_or_rewrites_a_local_path_option() -> None:
    registry = _every_kind_profile_registry()

    assert set(registry._resolvers) == set(_PATH_PIN_INPUTS)  # every kind is configured and exercised
    assert _profile_path_drift(registry) == []


class _PathLoweringResolver:
    """A test-local profile kind that lowers a local path, to prove the pin can fail."""

    def __init__(self, inner: OperatorProfileResolver) -> None:
        self._inner = inner

    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        lowered = self._inner.lower_options(alias, safe_options)
        executable = {**deep_thaw(lowered.executable_options), "provider_config": {"persist_directory": "/elsewhere/index"}}
        return replace(lowered, executable_options=MappingProxyType(executable))


def test_the_path_pin_names_a_profile_kind_that_lowers_a_path(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _every_kind_profile_registry()
    plugin_id = PluginId("transform", "azure_ai_search")
    monkeypatch.setitem(
        registry._resolvers, plugin_id, cast(OperatorProfileResolver, _PathLoweringResolver(registry._resolvers[plugin_id]))
    )

    drift = _profile_path_drift(registry)

    assert "transform:azure_ai_search: profile lowering adds or rewrites local path option 'provider_config.persist_directory'" in drift
    assert all(entry.startswith("transform:azure_ai_search:") for entry in drift)
