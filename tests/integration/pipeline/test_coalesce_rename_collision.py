"""Fork branches renaming one upstream field must coalesce cleanly (P9).

Regression: two fork branches renamed the same upstream field (``amount``) to
different names (``amount_aud`` / ``amount_usd``); the union merge carried
both branches' ``original_name='amount'`` into the merged contract, tripping
SchemaContract's original->normalized bijection invariant — a deterministic
first-row ValueError after green validation. The merge now breaks the
ambiguous lineage (identity original_name on colliding fields) while the
union audit metadata keeps the real cross-branch origins.
"""

from __future__ import annotations

import json
from pathlib import Path

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config


def test_cross_branch_rename_collision_coalesces(tmp_path: Path) -> None:
    input_path = tmp_path / "input.csv"
    output_path = tmp_path / "merged.json"
    input_path.write_text("id,amount,currency\n1,100,AUD\n2,250,USD\n3,75,AUD\n")

    settings = load_settings_from_yaml_string(
        f"""
sources:
  primary:
    plugin: csv
    on_success: raw
    options:
      path: {input_path}
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'amount: int'
        - 'currency: str'
      on_validation_failure: discard
gates:
- name: fan_out
  input: raw
  condition: 'True'
  routes:
    'true': fork
    'false': fork
  fork_to:
    - branch_a
    - branch_b
transforms:
- name: map_aud
  plugin: field_mapper
  input: branch_a
  on_success: aud_done
  on_error: discard
  options:
    mapping:
      id: id
      amount: amount_aud
    select_only: true
    schema:
      mode: observed
- name: map_usd
  plugin: field_mapper
  input: branch_b
  on_success: usd_done
  on_error: discard
  options:
    mapping:
      id: id
      amount: amount_usd
    select_only: true
    schema:
      mode: observed
coalesce:
- name: merge_currencies
  branches:
    branch_a: aud_done
    branch_b: usd_done
  policy: require_all
  merge: union
  on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      schema:
        mode: observed
"""
    )
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
    )
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
    )

    assert result.status.name == "COMPLETED"
    assert result.rows_processed == 3
    assert result.rows_succeeded == 3

    rows = [json.loads(line) for line in output_path.read_text().splitlines()]
    assert len(rows) == 3
    for row in rows:
        assert "amount_aud" in row, f"renamed branch_a field missing: {row}"
        assert "amount_usd" in row, f"renamed branch_b field missing: {row}"
