"""RowProcessor graph traversal, gate routing, coalesce settlement, and branch-loss tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest

# For node registration
from elspeth.contracts import (
    NodeType,
    TokenInfo,
    TransformResult,
)
from elspeth.contracts.enums import (
    FrameKind,
    TerminalOutcome,
    TerminalPath,
)
from elspeth.contracts.errors import (
    OrchestrationInvariantError,
)
from elspeth.contracts.identity import (
    LineageFrame,
)
from elspeth.contracts.results import GateResult
from elspeth.contracts.routing import RoutingAction
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.core.config import AggregationSettings, GateSettings
from elspeth.engine.coalesce_executor import CoalesceExecutor, CoalesceOutcome
from elspeth.engine.executors import GateOutcome
from elspeth.engine.processor import (
    DAGTraversalContext,
    RowProcessor,
    _FlushContext,
    _LiveBarrierHold,
)
from elspeth.engine.spans import SpanFactory
from elspeth.engine.work_items import WorkItem
from elspeth.testing import make_pipeline_row, make_row, make_token_info
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import (
    leader_coordination_token,
    member_token_for,
)
from tests.unit.engine.test_processor import (
    _DYNAMIC_SCHEMA,
    _assert_outcome_pair,
    _claim_processor_token,
    _make_contract,
    _make_factory,
    _make_mock_transform,
    _make_processor,
    _make_source_row,
    _persist_blocked_scheduler_work,
    _persist_token_for_scheduler,
)


class TestTraversalNextNodeInvariants:
    """Tests for strict Tier-1 traversal next-node invariants."""

    def test_resolve_next_node_missing_entry_raises_invariant(self) -> None:
        """Missing traversal next-node entry must crash, not silently return None."""
        _, factory = _make_factory()
        processor = _make_processor(
            factory,
            node_to_next={NodeID("source-0"): None},
        )

        with pytest.raises(OrchestrationInvariantError, match="missing from traversal next-node map"):
            processor._nav.resolve_next_node(NodeID("missing-node"))

    def test_process_row_raises_when_transform_missing_next_node_entry(self) -> None:
        """Processing nodes must have explicit next-node entries (None for terminal)."""
        _db, factory = _make_factory()
        source_row = _make_source_row()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        transform = _make_mock_transform()
        source_node = NodeID("source-0")
        transform_node = NodeID(transform.node_id)
        processor = _make_processor(
            factory,
            node_step_map={source_node: 0, transform_node: 1},
            node_to_next={source_node: transform_node},
            node_to_plugin={transform_node: transform},
        )

        with pytest.raises(OrchestrationInvariantError, match="missing from traversal next-node map"):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[transform],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )


# =============================================================================
# _resolve_audit_step_for_node invariants
# =============================================================================


class TestAuditStepResolutionInvariants:
    """Tests for strict Tier-1 audit step resolution invariants.

    _resolve_audit_step_for_node has three branches:
    1. node_id in step_map → return mapped step
    2. node_id == source_node_id (not in map) → return 0
    3. unknown node_id → raise OrchestrationInvariantError
    """

    def test_known_node_returns_mapped_step(self) -> None:
        """Nodes in the step map return their assigned step value."""
        _, factory = _make_factory()
        transform_node = NodeID("transform-1")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("source-0"): 0, transform_node: 3},
            node_to_next={NodeID("source-0"): None, transform_node: None},
        )

        assert processor._resolve_audit_step_for_node(transform_node) == 3

    def test_source_node_returns_zero(self) -> None:
        """Source node resolves to step 0 even when not in the step map.

        This is the convention distinguishing source-originated audit records
        from transform-originated ones. The _make_processor helper auto-adds
        the source to the step map, so we construct the processor directly
        to test the explicit fallback branch.
        """
        _, factory = _make_factory()
        source_node = NodeID("source-0")

        # Build traversal WITHOUT source in the step map
        traversal = DAGTraversalContext(
            node_step_map={},
            node_to_plugin={},
            node_to_next={source_node: None},
            coalesce_node_map={},
        )
        processor = RowProcessor(
            execution=factory.execution,
            data_flow=factory.data_flow,
            span_factory=SpanFactory(),
            run_id="test-run",
            source_node_id=source_node,
            source_on_success="default",
            traversal=traversal,
            scheduler=factory.scheduler,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        assert processor._resolve_audit_step_for_node(source_node) == 0

    def test_unknown_node_raises_invariant_error(self) -> None:
        """Unknown node IDs must crash, not silently return a default step."""
        _, factory = _make_factory()
        processor = _make_processor(
            factory,
            node_to_next={NodeID("source-0"): None},
        )

        with pytest.raises(OrchestrationInvariantError, match="missing from traversal step map"):
            processor._resolve_audit_step_for_node(NodeID("nonexistent-node"))

    def test_unknown_node_includes_node_id_in_error(self) -> None:
        """Error message includes the offending node ID for debugging."""
        _, factory = _make_factory()
        processor = _make_processor(
            factory,
            node_to_next={NodeID("source-0"): None},
        )

        with pytest.raises(OrchestrationInvariantError, match="phantom-node-42"):
            processor._resolve_audit_step_for_node(NodeID("phantom-node-42"))


# =============================================================================
# _get_gate_destinations
# =============================================================================


class TestGetGateDestinations:
    """Tests for the gate destination extraction helper."""

    def test_routed_to_sink(self) -> None:
        """Gate routed to a named sink returns that sink name."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        outcome = SimpleNamespace(sink_name="error-sink")
        assert processor._get_gate_destinations(outcome) == ("error-sink",)

    def test_fork_to_paths(self) -> None:
        """Fork returns branch names of child tokens."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        child_a = SimpleNamespace(branch_name="path_a")
        child_b = SimpleNamespace(branch_name="path_b")
        outcome = SimpleNamespace(
            sink_name=None,
            discarded=False,
            result=SimpleNamespace(action=RoutingAction.fork_to_paths(["path_a", "path_b"])),
            child_tokens=[child_a, child_b],
        )
        assert processor._get_gate_destinations(outcome) == ("path_a", "path_b")

    def test_continue_routing(self) -> None:
        """Continue routing returns ("continue",)."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        outcome = SimpleNamespace(
            sink_name=None,
            discarded=False,
            result=SimpleNamespace(action=RoutingAction.continue_()),
            next_node_id=None,
        )
        assert processor._get_gate_destinations(outcome) == ("continue",)

    def test_route_to_processing_uses_route_label(self) -> None:
        """Route-label branch to processing node reports chosen route label."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        outcome = SimpleNamespace(
            sink_name=None,
            discarded=False,
            next_node_id=NodeID("transform-2"),
            result=SimpleNamespace(action=RoutingAction.route("high")),
        )
        assert processor._get_gate_destinations(outcome) == ("high",)

    def test_gate_outcome_false_discarded_does_not_mask_fork_to_paths(self) -> None:
        """GateOutcome with discarded=False reports fork paths."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        child_a = make_token_info(
            data={"value": 1}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),)
        )
        child_b = make_token_info(
            data={"value": 2}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_b", member_key="path_b"),)
        )
        outcome = GateOutcome(
            result=GateResult(row={"value": 1}, action=RoutingAction.fork_to_paths(["path_a", "path_b"])),
            updated_token=make_token_info(data={"value": 1}),
            child_tokens=(child_a, child_b),
        )
        assert processor._get_gate_destinations(outcome) == ("path_a", "path_b")

    def test_gate_outcome_false_discarded_does_not_mask_continue_routing(self) -> None:
        """GateOutcome with discarded=False reports continue."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        outcome = GateOutcome(
            result=GateResult(row={"value": 1}, action=RoutingAction.continue_()),
            updated_token=make_token_info(data={"value": 1}),
        )
        assert processor._get_gate_destinations(outcome) == ("continue",)

    def test_gate_outcome_false_discarded_does_not_mask_processing_route_label(self) -> None:
        """GateOutcome with discarded=False reports route labels."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        outcome = GateOutcome(
            next_node_id=NodeID("transform-2"),
            result=GateResult(row={"value": 1}, action=RoutingAction.route("high")),
            updated_token=make_token_info(data={"value": 1}),
        )
        assert processor._get_gate_destinations(outcome) == ("high",)


