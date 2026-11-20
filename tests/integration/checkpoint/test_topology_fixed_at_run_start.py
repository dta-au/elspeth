# tests/integration/checkpoint/test_topology_fixed_at_run_start.py
"""The checkpoint topology hash is fixed at run start (E-9a).

Maintainer ruling 2026-11-20: a run's checkpoint topology hash is computed
once, when the run's graph is installed (before any plugin starts), and every
checkpoint of the run, a resumed segment's included, stores that same digest. The Landscape ``nodes`` and ``edges`` rows likewise
record each node's configuration once, at registration, so the two audit
records describe the same configuration: the configuration as of run start.

A plugin must not mutate its own config after build. ``deep_freeze`` detaches
mappings and lists but keeps opaque leaves such as a NumPy array by identity,
so a plugin that writes into such a leaf changes what its graph node's config
hashes to. Under the ruling that change reaches neither the checkpoints nor
the audit trail of the run already in progress.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PipelineRow, RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.core.canonical import compute_full_topology_hash, stable_hash
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import checkpoints_table, edges_table, nodes_table, runs_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink, ListSource


class _SelfMutatingTransform(BaseTransform):
    """Writes into its own config array on every row, then interrupts the run.

    This is the contract violation the ruling settles: the array leaf survives
    the build freeze by identity, so the graph node's config is the same
    object the plugin mutates.
    """

    name = "self_mutating_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self, *, interrupt_after: int, shutdown_event: threading.Event) -> None:
        super().__init__({"schema": {"mode": "observed"}, "array": np.array([1, 2])})
        self._interrupt_after = interrupt_after
        self._event = shutdown_event
        self._seen = 0

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        self._seen += 1
        self.config["array"][0] += 1
        if self._seen >= self._interrupt_after:
            self._event.set()
        return TransformResult.success(row, success_reason={"action": "mutated own config"})


def _topology_hash_from_landscape(db: LandscapeDB, run_id: str) -> str:
    """Rebuild ``compute_full_topology_hash``'s input from the run's audit rows."""
    with db.engine.connect() as conn:
        node_rows = conn.execute(select(nodes_table).where(nodes_table.c.run_id == run_id)).mappings().all()
        edge_rows = conn.execute(select(edges_table).where(edges_table.c.run_id == run_id)).mappings().all()
    assert node_rows
    assert edge_rows
    topology_data = {
        "nodes": sorted(
            [
                {
                    "node_id": row["node_id"],
                    "plugin_name": row["plugin_name"],
                    "node_type": NodeType(row["node_type"]).value,
                    "config_hash": row["config_hash"],
                }
                for row in node_rows
            ],
            key=lambda node: node["node_id"],
        ),
        "edges": sorted(
            [
                {
                    "from": row["from_node_id"],
                    "to": row["to_node_id"],
                    "key": row["label"],
                    "label": row["label"],
                    "mode": RoutingMode(row["default_mode"]).value,
                }
                for row in edge_rows
            ],
            key=lambda edge: (edge["from"], edge["to"], edge["key"]),
        ),
    }
    return stable_hash(topology_data)


def test_self_mutating_plugin_checkpoints_store_run_start_topology(db: LandscapeDB, payload_store: FilesystemPayloadStore) -> None:
    """Every checkpoint stores the run-start hash, which matches the Landscape topology.

    Maintainer ruling 2026-11-20: the checkpoint topology hash is fixed at run
    start. The plugin changes a hashed config leaf on every row; the run's
    sequence-0, per-row and graceful-shutdown checkpoints must all carry the
    digest computed when the graph was installed, and that digest must equal the
    topology rebuilt from the run's Landscape ``nodes`` and ``edges`` rows.
    Interruption behaves as for any run: ``GracefulShutdownError`` and
    INTERRUPTED.
    """
    shutdown_event = threading.Event()
    transform = _SelfMutatingTransform(interrupt_after=3, shutdown_event=shutdown_event)
    config = PipelineConfig(
        sources={"primary": as_source(ListSource([{"value": index} for index in range(6)]))},
        transforms=[as_transform(transform)],
        sinks={"output": as_sink(CollectSink("output"))},
    )
    graph = build_production_graph(config)
    graph_arrays = [node.config["array"] for node in graph.get_nodes() if "array" in node.config]
    assert len(graph_arrays) == 1
    assert graph_arrays[0] is transform.config["array"], "precondition: the graph shares the plugin's mutable leaf"
    run_start_hash = compute_full_topology_hash(graph)

    orchestrator = Orchestrator(
        db=db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    )
    with pytest.raises(GracefulShutdownError) as exc_info:
        orchestrator.run(config, graph=graph, payload_store=payload_store, shutdown_event=shutdown_event)
    run_id = exc_info.value.run_id

    # Control: the plugin really changed the live topology during the run.
    assert compute_full_topology_hash(graph) != run_start_hash

    with db.engine.connect() as conn:
        status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
        stored = conn.execute(
            select(checkpoints_table.c.sequence_number, checkpoints_table.c.upstream_topology_hash)
            .where(checkpoints_table.c.run_id == run_id)
            .order_by(checkpoints_table.c.sequence_number)
        ).all()
    assert RunStatus(status) is RunStatus.INTERRUPTED

    # Sequence 0 (run start), one per row written before the interrupt (3), then the shutdown checkpoint.
    assert [row.sequence_number for row in stored] == [0, 1, 2, 3, 4]
    first_checkpoint_hash = stored[0].upstream_topology_hash
    assert first_checkpoint_hash == run_start_hash
    assert [row.upstream_topology_hash for row in stored] == [first_checkpoint_hash] * len(stored)
    assert _topology_hash_from_landscape(db, run_id) == first_checkpoint_hash


class _MutateOnResumeTransform(BaseTransform):
    """Writes into its own config array in ``on_start`` or in ``process``, then interrupts the run.

    ``on_start`` runs after the resume path installs its graph and before the
    resumed segment's first checkpoint, so it exercises the window between
    resume validation and the first resumed draft.
    """

    name = "mutate_on_resume_transform"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self, *, where: str, interrupt_after: int | None, shutdown_event: threading.Event) -> None:
        super().__init__({"schema": {"mode": "observed"}, "array": np.array([1, 2])})
        self._where = where
        self._interrupt_after = interrupt_after
        self._event = shutdown_event
        self._seen = 0

    def on_start(self, ctx: Any) -> None:
        super().on_start(ctx)
        if self._where == "on_start":
            self.config["array"][0] = 99

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        self._seen += 1
        if self._where == "process":
            self.config["array"][0] = 99
        if self._interrupt_after is not None and self._seen >= self._interrupt_after:
            self._event.set()
        return TransformResult.success(row, success_reason={"action": "mutated own config"})


def _stored_checkpoints(db: LandscapeDB, run_id: str) -> tuple[RunStatus, list[tuple[int, str]]]:
    with db.engine.connect() as conn:
        status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
        rows = conn.execute(
            select(checkpoints_table.c.sequence_number, checkpoints_table.c.upstream_topology_hash)
            .where(checkpoints_table.c.run_id == run_id)
            .order_by(checkpoints_table.c.sequence_number)
        ).all()
    return RunStatus(status), [(row.sequence_number, row.upstream_topology_hash) for row in rows]


@pytest.mark.parametrize("where", ["on_start", "process"])
def test_resumed_segment_keeps_run_start_topology(
    where: str, db: LandscapeDB, payload_store: FilesystemPayloadStore, tmp_path: Path
) -> None:
    """A resumed segment stores the RUN-start hash, not a segment-start hash, and the run stays resumable.

    Maintainer ruling 2026-11-20: the checkpoint topology hash is fixed at run
    start. A fresh run is interrupted; it is resumed with a freshly built
    plugin and graph (original config values, so resume validation admits it)
    whose plugin mutates its own config array in ``on_start`` or ``process``,
    and interrupted again. Every checkpoint of both segments must store the
    run-start hash H0, which equals the Landscape topology, and a second resume
    against an original-valued graph must be admitted.
    """
    source_path = tmp_path / "input.jsonl"
    source_path.write_text("".join(json.dumps({"value": index}) + "\n" for index in range(8)))
    source = JSONSource(
        {"path": str(source_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard", "snapshot_for_resume": True}
    )
    sink = CollectSink("output")
    manager = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))

    def build(interrupt_after: int | None) -> tuple[threading.Event, PipelineConfig, Any]:
        event = threading.Event()
        transform = _MutateOnResumeTransform(where=where, interrupt_after=interrupt_after, shutdown_event=event)
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"output": as_sink(sink)},
        )
        return event, config, build_production_graph(config)

    # Segment 1: fresh run, interrupted after two rows.
    event, config, graph = build(interrupt_after=2)
    run_start_hash = compute_full_topology_hash(graph)
    with pytest.raises(GracefulShutdownError) as exc_info:
        Orchestrator(db=db, checkpoint_manager=manager, checkpoint_config=checkpoint_config).run(
            config, graph=graph, payload_store=payload_store, shutdown_event=event
        )
    run_id = exc_info.value.run_id
    assert compute_full_topology_hash(graph) != run_start_hash, "control: the plugin changed the live topology"
    status, segment_one = _stored_checkpoints(db, run_id)
    assert status is RunStatus.INTERRUPTED
    assert segment_one == [(sequence, run_start_hash) for sequence in range(4)]

    # Segment 2: resume with a freshly built, original-valued plugin and graph; interrupted again.
    recovery = RecoveryManager(db, manager)
    event, config, graph = build(interrupt_after=2)
    assert compute_full_topology_hash(graph) == run_start_hash
    assert recovery.can_resume(run_id, graph).can_resume
    resume_point = recovery.get_resume_point(run_id, graph)
    assert resume_point is not None
    with pytest.raises(GracefulShutdownError):
        Orchestrator(db=db, checkpoint_manager=manager, checkpoint_config=checkpoint_config).resume(
            resume_point=resume_point, config=config, graph=graph, payload_store=payload_store, shutdown_event=event
        )
    assert compute_full_topology_hash(graph) != run_start_hash, "control: the plugin changed the live topology again"
    status, both_segments = _stored_checkpoints(db, run_id)
    assert status is RunStatus.INTERRUPTED
    assert both_segments == [(sequence, run_start_hash) for sequence in range(7)]
    assert _topology_hash_from_landscape(db, run_id) == run_start_hash

    # A second resume against an original-valued graph is admitted.
    _event, _config, graph = build(interrupt_after=None)
    second_resume = recovery.can_resume(run_id, graph)
    assert second_resume.can_resume, second_resume.reason
