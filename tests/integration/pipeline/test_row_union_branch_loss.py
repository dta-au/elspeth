"""A lost row_union branch must fail its group closed, promptly.

Every path that can terminate a fork branch early — retry exhaustion,
filter drop, quarantine, error routing, gate routing, gate discard —
notifies the coalesce barrier so its held siblings stop waiting. row_union
was given the same notifier but it was never wired to any of those paths,
so a lost branch left its sibling blocked: at best until the end-of-source
flush (with a misleading `row_union_incomplete_at_flush` reason instead of
`row_union_branch_lost`), and for gate-routed loss the durable BLOCKED row
survived the flush and aborted the whole run
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
  # Row 1's control token is routed away mid-branch, so its group can never
  # complete; row 2's control token continues to the barrier.
  - name: control_filter
    input: control_staged
    condition: "row['id'] == 1"
    routes:
      'true': dropped
      'false': control_scored
transforms:
  - name: stage_control
    plugin: passthrough
    input: control_branch
    on_success: control_staged
    on_error: discard
    options:
      schema:
        mode: observed
  - name: tag_treatment
    plugin: passthrough
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
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
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
  dropped:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "dropped.jsonl"}
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


def test_gate_routed_branch_loss_fails_its_group_closed(tmp_path: Path) -> None:
    """The run completes, and the orphaned sibling is failed AS a branch loss."""
    result = _run(tmp_path)

    # The run must reach a terminal state at all: an unreleased durable
    # BLOCKED hold used to survive the end-of-input flush and abort it.
    assert result.rows_processed == 2

    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    union_states = dict(
        conn.execute("SELECT status, COUNT(*) FROM node_states WHERE node_id LIKE 'row_union_%' GROUP BY status").fetchall()
    )
    # Row 2's pair releases; row 1's surviving treatment token fails closed.
    assert union_states.get("completed") == 2, union_states
    assert union_states.get("failed") == 1, union_states

    # The failure is attributed to the branch loss, not to a generic
    # end-of-source sweep — the operator must be able to tell them apart.
    reasons = [
        row[0]
        for row in conn.execute("SELECT error_json FROM node_states WHERE node_id LIKE 'row_union_%' AND status = 'failed'").fetchall()
        if row[0]
    ]
    assert reasons, "failed barrier node state recorded no error payload"
    assert any("row_union_branch_lost" in reason for reason in reasons), reasons

    # The genuine pre-barrier loss persists a durable loss-ledger record.
    losses = conn.execute("SELECT coalesce_name, branch_name FROM coalesce_branch_losses").fetchall()
    assert ("variant_union", "control_branch") in losses, losses


def test_post_release_divert_records_no_branch_loss(tmp_path: Path) -> None:
    """A gate-to-sink route DOWNSTREAM of a released union is not a branch loss.

    Released row_union tokens keep their branch_name, so a terminal divert
    after the release re-enters the loss-notification path; it must not stage
    a durable coalesce_branch_losses record claiming a pre-barrier loss.
    """
    input_path = tmp_path / "input.jsonl"
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
  # Downstream of the union: every released control token is routed to a
  # side sink, terminating it after its group already released.
  - name: split_variants
    input: union_out
    condition: "row['prompt_variant'] == 'control'"
    routes:
      'true': dropped
      'false': output
transforms:
  - name: tag_control
    plugin: value_transform
    input: control_branch
    on_success: control_scored
    on_error: discard
    options:
      schema:
        mode: observed
      operations:
        - target: prompt_variant
          expression: "'control'"
  - name: tag_treatment
    plugin: value_transform
    input: treatment_branch
    on_success: treatment_scored
    on_error: discard
    options:
      schema:
        mode: observed
      operations:
        - target: prompt_variant
          expression: "'treatment'"
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
      path: {tmp_path / "out.jsonl"}
      format: jsonl
      schema:
        mode: observed
  dropped:
    plugin: json
    on_write_failure: discard
    options:
      path: {tmp_path / "dropped.jsonl"}
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

    assert result.rows_processed == 2
    assert result.rows_failed == 0

    conn = sqlite3.connect(str(tmp_path / "audit.db"))
    # Both groups released: four completed barrier states, no failures.
    union_states = dict(
        conn.execute("SELECT status, COUNT(*) FROM node_states WHERE node_id LIKE 'row_union_%' GROUP BY status").fetchall()
    )
    assert union_states == {"completed": 4}, union_states

    # The post-release divert must not fabricate a pre-barrier loss record.
    losses = conn.execute("SELECT coalesce_name, row_id, branch_name FROM coalesce_branch_losses").fetchall()
    assert losses == [], losses
