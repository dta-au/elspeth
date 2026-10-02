"""Observed CSV rows must satisfy TypeCoerce's declared output contract."""

import json
from pathlib import Path

import pytest

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config


@pytest.mark.parametrize("source_mode", ["observed", "flexible"])
@pytest.mark.parametrize("required_inputs", [False, True])
def test_csv_type_coerce_declared_metadata_executes(tmp_path: Path, source_mode: str, required_inputs: bool) -> None:
    source_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.jsonl"
    error_path = tmp_path / "errors.jsonl"
    source_path.write_text("id,x\na,2\nb,4\nc,invalid\n")
    source_fields = "fields: ['id: str', 'x: str']" if source_mode == "flexible" else ""
    required_input_fields = "required_input_fields: [id, x]" if required_inputs else ""
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: csv
    on_success: input_rows
    options:
      path: {source_path}
      on_validation_failure: discard
      schema:
        mode: {source_mode}
        {source_fields}
transforms:
  - name: coerce_x
    plugin: type_coerce
    input: input_rows
    on_success: output
    on_error: errors
    options:
      schema:
        mode: flexible
        fields: ['id: str', 'x: str']
      conversions: [{{field: x, to: int}}]
      {required_input_fields}
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema: {{mode: observed}}
  errors:
    plugin: json
    on_write_failure: discard
    options:
      path: {error_path}
      format: jsonl
      schema: {{mode: observed}}
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    if source_mode == "observed" and required_inputs:
        # Explicit consumption guarantees cannot be invented for an observed
        # source. This independent graph refusal is not the runtime defect.
        with pytest.raises(EdgeContractError, match="requires fields"):
            ExecutionGraph.from_plugin_instances(
                sources=bundle.sources,
                source_settings_map=bundle.source_settings_map,
                transforms=bundle.transforms,
                sinks=bundle.sinks,
                aggregations=bundle.aggregations,
                gates=list(settings.gates),
            )
        return
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    try:
        result = Orchestrator(db).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        )
        assert result.rows_processed == 3
        assert [json.loads(line) for line in output_path.read_text().splitlines()] == [{"id": "a", "x": 2}, {"id": "b", "x": 4}]
        assert [json.loads(line) for line in error_path.read_text().splitlines()] == [{"id": "c", "x": "invalid"}]
    finally:
        db.close()
