"""Identity-form row_union branches must execute, not just build.

`branches: [a, b]` is the ergonomic list form that RowUnionSettings
normalizes to an identity dict — the shape the settings docstring
advertises and most DAG unit tests use. Unlike the transform-chain form,
an identity branch reaches the barrier over a direct gate->union COPY
edge, so its work item is already parked AT the union node when the drain
decides how to block it. That path was never exercised end to end
(elspeth-a5b86149d4 remediation).
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


def _run_identity_union(tmp_path: Path, *, row_unions_yaml: str) -> tuple[object, Path]:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    input_path.write_text('{"id": 1, "amount": 10}\n{"id": 2, "amount": 20}\n')
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
{row_unions_yaml}
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
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
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
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
    return result, output_path


_IDENTITY_UNION = """
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
"""


def test_identity_form_branches_release_through_the_barrier(tmp_path: Path) -> None:
    """A list-form union releases both branches per row and completes the run."""
    result, output_path = _run_identity_union(tmp_path, row_unions_yaml=_IDENTITY_UNION)

    assert result.rows_processed == 2
    output_rows = [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()]
    # Two source rows forked into two branches each, released as whole groups.
    assert len(output_rows) == 4, f"expected 4 released branch tokens, got: {output_rows}"
    assert sorted(row["id"] for row in output_rows) == [1, 1, 2, 2]
