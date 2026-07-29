"""A row-multiplying transform inside a row_union branch must fail closed.

row_union correlates on (row_union_name, row_id), so a branch containing an
expanding transform emits several children sharing one row_id and cannot
satisfy the barrier. Coalesce — which correlates identically — surfaces that
as a loud duplicate-arrival invariant error. row_union must not be quieter:
before this was fixed the mid-branch continuation dropped the barrier
binding, so the children walked straight through the union node and the
group split silently, yielding a half-group statistic with no audit signal
(elspeth-a5b86149d4 remediation).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config


def _run(tmp_path: Path) -> object:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "out.jsonl"
    # Each row carries a two-element array, so the treatment branch explodes
    # one source row into two tokens that share its row_id.
    input_path.write_text('{"id": 1, "items": [10, 20]}\n{"id": 2, "items": [30, 40]}\n')
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
transforms:
  - name: tag_control
    plugin: passthrough
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
  - name: explode_treatment
    plugin: json_explode
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema:
        mode: observed
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
row_unions:
  - name: variant_union
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    on_success: union_out
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
    return Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
    )


def test_expanding_transform_in_a_branch_does_not_bypass_the_barrier(tmp_path: Path) -> None:
    """Every exploded child is accounted for AT the barrier, none walks past it.

    Two source rows fork into control + treatment; treatment explodes into
    two children sharing its row_id, so six branch tokens reach the union
    for four declared slots. The contract is not that the surplus succeeds —
    it cannot, correlation is per row_id — but that it is adjudicated by the
    barrier and recorded, exactly as coalesce adjudicates the same shape.
    When the binding was dropped, the treatment children never appeared at
    the union at all and continued downstream ungrouped.
    """
    _run(tmp_path)

    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    union_states = dict(
        conn.execute("SELECT status, COUNT(*) FROM node_states WHERE node_id LIKE 'row_union_%' GROUP BY status").fetchall()
    )

    # All six branch arrivals are adjudicated at the barrier: four release as
    # two complete groups, the two surplus expansion children fail closed.
    assert sum(union_states.values()) == 6, f"branch tokens bypassed the barrier: {union_states}"
    assert union_states.get("completed") == 4, union_states
    assert union_states.get("failed") == 2, union_states

    # Nothing ungrouped leaks downstream: only the released groups are written.
    sink_completed = conn.execute("SELECT COUNT(*) FROM node_states WHERE node_id LIKE 'sink_%' AND status = 'completed'").fetchone()[0]
    assert sink_completed == 4, f"expected only released group members at the sink, got {sink_completed}"
