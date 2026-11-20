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

import pytest

from elspeth.contracts import NodeType
from elspeth.contracts.barrier_scalars import AggregationNodeScalars, BarrierScalars
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.types import NodeID
from elspeth.core.canonical import compute_full_topology_hash
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.checkpoint.compatibility import CheckpointCompatibilityValidator
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.orchestrator.checkpointing import CheckpointCoordinator


@dataclass(frozen=True, slots=True)
class _ScalarProcessor:
    barrier_scalars: BarrierScalars

    def get_barrier_scalars(self) -> BarrierScalars:
        return self.barrier_scalars


def _make_coordinator(plugin_name: str = "test") -> CheckpointCoordinator:
    config = RuntimeCheckpointConfig(enabled=True, frequency=1, checkpoint_interval=None)
    coordinator = CheckpointCoordinator(
        checkpoint_manager=create_autospec(CheckpointManager, instance=True, spec_set=True),
        checkpoint_config=config,
    )
    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name=plugin_name, config={})
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


class TestTopologyHashFixedAtRunStart:
    """E-9a: the checkpoint topology hash is fixed at run start (maintainer ruling 2026-11-20).

    The coordinator hashes the graph when it is installed (if a checkpoint
    can be written) and reuses that digest for every checkpoint of the run, the graceful
    shutdown checkpoint included. ``set_active_graph`` discards it on every
    install and every clear.
    """

    def test_shutdown_checkpoint_reuses_run_start_hash(self) -> None:
        """A topology change after the first checkpoint does not reach the shutdown checkpoint.

        The shutdown path neither recomputes nor refuses: it writes the
        run-start digest and returns normally, as every other checkpoint does.
        """
        coordinator = _make_coordinator()
        graph = coordinator._active_graph
        assert graph is not None
        token = _token("run-fixed-hash")
        run_start_hash = compute_full_topology_hash(graph)
        coordinator.checkpoint_run_start(coordination_token=token)

        # Manually built graphs keep a mutable config dict; change a hashed input.
        graph.get_node_info("source").config["changed"] = True
        assert compute_full_topology_hash(graph) != run_start_hash

        coordinator.checkpoint_interrupted_progress(_loop_ctx({}), coordination_token=token)

        manager = coordinator._checkpoint_manager
        assert manager is not None
        drafts = [call.kwargs["draft"] for call in manager.create_checkpoint.call_args_list]
        assert [draft.sequence_number for draft in drafts] == [0, 1]
        assert [draft.upstream_topology_hash for draft in drafts] == [run_start_hash, run_start_hash]

    def test_install_and_clear_each_reset_the_cached_hash(self) -> None:
        """Installing a graph over a primed one, and clearing, each discard the cache.

        The second graph is installed WITHOUT a preceding clear (a fresh-run
        exception before the clearing ``finally``, or a failing plugin
        cleanup, can skip the clear), so a reset that only happens on clear
        hands the second run the first run's digest. Clearing is then checked
        on its own, so a reset that only happens on install fails too.
        """
        coordinator = _make_coordinator()
        first_graph = coordinator._active_graph
        assert first_graph is not None
        first_hash = compute_full_topology_hash(first_graph)
        coordinator.checkpoint_run_start(coordination_token=_token("first-run"))

        second_graph = ExecutionGraph()
        second_graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="different-source", config={})
        second_hash = compute_full_topology_hash(second_graph)
        assert second_hash != first_hash
        coordinator.set_active_graph(second_graph)
        coordinator.reset_sequence()
        coordinator.checkpoint_run_start(coordination_token=_token("second-run"))

        manager = coordinator._checkpoint_manager
        assert manager is not None
        drafts = [call.kwargs["draft"] for call in manager.create_checkpoint.call_args_list]
        assert [(draft.run_id, draft.upstream_topology_hash) for draft in drafts] == [
            ("first-run", first_hash),
            ("second-run", second_hash),
        ]

        # Clearing must discard the cache too. Through the public API this is
        # not observable: after a clear the gate refuses every draft, and the
        # next install recomputes. So "reset only on install" is behaviourally
        # equivalent and only this private-state check can fail it.
        assert coordinator._active_topology_hash == second_hash
        coordinator.set_active_graph(None)
        assert coordinator._active_topology_hash is None

    def test_interleaved_live_coordinators_keep_their_own_hash(self) -> None:
        """Two coordinators alive at once (concurrent web runs in one process) never share a hash.

        Installs and drafts are interleaved: install A, install B, draft A,
        draft B, draft A, draft B. A cache shared between coordinators (a
        class or module global), even one reset on every install, hands one
        run the other's digest.
        """
        coordinator_a = _make_coordinator("source-a")
        coordinator_b = _make_coordinator("source-b")
        graph_a = coordinator_a._active_graph
        graph_b = coordinator_b._active_graph
        assert graph_a is not None and graph_b is not None
        hash_a = compute_full_topology_hash(graph_a)
        hash_b = compute_full_topology_hash(graph_b)
        assert hash_a != hash_b
        token_a = _token("run-a")
        token_b = _token("run-b")

        coordinator_a.checkpoint_run_start(coordination_token=token_a)
        coordinator_b.checkpoint_run_start(coordination_token=token_b)
        coordinator_a.checkpoint_interrupted_progress(_loop_ctx({}), coordination_token=token_a)
        coordinator_b.checkpoint_interrupted_progress(_loop_ctx({}), coordination_token=token_b)

        for coordinator, run_id, expected in ((coordinator_a, "run-a", hash_a), (coordinator_b, "run-b", hash_b)):
            manager = coordinator._checkpoint_manager
            assert manager is not None
            drafts = [call.kwargs["draft"] for call in manager.create_checkpoint.call_args_list]
            assert [(draft.run_id, draft.sequence_number, draft.upstream_topology_hash) for draft in drafts] == [
                (run_id, 0, expected),
                (run_id, 1, expected),
            ]

    @pytest.mark.parametrize(
        ("enabled", "with_manager"),
        [pytest.param(False, True, id="checkpointing-disabled"), pytest.param(True, False, id="no-manager")],
    )
    def test_install_hashes_nothing_when_checkpoints_cannot_be_written(
        self, enabled: bool, with_manager: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Installing a graph costs no topology hash unless a checkpoint can be written."""
        calls: list[ExecutionGraph] = []
        original = CheckpointCompatibilityValidator.compute_full_topology_hash

        def counting(self: CheckpointCompatibilityValidator, graph: ExecutionGraph) -> str:
            calls.append(graph)
            return original(self, graph)

        monkeypatch.setattr(CheckpointCompatibilityValidator, "compute_full_topology_hash", counting)
        coordinator = CheckpointCoordinator(
            checkpoint_manager=create_autospec(CheckpointManager, instance=True, spec_set=True) if with_manager else None,
            checkpoint_config=RuntimeCheckpointConfig(enabled=enabled, frequency=1, checkpoint_interval=None),
        )
        graph = ExecutionGraph()
        graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="test", config={})

        coordinator.set_active_graph(graph)
        coordinator.checkpoint_run_start(coordination_token=_token("run-no-checkpoints"))
        coordinator.checkpoint_interrupted_progress(_loop_ctx({}), coordination_token=_token("run-no-checkpoints"))
        coordinator.set_active_graph(None)

        assert calls == []
