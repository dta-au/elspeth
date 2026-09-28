# tests/property/audit/test_fork_join_balance.py
"""Property-based tests for fork-join balance invariants.

FORK-JOIN BALANCE INVARIANT:
Every fork branch must have a destination, and every fork child must have
a parent link recorded in the audit trail.

This ensures:
1. No "orphan" branches that tokens disappear into
2. Complete lineage tracking for forked tokens
3. DAG construction rejects invalid fork configurations

Fork terminology:
- Fork gate: A gate that splits one token into multiple child tokens
- Branch: A named path from a fork (e.g., "path_a", "path_b")
- Coalesce: A merge point that joins tokens from multiple branches
- Parent token: The token that was forked
- Child tokens: The new tokens created, one per branch
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import UTC
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import text

from elspeth.contracts import CoalesceName, GateName, RoutingAction, RoutingMode, SinkName
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import _LEGAL_TERMINAL_PAIRS, Determinism, FrameKind, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.run_result import RunResult
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.checkpoint.serialization import checkpoint_loads
from elspeth.core.config import CoalesceSettings, ElspethSettings, GateSettings, SourceSettings
from elspeth.core.dag import ExecutionGraph, GraphValidationError
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from tests.fixtures.base_classes import (
    as_sink,
    as_source,
    as_transform,
)
from tests.fixtures.factories import wire_transforms
from tests.fixtures.group_lineage import ensure_fork_group_record
from tests.fixtures.landscape import leader_token_for, make_landscape_db, reseat_crashed_leader
from tests.fixtures.plugins import (
    CollectSink,
    ListSource,
    PassTransform,
)
from tests.fixtures.stores import MockPayloadStore
from tests.helpers.checkpoint import create_checkpoint

# =============================================================================
# Audit Verification Helpers
# =============================================================================


def counter_reconciliation_pairs(uninterrupted: RunResult, resumed: RunResult) -> tuple[tuple[str, int, int], ...]:
    """Explicit ``(field, uninterrupted, resumed)`` triples for every RunResult counter.

    The table is written out rather than reflected so a masquerading run result
    (one missing a counter) raises ``AttributeError`` here instead of silently
    skipping a comparison. The exact-set assertion below keeps the table honest:
    adding a counter to ``RunResult`` fails this helper until it is listed.
    """
    pairs: tuple[tuple[str, int, int], ...] = (
        ("rows_processed", uninterrupted.rows_processed, resumed.rows_processed),
        ("rows_succeeded", uninterrupted.rows_succeeded, resumed.rows_succeeded),
        ("rows_failed", uninterrupted.rows_failed, resumed.rows_failed),
        ("rows_routed_success", uninterrupted.rows_routed_success, resumed.rows_routed_success),
        ("rows_routed_failure", uninterrupted.rows_routed_failure, resumed.rows_routed_failure),
        ("rows_quarantined", uninterrupted.rows_quarantined, resumed.rows_quarantined),
        ("rows_forked", uninterrupted.rows_forked, resumed.rows_forked),
        ("rows_coalesced", uninterrupted.rows_coalesced, resumed.rows_coalesced),
        ("rows_coalesce_failed", uninterrupted.rows_coalesce_failed, resumed.rows_coalesce_failed),
        ("collector_groups_failed", uninterrupted.collector_groups_failed, resumed.collector_groups_failed),
        ("rows_expanded", uninterrupted.rows_expanded, resumed.rows_expanded),
        ("rows_buffered", uninterrupted.rows_buffered, resumed.rows_buffered),
        ("rows_diverted", uninterrupted.rows_diverted, resumed.rows_diverted),
    )
    covered = {name for name, _, _ in pairs}
    expected = {field.name for field in dataclass_fields(RunResult)} - {"run_id", "status", "routed_destinations"}
    assert covered == expected, f"counter table drifted from RunResult: missing={expected - covered}, extra={covered - expected}"
    return pairs


def count_fork_children_missing_parents(db: LandscapeDB, run_id: str) -> int:
    """Count fork children that lack parent links.

    This is a critical invariant: every fork child token must have a
    token_parents record linking it to the parent token.
    """
    with db.connection() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(DISTINCT t.token_id)
                FROM tokens t
                JOIN rows r ON r.row_id = t.row_id
                JOIN token_lineage_frames f ON f.token_id = t.token_id AND f.run_id = t.run_id AND f.kind = 'fork'
                LEFT JOIN token_parents p ON p.token_id = t.token_id
                WHERE r.run_id = :run_id
                  AND p.token_id IS NULL
            """),
            {"run_id": run_id},
        ).scalar()
        return result or 0


def count_forked_outcomes(db: LandscapeDB, run_id: str) -> int:
    """Count tokens with FORKED outcome (parent tokens that were split)."""
    with db.connection() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM token_outcomes o
                JOIN tokens t ON t.token_id = o.token_id
                JOIN rows r ON r.row_id = t.row_id
                WHERE r.run_id = :run_id
                  AND o.outcome = 'transient'
                  AND o.path = 'fork_parent'
            """),
            {"run_id": run_id},
        ).scalar()
        return result or 0


def get_fork_group_stats(db: LandscapeDB, run_id: str) -> dict[str, int]:
    """Get statistics about fork groups.

    Returns dict with:
    - total_fork_groups: Number of unique fork groups
    - total_fork_children: Number of fork child tokens
    - children_with_parents: Fork children that have parent links
    """
    with db.connection() as conn:
        # Count unique fork groups
        total_groups = (
            conn.execute(
                text("""
                SELECT COUNT(DISTINCT f.group_id)
                FROM token_lineage_frames f
                WHERE f.run_id = :run_id
                  AND f.kind = 'fork'
            """),
                {"run_id": run_id},
            ).scalar()
            or 0
        )

        # Count fork children
        total_children = (
            conn.execute(
                text("""
                SELECT COUNT(DISTINCT t.token_id)
                FROM tokens t
                JOIN rows r ON r.row_id = t.row_id
                JOIN token_lineage_frames f ON f.token_id = t.token_id AND f.run_id = t.run_id AND f.kind = 'fork'
                WHERE r.run_id = :run_id
            """),
                {"run_id": run_id},
            ).scalar()
            or 0
        )

        # Count children with parents
        with_parents = (
            conn.execute(
                text("""
                SELECT COUNT(DISTINCT t.token_id)
                FROM tokens t
                JOIN rows r ON r.row_id = t.row_id
                JOIN token_lineage_frames f ON f.token_id = t.token_id AND f.run_id = t.run_id AND f.kind = 'fork'
                JOIN token_parents p ON p.token_id = t.token_id
                WHERE r.run_id = :run_id
            """),
                {"run_id": run_id},
            ).scalar()
            or 0
        )

        return {
            "total_fork_groups": total_groups,
            "total_fork_children": total_children,
            "children_with_parents": with_parents,
        }


def count_fork_groups_with_unexpected_children(db: LandscapeDB, run_id: str, expected_children: int) -> int:
    """Count fork groups that don't have the expected number of children."""
    with db.connection() as conn:
        result = conn.execute(
            text("""
                SELECT COUNT(*)
                FROM (
                    SELECT f.group_id, COUNT(DISTINCT t.token_id) AS child_count
                    FROM tokens t
                    JOIN rows r ON r.row_id = t.row_id
                    JOIN token_lineage_frames f ON f.token_id = t.token_id AND f.run_id = t.run_id AND f.kind = 'fork'
                    WHERE r.run_id = :run_id
                    GROUP BY f.group_id
                    HAVING COUNT(DISTINCT t.token_id) != :expected_children
                ) bad_groups
            """),
            {"run_id": run_id, "expected_children": expected_children},
        ).scalar()
        return result or 0


# =============================================================================
# Hypothesis Strategies
# =============================================================================

# Strategy for row values
row_for_fork = st.fixed_dictionaries(
    {"value": st.integers(min_value=0, max_value=1000)},
)


# =============================================================================
# Helper: Build production graph from PipelineConfig
# =============================================================================


def _build_production_graph(config: PipelineConfig) -> ExecutionGraph:
    """Build graph using production code path (from_plugin_instances).

    Replacement for v1 build_production_graph, inlined to avoid v1 imports.
    Auto-sets on_success on terminal transform for linear pipelines.
    """
    transforms = list(config.transforms)
    sink_name = next(iter(config.sinks))
    source_on_success = "source_out" if transforms else sink_name
    wired_transforms = (
        wire_transforms(
            transforms,
            source_connection=source_on_success,
            final_sink=sink_name,
        )
        if transforms
        else []
    )

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": config.sources["primary"]},
        source_settings_map={"primary": SourceSettings(plugin=config.sources["primary"].name, on_success=source_on_success, options={})},
        transforms=wired_transforms,
        sinks=config.sinks,
        aggregations={},
        gates=list(config.gates),
        coalesce_settings=list(config.coalesce_settings) if config.coalesce_settings else None,
    )


