# tests/unit/engine/orchestrator/test_checkpoint_leader_fence_guard.py
"""CheckpointCoordinator carries no leader token of its own (ADR-048 §3).

Every checkpoint create/delete the coordinator performs takes the leader
``CoordinationToken`` as a PARAMETER of that call and forwards exactly that
object to ``CheckpointManager`` — never a token bound earlier, never one
re-read mid-run. The run a checkpoint belongs to is ``coordination_token.run_id``
(ADR-048 §2), so the draft the coordinator builds is addressed to the token's
run; the manager refuses any other pairing before its first database effect
(pinned in ``tests/unit/core/checkpoint/test_manager.py``).

Ordering pins kept from the former bind-once guard (elspeth-fab455790d):
- disabled/unconfigured checkpointing still short-circuits before any manager
  call (a no-checkpoint run never touches the fence);
- delete has no enabled gate by design — only the manager-None arm skips.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elspeth.contracts import NodeType
from elspeth.contracts.barrier_scalars import BarrierScalars
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.coordination import CoordinationToken
from elspeth.core.checkpoint import CheckpointManager
from elspeth.core.dag import ExecutionGraph
from elspeth.engine.orchestrator import checkpointing
from elspeth.engine.orchestrator.checkpointing import CheckpointCoordinator

RUN_ID = "run-fenced"


def _token(run_id: str = RUN_ID) -> CoordinationToken:
    return CoordinationToken(run_id=run_id, worker_id="leader-a", leader_epoch=1)


def _make_coordinator(
    *,
    manager: Mock | None = ...,  # type: ignore[assignment]
    enabled: bool = True,
    frequency: int = 1,
) -> tuple[CheckpointCoordinator, Mock | None]:
    resolved_manager: Mock | None = Mock(spec=CheckpointManager) if manager is ... else manager
    config = Mock(spec=RuntimeCheckpointConfig)
    config.enabled = enabled
    config.frequency = frequency
    coordinator = CheckpointCoordinator(checkpoint_manager=resolved_manager, checkpoint_config=config)
    graph = ExecutionGraph()
    graph.add_node("source", node_type=NodeType.SOURCE, plugin_name="test", config={})
    coordinator.set_active_graph(graph)
    return coordinator, resolved_manager


def _loop_ctx() -> SimpleNamespace:
    processor = SimpleNamespace(get_barrier_scalars=lambda: BarrierScalars(aggregation={}, coalesce={}))
    return SimpleNamespace(processor=processor)


def _fire(coordinator: CheckpointCoordinator, path: str, token: CoordinationToken) -> None:
    if path == "run_start":
        coordinator.checkpoint_run_start(coordination_token=token)
    elif path == "maybe":
        coordinator.maybe_checkpoint(coordination_token=token, barrier_scalars=None)
    elif path == "interrupted":
        coordinator.checkpoint_interrupted_progress(_loop_ctx(), coordination_token=token)  # type: ignore[arg-type]
    elif path == "after_sink":
        factory = coordinator.make_checkpoint_after_sink_factory(_loop_ctx().processor, coordination_token=token)
        factory("sink-0")(Mock())
    else:
        raise AssertionError(f"unknown path {path!r}")


CREATE_PATHS = ["run_start", "maybe", "interrupted", "after_sink"]


class TestCreatePathsForwardTheParameterToken:
    @pytest.mark.parametrize("path", CREATE_PATHS)
    def test_the_exact_token_reaches_the_manager_with_a_draft_for_its_run(self, path: str) -> None:
        coordinator, manager = _make_coordinator()
        assert manager is not None
        token = _token()
        _fire(coordinator, path, token)
        assert manager.create_checkpoint.call_count == 1
        assert manager.create_checkpoint.call_args.kwargs["coordination_token"] is token
        assert manager.create_checkpoint.call_args.kwargs["draft"].run_id == token.run_id

    @pytest.mark.parametrize("path", CREATE_PATHS)
    def test_the_token_is_a_required_keyword(self, path: str) -> None:
        """No default, no bound fallback: a caller without the seat cannot write."""
        coordinator, manager = _make_coordinator()
        assert manager is not None
        with pytest.raises(TypeError, match="coordination_token"):
            if path == "run_start":
                coordinator.checkpoint_run_start()  # type: ignore[call-arg]
            elif path == "maybe":
                coordinator.maybe_checkpoint(barrier_scalars=None)  # type: ignore[call-arg]
            elif path == "interrupted":
                coordinator.checkpoint_interrupted_progress(_loop_ctx())  # type: ignore[arg-type,call-arg]
            else:
                coordinator.make_checkpoint_after_sink_factory(_loop_ctx().processor)  # type: ignore[call-arg]
        manager.create_checkpoint.assert_not_called()

    @pytest.mark.parametrize("path", CREATE_PATHS)
    def test_disabled_checkpointing_short_circuits(self, path: str) -> None:
        """The gate runs first: a no-checkpoint run never reaches the manager or the fence."""
        coordinator, manager = _make_coordinator(enabled=False)
        assert manager is not None
        _fire(coordinator, path, _token())  # must not raise
        manager.create_checkpoint.assert_not_called()

    @pytest.mark.parametrize("path", CREATE_PATHS)
    def test_unconfigured_manager_short_circuits(self, path: str) -> None:
        coordinator, _manager = _make_coordinator(manager=None)
        _fire(coordinator, path, _token())  # must not raise

    def test_every_n_skip_still_forwards_nothing_and_writes_nothing(self) -> None:
        coordinator, manager = _make_coordinator(frequency=1000)
        assert manager is not None
        coordinator.maybe_checkpoint(coordination_token=_token(), barrier_scalars=None)
        manager.create_checkpoint.assert_not_called()


class TestDeleteForwardsTheParameterToken:
    def test_the_exact_token_reaches_the_manager(self) -> None:
        coordinator, manager = _make_coordinator()
        assert manager is not None
        token = _token()
        coordinator.delete_checkpoints(coordination_token=token)
        assert manager.delete_checkpoints.call_count == 1
        assert manager.delete_checkpoints.call_args.kwargs == {"coordination_token": token}

    def test_the_token_is_a_required_keyword(self) -> None:
        coordinator, manager = _make_coordinator()
        assert manager is not None
        with pytest.raises(TypeError, match="coordination_token"):
            coordinator.delete_checkpoints()  # type: ignore[call-arg]
        manager.delete_checkpoints.assert_not_called()

    def test_manager_none_returns_silently(self) -> None:
        """delete has no _checkpoint_gate by design; the manager-None arm stays."""
        coordinator, _manager = _make_coordinator(manager=None)
        coordinator.delete_checkpoints(coordination_token=_token())  # must not raise


def test_coordinator_holds_no_token_of_its_own() -> None:
    """ADR-048 §3 structural pin: the coordinator never stores or rebinds a token.

    The former ``bind_coordination`` seam kept a token on the instance and
    every write read it back, so a token bound for one run or epoch could
    fence a later write. Parsed from source (not ``hasattr``): no method
    binds a token, and no attribute holding one is assigned anywhere.
    """
    tree = ast.parse(Path(checkpointing.__file__).read_text(encoding="utf-8"))
    method_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "bind_coordination" not in method_names
    assert "_require_fence" not in method_names
    stored_attributes = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
    }
    assert not {name for name in stored_attributes if "token" in name}, stored_attributes