class TestProcessRowGateBranching:
    """Tests for non-linear gate branching through next_node_id."""

    def test_config_gate_processing_node_jump_preloads_subchain_sink_for_expanded_children(self) -> None:
        """Config gate PROCESSING_NODE jumps should refresh inherited sink from jumped subchain."""
        _db, factory = _make_factory()
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_node = NodeID("cfg-gate-1")
        expander_node = NodeID("expander-2")
        terminal_node = NodeID("terminal-3")
        source_node = NodeID("source-0")

        config_gate = GateSettings(
            name="cfg_router",
            input="in_conn",
            condition="'branch_a'",
            routes={"branch_a": "branch_conn"},
        )
        expander = _make_mock_transform(
            node_id=str(expander_node),
            name="expander",
            creates_tokens=True,
            on_success=None,
        )
        terminal = _make_mock_transform(
            node_id=str(terminal_node),
            name="terminal",
            on_success="branch_sink",
        )

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            node_step_map={
                source_node: 0,
                gate_node: 1,
                expander_node: 2,
                terminal_node: 3,
            },
            node_to_next={
                source_node: gate_node,
                gate_node: None,
                expander_node: terminal_node,
                terminal_node: None,
            },
            node_to_plugin={
                gate_node: config_gate,
                expander_node: expander,
                terminal_node: terminal,
            },
        )

        gate_contract = _make_contract()
        gate_result = GateResult(
            row={"value": 10},
            action=RoutingAction.route("branch_a"),
            contract=gate_contract,
        )
        expand_result = TransformResult.success_multi(
            [
                make_row({"value": 10, "idx": 1}, contract=gate_contract),
                make_row({"value": 10, "idx": 2}, contract=gate_contract),
            ],
            success_reason={"action": "expand"},
        )

        def config_gate_side_effect(*, gate_config, node_id, token, ctx, token_manager=None, attempt_offset=0):
            return GateOutcome(
                result=gate_result,
                updated_token=token,
                next_node_id=expander_node,
            )

        def transform_side_effect(*, transform, token, ctx, attempt=0):
            if transform.name == "expander":
                return (expand_result, token, None)
            raise AssertionError("terminal transform should not execute in this regression harness")

        inherited_sinks: list[str | None] = []

        def continuation_side_effect(*, token, current_node_id, coalesce_name=None, row_union_name=None, on_success_sink=None):
            inherited_sinks.append(on_success_sink)
            return WorkItem(
                token=token,
                current_node_id=None,
                coalesce_node_id=None,
                coalesce_name=coalesce_name,
                row_union_node_id=NodeID(f"row_union::{row_union_name}") if row_union_name is not None else None,
                row_union_name=row_union_name,
                on_success_sink=on_success_sink,
            )

        with (
            patch.object(processor._gate_executor, "execute_config_gate", side_effect=config_gate_side_effect),
            patch.object(processor._transform_executor, "execute_transform", side_effect=transform_side_effect),
            patch.object(
                processor,
                "_work_items",
                SimpleNamespace(
                    create=processor._work_items.create,
                    create_continuation=continuation_side_effect,
                ),
            ),
        ):
            results = processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[expander, terminal],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )

        completed = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)]
        assert len(completed) == 2
        assert inherited_sinks == ["branch_sink", "branch_sink"]
        assert all(r.sink_name == "branch_sink" for r in completed)

    def test_jump_target_terminal_coalesce_missing_on_success_mapping_raises(self) -> None:
        """Terminal coalesce reached via jump must have an on_success sink mapping."""
        _db, factory = _make_factory()

        source_node = NodeID("source-0")
        router_node = NodeID("router-1")
        coalesce_node = NodeID("coalesce::merge")

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            node_step_map={
                source_node: 0,
                router_node: 1,
                coalesce_node: 2,
            },
            node_to_next={
                source_node: router_node,
                router_node: coalesce_node,
                coalesce_node: None,
            },
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            # Intentionally omit coalesce_on_success_map
            # router-1 is plugin-less walk topology — declare it structural so
            # the jump walk reaches the coalesce on_success invariant under test.
            structural_node_ids=frozenset({source_node, router_node}),
        )

        with pytest.raises(OrchestrationInvariantError, match="Coalesce 'merge' not in on_success map"):
            processor._nav.resolve_jump_target_sink(router_node)

    def test_jump_target_resolution_raises_when_no_sink_and_no_gate(self) -> None:
        """Jump path with only transforms and no terminal sink must fail closed."""
        _db, factory = _make_factory()

        source_node = NodeID("source-0")
        jump_start_node = NodeID("branch-transform-1")
        downstream_transform_node = NodeID("branch-transform-2")

        branch_transform1 = _make_mock_transform(
            node_id=str(jump_start_node),
            name="branch_transform1",
            on_success="branch_conn",
        )
        branch_transform2 = _make_mock_transform(
            node_id=str(downstream_transform_node),
            name="branch_transform2",
            on_success="nonexistent_conn",
        )

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            sink_names=frozenset({"source_sink", "branch_sink"}),
            node_step_map={
                source_node: 0,
                jump_start_node: 1,
                downstream_transform_node: 2,
            },
            node_to_next={
                source_node: jump_start_node,
                jump_start_node: downstream_transform_node,
                downstream_transform_node: None,
            },
            node_to_plugin={
                jump_start_node: branch_transform1,
                downstream_transform_node: branch_transform2,
            },
        )

        with pytest.raises(OrchestrationInvariantError, match="no sink"):
            processor._nav.resolve_jump_target_sink(jump_start_node)

    def test_branch_to_sink_routing_applies_for_terminal_fork_children(self) -> None:
        """Branch-routed tokens bypassing coalesce should resolve sink via branch_to_sink."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-branch-1",
            row_data=make_row({"value": 1}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        processor = _make_processor(
            factory,
            source_on_success="source_sink",
            branch_to_sink={BranchName("path_a"): "branch_sink"},
            sink_names=frozenset({"source_sink", "branch_sink"}),
            node_step_map={NodeID("source-0"): 0},
            node_to_next={NodeID("source-0"): None},
        )
        _persist_token_for_scheduler(factory, token)

        results = processor._drain_work_queue(processor._work_items.create(token=token, current_node_id=None), ctx)

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "branch_sink"

    def test_fork_to_sink_children_bypass_gate_continuation_successor(self) -> None:
        """Regression: fork children in _branch_to_sink must not traverse downstream nodes.

        Topology: gate-1 → transform-1 (gate's structural successor)
        Gate forks to branches sink_a and sink_b (both in _branch_to_sink).

        Without fix: children get current_node_id=transform-1 and execute the transform.
        With fix: children get current_node_id=None, skip the loop, resolve via _branch_to_sink.
        """
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_node = NodeID("gate-1")
        transform_node = NodeID("transform-1")
        source_node = NodeID("source-0")

        # Register nodes for FK constraints
        factory.data_flow.register_node(
            plugin_name="fork-gate",
            node_type=NodeType.GATE,
            plugin_version="1.0",
            config={},
            node_id="gate-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.data_flow.register_node(
            plugin_name="downstream-transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id="transform-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Config gate: forks on "true" (always fires)
        gate_config = GateSettings(
            name="fork-gate",
            input="source_out",
            condition="True",
            routes={"true": "fork", "false": "sink_c"},
            fork_to=["sink_a", "sink_b"],
        )

        # Transform is the gate's continuation successor — must NOT execute for fork children
        transform = _make_mock_transform(
            node_id="transform-1",
            result=TransformResult.success(make_pipeline_row({"value": 99, "transformed": True}), success_reason={"action": "test"}),
        )

        processor = _make_processor(
            factory,
            source_on_success="sink_c",
            branch_to_sink={BranchName("sink_a"): "sink_a", BranchName("sink_b"): "sink_b"},
            sink_names=frozenset({"sink_a", "sink_b", "sink_c"}),
            node_step_map={source_node: 0, gate_node: 1, transform_node: 2},
            node_to_next={source_node: gate_node, gate_node: transform_node, transform_node: None},
            node_to_plugin={gate_node: gate_config, transform_node: transform},
        )

        # Mock gate executor to return FORK outcome with two child tokens.
        # This isolates the fork routing logic from gate execution infrastructure.
        def mock_execute_config_gate(gate_config, node_id, token, ctx, token_manager=None, *, attempt_offset=0):
            child_a = TokenInfo(
                row_id=token.row_id,
                token_id="token-fork-a",
                row_data=token.row_data,
                lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-sink_a", member_key="sink_a"),),
            )
            child_b = TokenInfo(
                row_id=token.row_id,
                token_id="token-fork-b",
                row_data=token.row_data,
                lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-sink_b", member_key="sink_b"),),
            )
            _persist_token_for_scheduler(factory, child_a)
            _persist_token_for_scheduler(factory, child_b)
            fork_action = RoutingAction.fork_to_paths(["sink_a", "sink_b"])
            fork_result = GateResult(
                row=token.row_data.to_dict(),
                action=fork_action,
                contract=token.row_data.contract,
            )
            fork_result.input_hash = "test-hash"
            fork_result.output_hash = "test-hash"
            fork_result.duration_ms = 0.1
            return GateOutcome(
                result=fork_result,
                updated_token=token,
                child_tokens=[child_a, child_b],
            )

        processor._gate_executor.execute_config_gate = mock_execute_config_gate  # type: ignore[method-assign]

        source_row = _make_source_row()
        results = processor.process_row(
            row_index=0,
            source_row=source_row,
            transforms=[],
            ctx=ctx,
            source_row_index=0,
            ingest_sequence=0,
        )

        # Parent should be FORKED
        forked = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.TRANSIENT, TerminalPath.FORK_PARENT)]
        assert len(forked) == 1

        # Fork children should complete at their branch sinks
        completed = [r for r in results if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)]
        assert len(completed) == 2
        sink_names = sorted(r.sink_name for r in completed if r.sink_name is not None)
        assert sink_names == ["sink_a", "sink_b"]

        # Downstream transform must NOT have been called for fork children
        transform.process.assert_not_called()

    def test_overlapping_branch_to_coalesce_and_branch_to_sink_raises(self) -> None:
        """A branch name in both branch_to_coalesce and branch_to_sink is an invariant violation."""
        _db, factory = _make_factory()
        with pytest.raises(OrchestrationInvariantError, match="both branch_to_coalesce and branch_to_sink"):
            _make_processor(
                factory,
                source_on_success="output",
                branch_to_coalesce={BranchName("path_a"): CoalesceName("merge_point")},
                branch_to_sink={BranchName("path_a"): "direct_sink"},
                coalesce_node_ids={CoalesceName("merge_point"): NodeID("coalesce-0")},
                sink_names=frozenset({"output", "direct_sink"}),
                node_step_map={NodeID("source-0"): 0, NodeID("coalesce-0"): 1},
                node_to_next={NodeID("source-0"): None},
            )


# =============================================================================
# _process_single_token: Inner Traversal Cycle Guard
# =============================================================================


class TestInnerTraversalCycleGuard:
    """Tests for the inner_iterations cycle guard in _process_single_token."""

    def test_cycle_in_node_to_next_raises(self) -> None:
        """Cycle in node_to_next map triggers inner traversal iteration guard.

        Uses structural nodes (not in node_to_plugin) to avoid DB interactions
        during traversal — structural nodes are traversed but not executed.
        """
        _db, factory = _make_factory()

        # Build a topology with a cycle of structural nodes: s-1 -> s-2 -> s-1
        s1 = NodeID("s-1")
        s2 = NodeID("s-2")

        processor = _make_processor(
            factory,
            node_to_plugin={},  # no plugin nodes — all structural
            node_to_next={
                NodeID("source-0"): s1,
                s1: s2,
                s2: s1,  # cycle back
            },
            node_step_map={NodeID("source-0"): 0, s1: 1, s2: 2},
            structural_node_ids=frozenset({NodeID("source-0"), s1, s2}),
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 1})

        with pytest.raises(OrchestrationInvariantError, match=r"Inner traversal exceeded.*Possible cycle"):
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=s1,
                attempt_offset=0,
            )


# =============================================================================
# _maybe_coalesce_token
# =============================================================================


class TestMaybeCoalesceToken:
    """Tests for _maybe_coalesce_token branch routing."""

    def test_no_coalesce_executor_returns_not_handled(self) -> None:
        """Without coalesce_executor, always returns (False, None)."""
        _, factory = _make_factory()
        processor = _make_processor(factory, coalesce_executor=None)
        token = make_token_info()

        handled, result = processor._maybe_coalesce_token(
            token,
            current_node_id=NodeID("coalesce::merge"),
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
            child_items=[],
        )

        assert handled is False
        assert result is None

    def test_token_without_branch_returns_not_handled(self) -> None:
        """Token without branch_name is not a fork child, skip coalesce."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 1},
        )
        token = make_token_info()
        # Ensure branch_name is None
        token = TokenInfo(
            row_id=token.row_id,
            token_id=token.token_id,
            row_data=token.row_data,
        )

        handled, _result = processor._maybe_coalesce_token(
            token,
            current_node_id=NodeID("coalesce::merge"),
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
            child_items=[],
        )

        assert handled is False

    def test_current_node_not_coalesce_node_returns_not_handled(self) -> None:
        """Coalesce is only triggered when traversal reaches the coalesce node."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 5},
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        handled, _result = processor._maybe_coalesce_token(
            token,
            current_node_id=NodeID("transform-3"),
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
            child_items=[],
        )

        assert handled is False

    def test_coalesce_held_returns_handled_with_none(self) -> None:
        """Token accepted but not all branches arrived → handled=True, result=None."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(held=True, merged_token=None)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 2},
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        handled, result = processor._maybe_coalesce_token(
            token,
            current_node_id=NodeID("coalesce::merge"),
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
            child_items=[],
        )

        assert handled is True
        assert result is None

    def test_coalesce_failure_always_records_through_settlement_channel(self) -> None:
        """Task 6 (spec §6.1): the executor never records a consumed sibling's
        terminal outcome itself anymore — the caller always does, through the
        settlement channel; there is structurally only one write site now.
        Fix round 1 (Ruling 37) deleted `CoalesceOutcome.outcomes_recorded`
        entirely — it was constant-False with zero consumers once this task
        landed — so this outcome no longer carries the field at all.

        Slice 3 re-pin (ADR-030 §E.2): the accept-time failure surfaces from
        the journal-first intake (the arrival blocked first, then the
        adoption-time executor accept produced the failure), not in-claim.
        """
        _, factory = _make_factory()
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(
            held=False,
            merged_token=None,
            failure_reason="merge_failed:path_b_lost",
            consumed_tokens=(token,),
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 2},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        _persist_blocked_scheduler_work(factory, processor, token, node_id=NodeID("coalesce::merge"), barrier_key="merge", adopted=False)
        processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(
            token=token, barrier_key="merge", arrived_monotonic=processor._clock.monotonic()
        )

        with (
            patch.object(factory.data_flow, "record_token_outcome_leader") as record_outcome,
            # A group failure is terminalized and surfaced by the processor's
            # one failed-group seam (settle_failed_coalesce_group), which
            # emits through the processor attribute at call time.
            patch.object(processor, "_emit_token_completed") as emit_token_completed,
        ):
            results, child_items = processor._run_barrier_intake_pass(ctx)

        assert child_items == []
        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
        record_outcome.assert_called_once()
        assert record_outcome.call_args.kwargs["ref"].token_id == "token-1"
        emit_token_completed.assert_called_once()
        # Backdated accept timing (§H 476): the intake passed an explicit
        # monotonic arrival anchor derived from barrier_blocked_at.
        assert coalesce.accept.call_args.kwargs["arrival_time"] is not None

    def test_coalesce_merged_at_terminal_emits_pending_sink_handoff(self) -> None:
        """All branches arrived at terminal coalesce → durable COALESCED handoff.

        Slice 3 re-pin (ADR-030 §E.2/§E.3): the merge fires from the
        journal-first intake; for a TERMINAL coalesce the COALESCED output is
        emitted as a fresh PENDING_SINK row atomically with the consumed
        branches (F1/D6) — the old in-claim memory-only ride between the
        consumption and the claim disposition is gone.
        """
        db, factory = _make_factory()
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={},
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        merged_token = make_token_info(row_id=row.row_id, token_id="merged-1", data={"merged": True})
        factory.data_flow.create_token(
            row.row_id,
            token_id="merged-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(
            held=False, merged_token=merged_token, consumed_tokens=(token,), join_group_id="join-1"
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 3},
            coalesce_on_success_map={CoalesceName("merge"): "output"},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        _persist_blocked_scheduler_work(factory, processor, token, node_id=NodeID("coalesce::merge"), barrier_key="merge", adopted=False)
        processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(
            token=token, barrier_key="merge", arrived_monotonic=processor._clock.monotonic()
        )

        results, child_items = processor._run_barrier_intake_pass(ctx)

        # Terminal coalesce: the COALESCED sink-bound result is emitted as a
        # fresh PENDING_SINK row in the SAME atomic completion (no READY hop)
        # — the merged output is journal-durable before the sink write.
        assert child_items == []
        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
        assert results[0].sink_name == "output"
        assert results[0].scheduler_pending_sink is True
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            rows = dict(
                conn.execute(
                    select(token_work_items_table.c.token_id, token_work_items_table.c.status).where(
                        token_work_items_table.c.run_id == "test-run"
                    )
                ).all()
            )
        assert rows["merged-1"] == "pending_sink"
        assert rows["token-1"] == "terminal"

    def test_coalesce_merged_at_terminal_missing_sink_mapping_raises(self) -> None:
        """Terminal coalesce merge without sink mapping is an internal bug.

        Slice 3 re-pin (ADR-030 §E.2): the terminal fire resolves the sink to
        build the durable PENDING_SINK emission, so the invariant raises from
        the intake pass.
        """
        _, factory = _make_factory()
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={},
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        merged_token = make_token_info(row_id=row.row_id, token_id="merged-1", data={"merged": True})
        factory.data_flow.create_token(
            row.row_id,
            token_id="merged-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(
            held=False, merged_token=merged_token, consumed_tokens=(token,), join_group_id="join-1"
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 3},
            # Intentionally omit coalesce_on_success_map
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        _persist_blocked_scheduler_work(factory, processor, token, node_id=NodeID("coalesce::merge"), barrier_key="merge", adopted=False)
        processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(
            token=token, barrier_key="merge", arrived_monotonic=processor._clock.monotonic()
        )

        with pytest.raises(OrchestrationInvariantError, match="Coalesce 'merge' not in on_success map"):
            processor._run_barrier_intake_pass(ctx)

    def test_coalesce_merged_at_non_terminal_queues_work(self) -> None:
        """Merged at non-terminal step → child work item added, no result.

        F1/D6: the merged child's READY continuation is journal-durable the
        moment the coalesce fires (inserted by the atomic barrier completion),
        so the merged token and its row must exist in the audit DB.
        """
        db, factory = _make_factory()
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={},
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        factory.data_flow.register_node(
            plugin_name="coalesce:merge",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id="coalesce::merge",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        merged_token = make_token_info(row_id=row.row_id, token_id="merged-1", data={"merged": True})
        factory.data_flow.create_token(
            row.row_id,
            token_id="merged-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = CoalesceOutcome(
            held=False, merged_token=merged_token, consumed_tokens=(token,), join_group_id="join-1"
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 2},
            node_to_next={NodeID("coalesce::merge"): NodeID("transform-5")},
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        # Slice 3 re-pin (ADR-030 §E.2): the merge fires from the
        # journal-first intake, not from an in-claim accept.
        _persist_blocked_scheduler_work(factory, processor, token, node_id=NodeID("coalesce::merge"), barrier_key="merge", adopted=False)
        processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(
            token=token, barrier_key="merge", arrived_monotonic=processor._clock.monotonic()
        )

        results, child_items = processor._run_barrier_intake_pass(ctx)

        assert results == []
        assert len(child_items) == 1
        assert child_items[0].current_node_id == NodeID("coalesce::merge")
        # The merged child's continuation is already journal-durable (F1/D6).
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, node_id = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.node_id).where(
                    token_work_items_table.c.token_id == "merged-1"
                )
            ).one()
        assert (status, node_id) == ("ready", "coalesce::merge")

    def test_invalid_coalesce_outcome_state_raises_invariant(self) -> None:
        """CoalesceOutcome must be held, merged, or failed; empty state is invalid.

        Slice 3 re-pin (ADR-030 §E.2): the executor accept runs at intake, so
        the invalid-state invariant fires from the intake pass.
        """
        _db, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.accept.return_value = SimpleNamespace(
            held=False,
            merged_token=None,
            failure_reason=None,
            late_arrival=False,
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 2},
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        _persist_blocked_scheduler_work(factory, processor, token, node_id=NodeID("coalesce::merge"), barrier_key="merge", adopted=False)
        processor._live_barrier_holds[token.token_id] = _LiveBarrierHold(
            token=token, barrier_key="merge", arrived_monotonic=processor._clock.monotonic()
        )

        with pytest.raises(OrchestrationInvariantError, match="invalid state"):
            processor._run_barrier_intake_pass(ctx)