# =============================================================================
# Property Tests: DAG Construction Invariants
# =============================================================================


class TestDagForkBranchValidation:
    """Property tests for DAG-level fork branch validation.

    These test that ExecutionGraph.from_plugin_instances() correctly
    validates fork configurations at construction time.
    """

    def test_fork_to_unknown_destination_rejected(self) -> None:
        """Fork branch to non-existent destination is rejected at DAG construction.

        This is a critical safety check - typos in branch names would otherwise
        cause tokens to disappear silently.
        """
        # Create a gate config that forks to a branch that doesn't exist
        gate = GateSettings(
            name="bad_fork_gate",
            input="gate_in",
            condition="True",  # Always fork
            routes={"true": "fork", "false": "default"},  # Route to fork action
            fork_to=["unknown_branch"],  # No coalesce or sink with this name
        )

        source = ListSource([{"value": 1}], on_success="default")
        sink = CollectSink()

        # This should fail at graph construction
        with pytest.raises(GraphValidationError, match="unknown_branch"):
            ExecutionGraph.from_plugin_instances(
                sources={"primary": as_source(source)},
                source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
                transforms=[],
                sinks={"default": as_sink(sink)},  # No "unknown_branch" sink
                gates=[gate],
                aggregations={},
                coalesce_settings=[],  # No coalesce with "unknown_branch"
            )

    def test_fork_to_sink_is_valid(self) -> None:
        """Fork branch targeting a sink is accepted."""
        gate = GateSettings(
            name="fork_to_sink_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "sink_a"},
            fork_to=["sink_a", "sink_b"],
        )

        source = ListSource([{"value": 1}], on_success="sink_a")
        sink_a = CollectSink("sink_a")
        sink_b = CollectSink("sink_b")

        # This should succeed - branches match sink names
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
            transforms=[],
            sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
            gates=[gate],
            aggregations={},
            coalesce_settings=[],
        )

        gate_id = graph.get_config_gate_id_map()[GateName(gate.name)]
        sink_ids = graph.get_sink_id_map()
        edges = graph.get_edges()

        def has_fork_edge(branch: str, sink_name: str) -> bool:
            sink_id = sink_ids[SinkName(sink_name)]
            return any(
                edge.from_node == gate_id and edge.to_node == sink_id and edge.label == branch and edge.mode == RoutingMode.COPY
                for edge in edges
            )

        assert has_fork_edge("sink_a", "sink_a")
        assert has_fork_edge("sink_b", "sink_b")

    def test_fork_to_coalesce_is_valid(self) -> None:
        """Fork branch targeting a coalesce is accepted."""
        gate = GateSettings(
            name="fork_to_coalesce_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "default"},
            fork_to=["branch_a", "branch_b"],
        )

        coalesce = CoalesceSettings(
            name="merge_point",
            branches=["branch_a", "branch_b"],
            on_success="default",
        )

        source = ListSource([{"value": 1}], on_success="default")
        sink = CollectSink()

        # This should succeed - branches match coalesce branches
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
            transforms=[],
            sinks={"default": as_sink(sink)},
            gates=[gate],
            aggregations={},
            coalesce_settings=[coalesce],
        )

        branch_map = graph.get_branch_to_coalesce_map()
        assert branch_map == {"branch_a": "merge_point", "branch_b": "merge_point"}

        gate_id = graph.get_config_gate_id_map()[GateName(gate.name)]
        coalesce_id = graph.get_coalesce_id_map()[CoalesceName(coalesce.name)]
        edges = graph.get_edges()

        def has_fork_edge(branch: str) -> bool:
            return any(
                edge.from_node == gate_id and edge.to_node == coalesce_id and edge.label == branch and edge.mode == RoutingMode.COPY
                for edge in edges
            )

        assert has_fork_edge("branch_a")
        assert has_fork_edge("branch_b")

    def test_duplicate_fork_branches_rejected(self) -> None:
        """Fork with duplicate branch names is rejected."""
        gate = GateSettings(
            name="dup_fork_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "default"},
            fork_to=["branch_a", "branch_a"],  # Duplicate!
        )

        source = ListSource([{"value": 1}], on_success="default")
        sink = CollectSink()

        # RoutingAction.fork_to_paths() validates uniqueness
        with pytest.raises((GraphValidationError, ValueError), match=r"[Dd]uplicate"):
            ExecutionGraph.from_plugin_instances(
                sources={"primary": as_source(source)},
                source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
                transforms=[],
                sinks={"default": as_sink(sink)},
                gates=[gate],
                aggregations={},
                coalesce_settings=[],
            )

    def test_coalesce_branch_not_produced_rejected(self) -> None:
        """Coalesce expecting a branch that no gate produces is rejected."""
        # Gate only produces branch_a, but coalesce expects both
        gate = GateSettings(
            name="partial_fork",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "default"},
            fork_to=["branch_a"],  # Only one branch
        )

        coalesce = CoalesceSettings(
            name="merge_point",
            branches=["branch_a", "branch_b"],  # Expects branch_b too!
        )

        source = ListSource([{"value": 1}], on_success="default")
        sink = CollectSink()

        with pytest.raises(GraphValidationError, match="branch_b"):
            ExecutionGraph.from_plugin_instances(
                sources={"primary": as_source(source)},
                source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
                transforms=[],
                sinks={"default": as_sink(sink)},
                gates=[gate],
                aggregations={},
                coalesce_settings=[coalesce],
            )


class TestForkJoinRuntimeBalance:
    """Property tests for runtime fork-join balance.

    These test that when forks execute, the audit trail correctly
    records parent-child relationships.
    """

    @given(n_rows=st.integers(min_value=1, max_value=20))
    @settings(max_examples=30, deadline=None)
    def test_fork_to_sinks_all_children_have_parents(self, n_rows: int) -> None:
        """Property: When forking to sinks, all child tokens have parent links.

        This tests the simpler fork case (no coalesce) to verify parent
        link recording works correctly.
        """
        from elspeth.core.config import ElspethSettings

        db = make_landscape_db()
        payload_store = MockPayloadStore()

        rows = [{"value": i} for i in range(n_rows)]
        source = ListSource(rows, on_success="sink_a")
        sink_a = CollectSink("sink_a")
        sink_b = CollectSink("sink_b")

        # Gate that forks all rows to both sinks
        gate = GateSettings(
            name="fork_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "sink_a"},
            fork_to=["sink_a", "sink_b"],
        )

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
            gates=[gate],
        )

        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
            transforms=[],
            sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
            gates=[gate],
            aggregations={},
            coalesce_settings=[],
        )

        # Settings needed for fork execution
        settings_obj = ElspethSettings(
            sources={"primary": {"plugin": "test", "on_success": "sink_a", "options": {}}},
            sinks={
                "sink_a": {"plugin": "test", "on_write_failure": "discard"},
                "sink_b": {"plugin": "test", "on_write_failure": "discard"},
            },
            gates=[gate],
        )

        orchestrator = Orchestrator(db)
        run = orchestrator.run(config, graph=graph, settings=settings_obj, payload_store=payload_store)

        # Verify fork audit integrity
        missing_parents = count_fork_children_missing_parents(db, run.run_id)
        assert missing_parents == 0, (
            f"FORK AUDIT VIOLATION: {missing_parents} fork children missing parent links. Rows: {n_rows}. Fork lineage would be incomplete."
        )

        # Verify FORKED outcomes recorded for parent tokens
        forked_count = count_forked_outcomes(db, run.run_id)
        assert forked_count == n_rows, f"Expected {n_rows} FORKED outcomes (one per parent token), got {forked_count}"

        # Verify fork statistics
        stats = get_fork_group_stats(db, run.run_id)
        expected_children_per_group = len(gate.fork_to or [])
        expected_children_total = n_rows * expected_children_per_group
        assert stats["total_fork_children"] == stats["children_with_parents"], (
            f"Not all fork children have parents: {stats['children_with_parents']}/{stats['total_fork_children']}"
        )
        assert stats["total_fork_children"] == expected_children_total, (
            f"Expected {expected_children_total} fork children (rows={n_rows}, branches={expected_children_per_group}), "
            f"got {stats['total_fork_children']}."
        )
        assert stats["total_fork_groups"] == n_rows, (
            f"Expected {n_rows} fork groups (one per parent token), got {stats['total_fork_groups']}."
        )
        bad_groups = count_fork_groups_with_unexpected_children(db, run.run_id, expected_children=expected_children_per_group)
        assert bad_groups == 0, f"{bad_groups} fork groups have unexpected child counts."


