"""Column selection must preserve known reference CSV value types at preflight."""

import csv
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


@pytest.mark.parametrize("mapper_mode", ["observed", "flexible"])
@pytest.mark.parametrize("sla_type", ["str", "int"])
def test_selected_reference_csv_type_proof(tmp_path: Path, mapper_mode: str, sla_type: str) -> None:
    source_path = tmp_path / "input.csv"
    output_path = tmp_path / "output.csv"
    source_path.write_text("id,complaint,category\na,charged twice,billing\nb,no service,outage\nc,question,other\n")
    mapper_fields = "fields: ['response_sla_hours: str']" if mapper_mode == "flexible" else ""
    settings = load_settings_from_yaml_string(
        f"""
sources:
  complaints:
    plugin: csv
    on_success: categorized
    options:
      path: {source_path}
      on_validation_failure: discard
      schema:
        mode: fixed
        fields: ['id: str', 'complaint: str', 'category: str']
transforms:
  - name: add_sla
    plugin: reference_join
    input: categorized
    on_success: joined
    on_error: discard
    options:
      schema:
        mode: observed
        guaranteed_fields: [id, complaint, category, response_sla_hours]
      required_input_fields: [category]
      reference_content: "category,response_sla_hours\\nbilling,24\\noutage,4\\nother,48\\n"
      reference_format: csv
      key_field: category
      reference_key_name: category
      output: {{response_sla_hours: "ref['response_sla_hours']"}}
      on_miss: fail
  - name: keep_columns
    plugin: field_mapper
    input: joined
    on_success: output
    on_error: discard
    options:
      schema:
        mode: {mapper_mode}
        {mapper_fields}
      mapping: {{id: id, complaint: complaint, category: category, response_sla_hours: response_sla_hours}}
      select_only: true
      required_input_fields: [id, complaint, category, response_sla_hours]
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: {output_path}
      schema:
        mode: fixed
        fields: ['id: str', 'complaint: str', 'category: str', 'response_sla_hours: {sla_type}']
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    if sla_type == "int":
        with pytest.raises(EdgeContractError, match="response_sla_hours"):
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
        with output_path.open(newline="") as output:
            assert list(csv.DictReader(output)) == [
                {"id": "a", "complaint": "charged twice", "category": "billing", "response_sla_hours": "24"},
                {"id": "b", "complaint": "no service", "category": "outage", "response_sla_hours": "4"},
                {"id": "c", "complaint": "question", "category": "other", "response_sla_hours": "48"},
            ]
    finally:
        db.close()