# =============================================================================
# complete_coalesce_merge (out-of-claim timeout/EOF coalesce fire, F1/D6)
# =============================================================================


class TestCompleteCoalesceMerge:
    """Real-journal proof of the orchestrator-facing atomic coalesce merge verb."""

    def test_complete_coalesce_merge_consumes_siblings_and_drives_merged_child(self) -> None:
        """ONE atomic transition: held branches -> TERMINAL, merged child READY+claimed.

        Out-of-claim fires (timeout/EOF) have no LEASED triggering token: every
        held branch is BLOCKED, and the merged child must be journal-durable
        before the consumed branches are terminalized. The drain's initial
        enqueue then reconciles idempotently against the READY row inserted by
        the barrier completion (strict field equality) and claims it.
        """
        db, factory = _make_factory()
        coalesce_node = NodeID("coalesce::merge")
        factory.data_flow.register_node(
            plugin_name="coalesce:merge",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id=str(coalesce_node),
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        payload = make_row({"value": 7})
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data=payload.to_dict(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        # One held branch, BLOCKED at the coalesce barrier through the
        # production verbs (enqueue -> claim -> mark_blocked).
        factory.data_flow.create_token(
            row.row_id,
            token_id="token-held-a",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        datetime.now(UTC)
        factory.scheduler.enqueue_ready(
            token_id="token-held-a",
            row_id=row.row_id,
            node_id=str(coalesce_node),
            step_index=1,
            ingest_sequence=0,
            row_payload_json=factory.scheduler.serialize_row_payload(payload),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),),
            coalesce_node_id=str(coalesce_node),
            coalesce_name="merge",
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        claimed = factory.scheduler.claim_ready(
            lease_owner="seeder",
            lease_seconds=60,
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="seeder"),
        )
        assert claimed is not None and claimed.token_id == "token-held-a"
        factory.scheduler.mark_blocked(
            work_item_id=claimed.work_item_id,
            row_payload_json=claimed.row_payload_json,
            queue_key=None,
            barrier_key="merge",
            expected_lease_owner="seeder",
            member_token=member_token_for(factory._db.engine, run_id="test-run", worker_id="seeder"),
        )
        held_token = TokenInfo(
            row_id=row.row_id,
            token_id="token-held-a",
            row_data=payload,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="path_a"),),
        )
        merged_token = make_token_info(row_id=row.row_id, token_id="merged-1", data={"value": 7})
        factory.data_flow.create_token(
            row.row_id,
            token_id="merged-1",
            join_group_id="join-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        processor = _make_processor(
            factory,
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            node_step_map={NodeID("source-0"): 0, coalesce_node: 1},
            node_to_next={NodeID("source-0"): coalesce_node, coalesce_node: None},
            coalesce_on_success_map={CoalesceName("merge"): "merged_sink"},
            scheduler=factory.scheduler,
        )
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        results = processor.complete_coalesce_merge(
            coalesce_name=CoalesceName("merge"),
            consumed_tokens=(held_token,),
            merged_token=merged_token,
            coalesce_node_id=coalesce_node,
            ctx=ctx,
        )

        # The merged token was driven to its terminal coalesce sink handoff:
        # the merged token resolves the coalesce on_success sink.
        assert len(results) == 1
        assert results[0].token.token_id == "merged-1"
        assert (results[0].outcome, results[0].path) == (TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert results[0].sink_name == "merged_sink"
        assert results[0].scheduler_pending_sink is True

        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            statuses = dict(
                conn.execute(
                    select(token_work_items_table.c.token_id, token_work_items_table.c.status).where(
                        token_work_items_table.c.token_id.in_(["token-held-a", "merged-1"])
                    )
                ).all()
            )
        assert statuses == {
            "token-held-a": "terminal",
            "merged-1": "pending_sink",
        }


# =============================================================================
# _settle_member_losses (COALESCE arm) — WS3 Task 5 settle-member seam.
# Pre-WS3 these called the retired _notify_coalesce_of_lost_branch directly;
# renamed onto the unified seam, unchanged args (_make_processor synthesizes
# a GroupBindingRegistry from the legacy branch_to_coalesce/coalesce_node_ids
# kwargs these tests already pass — see test_settle_member_seam.py for the
# seam's own walk/dispatch tests against a real registry).
# =============================================================================


class TestNotifyCoalesceOfLostBranch:
    """Tests for the branch loss notification to coalesce executor, exercised
    through `_settle_member_losses` (spec §6.1)."""

    def test_empty_aggregation_commit_failure_cannot_fire_terminal_coalesce(self) -> None:
        """Fork loss stays staged when the owning aggregation transaction fails."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 5},
            coalesce_on_success_map={CoalesceName("merge"): "output"},
        )
        token = make_token_info(
            row_id="row-1",
            token_id="token-1",
            data={"value": 1},
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        fctx = _FlushContext(
            node_id=NodeID("aggregate-1"),
            transform=_make_mock_transform(node_id="aggregate-1", name="batch-transform"),
            settings=AggregationSettings(
                name="agg",
                plugin="batch-plugin",
                input="source",
                on_error="discard",
                trigger={"count": 1},
            ),
            buffered_tokens=(token,),
            batch_id="batch-1",
            expand_parent_token=token,
            triggering_token=token,
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
        )
        results, child_items = processor._route_empty_emission_results(fctx)

        coalesce.notify_branch_lost.assert_not_called()
        assert len(processor._pending_group_losses) == 1
        with (
            patch.object(processor._scheduler, "complete_barrier", side_effect=RuntimeError("aggregation commit failed")),
            patch.object(processor._barrier_intake, "replay_durable_group_losses") as replay,
            patch.object(processor, "_complete_coalesce_fire") as complete_coalesce,
            pytest.raises(RuntimeError, match="aggregation commit failed"),
        ):
            processor._complete_aggregation_flush(
                NodeID("aggregate-1"),
                results,
                [token],
                child_items,
                batch_id="batch-1",
                members_terminate=True,
            )

        coalesce.notify_branch_lost.assert_not_called()
        replay.assert_not_called()
        complete_coalesce.assert_not_called()
        assert len(processor._pending_group_losses) == 1

        ordered_events: list[str] = []

        def committed_barrier(**kwargs: object) -> None:
            group_losses = kwargs["group_losses"]
            assert isinstance(group_losses, tuple)
            assert len(group_losses) == 1
            ordered_events.append("aggregation_committed")

        def replay_after_commit() -> tuple[object, ...]:
            ordered_events.append("loss_replayed")
            return ()

        with (
            patch.object(processor._scheduler, "complete_barrier", side_effect=committed_barrier),
            patch.object(processor._barrier_intake, "replay_durable_group_losses", side_effect=replay_after_commit),
        ):
            processor._complete_aggregation_flush(
                NodeID("aggregate-1"),
                results,
                [token],
                child_items,
                batch_id="batch-1",
                members_terminate=True,
            )

        assert ordered_events == ["aggregation_committed", "loss_replayed"]
        assert processor._pending_group_losses == []

    def test_no_coalesce_executor_returns_empty(self) -> None:
        """Without coalesce_executor, returns empty list."""
        _, factory = _make_factory()
        processor = _make_processor(factory, coalesce_executor=None)
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            [],
        )

        assert results == []

    def test_token_without_branch_returns_empty(self) -> None:
        """Non-forked token → no coalesce notification needed."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        processor = _make_processor(factory, coalesce_executor=coalesce)
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            [],
        )

        assert results == []

    def test_branch_not_in_coalesce_map_returns_empty(self) -> None:
        """Branch without coalesce mapping → no notification."""
        _, factory = _make_factory()
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={},  # Empty map
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-unmapped_branch", member_key="unmapped_branch"),),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            [],
        )

        assert results == []

    def test_lost_branch_with_failure_returns_sibling_results(self) -> None:
        """Branch loss causing coalesce failure returns FAILED sibling results."""
        _, factory = _make_factory()
        # Task 6 (spec §6.1): the settlement channel pops the consumed
        # sibling's own FORK frame before walking its remaining lineage, so
        # a crafted consumed token needs real fork lineage — the same
        # fork event as `token` below, a different (sibling) branch.
        sibling_token = make_token_info(
            data={"value": 99},
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_b"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = CoalesceOutcome(
            held=False,
            merged_token=None,
            failure_reason="not enough branches",
            consumed_tokens=(sibling_token,),
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 3},
        )
        _persist_blocked_scheduler_work(
            factory,
            processor,
            sibling_token,
            node_id=NodeID("coalesce::merge"),
            barrier_key="merge",
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            [],
        )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.FAILURE, TerminalPath.UNROUTED)
        assert results[0].error is not None
        assert "not enough branches" in results[0].error.message

    def test_lost_branch_triggers_terminal_merge(self) -> None:
        """Branch loss triggers merge at terminal coalesce → COALESCED result."""
        _, factory = _make_factory()
        merged_token = make_token_info(data={"merged": True})
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = CoalesceOutcome(
            held=False,
            merged_token=merged_token,
            failure_reason=None,
            consumed_tokens=(),
            join_group_id="join-1",
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 5},
            coalesce_on_success_map={CoalesceName("merge"): "output"},
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            [],
        )

        assert len(results) == 1
        _assert_outcome_pair(results[0], TerminalOutcome.SUCCESS, TerminalPath.COALESCED)
        assert results[0].sink_name == "output"

    def test_lost_branch_terminal_merge_missing_sink_mapping_raises(self) -> None:
        """Terminal coalesce merge from branch loss must have sink mapping."""
        _, factory = _make_factory()
        merged_token = make_token_info(data={"merged": True})
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = CoalesceOutcome(
            held=False,
            merged_token=merged_token,
            failure_reason=None,
            consumed_tokens=(),
            join_group_id="join-1",
        )
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 5},
            # Intentionally omit coalesce_on_success_map
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        with pytest.raises(OrchestrationInvariantError, match="Coalesce 'merge' not in on_success map"):
            processor._settle_member_losses(
                token,
                "quarantined:bad_value",
                [],
            )

    def test_lost_branch_triggers_nonterminal_merge_queues_work(self) -> None:
        """Branch loss triggers merge at non-terminal step → queues work.

        F1/D6: the merged child's READY continuation is journal-durable the
        moment the merge fires, so the merged token and its row must exist
        in the audit DB.
        """
        db, factory = _make_factory()
        row = factory.data_flow.create_row_with_token(
            source_node_id="source-0",
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={},
            coordination_token=leader_coordination_token(factory, "test-run"),
        )[0]
        factory.data_flow.register_node(
            plugin_name="coalesce:merge",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id="coalesce::merge",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        merged_token = make_token_info(row_id=row.row_id, token_id="merged-1", data={"merged": True})
        factory.data_flow.create_token(
            row.row_id,
            token_id="merged-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = CoalesceOutcome(
            held=False,
            merged_token=merged_token,
            failure_reason=None,
            consumed_tokens=(),
            join_group_id="join-1",
        )
        child_items: list[WorkItem] = []
        processor = _make_processor(
            factory,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
            node_step_map={NodeID("coalesce::merge"): 3},
            node_to_next={NodeID("coalesce::merge"): NodeID("transform-4")},
        )
        token = TokenInfo(
            row_id=row.row_id,
            token_id="token-1",
            row_data=make_row({}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        results = processor._settle_member_losses(
            token,
            "quarantined:bad_value",
            child_items,
        )

        assert results == []
        assert len(child_items) == 1
        assert child_items[0].current_node_id == NodeID("coalesce::merge")
        # Flat/unnested merge (merged_token has no branch_name): the
        # continuation's coalesce_name/coalesce_node_id both resolve to the
        # just-completed barrier — both supplied, restoring
        # WorkItemFactory.create's mismatch cross-check on this path
        # (elspeth-0bd2cde19a round-2 F4/N3, same pattern as
        # _fire_coalesce_merge/complete_coalesce_merge).
        assert child_items[0].coalesce_name == CoalesceName("merge")
        assert child_items[0].coalesce_node_id == NodeID("coalesce::merge")
        # The merged child's continuation is already journal-durable (F1/D6).
        from sqlalchemy import select

        from elspeth.core.landscape.schema import token_work_items_table

        with db.connection() as conn:
            status, node_id = conn.execute(
                select(token_work_items_table.c.status, token_work_items_table.c.node_id).where(
                    token_work_items_table.c.token_id == "merged-1"
                )
            ).one()
        assert (status, node_id) == ("ready", "coalesce::merge")


class TestRoutingInvariantFailures:
    """Regression tests for strict fail-closed routing invariants."""

    def test_unhandled_config_gate_routing_kind_raises(self) -> None:
        """ROUTE outcome with no sink_name or next_node_id is caught at GateOutcome construction."""
        with pytest.raises(ValueError, match="ROUTE action must have exactly one of sink_name, next_node_id, or discarded=True"):
            GateOutcome(
                result=GateResult(
                    row={"value": 10},
                    action=RoutingAction.route("branch_a"),
                    contract=_make_contract(),
                ),
                updated_token=make_token_info(data={"value": 10}),
                sink_name=None,
                next_node_id=None,
                child_tokens=(),
            )

    def test_missing_effective_sink_raises_invariant(self) -> None:
        """Terminal completion must not fall back when no sink can be resolved."""
        _db, factory = _make_factory()
        source_row = _make_source_row({"value": 10})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        processor = _make_processor(
            factory,
            source_on_success="   ",
            node_step_map={NodeID("source-0"): 0},
            node_to_next={NodeID("source-0"): None},
        )

        with pytest.raises(OrchestrationInvariantError, match="No effective sink for token"):
            processor.process_row(
                row_index=0,
                source_row=source_row,
                transforms=[],
                ctx=ctx,
                source_row_index=0,
                ingest_sequence=0,
            )


class TestWorkItemCoalesceInvariant:
    """WorkItem must carry complete coalesce metadata together."""

    def test_missing_coalesce_name_with_coalesce_node_id_raises(self) -> None:
        """Coalesce node without coalesce name is an invariant violation."""
        token = make_token_info(data={"value": 1})
        with pytest.raises(OrchestrationInvariantError, match="coalesce fields must be both set or both None"):
            WorkItem(
                token=token,
                current_node_id=NodeID("coalesce::merge"),
                coalesce_node_id=NodeID("coalesce::merge"),
                coalesce_name=None,
            )


# =============================================================================
# Regression: hscm.2 — Coalesce traversal invariant check
# =============================================================================


class TestCoalesceTraversalInvariant:
    """Regression tests for hscm.2: tokens with coalesce metadata must not
    start processing downstream of their coalesce point."""

    def test_work_item_downstream_of_coalesce_raises_invariant_error(self) -> None:
        """A work item starting past the coalesce node must raise OrchestrationInvariantError."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        # Build DAG: source → transform → coalesce → downstream
        source_node = NodeID("source-0")
        transform_node = NodeID("transform-1")
        coalesce_node = NodeID("coalesce-2")
        downstream_node = NodeID("downstream-3")

        # Register coalesce node for FK constraints
        factory.data_flow.register_node(
            plugin_name="coalesce",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id="coalesce-2",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.data_flow.register_node(
            plugin_name="downstream",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            node_id="downstream-3",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        transform = _make_mock_transform(
            node_id="downstream-3",
            name="downstream",
            on_success="output",
        )

        processor = _make_processor(
            factory,
            node_step_map={
                source_node: 0,
                transform_node: 1,
                coalesce_node: 2,
                downstream_node: 3,
            },
            node_to_next={
                source_node: transform_node,
                transform_node: coalesce_node,
                coalesce_node: downstream_node,
                downstream_node: None,
            },
            node_to_plugin={downstream_node: transform},
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            coalesce_on_success_map={CoalesceName("merge"): "output"},
        )

        # Create a malformed work item starting PAST the coalesce node
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=make_row({"value": 1}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        with pytest.raises(OrchestrationInvariantError, match="downstream of coalesce"):
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=downstream_node,  # step 3 > coalesce step 2
                coalesce_node_id=coalesce_node,
                coalesce_name=CoalesceName("merge"),
                attempt_offset=0,
            )

    def test_work_item_at_coalesce_does_not_raise(self) -> None:
        """A work item starting exactly at the coalesce node should not raise."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        coalesce_node = NodeID("coalesce-1")

        factory.data_flow.register_node(
            plugin_name="coalesce",
            node_type=NodeType.COALESCE,
            plugin_version="1.0",
            config={},
            node_id="coalesce-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        processor = _make_processor(
            factory,
            source_on_success="output",
            node_step_map={source_node: 0, coalesce_node: 1},
            node_to_next={source_node: coalesce_node, coalesce_node: None},
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            coalesce_on_success_map={CoalesceName("merge"): "output"},
        )

        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=make_row({"value": 1}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )
        # Should not raise — at coalesce node, not past it.
        # ADR-030 §B (slice 5): without coalesce_executor (follower mode),
        # _maybe_coalesce_token returns (True, None) → mark_blocked hand-off.
        # result is None and child_items is [] — the outer drain calls
        # mark_blocked so the leader's next intake adopts the arrival.
        result, child_items = processor._process_single_token(
            token=token,
            ctx=ctx,
            current_node_id=coalesce_node,
            coalesce_node_id=coalesce_node,
            coalesce_name=CoalesceName("merge"),
            attempt_offset=0,
        )
        # Follower coalesce barrier: (None, []) → mark_blocked, not a completion.
        assert result is None
        assert child_items == []


class TestRowUnionTraversalInvariant:
    def test_work_item_downstream_of_row_union_raises_invariant_error(self) -> None:
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_node = NodeID("source-0")
        row_union_node = NodeID("row-union-1")
        downstream_node = NodeID("downstream-2")
        processor = _make_processor(
            factory,
            source_on_success="output",
            node_step_map={source_node: 0, row_union_node: 1, downstream_node: 2},
            node_to_next={source_node: row_union_node, row_union_node: downstream_node, downstream_node: None},
            node_to_plugin={},
            row_union_node_ids={RowUnionName("variant_union"): row_union_node},
            structural_node_ids=frozenset({source_node, row_union_node, downstream_node}),
        )
        token = make_token_info(
            row_id="row-1",
            token_id="tok-1",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-variant_a", member_key="variant_a"),),
        )
        _persist_token_for_scheduler(factory, token)

        with pytest.raises(OrchestrationInvariantError, match="downstream of row_union"):
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=downstream_node,
                row_union_node_id=row_union_node,
                row_union_name=RowUnionName("variant_union"),
                attempt_offset=0,
            )


class TestTerminalWorkItemInvariant:
    """Tests for current_node_id=None work-item validation."""

    def test_none_current_node_without_sink_context_raises(self) -> None:
        """None current_node_id must not default to source_on_success silently."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        processor = _make_processor(factory, source_on_success="source_sink")
        token = make_token_info(data={"value": 1})

        with pytest.raises(OrchestrationInvariantError, match="current_node_id=None"):
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=None,
                attempt_offset=0,
            )

    def test_none_current_node_with_inherited_sink_is_allowed(self) -> None:
        """Explicit on_success_sink context allows terminal completion with None node."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        processor = _make_processor(factory, source_on_success="source_sink")
        token = make_token_info(data={"value": 1})

        result, _child_items = processor._process_single_token(
            token=token,
            ctx=ctx,
            current_node_id=None,
            on_success_sink="terminal_sink",
            attempt_offset=0,
        )

        assert result is not None
        assert not isinstance(result, tuple)
        _assert_outcome_pair(result, TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW)
        assert result.sink_name == "terminal_sink"


# =============================================================================
# Regression: P1-2026-02-14 — Gate sink routing notifies coalesce of lost branch
# =============================================================================


class TestGateSinkRoutingNotifiesCoalesce:
    """Regression tests for P1-2026-02-14: gate-routed-to-sink tokens on fork
    branches must notify coalesce of the lost branch, same as transform error
    paths (max retries, quarantine, error-routed)."""

    def test_gate_sink_route_notifies_coalesce_of_lost_branch(self) -> None:
        """Gate routing a fork-branch token to a sink must call _settle_member_losses.

        Before fix: gate sink routing returned immediately without notifying coalesce,
        causing sibling branches to remain held until timeout/end-of-source.
        After fix: coalesce is notified with the category token 'gate_routed_to_sink'
        (bare token per elspeth-74b795208f — the reason column is String(64) and the
        sink name is durably recorded on the ROUTED token outcome instead).
        """
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")

        factory.data_flow.register_node(
            plugin_name="router-gate",
            node_type=NodeType.GATE,
            plugin_version="1.0",
            config={},
            node_id="gate-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_config = GateSettings(
            name="router-gate",
            input="default",
            condition="True",
            routes={"true": "error_sink", "false": "default"},
        )

        coalesce = create_autospec(CoalesceExecutor, instance=True)
        # notify_branch_lost returns None = no immediate consequence
        coalesce.notify_branch_lost.return_value = None

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={source_node: 0, gate_node: 1},
            node_to_next={source_node: gate_node, gate_node: None},
            node_to_plugin={gate_node: gate_config},
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
        )

        # Create a fork-branch token
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-branch-a",
            row_data=make_row({"value": 42}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        # Mock gate executor to return a sink routing outcome
        sink_outcome = GateOutcome(
            result=GateResult(
                row={"value": 42},
                action=RoutingAction.route("true"),
                contract=_make_contract(),
            ),
            updated_token=token,
            sink_name="error_sink",
        )

        with patch.object(
            processor._gate_executor,
            "execute_config_gate",
            return_value=sink_outcome,
        ):
            result, _child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                coalesce_node_id=NodeID("coalesce::merge"),
                coalesce_name=CoalesceName("merge"),
                attempt_offset=0,
            )

        # Gate should produce ROUTED result
        if isinstance(result, tuple):
            routed = [r for r in result if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED)]
            assert len(routed) == 1
            assert routed[0].sink_name == "error_sink"
        else:
            assert result is not None
            _assert_outcome_pair(result, TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED)
            assert result.sink_name == "error_sink"

        # Coalesce must have been notified of the lost branch, keyed on the
        # token's fork_group_id (WS4 Task 8 re-key), not row_id.
        coalesce.notify_branch_lost.assert_called_once_with(
            coalesce_name=CoalesceName("merge"),
            fork_group_id="fg-path_a",
            lost_branch="path_a",
            reason="gate_routed_to_sink",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

    def test_gate_sink_route_with_coalesce_failure_returns_sibling_results(self) -> None:
        """Gate sink routing that triggers coalesce failure returns sibling FAILED results."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")

        factory.data_flow.register_node(
            plugin_name="router-gate",
            node_type=NodeType.GATE,
            plugin_version="1.0",
            config={},
            node_id="gate-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_config = GateSettings(
            name="router-gate",
            input="default",
            condition="True",
            routes={"true": "error_sink", "false": "default"},
        )

        # Task 6 (spec §6.1): the settlement channel pops the consumed
        # sibling's own FORK frame before walking its remaining lineage, so
        # a crafted consumed token needs real fork lineage.
        sibling_token = make_token_info(
            data={"value": 99},
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_b"),),
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = CoalesceOutcome(
            held=False,
            merged_token=None,
            failure_reason="require_all policy violated",
            consumed_tokens=(sibling_token,),
        )

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={source_node: 0, gate_node: 1},
            node_to_next={source_node: gate_node, gate_node: None},
            node_to_plugin={gate_node: gate_config},
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): CoalesceName("merge")},
            coalesce_node_ids={CoalesceName("merge"): NodeID("coalesce::merge")},
        )
        _persist_blocked_scheduler_work(
            factory,
            processor,
            sibling_token,
            node_id=NodeID("coalesce::merge"),
            barrier_key="merge",
        )

        token = TokenInfo(
            row_id="row-1",
            token_id="tok-branch-a",
            row_data=make_row({"value": 42}),
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),),
        )

        sink_outcome = GateOutcome(
            result=GateResult(
                row={"value": 42},
                action=RoutingAction.route("true"),
                contract=_make_contract(),
            ),
            updated_token=token,
            sink_name="error_sink",
        )

        with patch.object(
            processor._gate_executor,
            "execute_config_gate",
            return_value=sink_outcome,
        ):
            result, _child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                coalesce_node_id=NodeID("coalesce::merge"),
                coalesce_name=CoalesceName("merge"),
                attempt_offset=0,
            )

        # Result must be a list: ROUTED (current) + FAILED (sibling)
        assert isinstance(result, tuple)
        assert len(result) == 2

        routed = [r for r in result if (r.outcome, r.path) == (TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED)]
        failed = [r for r in result if (r.outcome, r.path) == (TerminalOutcome.FAILURE, TerminalPath.UNROUTED)]
        assert len(routed) == 1
        assert routed[0].sink_name == "error_sink"
        assert len(failed) == 1
        assert failed[0].error is not None
        assert "require_all" in failed[0].error.message

    def test_gate_sink_route_without_branch_skips_coalesce(self) -> None:
        """Gate sink routing a non-fork token does not attempt coalesce notification."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")

        factory.data_flow.register_node(
            plugin_name="router-gate",
            node_type=NodeType.GATE,
            plugin_version="1.0",
            config={},
            node_id="gate-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_config = GateSettings(
            name="router-gate",
            input="default",
            condition="True",
            routes={"true": "error_sink", "false": "default"},
        )

        coalesce = create_autospec(CoalesceExecutor, instance=True)

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={source_node: 0, gate_node: 1},
            node_to_next={source_node: gate_node, gate_node: None},
            node_to_plugin={gate_node: gate_config},
            coalesce_executor=coalesce,
        )

        # Non-fork token: branch_name is None
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=make_row({"value": 42}),
        )

        sink_outcome = GateOutcome(
            result=GateResult(
                row={"value": 42},
                action=RoutingAction.route("true"),
                contract=_make_contract(),
            ),
            updated_token=token,
            sink_name="error_sink",
        )

        with patch.object(
            processor._gate_executor,
            "execute_config_gate",
            return_value=sink_outcome,
        ):
            result, _child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                attempt_offset=0,
            )

        # Should still route correctly
        assert result is not None
        assert not isinstance(result, tuple)
        _assert_outcome_pair(result, TerminalOutcome.SUCCESS, TerminalPath.GATE_ROUTED)
        assert result.sink_name == "error_sink"

        # notify_branch_lost should NOT have been called (no branch_name)
        coalesce.notify_branch_lost.assert_not_called()

    def test_gate_discard_records_terminal_discard_outcome(self) -> None:
        """Gate route target 'discard' records a terminal audit outcome without a sink."""
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")

        factory.data_flow.register_node(
            plugin_name="drop-gate",
            node_type=NodeType.GATE,
            plugin_version="1.0",
            config={},
            node_id="gate-1",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        gate_config = GateSettings(
            name="drop-gate",
            input="default",
            condition="False",
            routes={"true": "main", "false": "discard"},
        )

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={source_node: 0, gate_node: 1},
            node_to_next={source_node: gate_node, gate_node: None},
            node_to_plugin={gate_node: gate_config},
            coalesce_executor=create_autospec(CoalesceExecutor, instance=True),
        )
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=make_row({"value": 42}),
        )
        discard_outcome = GateOutcome(
            result=GateResult(
                row={"value": 42},
                action=RoutingAction.route("false"),
                contract=_make_contract(),
            ),
            updated_token=token,
            discarded=True,
        )
        ctx.work_item = _claim_processor_token(factory, token, node_id="gate-1")

        with (
            patch.object(processor._gate_executor, "execute_config_gate", return_value=discard_outcome) as gate_execution,
            patch.object(factory.data_flow, "record_token_outcome") as record_outcome,
        ):
            result, _child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                attempt_offset=2,
            )

        assert result is not None
        assert not isinstance(result, tuple)
        _assert_outcome_pair(result, TerminalOutcome.SUCCESS, TerminalPath.GATE_DISCARDED)
        assert result.sink_name is None
        assert gate_execution.call_args.kwargs["attempt_offset"] == 2
        record_outcome.assert_called_once()
        assert record_outcome.call_args.kwargs["outcome"] == TerminalOutcome.SUCCESS
        assert record_outcome.call_args.kwargs["path"] == TerminalPath.GATE_DISCARDED