class TestForkJoinEnumProperties:
    """Property tests for fork-related enums and outcomes."""

    def test_fork_parent_is_terminal_pair(self) -> None:
        """FORK_PARENT is a terminal pair (parent token's journey ends)."""
        assert (TerminalOutcome.TRANSIENT, TerminalPath.FORK_PARENT) in _LEGAL_TERMINAL_PAIRS

    def test_coalesced_is_terminal_pair(self) -> None:
        """COALESCED is a terminal pair (branch tokens merge)."""
        assert (TerminalOutcome.SUCCESS, TerminalPath.COALESCED) in _LEGAL_TERMINAL_PAIRS

    def test_routing_action_fork_requires_paths(self) -> None:
        """RoutingAction.fork_to_paths() requires at least one path."""
        with pytest.raises(ValueError, match="at least one"):
            RoutingAction.fork_to_paths([])

    def test_routing_action_fork_rejects_duplicates(self) -> None:
        """RoutingAction.fork_to_paths() rejects duplicate paths."""
        with pytest.raises(ValueError, match=r"[Dd]uplicate"):
            RoutingAction.fork_to_paths(["a", "b", "a"])


class TestForkJoinEdgeCases:
    """Edge case tests for fork-join behavior."""

    def test_no_fork_no_fork_groups(self) -> None:
        """Pipeline without forks should have no fork groups."""
        db = make_landscape_db()
        payload_store = MockPayloadStore()

        source = ListSource([{"value": 1}, {"value": 2}])
        transform = PassTransform()
        sink = CollectSink()

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        orchestrator = Orchestrator(db)
        run = orchestrator.run(config, graph=_build_production_graph(config), payload_store=payload_store)

        stats = get_fork_group_stats(db, run.run_id)
        assert stats["total_fork_groups"] == 0
        assert stats["total_fork_children"] == 0

    def test_empty_source_no_fork_issues(self) -> None:
        """Empty source with fork config should not cause issues."""
        db = make_landscape_db()
        payload_store = MockPayloadStore()

        source = ListSource([], on_success="sink_a")  # Empty
        sink_a = CollectSink("sink_a")
        sink_b = CollectSink("sink_b")

        gate = GateSettings(
            name="fork_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "sink_a"},
            fork_to=["sink_a", "sink_b"],
        )

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
            gates=[gate],
        )

        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
            transforms=[],
            sinks={"sink_a": as_sink(sink_a), "sink_b": as_sink(sink_b)},
            gates=[gate],
            aggregations={},
            coalesce_settings=[],
        )

        from elspeth.core.config import ElspethSettings

        settings_obj = ElspethSettings(
            sources={"primary": {"plugin": "test", "on_success": "sink_a", "options": {}}},
            sinks={
                "sink_a": {"plugin": "test", "on_write_failure": "discard"},
                "sink_b": {"plugin": "test", "on_write_failure": "discard"},
            },
            gates=[gate],
        )

        orchestrator = Orchestrator(db)
        run = orchestrator.run(config, graph=graph, settings=settings_obj, payload_store=payload_store)

        # No rows means no forks
        stats = get_fork_group_stats(db, run.run_id)
        assert stats["total_fork_groups"] == 0
        missing = count_fork_children_missing_parents(db, run.run_id)
        assert missing == 0


