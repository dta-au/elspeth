# tests/unit/engine/orchestrator/test_checkpoint_interrupted_edge.py
"""Shutdown-checkpoint behavior for the scalar barrier projection.

History: the original edge here was the token-anchor selection chain
(commit f487d7b13 fixed a ``tokens[-1]`` IndexError on counter-only nodes).
The anchor itself was deleted as vestigial (F2, 2026-06-10) — the entire
fallback chain is gone, so the crash surface no longer exists. F1 Task 1.2
then retired the blob persistence (checkpoint carries only BarrierScalars;
counters derive from audit tables at restore, design D3), Task 2.1 retired
the executor blob state, and Task 2.4 unified the write path: the processor
surfaces ONE composed ``get_barrier_scalars() -> BarrierScalars`` and the
coordinator hands it to ``create_checkpoint`` unconditionally (the manager
serializes NULL when ``has_state`` is False). What these tests pin:

- unlatched / counter-only aggregation nodes contribute NO persisted scalars
  (their counters are derivable; their fire offsets are None);
- latched nodes contribute per-node trigger-latch scalars;
- a shutdown with no latched barriers anywhere still writes a recovery
  checkpoint (the former no-anchor skip arm is gone) whose empty
  BarrierScalars persists as NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import create_autospec

from elspeth.contracts import NodeType
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.types import NodeID
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.orchestrator.checkpointing import CheckpointCoordinator


@dataclass(frozen=True, slots=True)
class _ScalarProcessor:
    barrier_scalars: BarrierScalars

    def get_barrier_scalars(self) -> BarrierScalars:
        return self.barrier_scalars


def _make_coordinator() -> CheckpointCoordinator:
    config = RuntimeCheckpointConfig(enabled=True, frequency=1, checkpoint_interval=None)
    coordinator = CheckpointCoordinator(
        checkpoint_manager=create_autospec(CheckpointManager, instance=True, spec_set=True),
        checkpoint_config=config,
    )
    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="test", config={})
    coordinator.set_active_graph(graph)
    return coordinator


def _token(run_id: str) -> CoordinationToken:
    # The manager is an autospec double, so there is no seat to read back;
    # the coordinator forwards whatever token the drain hands it (ADR-048 §3).
    return CoordinationToken(run_id=run_id, worker_id="test-leader", leader_epoch=1)


def _loop_ctx(
    aggregation_scalars: dict[NodeID, AggregationNodeScalars],
    *,
    pending_tokens: dict[str, list] | None = None,
) -> SimpleNamespace:
    processor = _ScalarProcessor(
        BarrierScalars(
            aggregation={str(node_id): scalars for node_id, scalars in aggregation_scalars.items()},
            coalesce={},
        )
    )

    return SimpleNamespace(
        processor=processor,
        pending_tokens=pending_tokens if pending_tokens is not None else {"default": []},
    )


class TestFlushEmptiedAggregationCheckpoints:
    """checkpoint_interrupted_progress with unlatched/counter-only aggregation nodes."""

    def test_unlatched_nodes_contribute_no_scalars(self) -> None:
        """A flush-emptied or unlatched aggregation node must not crash the shutdown checkpoint.

        F1 design D3: durable counters (flush_index / rows_seen_total
        provenance) derive from audit tables at restore, and unlatched
        triggers have no fire offsets — the executor emits no entry for such
        nodes, so the checkpoint is still written with an empty BarrierScalars
        (``has_state`` False → the manager persists NULL).
        """
        coordinator = _make_coordinator()
        loop_ctx = _loop_ctx({})  # executor emitted nothing — no latched nodes

        coordinator.checkpoint_interrupted_progress(loop_ctx, coordination_token=_token("run-counter-only"))  # type: ignore[arg-type]

        coordinator._checkpoint_manager.create_checkpoint.assert_called_once()
        kwargs = coordinator._checkpoint_manager.create_checkpoint.call_args.kwargs
        assert isinstance(kwargs["draft"].barrier_scalars, BarrierScalars)
        assert kwargs["draft"].barrier_scalars.has_state is False

    def test_latched_nodes_persist_their_scalars(self) -> None:
        """Latched aggregation nodes surface as per-node BarrierScalars entries.

        The executor's get_barrier_scalars() already filters to latched nodes;
        the projection must carry them through verbatim.
        """
        coordinator = _make_coordinator()
        loop_ctx = _loop_ctx(
            {NodeID("agg_latched"): AggregationNodeScalars(count_fire_offset=0.25, condition_fire_offset=None)},
        )

        coordinator.checkpoint_interrupted_progress(loop_ctx, coordination_token=_token("run-mixed-agg"))  # type: ignore[arg-type]

        coordinator._checkpoint_manager.create_checkpoint.assert_called_once()
        kwargs = coordinator._checkpoint_manager.create_checkpoint.call_args.kwargs
        scalars = kwargs["draft"].barrier_scalars
        assert isinstance(scalars, BarrierScalars)
        assert set(scalars.aggregation) == {"agg_latched"}
        assert scalars.aggregation["agg_latched"].count_fire_offset == 0.25
        assert scalars.coalesce == {}

    def test_no_buffered_tokens_anywhere_still_checkpoints(self) -> None:
        """No latched barriers and no pending sink tokens: the shutdown
        checkpoint is still written (the former no-anchor skip arm is gone),
        addressed to the token's run (ADR-048 §2)."""
        coordinator = _make_coordinator()
        loop_ctx = _loop_ctx({})
        token = _token("run-no-anchor")

        coordinator.checkpoint_interrupted_progress(loop_ctx, coordination_token=token)  # type: ignore[arg-type]

        coordinator._checkpoint_manager.create_checkpoint.assert_called_once()
        kwargs = coordinator._checkpoint_manager.create_checkpoint.call_args.kwargs
        assert kwargs["draft"].run_id == "run-no-anchor"
        assert kwargs["coordination_token"] is token
        assert kwargs["draft"].barrier_scalars.has_state is False