# =============================================================================
# Bug D3: Gate jump past coalesce ordering invariant
# =============================================================================


class TestGateJumpPastCoalesceInvariant:
    """Regression tests for Bug D3: coalesce ordering invariant must be
    re-validated after a gate jump sets node_id = outcome.next_node_id.

    Without the fix, a gate jump past the coalesce node would silently
    bypass join handling because _maybe_coalesce_token only triggers
    on exact node_id equality with coalesce_node_id.
    """

    def test_gate_jump_past_coalesce_raises_invariant_error(self) -> None:
        """Gate routing past a coalesce node must raise OrchestrationInvariantError.

        Setup: source -> gate(step=1) -> coalesce(step=2) -> transform(step=3)
        The gate jumps directly to transform(step=3), skipping coalesce(step=2).
        This must raise because the token would never hit the coalesce barrier.
        """
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")
        coalesce_node = NodeID("coalesce::merge")
        past_coalesce_node = NodeID("transform-3")

        config_gate = GateSettings(
            name="router",
            input="in_conn",
            condition="'skip_ahead'",
            routes={"skip_ahead": "skip_conn"},
        )

        # Need a transform at the jump target with on_success so that
        # resolve_jump_target_sink succeeds before the coalesce check.
        past_coalesce_transform = _make_mock_transform(
            node_id=str(past_coalesce_node),
            name="past-coalesce",
            on_success="some_sink",
        )

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={
                source_node: 0,
                gate_node: 1,
                coalesce_node: 2,
                past_coalesce_node: 3,
            },
            node_to_next={
                source_node: gate_node,
                gate_node: coalesce_node,
                coalesce_node: past_coalesce_node,
                past_coalesce_node: None,
            },
            node_to_plugin={
                gate_node: config_gate,
                past_coalesce_node: past_coalesce_transform,
            },
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
        )

        gate_contract = _make_contract()
        gate_result = GateResult(
            row={"value": 42},
            action=RoutingAction.route("skip_ahead"),
            contract=gate_contract,
        )

        def config_gate_side_effect(*, gate_config, node_id, token, ctx, token_manager=None, attempt_offset=0):
            return GateOutcome(
                result=gate_result,
                updated_token=token,
                next_node_id=past_coalesce_node,  # Jump past coalesce!
            )

        with (
            patch.object(
                processor._gate_executor,
                "execute_config_gate",
                side_effect=config_gate_side_effect,
            ),
            pytest.raises(
                OrchestrationInvariantError,
                match=r"Gate jump moved token.*past its coalesce node",
            ),
        ):
            # Process with coalesce metadata so the invariant check triggers
            token = make_token_info(
                row_id="row-1",
                token_id="tok-1",
                data={"value": 42},
                lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-branch_a", member_key="branch_a"),),
            )
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                coalesce_node_id=coalesce_node,
                coalesce_name=CoalesceName("merge"),
                attempt_offset=0,
            )

    def test_gate_jump_past_row_union_raises_invariant_error(self) -> None:
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")
        row_union_node = NodeID("row-union::variant_union")
        past_row_union_node = NodeID("transform-3")
        config_gate = GateSettings(
            name="router",
            input="in_conn",
            condition="'skip_ahead'",
            routes={"skip_ahead": "skip_conn"},
        )
        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={source_node: 0, gate_node: 1, row_union_node: 2, past_row_union_node: 3},
            node_to_next={
                source_node: gate_node,
                gate_node: row_union_node,
                row_union_node: past_row_union_node,
                past_row_union_node: None,
            },
            node_to_plugin={gate_node: config_gate},
            row_union_node_ids={RowUnionName("variant_union"): row_union_node},
            structural_node_ids=frozenset({source_node, row_union_node, past_row_union_node}),
        )
        gate_result = GateResult(
            row={"value": 42},
            action=RoutingAction.route("skip_ahead"),
            contract=_make_contract(),
        )

        def config_gate_side_effect(*, gate_config, node_id, token, ctx, token_manager=None, attempt_offset=0):
            return GateOutcome(result=gate_result, updated_token=token, next_node_id=past_row_union_node)

        token = make_token_info(
            row_id="row-1",
            token_id="tok-1",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-variant_a", member_key="variant_a"),),
        )
        _persist_token_for_scheduler(factory, token)

        with (
            patch.object(processor._gate_executor, "execute_config_gate", side_effect=config_gate_side_effect),
            patch.object(processor._nav, "resolve_jump_target_sink", return_value="some_sink"),
            pytest.raises(OrchestrationInvariantError, match=r"Gate jump moved token.*past its row_union node"),
        ):
            processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                row_union_node_id=row_union_node,
                row_union_name=RowUnionName("variant_union"),
                attempt_offset=0,
            )

    def test_gate_jump_before_coalesce_is_allowed(self) -> None:
        """Gate jump to a node BEFORE the coalesce node must NOT raise.

        Setup: source -> gate(step=1) -> transform(step=2) -> coalesce(step=3)
        The gate jumps to transform(step=2) which is before coalesce(step=3).
        This is fine — the token still has to pass through the coalesce.
        """
        _db, factory = _make_factory()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        source_node = NodeID("source-0")
        gate_node = NodeID("gate-1")
        transform_node = NodeID("transform-2")
        coalesce_node = NodeID("coalesce::merge")

        config_gate = GateSettings(
            name="router",
            input="in_conn",
            condition="'jump_ok'",
            routes={"jump_ok": "ok_conn"},
        )
        transform = _make_mock_transform(
            node_id=str(transform_node),
            name="passthrough",
            on_success="out_sink",
            result=TransformResult.success(
                make_row({"value": 42}),
                success_reason={"action": "pass"},
            ),
        )

        coalesce_exec = create_autospec(CoalesceExecutor, instance=True)
        coalesce_exec.accept.return_value = CoalesceOutcome(held=True, merged_token=None)

        processor = _make_processor(
            factory,
            source_on_success="default",
            node_step_map={
                source_node: 0,
                gate_node: 1,
                transform_node: 2,
                coalesce_node: 3,
            },
            node_to_next={
                source_node: gate_node,
                gate_node: None,
                transform_node: coalesce_node,
                coalesce_node: None,
            },
            node_to_plugin={
                gate_node: config_gate,
                transform_node: transform,
            },
            coalesce_node_ids={CoalesceName("merge"): coalesce_node},
            coalesce_executor=coalesce_exec,
            coalesce_on_success_map={CoalesceName("merge"): "merge_sink"},
        )

        gate_contract = _make_contract()
        gate_result = GateResult(
            row={"value": 42},
            action=RoutingAction.route("jump_ok"),
            contract=gate_contract,
        )

        def config_gate_side_effect(*, gate_config, node_id, token, ctx, token_manager=None, attempt_offset=0):
            return GateOutcome(
                result=gate_result,
                updated_token=token,
                next_node_id=transform_node,  # Jump to BEFORE coalesce — allowed
            )

        def transform_side_effect(*, transform, token, ctx, attempt=0):
            return (
                TransformResult.success(
                    make_row({"value": 42}),
                    success_reason={"action": "pass"},
                ),
                token,
                None,
            )

        with (
            patch.object(
                processor._gate_executor,
                "execute_config_gate",
                side_effect=config_gate_side_effect,
            ),
            patch.object(
                processor._transform_executor,
                "execute_transform",
                side_effect=transform_side_effect,
            ),
        ):
            token = make_token_info(
                row_id="row-1",
                token_id="tok-1",
                data={"value": 42},
                lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-branch_a", member_key="branch_a"),),
            )
            # Should NOT raise — jump target is before coalesce
            result, _child_items = processor._process_single_token(
                token=token,
                ctx=ctx,
                current_node_id=gate_node,
                coalesce_node_id=coalesce_node,
                coalesce_name=CoalesceName("merge"),
                attempt_offset=0,
            )

        # Token should be held at the coalesce node without emitting a terminal result.
        assert result is None
        assert _child_items == []
        # Slice 3 re-pin (ADR-030 §E.2): acceptance is journal-first — the
        # in-claim accept is gone. The hold is stashed for the next drain
        # iteration's intake (which runs the executor accept post-adoption).
        coalesce_exec.accept.assert_not_called()
        hold = processor._live_barrier_holds["tok-1"]
        assert hold.barrier_key == "merge"
        assert hold.token.token_id == "tok-1"
