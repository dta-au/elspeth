"""An allowlist cannot grant unmetered local database output in Web runs."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.preflight import instantiate_runtime_plugins, make_policy_bound_sink_factory, resolve_runtime_yaml_paths
from elspeth.web.execution.validation import validate_pipeline
from elspeth.web.plugin_policy.models import PluginId
from tests.unit.web.execution.test_preflight_side_effects import _web_settings
from tests.unit.web.plugin_policy.test_availability import _build_with_policy


def _chroma_state(tmp_path: Path, *, persistent: bool) -> CompositionState:
    input_dir = tmp_path / "blobs" / "test-session"
    input_dir.mkdir(parents=True)
    source_path = input_dir / "input.csv"
    source_path.write_text("id,text\none,Ada\n", encoding="utf-8")
    options = {
        "collection": "documents",
        "mode": "persistent" if persistent else "client",
        "field_mapping": {"id_field": "id", "document_field": "text"},
        "schema": {"mode": "fixed", "fields": ["id: str", "text: str"]},
    }
    if persistent:
        options["persist_directory"] = str(tmp_path / "outputs" / "test-session" / "chroma")
    else:
        options["host"] = "8.8.8.8"
        options["ssl"] = True
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(source_path), "schema": {"mode": "fixed", "fields": ["id: str", "text: str"]}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(OutputSpec(name="primary", plugin="chroma_sink", options=options, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.mark.parametrize("persistent", [True, False])
def test_explicitly_allowlisted_chroma_readiness_refuses_only_unmetered_local_mode(tmp_path: Path, persistent: bool) -> None:
    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:chroma_sink",)})
    policy, snapshot = _build_with_policy(settings)
    assert PluginId("sink", "chroma_sink") in policy.authorized
    assert PluginId("sink", "chroma_sink") in snapshot.available
    state = _chroma_state(tmp_path, persistent=persistent)
    with (
        patch("elspeth.plugins.sinks.chroma_sink.chromadb.PersistentClient") as local_client,
        patch("elspeth.plugins.sinks.chroma_sink.chromadb.HttpClient") as remote_client,
    ):
        result = validate_pipeline(
            state,
            settings,
            yaml_generator,
            plugin_snapshot=snapshot,
            profile_registry=None,
            catalog=create_catalog_service(),
            session_id="test-session",
        )
    local_client.assert_not_called()
    remote_client.assert_not_called()
    if persistent:
        assert not result.is_valid
        assert not result.readiness.execution_ready
        assert any("storage quotas" in error.message and "mode='client'" in error.message for error in result.errors)
        assert not (tmp_path / "outputs" / "test-session" / "chroma").exists()
    else:
        assert result.is_valid, result.errors
        assert result.readiness.execution_ready, result.readiness.blockers


def test_chroma_persistent_runtime_and_delayed_factory_refuse_before_construction_but_cli_is_unchanged(tmp_path: Path) -> None:
    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:chroma_sink",)})
    _policy, snapshot = _build_with_policy(settings)
    state = _chroma_state(tmp_path, persistent=True)
    runtime = load_settings_from_yaml_string(
        resolve_runtime_yaml_paths(yaml_generator.generate_yaml(state), str(tmp_path), session_id="test-session")
    )
    with patch("elspeth.web.execution.preflight.instantiate_plugins_from_config") as construct:
        with pytest.raises(PluginConfigError, match="storage quotas"):
            instantiate_runtime_plugins(runtime, plugin_snapshot=snapshot)
        construct.assert_not_called()
    with pytest.raises(PluginConfigError, match="storage quotas"):
        make_policy_bound_sink_factory(runtime, plugin_snapshot=snapshot)("primary")
    bundle = instantiate_plugins_from_config(runtime, preflight_mode=True)
    assert bundle.sinks["primary"].name == "chroma_sink"
    assert not (tmp_path / "outputs" / "test-session" / "chroma").exists()


@pytest.mark.parametrize("driver", ["sqlite", "sqlite+pysqlite"])
def test_allowlisted_local_database_sink_cannot_bypass_same_admission(tmp_path: Path, driver: str) -> None:
    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:database",)})
    policy, snapshot = _build_with_policy(settings)
    assert PluginId("sink", "database") in policy.authorized
    assert PluginId("sink", "database") in snapshot.available
    state = _chroma_state(tmp_path, persistent=True)
    state = replace(
        state,
        outputs=(
            OutputSpec(
                name="primary",
                plugin="database",
                options={
                    "url": f"{driver}:///{tmp_path / 'output.db'}",
                    "table": "records",
                    "schema": {"mode": "observed"},
                    "effect_ledger": {"table": "_elspeth_effects", "schema_version": 1, "permissions": ["select", "insert"]},
                },
                on_write_failure="discard",
            ),
        ),
    )
    runtime = load_settings_from_yaml_string(yaml_generator.generate_yaml(state))
    with pytest.raises(PluginConfigError, match="storage quotas"):
        instantiate_runtime_plugins(runtime, plugin_snapshot=snapshot)
    with pytest.raises(PluginConfigError, match="PostgreSQL"):
        make_policy_bound_sink_factory(runtime, plugin_snapshot=snapshot)("primary")
    bundle = instantiate_plugins_from_config(runtime, preflight_mode=True)
    assert bundle.sinks["primary"].name == "database"
    assert not (tmp_path / "output.db").exists()


def test_remote_postgresql_database_sink_retains_runtime_and_delayed_factory_admission(tmp_path: Path) -> None:
    import yaml

    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:database",)})
    _policy, snapshot = _build_with_policy(settings)
    state = _chroma_state(tmp_path, persistent=False)
    document = yaml.safe_load(yaml_generator.generate_yaml(state))
    document["sinks"]["primary"] = {
        "plugin": "database",
        "on_write_failure": "discard",
        "options": {
            "url": "postgresql+psycopg://8.8.8.8/records",
            "table": "records",
            "effect_ledger": {"table": "_elspeth_effects", "schema_version": 1, "permissions": ["select", "insert"]},
            "schema": {"mode": "observed"},
        },
    }
    runtime = load_settings_from_yaml_string(yaml.safe_dump(document))
    bundle = instantiate_runtime_plugins(runtime, plugin_snapshot=snapshot)
    assert bundle.sinks["primary"].name == "database"
    binding = make_policy_bound_sink_factory(runtime, plugin_snapshot=snapshot)("primary")
    assert binding.sink.name == "database"


@pytest.mark.parametrize("persistent", [True, False])
def test_chroma_retrieval_readiness_refuses_local_initialization_and_preserves_remote_client(tmp_path: Path, persistent: bool) -> None:
    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("transform:rag_retrieval",)})
    policy, snapshot = _build_with_policy(settings)
    assert PluginId("transform", "rag_retrieval") in policy.authorized
    assert PluginId("transform", "rag_retrieval") in snapshot.available
    original = _chroma_state(tmp_path, persistent=persistent)
    provider_config = {"collection": "documents", "mode": "persistent" if persistent else "client"}
    if persistent:
        provider_config["persist_directory"] = str(tmp_path / "outputs" / "test-session" / "retrieval")
    else:
        provider_config["host"] = "8.8.8.8"
    state = replace(
        original,
        sources={name: replace(source, on_success="rag_input") for name, source in original.sources.items()},
        nodes=(
            NodeSpec(
                id="retrieve",
                node_type="transform",
                plugin="rag_retrieval",
                input="rag_input",
                on_success="primary",
                on_error="discard",
                options={
                    "provider": "chroma",
                    "provider_config": provider_config,
                    "query_field": "text",
                    "output_prefix": "found",
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
                name="primary",
                plugin="csv",
                options={"path": str(tmp_path / "outputs" / "test-session" / "result.csv"), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
    )
    with patch("chromadb.PersistentClient") as persistent_client, patch("chromadb.HttpClient") as remote_client:
        result = validate_pipeline(
            state,
            settings,
            yaml_generator,
            plugin_snapshot=snapshot,
            profile_registry=None,
            catalog=create_catalog_service(),
            session_id="test-session",
        )
    persistent_client.assert_not_called()
    remote_client.assert_not_called()
    if persistent:
        assert not result.is_valid
        assert not result.readiness.execution_ready
        assert any("storage quotas" in error.message and "mode='client'" in error.message for error in result.errors)
    else:
        assert result.is_valid, result.errors
        assert result.readiness.execution_ready, result.readiness.blockers


@pytest.mark.parametrize("persistent", [True, False])
def test_web_collection_probes_refuse_local_chroma_before_construction(tmp_path: Path, persistent: bool) -> None:
    import yaml

    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:chroma_sink",)})
    _policy, snapshot = _build_with_policy(settings)
    state = _chroma_state(tmp_path, persistent=False)
    document = yaml.safe_load(yaml_generator.generate_yaml(state))
    provider_config = (
        {"mode": "persistent", "persist_directory": str(tmp_path / "probe-db")} if persistent else {"mode": "client", "host": "8.8.8.8"}
    )
    document["collection_probes"] = [{"collection": "documents", "provider": "chroma", "provider_config": provider_config}]
    runtime = load_settings_from_yaml_string(yaml.safe_dump(document))
    if persistent:
        with pytest.raises(PluginConfigError, match="storage quotas"):
            instantiate_runtime_plugins(runtime, plugin_snapshot=snapshot)
    else:
        bundle = instantiate_runtime_plugins(runtime, plugin_snapshot=snapshot)
        assert bundle.sinks["primary"].name == "chroma_sink"
    assert not (tmp_path / "probe-db").exists()
