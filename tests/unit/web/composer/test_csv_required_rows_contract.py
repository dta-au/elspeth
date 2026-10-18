"""Required CSV retention is exposed and preserved by ordinary authoring."""

from elspeth.core.config import SinkSettings
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.sinks.csv_sink import CSVSinkConfig
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_generator import generate_pipeline_dict


def test_csv_catalog_exposes_required_pipeline_row_policy() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    schema = CatalogServiceImpl(manager).get_schema("sink", "csv")
    option = schema.json_schema["properties"]["require_all_rows"]
    assert option["type"] == "boolean"
    assert option["default"] is False
    assert "pipeline rows" in option["description"]
    assert "before publication" in option["description"]
    assert any("require_all_rows=true" in hint for hint in schema.composer_hints)


def test_csv_policy_survives_saved_state_and_runtime_lowering() -> None:
    state = CompositionState(
        sources={
            "input": SourceSpec(
                plugin="csv",
                options={"path": "input.csv", "schema": {"mode": "observed"}},
                on_success="archive",
                on_validation_failure="archive",
            )
        },
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="archive",
                plugin="csv",
                options={
                    "path": "retained.csv",
                    "schema": {"mode": "observed"},
                    "collision_policy": "auto_increment",
                    "require_all_rows": True,
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )
    restored = CompositionState.from_dict(state.to_dict())
    assert restored == state
    runtime = generate_pipeline_dict(restored)
    sink = SinkSettings.model_validate(runtime["sinks"]["archive"])
    assert sink.on_write_failure == "discard"
    assert sink.options["require_all_rows"] is True
    assert CSVSinkConfig.from_dict(sink.options).require_all_rows is True
