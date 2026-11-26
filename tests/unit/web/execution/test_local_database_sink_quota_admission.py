"""An allowlist cannot grant unmetered local database output in Web runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.web.composer import yaml_generator
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.execution.preflight import instantiate_runtime_plugins, make_policy_bound_sink_factory
from elspeth.web.plugin_policy.models import PluginId
from tests.unit.web.execution.test_preflight_side_effects import _web_settings
from tests.unit.web.plugin_policy.test_availability import _build_with_policy


def _state_with_output(tmp_path: Path, output: OutputSpec) -> CompositionState:
    input_dir = tmp_path / "blobs" / "test-session"
    input_dir.mkdir(parents=True)
    source_path = input_dir / "input.csv"
    source_path.write_text("id,text\none,Ada\n", encoding="utf-8")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="primary",
            options={"path": str(source_path), "schema": {"mode": "fixed", "fields": ["id: str", "text: str"]}},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(output,),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.mark.parametrize("driver", ["sqlite", "sqlite+pysqlite"])
def test_allowlisted_local_database_sink_cannot_bypass_same_admission(tmp_path: Path, driver: str) -> None:
    settings = _web_settings(tmp_path).model_copy(update={"plugin_allowlist": ("sink:database",)})
    policy, snapshot = _build_with_policy(settings)
    assert PluginId("sink", "database") in policy.authorized
    assert PluginId("sink", "database") in snapshot.available
    state = _state_with_output(
        tmp_path,
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
    # The csv output is a placeholder: the document's sink is replaced below.
    state = _state_with_output(
        tmp_path,
        OutputSpec(
            name="primary",
            plugin="csv",
            options={"path": str(tmp_path / "outputs" / "test-session" / "placeholder.csv"), "schema": {"mode": "observed"}},
            on_write_failure="discard",
        ),
    )
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