class TestForkRecoveryInvariant:
    """Property tests for recovery invariant with forked tokens.

    These tests verify that the recovery system correctly detects partial
    fork completion. Bug P2-2026-01-29-recovery-skips-partial-forks showed
    that recovery could miss rows where only some fork children completed.
    """

    def test_expand_token_persists_per_child_payload(self) -> None:
        """expand_token stores a {data, contract} envelope and writes tokens.token_data_ref.

        Verifies that:
        1. Each child has a non-null token_data_ref.
        2. Retrieving the ref bytes and loading via checkpoint_loads gives an envelope
           dict with keys "data" and "contract" — NOT a bare data dict (ADDENDUM 3).
        3. env["data"] round-trips the CORRECT child's payload, proving per-child
           value alignment and type fidelity (datetime survives as datetime, not string).
        4. SchemaContract.from_checkpoint(env["contract"]) restores the contract with
           field names and mode equal to the persisted contract (hash-validated by
           from_checkpoint itself — Tier-1 integrity).

        This is the Tier-1 audit invariant: every expand child is self-contained and
        reconstructable from its token_data_ref on resume without any nodes-table lookup.
        """
        from datetime import datetime

        from elspeth.contracts.schema_contract import FieldContract, SchemaContract

        _OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
        payload_store = MockPayloadStore()
        db = make_landscape_db()
        factory = RecorderFactory(db, payload_store=payload_store)

        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        coordination_token = leader_token_for(db, run.run_id)
        source = factory.data_flow.register_node(
            coordination_token=coordination_token,
            plugin_name="explode",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            determinism=Determinism.DETERMINISTIC,
            schema_config=_OBSERVED_SCHEMA,
        )
        row, parent_token = factory.data_flow.create_row_with_token(
            coordination_token=coordination_token,
            source_node_id=source.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={"items": [1, 2]},
        )

        # Build a real SchemaContract — the one the expand step would produce.
        output_contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(
                FieldContract(normalized_name="name", original_name="name", python_type=str, required=True, source="declared"),
                FieldContract(normalized_name="value", original_name="value", python_type=int, required=True, source="declared"),
                FieldContract(normalized_name="ts", original_name="ts", python_type=datetime, required=True, source="declared"),
            ),
            locked=True,
        )

        # Two DISTINCT payloads — one with a datetime (type-fidelity witness).
        # canonical_json would stringify the datetime; checkpoint_dumps preserves it.
        aware_dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        child_payloads = [
            {"name": "alpha", "value": 1, "ts": aware_dt},
            {"name": "beta", "value": 2, "ts": aware_dt},
        ]

        children, expand_group_id = factory.data_flow.expand_token(
            member_token=coordination_token.membership,
            parent_ref=TokenRef(token_id=parent_token.token_id, run_id=run.run_id),
            row_id=row.row_id,
            child_payloads=child_payloads,
            output_contract=output_contract,
            step_in_pipeline=1,
        )

        assert len(children) == 2
        assert expand_group_id is not None

        # Both children must have non-null token_data_ref
        for child in children:
            assert child.token_data_ref is not None, (
                f"expand_token must set token_data_ref on every child (epoch 11 invariant); child {child.token_id} has token_data_ref=None"
            )

        # Round-trip: retrieve bytes → checkpoint_loads → assert envelope shape + content.
        # Critically, verify EACH child has ITS OWN payload (not the sibling's).
        for i, (child, expected_payload) in enumerate(zip(children, child_payloads, strict=True)):
            raw_bytes = payload_store.retrieve(child.token_data_ref)
            env = checkpoint_loads(raw_bytes.decode("utf-8"))

            # Envelope shape — must be {data, contract}, NOT a bare data dict (ADDENDUM 3).
            assert isinstance(env, dict) and "data" in env and "contract" in env, (
                f"Child {i} token_data_ref payload is not a {{data, contract}} envelope; "
                f"got keys={sorted(env.keys()) if isinstance(env, dict) else type(env).__name__!r}"
            )

            data = env["data"]

            # Value alignment: correct child, correct fields
            assert data["name"] == expected_payload["name"], (
                f"Child {i} (token_data_ref={child.token_data_ref!r}) stored wrong payload: "
                f"expected name={expected_payload['name']!r}, got {data['name']!r}"
            )
            assert data["value"] == expected_payload["value"], (
                f"Child {i} stored wrong value: expected {expected_payload['value']}, got {data['value']}"
            )

            # Type fidelity: datetime must come back as datetime, not a string.
            # This proves checkpoint_dumps was used (canonical_json would stringify datetime).
            assert isinstance(data["ts"], datetime), (
                f"Type fidelity failure: 'ts' field came back as {type(data['ts']).__name__!r}, "
                f"not datetime — checkpoint_dumps was not used (canonical_json stringifies datetime)"
            )
            assert data["ts"].tzinfo is not None, "Restored datetime must be timezone-aware"
            assert data["ts"] == aware_dt, f"datetime value mismatch: expected {aware_dt!r}, got {data['ts']!r}"

            # Contract round-trip: SchemaContract.from_checkpoint validates hash integrity
            # (Tier-1 — raises AuditIntegrityError on mismatch) and restores the contract.
            restored_contract = SchemaContract.from_checkpoint(env["contract"])
            assert restored_contract.mode == output_contract.mode, (
                f"Contract mode mismatch: expected {output_contract.mode!r}, got {restored_contract.mode!r}"
            )
            assert restored_contract.locked == output_contract.locked
            restored_names = {fc.normalized_name for fc in restored_contract.fields}
            expected_names = {fc.normalized_name for fc in output_contract.fields}
            assert restored_names == expected_names, f"Contract field names mismatch: expected {expected_names!r}, got {restored_names!r}"

    def test_coalesce_token_persists_merged_payload(self) -> None:
        """coalesce_tokens stores a {data, contract} envelope and writes tokens.token_data_ref.

        Verifies that:
        1. The merged token has a non-null token_data_ref.
        2. Retrieving the ref bytes and loading via checkpoint_loads gives an envelope
           dict with keys "data" and "contract" — NOT a bare data dict (ADDENDUM 3).
        3. env["data"] round-trips the merged payload with full type fidelity (datetime
           survives as datetime, not string).
        4. SchemaContract.from_checkpoint(env["contract"]) restores the contract with
           field names and mode equal to the persisted contract (hash-validated by
           from_checkpoint itself — Tier-1 integrity).

        This is the Tier-1 audit invariant: the merged token is reconstructable
        from its token_data_ref on resume without re-executing the merge strategy
        and without any nodes-table lookup (ADDENDUM 3).
        """
        from datetime import datetime

        from elspeth.contracts.schema_contract import FieldContract, SchemaContract

        _OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
        payload_store = MockPayloadStore()
        db = make_landscape_db()
        factory = RecorderFactory(db, payload_store=payload_store)

        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        coordination_token = leader_token_for(db, run.run_id)
        source = factory.data_flow.register_node(
            coordination_token=coordination_token,
            plugin_name="source",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=_OBSERVED_SCHEMA,
        )
        row, _ = factory.data_flow.create_row_with_token(
            coordination_token=coordination_token,
            source_node_id=source.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={"key": "value"},
        )

        # Build a real SchemaContract — the one the coalesce step would produce.
        merged_contract = SchemaContract(
            mode="FIXED",
            fields=(
                FieldContract(normalized_name="merged", original_name="merged", python_type=bool, required=True, source="declared"),
                FieldContract(normalized_name="count", original_name="count", python_type=int, required=True, source="declared"),
                FieldContract(
                    normalized_name="result_score", original_name="result_score", python_type=float, required=True, source="declared"
                ),
                FieldContract(
                    normalized_name="resolved_at", original_name="resolved_at", python_type=datetime, required=True, source="declared"
                ),
            ),
            locked=True,
        )

        # Create two branch tokens to coalesce. coalesce_tokens' durable strict
        # pop (spec rulings 24/28) requires an innermost shared FORK lineage
        # frame on every parent — crafted here via the
        # create_token(..., lineage_frames=) seam to model the shape a real
        # fork_token would have produced.
        token_a = factory.data_flow.create_token(
            coordination_token=coordination_token,
            row_id=row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-join-balance-grp", member_key="a"),),
        )
        token_b = factory.data_flow.create_token(
            coordination_token=coordination_token,
            row_id=row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-join-balance-grp", member_key="b"),),
        )
        # META-38: the crafted fork group needs the group_records row a real
        # fork mints — coalesce_tokens reads the written release fact for it.
        ensure_fork_group_record(factory, run_id=run.run_id, group_id="fork-join-balance-grp", opener_token_id=token_a.token_id)

        # Merged payload includes a datetime to verify type fidelity.
        # canonical_json would stringify datetime; checkpoint_dumps preserves it.
        aware_dt = datetime(2025, 3, 10, 8, 30, 0, tzinfo=UTC)
        merged_payload = {
            "merged": True,
            "count": 2,
            "result_score": 0.87,
            "resolved_at": aware_dt,
        }

        merged_token = factory.data_flow.coalesce_tokens(
            coordination_token=coordination_token,
            parent_refs=[
                TokenRef(token_id=token_a.token_id, run_id=run.run_id),
                TokenRef(token_id=token_b.token_id, run_id=run.run_id),
            ],
            row_id=row.row_id,
            merged_payload=merged_payload,
            merged_contract=merged_contract,
            step_in_pipeline=2,
        )

        # Merged token must have non-null token_data_ref
        assert merged_token.token_data_ref is not None, "coalesce_tokens must set token_data_ref on the merged token (epoch 11 invariant)"
        assert merged_token.join_group_id is not None

        # Round-trip: retrieve bytes → checkpoint_loads → assert envelope shape + content.
        raw_bytes = payload_store.retrieve(merged_token.token_data_ref)
        env = checkpoint_loads(raw_bytes.decode("utf-8"))

        # Envelope shape — must be {data, contract}, NOT a bare data dict (ADDENDUM 3).
        assert isinstance(env, dict) and "data" in env and "contract" in env, (
            f"Merged token payload is not a {{data, contract}} envelope; "
            f"got keys={sorted(env.keys()) if isinstance(env, dict) else type(env).__name__!r}"
        )

        data = env["data"]

        assert data["merged"] is True
        assert data["count"] == 2
        assert abs(data["result_score"] - 0.87) < 1e-9

        # Type fidelity: datetime must come back as datetime, not a string.
        # This proves checkpoint_dumps was used (canonical_json would stringify datetime).
        assert isinstance(data["resolved_at"], datetime), (
            f"Type fidelity failure: 'resolved_at' came back as {type(data['resolved_at']).__name__!r}, "
            f"not datetime — checkpoint_dumps was not used (canonical_json stringifies datetime)"
        )
        assert data["resolved_at"].tzinfo is not None, "Restored datetime must be timezone-aware"

        # Contract round-trip: SchemaContract.from_checkpoint validates hash integrity
        # (Tier-1 — raises AuditIntegrityError on mismatch) and restores the contract.
        restored_contract = SchemaContract.from_checkpoint(env["contract"])
        assert restored_contract.mode == merged_contract.mode, (
            f"Contract mode mismatch: expected {merged_contract.mode!r}, got {restored_contract.mode!r}"
        )
        assert restored_contract.locked == merged_contract.locked
        restored_names = {fc.normalized_name for fc in restored_contract.fields}
        expected_names = {fc.normalized_name for fc in merged_contract.fields}
        assert restored_names == expected_names, f"Contract field names mismatch: expected {expected_names!r}, got {restored_names!r}"

    def test_lineage_read_path_surfaces_token_data_ref_without_hydration(self) -> None:
        """The lineage read path surfaces the real token_data_ref and never hydrates it.

        After expand_token() persists an expand child with a non-null token_data_ref,
        QueryRepository.get_token() (the lineage/audit read path) returns the REAL ref
        persisted in the DB — TokenLoader.load() reads the column faithfully (omitting
        it fabricated None for a value the audit trail recorded) — but does NOT hydrate
        the payload bytes (it never touches the payload store). A payload-store spy
        enforces the no-hydration half.
        """
        from elspeth.contracts.audit import TokenRef
        from elspeth.contracts.enums import Determinism, NodeType
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.core.landscape.factory import RecorderFactory

        class _RetrieveSpyPayloadStore(MockPayloadStore):
            """MockPayloadStore that counts retrieve() (payload hydration) calls."""

            def __init__(self) -> None:
                super().__init__()
                self.retrieve_count = 0

            def retrieve(self, content_hash: str) -> bytes:
                self.retrieve_count += 1
                return super().retrieve(content_hash)

        _OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
        payload_store = _RetrieveSpyPayloadStore()
        db = make_landscape_db()
        factory = RecorderFactory(db, payload_store=payload_store)

        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        coordination_token = leader_token_for(db, run.run_id)
        source_node = factory.data_flow.register_node(
            coordination_token=coordination_token,
            plugin_name="explode",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            determinism=Determinism.DETERMINISTIC,
            schema_config=_OBSERVED_SCHEMA,
        )
        row, parent_token = factory.data_flow.create_row_with_token(
            coordination_token=coordination_token,
            source_node_id=source_node.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={"items": [1, 2]},
        )

        # Persist two expand children — both write token_data_ref (epoch 11 invariant).
        from elspeth.contracts.schema_contract import FieldContract, SchemaContract

        _read_path_contract = SchemaContract(
            mode="OBSERVED",
            fields=(FieldContract(normalized_name="name", original_name="name", python_type=str, required=True, source="inferred"),),
            locked=True,
        )
        children, _expand_group_id = factory.data_flow.expand_token(
            member_token=coordination_token.membership,
            parent_ref=TokenRef(token_id=parent_token.token_id, run_id=run.run_id),
            row_id=row.row_id,
            child_payloads=[{"name": "alpha"}, {"name": "beta"}],
            output_contract=_read_path_contract,
            step_in_pipeline=1,
        )
        assert len(children) == 2
        child = children[0]

        # Precondition: the expand child genuinely has a non-null token_data_ref.
        assert child.token_data_ref is not None, "expand_token must set token_data_ref (epoch 11 invariant); test precondition failed"

        # (a) Lineage read path: QueryRepository.get_token() reads the real ref column
        # faithfully (no fabrication) but does NOT hydrate the payload bytes.
        token_via_loader = factory.query.get_token(child.token_id)
        assert token_via_loader is not None, "get_token returned None for a persisted expand child"
        assert token_via_loader.token_data_ref == child.token_data_ref, (
            f"get_token (lineage path) must surface the persisted token_data_ref, not a fabricated None; "
            f"got {token_via_loader.token_data_ref!r}, expected {child.token_data_ref!r}"
        )
        assert payload_store.retrieve_count == 0, (
            f"Lineage read path must NOT hydrate the payload (reading the ref column is not retrieval); "
            f"payload_store.retrieve was called {payload_store.retrieve_count} time(s) during get_token"
        )

    # ─────────────────────────────────────────────────────────────────────
    # F1 Regression Cells (Task 9) — fork→coalesce + post-coalesce (B1)
    # ─────────────────────────────────────────────────────────────────────

    def _setup_coalesce_pipeline(
        self,
        *,
        branch_gate: bool = False,
    ) -> tuple[
        LandscapeDB,
        MockPayloadStore,
        PipelineConfig,
        ExecutionGraph,
        ElspethSettings,
        str,  # run_id
        RunResult,  # the completed run-1 RunResult (live counters)
    ]:
        """Shared setup: build and run a fork→PassTransform→coalesce→sink pipeline.

        Topology: source → gate(fork_to=[path_a, path_b])
                    → path_a: [optional branch_gate] → PassTransform(pass_a) → coalesce 'merge'
                    → path_b: PassTransform(pass_b) → coalesce 'merge'
                    → sink 'output'

        PassTransforms on both branches ensure branch_first_node is a REAL
        processing node distinct from the coalesce barrier — this exercises
        resolve_branch_first_node() (not the coalesce node itself).

        CoalesceSettings.on_success='output' makes the coalesce TERMINAL
        (resolve_next_node(coalesce_node_id) is None in the traversal map —
        the sink is reached via resolve_coalesce_sink, not the next-node map).

        Returns (db, payload_store, config, graph, settings_obj, run_id, run)
        for the completed run.  ``run`` is the live RunResult of the
        uninterrupted run-1 (its counters come from the LIVE accumulator, a
        different code path from derive_resume_terminal_status_from_audit) —
        callers that need a same-topology uninterrupted oracle for
        field-for-field reconciliation against a RESUMED run should build a
        SEPARATE _setup_coalesce_pipeline() instance for run A and use this
        ``run`` as that oracle (do NOT interrupt run A).  The same
        payload_store MUST be used for both the initial run and the resume
        (merged token_data_ref lives there).
        """
        from elspeth.core.config import ElspethSettings

        db = make_landscape_db()
        payload_store = MockPayloadStore()

        source = ListSource([{"value": 1}], on_success="gate_in")
        sink = CollectSink("output")

        pass_a = PassTransform(name="pass_a")
        pass_b = PassTransform(name="pass_b")

        gate = GateSettings(
            name="fork_gate",
            input="gate_in",
            condition="True",
            routes={"true": "fork", "false": "output"},
            fork_to=["path_a", "path_b"],
        )
        gates = [gate]
        if branch_gate:
            gates.append(
                GateSettings(
                    name="branch_gate",
                    input="path_a",
                    condition="True",
                    routes={"true": "gated_a", "false": "gated_a"},
                )
            )

        # branches dict maps branch-name → final-connection-into-coalesce.
        # wire_transforms wires: path_a → pass_a → done_a (consumed by coalesce 'merge').
        coalesce = CoalesceSettings(
            name="merge",
            branches={"path_a": "done_a", "path_b": "done_b"},
            policy="require_all",
            merge="union",
            on_success="output",
        )

        wired_a = wire_transforms([pass_a], source_connection="gated_a" if branch_gate else "path_a", final_sink="done_a", names=["pass_a"])
        wired_b = wire_transforms([pass_b], source_connection="path_b", final_sink="done_b", names=["pass_b"])

        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="gate_in", options={})},
            transforms=wired_a + wired_b,
            sinks={"output": as_sink(sink)},
            gates=gates,
            aggregations={},
            coalesce_settings=[coalesce],
        )

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(pass_a), as_transform(pass_b)],
            sinks={"output": as_sink(sink)},
            gates=gates,
            coalesce_settings=[coalesce],
        )

        settings_obj = ElspethSettings(
            sources={"primary": {"plugin": "test", "on_success": "gate_in", "options": {}}},
            sinks={"output": {"plugin": "test", "on_write_failure": "discard"}},
            gates=gates,
            coalesce=[coalesce],
        )

        orchestrator = Orchestrator(db)
        run = orchestrator.run(config, graph=graph, settings=settings_obj, payload_store=payload_store)
        run_id = run.run_id
        return db, payload_store, config, graph, settings_obj, run_id, run

    # ─────────────────────────────────────────────────────────────────────
    # Task 11: F1/F2 counter-field reconciliation guard
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_end_of_source_flush_aggregation(
        n_source_rows: int,
        trigger_count: int | None = None,
    ) -> tuple[LandscapeDB, PipelineConfig, ExecutionGraph, ElspethSettings]:
        """Build a fresh source(N rows) → batch aggregation → sink pipeline.

        ``trigger_count`` is the aggregation's ``count`` trigger. Default
        (``None`` → ``N + 1``) is the canonical end-of-source-flush topology:
        the aggregation NEVER triggers mid-stream — all N input rows are BUFFERED
        and flushed together at end-of-source, and ``live == derive == N`` for
        ``rows_buffered``. Pass ``trigger_count=N`` to build the count==N
        mid-stream-trigger topology that the rows_buffered live/derive divergence
        pinning test (see ``test_count_equals_n_rows_buffered_divergence_is_pinned``)
        exercises.

        WHY count > N (NOT count == N): a count==N trigger FIRES on the Nth row
        mid-stream, and the live accumulator and derive() then DISAGREE on
        rows_buffered (live counts N-1, derive counts N from the persisted
        BATCH_CONSUMED→BUFFERED records — a separate divergence, out of scope for
        THIS reconciliation cell but now tracked (archived issue elspeth-e1dd5e1303) and
        pinned by test_count_equals_n_rows_buffered_divergence_is_pinned).  The
        end-of-source-flush path (count > N) is the CANONICAL buffered path on which
        live == derive == N for every field, so it is the honest topology for a
        field-for-field live-vs-derive reconciliation cell.  (Mirrors
        tests/property/audit/test_terminal_states.py's count=9999 construction.)

        Returns (db, config, graph, settings) — caller runs / resumes it.
        """
        from elspeth.contracts.enums import Determinism, OutputMode
        from elspeth.contracts.schema_contract import PipelineRow
        from elspeth.core.config import AggregationSettings, SourceSettings, TriggerConfig
        from elspeth.plugins.infrastructure.base import BaseTransform
        from elspeth.plugins.infrastructure.results import TransformResult
        from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
        from tests.fixtures.plugins import CollectSink, ListSource

        class _SumAggregator(BaseTransform):
            name = "sum-aggregator"
            determinism = Determinism.DETERMINISTIC
            plugin_version = "1.0.0"
            source_file_hash = None
            input_schema = _TestSchema
            output_schema = _TestSchema
            is_batch_aware = True
            passes_through_input = False
            forwards_input_fields = False
            removed_input_fields = frozenset()
            on_success = "output"
            on_error = "discard"

            def __init__(self) -> None:
                super().__init__({"schema": {"mode": "observed"}})

            def process(self, rows: list[PipelineRow], ctx: object) -> TransformResult:  # type: ignore[override]
                if not rows:
                    # Unreachable in practice (a batch flush always carries rows);
                    # "invalid_input" is a valid TransformResult.error reason literal.
                    return TransformResult.error({"reason": "invalid_input"}, retryable=False)
                total = sum(r.to_dict().get("value", 0) for r in rows)
                return TransformResult.success(PipelineRow({"sum": total}, rows[0].contract), success_reason={"action": "sum"})

        db = make_landscape_db()
        src = ListSource([{"value": i + 1} for i in range(n_source_rows)], name="list_source", on_success="agg_in")
        out = CollectSink("output")
        agg = _SumAggregator()
        agg_settings = AggregationSettings(
            name="sum_agg",
            plugin=agg.name,
            input="agg_in",
            on_success="output",
            on_error="discard",
            # Default count > N → never fires mid-stream → all N rows buffer to
            # end-of-source. trigger_count=N forces the mid-stream-trigger topology.
            trigger=TriggerConfig(count=trigger_count if trigger_count is not None else n_source_rows + 1, timeout_seconds=3600),
            output_mode=OutputMode.TRANSFORM,
        )
        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(src)},
            source_settings_map={"primary": SourceSettings(plugin=src.name, on_success="agg_in", options={})},
            transforms=[],
            sinks={"output": as_sink(out)},
            aggregations={"sum_agg": (as_transform(agg), agg_settings)},
            gates=[],
        )
        agg_id_map = graph.get_aggregation_id_map()
        agg_node_id = agg_id_map[next(iter(agg_id_map))]
        agg.node_id = agg_node_id
        config = PipelineConfig(
            sources={"primary": as_source(src)},
            transforms=[as_transform(agg)],
            sinks={"output": as_sink(out)},
            aggregation_settings={agg_node_id: agg_settings},
        )
        settings = ElspethSettings(
            sources={"primary": {"plugin": src.name, "on_success": "agg_in", "options": {}}},
            sinks={"output": {"plugin": "test", "on_write_failure": "discard"}},
        )
        return db, config, graph, settings

    def test_resume_buffered_counter_reconciles_with_uninterrupted_run(self) -> None:
        """A resumed aggregation run reconciles EVERY counter field — including
        rows_buffered > 0 (non-vacuous) — with an uninterrupted run.

        WHY THIS CELL EXISTS (regression-detection gap closed):

        ``test_resume_counters_reconcile_with_uninterrupted_run`` (fork→sink) and
        the ``test_adr_019_resume_counter_parity.py`` snapshot tests all use
        topologies with ``rows_buffered == 0`` on both sides — so that field
        reconciled only VACUOUSLY (0 == 0).  ``rows_buffered`` is NOT always 0 at
        a COMPLETED run: a batch aggregation leaves one non-completed
        ``(None, BUFFERED)`` audit record per input row that derive() counts
        (run_status.py line ~113), so an N-row aggregation completes with
        ``rows_buffered == N`` (cf. test_terminal_states.py and
        test_orchestrator_execute_run_characterization.py, which both assert
        ``rows_buffered == N`` on COMPLETED aggregation runs).  This cell reconciles all 12 fields on
        such a topology where ``rows_buffered >= 1`` (non-vacuous).

        TOPOLOGY (``_build_end_of_source_flush_aggregation``, N=3):
            source(3 rows) → batch aggregation(count=4 → NEVER fires mid-stream;
                              all 3 rows BUFFERED, flushed together at
                              end-of-source) → sink 'output'
        The 3 input rows each get a persisted ``(None, BUFFERED)`` audit record
        (TerminalPath.BUFFERED, non-completed) → ``rows_buffered == 3``
        (non-vacuous).  count > N is deliberate: see the helper docstring — a
        count==N mid-stream trigger makes the live accumulator and derive()
        DISAGREE on rows_buffered (tracked: elspeth-e1dd5e1303; pinned by
        test_count_equals_n_rows_buffered_divergence_is_pinned).

        RUN A (uninterrupted oracle): a SEPARATE
        ``_build_end_of_source_flush_aggregation`` instance, NOT interrupted.
        Its counters come from the LIVE accumulator.

        RUN B (run-1 + interrupt + resume): the SAME topology; run-1 completes
        with all 3 BUFFERED records + the aggregate result + sink write persisted
        intact.  The interrupt creates a checkpoint and marks the run failed but
        deletes NO token_outcomes — so on resume no scheduler work remains to
        re-drive and resume takes the NO-WORK finalize
        branch (the branch Phase-2.2 introduced
        ``derive_resume_terminal_status_from_audit`` for).  derive() reconstructs
        the cumulative counters — including ``rows_buffered`` from the 3 intact
        BUFFERED records — and the run finalizes COMPLETED.

        ``rows_buffered`` is reconstructed purely by derive's ``(None, BUFFERED)``
        arm in run_status.py (nothing is grafted from live counters;
        ``rows_coalesce_failed`` is likewise audit-derived, via
        ``count_failed_coalesce_barrier_rows``).  So the derive-arm lever bites
        the RESUMED run B (derive-reconstructed) while run A's value comes from
        the live accumulator.

        OBSERVED RED (lever: delete ``counters.rows_buffered += 1`` from the
        ``(None, BUFFERED)`` arm in run_status.py — the non-completed branch,
        leaving the ``continue``; run
        ``-k test_resume_buffered_counter_reconciles_with_uninterrupted_run``):
            AssertionError: Resumed aggregation run must record at least one
            BUFFERED record (non-vacuous); got rows_buffered=0. ...
            assert 0 >= 1
        Run A (live accumulator) reports rows_buffered=3; run B
        (derive-reconstructed, lever removed) reports 0 — a 3-vs-0 mismatch, not
        a both-zero vacuity (the non-vacuity guard fires first; the field-loop
        ``uninterrupted=3, resumed=0`` mismatch is the same signal).  Reverting
        the lever restores GREEN.
        """
        from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
        from elspeth.contracts.enums import RunStatus
        from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
        from elspeth.core.config import CheckpointSettings

        n = 3

        # ── Run A (uninterrupted oracle) ──────────────────────────────────────
        db_a, config_a, graph_a, settings_a = self._build_end_of_source_flush_aggregation(n)
        run_a = Orchestrator(db_a).run(config_a, graph=graph_a, settings=settings_a, payload_store=MockPayloadStore())
        assert run_a.status == RunStatus.COMPLETED, run_a.status
        # Non-vacuity precondition: all N rows buffered → rows_buffered == N >= 1.
        assert run_a.rows_buffered == n, (
            f"Run A (uninterrupted end-of-source-flush aggregation of {n} rows) must record "
            f"rows_buffered={n} (one BUFFERED record per input row); got {run_a.rows_buffered}"
        )
        assert run_a.rows_buffered >= 1, "non-vacuity precondition"

        # ── Run B (run-1 + interrupt + resume via the all-terminal branch) ────
        db, config, graph, settings_obj = self._build_end_of_source_flush_aggregation(n)
        payload_store = MockPayloadStore()
        run_b1 = Orchestrator(db).run(config, graph=graph, settings=settings_obj, payload_store=payload_store)
        run_id = run_b1.run_id
        assert run_b1.rows_buffered == n, run_b1.rows_buffered

        # Interrupt: checkpoint + mark failed, deleting NO outcomes.  Every token
        # already has its terminal (or non-completed BUFFERED) record and no
        # scheduler work remains → the no-work finalize branch
        # reconstructs the cumulative counters from the intact audit trail.
        reseat_crashed_leader(db, run_id)
        checkpoint_mgr = CheckpointManager(db)
        recovery_mgr = RecoveryManager(db, checkpoint_mgr)
        create_checkpoint(
            checkpoint_mgr,
            run_id=run_id,
            sequence_number=1,
            barrier_scalars=None,
            graph=graph,
        )
        with db.engine.connect() as conn:
            conn.execute(
                text("UPDATE runs SET status = 'failed' WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            conn.commit()

        check = recovery_mgr.can_resume(run_id, graph)
        assert check.can_resume, f"cannot resume: {check.reason}"
        resume_point = recovery_mgr.get_resume_point(run_id, graph)
        assert resume_point is not None

        checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
        resume_orchestrator = Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config)
        run_b_resume = resume_orchestrator.resume(resume_point, config, graph, payload_store=payload_store, settings=settings_obj)

        # ── Reconciliation: EVERY counter field, non-vacuous on rows_buffered ─
        assert run_b_resume.status == RunStatus.COMPLETED, (
            f"Resume of the aggregation pipeline must reach COMPLETED; got {run_b_resume.status}"
        )
        assert run_b_resume.rows_buffered >= 1, (
            f"Resumed aggregation run must record at least one BUFFERED record (non-vacuous); "
            f"got rows_buffered={run_b_resume.rows_buffered}. If 0, derive's (None, BUFFERED) arm "
            f"miscounts the persisted BUFFERED records."
        )
        assert run_b_resume.rows_buffered == run_a.rows_buffered, (
            f"rows_buffered must equal the uninterrupted run: A={run_a.rows_buffered}, "
            f"B={run_b_resume.rows_buffered}. derive reconstructs this purely from the "
            f"(None, BUFFERED) arm in run_status.py (not grafted); a divergence means that "
            f"arm regressed or the BUFFERED records were not preserved across resume."
        )

        for field, a_val, b_val in counter_reconciliation_pairs(run_a, run_b_resume):
            assert b_val == a_val, (
                f"F2 reconciliation failure on '{field}': resumed run (run1 + resume) must equal "
                f"the uninterrupted run field-for-field. uninterrupted={a_val}, resumed={b_val}. "
                f"derive_resume_terminal_status_from_audit must reconstruct this field from the "
                f"audit trail to match the live accumulator."
            )

        assert dict(run_b_resume.routed_destinations) == dict(run_a.routed_destinations), (
            f"F2 reconciliation failure on routed_destinations: "
            f"uninterrupted={dict(run_a.routed_destinations)}, resumed={dict(run_b_resume.routed_destinations)}"
        )

    def test_rows_buffered_live_equals_derive_after_unification(self) -> None:
        """rows_buffered parity on the count==N mid-stream trigger (elspeth-e1dd5e1303 FIXED).

        For a batch aggregation with a ``count == N`` trigger (fires mid-stream on
        the Nth row, NOT at end-of-source), the live accumulator historically
        under-counted ``rows_buffered`` by one (N-1): the count-trigger's own token
        was consumed by the flush without ever yielding a ``(None, BUFFERED)``
        RowResult, while derive (which reconstructs the counter from the BUFFERED
        audit records — one per accepted row) reported N.  This test pinned that
        exact +1 divergence as a characterization test
        (``test_count_equals_n_rows_buffered_divergence_is_pinned``).

        F1 Task 4.3 Step 2 unified the two: every buffer-accept now yields exactly
        one ``(None, BUFFERED)`` RowResult — the flush-triggering token gets a
        synthetic BUFFERED result emitted by the drain — so the live counter equals
        the audit-derived value:

            uninterrupted oracle (live):  rows_buffered == N
            resumed (derive):             rows_buffered == N

        INVARIANT (same strength as the old pin, flipped to the unified value):
        live == derive == N exactly, and EVERY other counter field reconciles too —
        any re-divergence in either direction goes RED here.
        """
        from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
        from elspeth.contracts.enums import RunStatus
        from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
        from elspeth.core.config import CheckpointSettings

        n = 3

        # ── Run A (uninterrupted oracle, count == N → mid-stream trigger) ──────
        db_a, config_a, graph_a, settings_a = self._build_end_of_source_flush_aggregation(n, trigger_count=n)
        run_a = Orchestrator(db_a).run(config_a, graph=graph_a, settings=settings_a, payload_store=MockPayloadStore())
        assert run_a.status == RunStatus.COMPLETED, run_a.status

        # ── Run B (run-1 + interrupt + resume via the all-terminal branch) ────
        db, config, graph, settings_obj = self._build_end_of_source_flush_aggregation(n, trigger_count=n)
        payload_store = MockPayloadStore()
        run_b1 = Orchestrator(db).run(config, graph=graph, settings=settings_obj, payload_store=payload_store)
        run_id = run_b1.run_id
        checkpoint_mgr = CheckpointManager(db)
        recovery_mgr = RecoveryManager(db, checkpoint_mgr)
        reseat_crashed_leader(db, run_id)
        create_checkpoint(checkpoint_mgr, run_id=run_id, sequence_number=1, barrier_scalars=None, graph=graph)
        with db.engine.connect() as conn:
            conn.execute(text("UPDATE runs SET status = 'failed' WHERE run_id = :run_id"), {"run_id": run_id})
            conn.commit()
        check = recovery_mgr.can_resume(run_id, graph)
        assert check.can_resume, f"cannot resume: {check.reason}"
        resume_point = recovery_mgr.get_resume_point(run_id, graph)
        assert resume_point is not None
        checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
        resume_orchestrator = Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config)
        run_b_resume = resume_orchestrator.resume(resume_point, config, graph, payload_store=payload_store, settings=settings_obj)
        assert run_b_resume.status == RunStatus.COMPLETED, run_b_resume.status

        # ── Unified value: live == derive == N exactly (elspeth-e1dd5e1303 fix) ──
        assert run_a.rows_buffered == n, (
            f"UNIFICATION: uninterrupted oracle (live accumulator) must report rows_buffered == N == {n} "
            f"on a count==N mid-stream trigger — every buffer-accept (including the flush-triggering "
            f"token) yields exactly one (None, BUFFERED) RowResult; got {run_a.rows_buffered}. "
            f"An N-1 here means the count-trigger token's synthetic BUFFERED emission regressed "
            f"(F1 Task 4.3 Step 2, elspeth-e1dd5e1303)."
        )
        assert run_b_resume.rows_buffered == n, (
            f"UNIFICATION: resumed run (derive) must report rows_buffered == N == {n} (one BUFFERED audit "
            f"record per input row); got {run_b_resume.rows_buffered}. If this changed, derive's "
            f"(None, BUFFERED) arm moved — see elspeth-e1dd5e1303."
        )
        assert run_b_resume.rows_buffered == run_a.rows_buffered, (
            f"UNIFICATION: live and derive must agree on rows_buffered for the count==N topology. "
            f"oracle={run_a.rows_buffered}, resumed={run_b_resume.rows_buffered}. ANY delta is a "
            f"re-divergence of the unified counter (elspeth-e1dd5e1303) and may not land silently."
        )

        # ── EVERY counter field must reconcile (no divergent field remains) ────
        for field, a_val, b_val in counter_reconciliation_pairs(run_a, run_b_resume):
            assert b_val == a_val, (
                f"'{field}' must reconcile on the count==N topology (live/derive unification, "
                f"elspeth-e1dd5e1303). uninterrupted={a_val}, resumed={b_val}."
            )

    def test_resume_expand_coalesce_full_type_domain_roundtrip(self) -> None:
        """Task 12 addition (ADDENDUM 6): the token_data_ref envelope round-trips the FULL
        row-payload type domain through expand AND coalesce, with resume reconstruction.

        INTENT: mechanically enforce envelope completeness so a future type added to
        canonical_json (the proven row-payload domain) but NOT to checkpoint
        serialization is caught here.  checkpoint_dumps is called on the row payload at
        expand/coalesce time during a NORMAL run (Task 3 token_data_ref envelope), so a
        missing type is a happy-path crash, not just a resume bug.

        TYPE DOMAIN exercised (every type canonical_json accepts):
          str, int, float, bool, None, Decimal, datetime (tz-aware), date, time, bytes,
          UUID, nested dict, list, tuple, numpy scalar (np.int64 / np.float64 / np.bool_).

        END-TO-END PATH (production token_data_ref write + resume read):
          1. expand_token(child_payloads=[full-domain payload]) → writes the child's
             token_data_ref via checkpoint_dumps (the real expand envelope path).
          2. coalesce_tokens(merged_payload=full-domain payload) → writes the merged
             token's token_data_ref via checkpoint_dumps (the real coalesce envelope path).
          3. Read each envelope back from the payload store (the committed-barrier
             restore's input): checkpoint_loads + SchemaContract.from_checkpoint
             (hash-validated, Tier-1).
          4. Assert every reconstructed value is byte/type-faithful to the original.

        NORMALIZATION CAVEATS (asserted, per ADDENDUM 6 + serialization.py):
          - numpy scalars are NORMALIZED to Python primitives (np.int64→int,
            np.float64→float, np.bool_→bool).  numpy-ness is not semantic; we assert the
            VALUE and the PYTHON-PRIMITIVE type, NOT numpy identity.
          - tuple round-trips as tuple (envelope-tagged); list stays list.  A list nested
            inside the payload comes back as list; a tuple comes back as tuple.

        This is fully end-to-end through the envelope (the production write+read paths),
        so no serializer-level fallback is needed — every type traverses the real
        token_data_ref path.  (A bare checkpoint_dumps/_loads symmetry check is also
        performed as a defense-in-depth cross-check on the exact same payload.)

        RED LEVER (this is a completeness GATE, not a dispatch cell — its lever is a
        missing serializer type, the exact regression class ADDENDUM 6 closes): drop one
        envelope-tag branch from checkpoint_dumps (e.g. the `bytes` handler) so that type
        is no longer serializable.  The expand_token write path calls checkpoint_dumps on
        the payload during the NORMAL run → happy-path crash.
        Observed (bytes handler dropped): TypeError — "Cannot serialize value of type
        'bytes' into a checkpoint payload ...".  This confirms the gate mechanically
        catches a domain type the serializer fails to handle.
        """
        import datetime as _dt
        from decimal import Decimal
        from uuid import UUID

        import numpy as np

        from elspeth.contracts.audit import TokenRef
        from elspeth.contracts.enums import Determinism, NodeType
        from elspeth.contracts.schema import SchemaConfig
        from elspeth.contracts.schema_contract import FieldContract, SchemaContract
        from elspeth.core.checkpoint.serialization import checkpoint_dumps, checkpoint_loads

        # ── The full-domain payload.  Each key documents the type under test. ──
        aware_dt = _dt.datetime(2024, 6, 15, 12, 30, 45, tzinfo=UTC)
        a_date = _dt.date(2023, 1, 2)
        a_time = _dt.time(8, 9, 10)
        a_uuid = UUID("12345678-1234-5678-1234-567812345678")
        a_decimal = Decimal("12345.6789")
        a_bytes = b"\x00\x01\x02binary\xff"

        domain_payload: dict[str, object] = {
            "f_str": "hello",
            "f_int": 42,
            "f_float": 3.14159,
            "f_bool": True,
            "f_none": None,
            "f_decimal": a_decimal,
            "f_datetime": aware_dt,
            "f_date": a_date,
            "f_time": a_time,
            "f_bytes": a_bytes,
            "f_uuid": a_uuid,
            "f_nested_dict": {"inner_str": "deep", "inner_dt": aware_dt, "inner_dec": a_decimal},
            "f_list": [1, "two", a_decimal, aware_dt],
            "f_tuple": ("a", 2, a_date),
            "f_np_int": np.int64(7),
            "f_np_float": np.float64(2.5),
            "f_np_bool": np.bool_(True),
        }

        def _assert_domain_faithful(data: dict[str, Any], *, context: str, tuple_as_tuple: bool) -> None:
            """Assert each reconstructed value is byte/type-faithful to the original.

            ``tuple_as_tuple``: at the bare checkpoint_dumps/_loads serializer level a tuple
            round-trips AS a tuple (envelope-tagged).  But the resume reconstruction returns
            a PipelineRow, and PipelineRow.to_dict() runs deep_thaw, which normalizes tuples
            to lists (a row's tuple is not a first-class row type — consistent with
            canonical_json treating tuples as JSON arrays).  So through the PipelineRow path
            the tuple legitimately comes back as a list.  The SERIALIZER fidelity (tuple→tuple)
            is proven by the bare round-trip; the row-path normalization (tuple→list) is the
            documented PipelineRow behaviour, not a serializer gap."""
            assert data["f_str"] == "hello", context
            assert data["f_int"] == 42 and type(data["f_int"]) is int, context
            assert abs(data["f_float"] - 3.14159) < 1e-12, context
            assert data["f_bool"] is True, context
            assert data["f_none"] is None, context
            # Decimal: exact value + decimal.Decimal instance (not float/str).
            assert isinstance(data["f_decimal"], Decimal) and data["f_decimal"] == a_decimal, f"{context}: f_decimal={data['f_decimal']!r}"
            # datetime: tz-aware datetime instance (not str).
            assert isinstance(data["f_datetime"], _dt.datetime) and data["f_datetime"] == aware_dt, context
            assert data["f_datetime"].tzinfo is not None, context
            # date: date instance, NOT datetime (subclass-ordering load-bearing in serializer).
            assert type(data["f_date"]) is _dt.date and data["f_date"] == a_date, f"{context}: f_date type={type(data['f_date']).__name__}"
            # time: time instance.
            assert isinstance(data["f_time"], _dt.time) and data["f_time"] == a_time, context
            # bytes: exact bytes (base64 round-trip).
            assert isinstance(data["f_bytes"], bytes) and data["f_bytes"] == a_bytes, f"{context}: f_bytes={data['f_bytes']!r}"
            # UUID: UUID instance, exact value.
            assert isinstance(data["f_uuid"], UUID) and data["f_uuid"] == a_uuid, context
            # nested dict: recursively type-faithful.
            nested = data["f_nested_dict"]
            assert isinstance(nested, dict), context
            assert nested["inner_str"] == "deep", context
            assert isinstance(nested["inner_dt"], _dt.datetime) and nested["inner_dt"] == aware_dt, context
            assert isinstance(nested["inner_dec"], Decimal) and nested["inner_dec"] == a_decimal, context
            # list: stays a list; elements type-faithful.
            lst = data["f_list"]
            assert isinstance(lst, list) and lst[0] == 1 and lst[1] == "two", context
            assert isinstance(lst[2], Decimal) and lst[2] == a_decimal, context
            assert isinstance(lst[3], _dt.datetime) and lst[3] == aware_dt, context
            # tuple: at the serializer level it round-trips as a TUPLE (envelope-tagged);
            # through the PipelineRow path it normalizes to a list (deep_thaw).  Either way
            # the ELEMENTS must be type-faithful.
            tup = data["f_tuple"]
            if tuple_as_tuple:
                assert isinstance(tup, tuple), f"{context}: f_tuple came back as {type(tup).__name__}, expected tuple (serializer level)"
            else:
                assert isinstance(tup, list), (
                    f"{context}: f_tuple via PipelineRow must normalize to list (deep_thaw); got {type(tup).__name__}"
                )
            assert tup[0] == "a" and tup[1] == 2, context
            assert type(tup[2]) is _dt.date and tup[2] == a_date, context
            # numpy scalars: NORMALIZED to Python primitives (numpy-ness is not semantic).
            assert data["f_np_int"] == 7 and type(data["f_np_int"]) is int, f"{context}: f_np_int type={type(data['f_np_int']).__name__}"
            assert abs(data["f_np_float"] - 2.5) < 1e-12 and type(data["f_np_float"]) is float, context
            assert data["f_np_bool"] is True and type(data["f_np_bool"]) is bool, (
                f"{context}: f_np_bool type={type(data['f_np_bool']).__name__}"
            )

        # ── Defense-in-depth: bare checkpoint_dumps/_loads symmetry on the exact payload ──
        # (Catches a serializer asymmetry independent of the expand/coalesce envelope.)
        bare_roundtrip = checkpoint_loads(checkpoint_dumps(domain_payload))
        _assert_domain_faithful(bare_roundtrip, context="bare checkpoint_dumps/_loads symmetry", tuple_as_tuple=True)

        # ── Build the contract covering the full domain (python_type=object for the
        #    catch-all keys is what an OBSERVED/FLEXIBLE row would carry). ──
        full_contract = SchemaContract(
            mode="FLEXIBLE",
            fields=tuple(
                FieldContract(normalized_name=k, original_name=k, python_type=object, required=False, source="inferred")
                for k in domain_payload
            ),
            locked=True,
        )

        _OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
        payload_store = MockPayloadStore()
        db = make_landscape_db()
        factory = RecorderFactory(db, payload_store=payload_store)

        run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
        coordination_token = leader_token_for(db, run.run_id)
        node = factory.data_flow.register_node(
            coordination_token=coordination_token,
            plugin_name="explode",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            determinism=Determinism.DETERMINISTIC,
            schema_config=_OBSERVED_SCHEMA,
        )
        row, parent = factory.data_flow.create_row_with_token(
            coordination_token=coordination_token,
            source_node_id=node.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            data={"seed": 1},
        )

        # ── (1) EXPAND path: child token_data_ref envelope carries the full domain ──
        children, _expand_group_id = factory.data_flow.expand_token(
            member_token=coordination_token.membership,
            parent_ref=TokenRef(token_id=parent.token_id, run_id=run.run_id),
            row_id=row.row_id,
            child_payloads=[dict(domain_payload)],
            output_contract=full_contract,
            step_in_pipeline=1,
        )
        assert len(children) == 1
        expand_child = children[0]
        assert expand_child.token_data_ref is not None, "expand child must carry token_data_ref"

        # ── (2) COALESCE path: merged token_data_ref envelope carries the full domain ──
        # coalesce_tokens' durable strict pop (spec rulings 24/28) requires an
        # innermost shared FORK lineage frame on every parent — crafted here via
        # the create_token(..., lineage_frames=) seam.
        token_x = factory.data_flow.create_token(
            coordination_token=coordination_token,
            row_id=row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-join-balance-domain-grp", member_key="x"),),
        )
        token_y = factory.data_flow.create_token(
            coordination_token=coordination_token,
            row_id=row.row_id,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-join-balance-domain-grp", member_key="y"),),
        )
        ensure_fork_group_record(factory, run_id=run.run_id, group_id="fork-join-balance-domain-grp", opener_token_id=token_x.token_id)
        merged = factory.data_flow.coalesce_tokens(
            coordination_token=coordination_token,
            parent_refs=[
                TokenRef(token_id=token_x.token_id, run_id=run.run_id),
                TokenRef(token_id=token_y.token_id, run_id=run.run_id),
            ],
            row_id=row.row_id,
            merged_payload=dict(domain_payload),
            merged_contract=full_contract,
            step_in_pipeline=2,
        )
        assert merged.token_data_ref is not None, "merged token must carry token_data_ref"

        # ── (3) PERSISTED ENVELOPES: read each token_data_ref back from the payload
        #    store (the {data, contract} envelope the committed-barrier restore reads)
        #    and hash-validate the contract (SchemaContract.from_checkpoint, Tier-1) ──
        for label, token_data_ref in (("expand child", expand_child.token_data_ref), ("merged token", merged.token_data_ref)):
            envelope = checkpoint_loads(payload_store.retrieve(token_data_ref).decode("utf-8"))
            _assert_domain_faithful(dict(envelope["data"]), context=f"{label} token_data_ref envelope", tuple_as_tuple=True)
            restored_contract = SchemaContract.from_checkpoint(envelope["contract"])
            assert {fc.normalized_name for fc in restored_contract.fields} == set(domain_payload), (
                f"{label} envelope contract must carry the full domain field set"
            )
