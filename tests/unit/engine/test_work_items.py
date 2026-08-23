"""Unit tests for WorkItem cursor objects and factory helpers."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.engine.dag_navigator import DAGNavigator
from elspeth.engine.work_items import WorkItem, WorkItemFactory, resolve_merged_branch_barrier
from elspeth.testing import make_token_info


def test_work_item_boundary_is_outside_dag_navigator() -> None:
    assert WorkItem.__module__ == "elspeth.engine.work_items"
    assert WorkItemFactory.__module__ == "elspeth.engine.work_items"
    assert not hasattr(DAGNavigator, "create_work_item")
    assert not hasattr(DAGNavigator, "create_continuation_work_item")


def test_factory_rejects_mismatched_coalesce_name_and_node_id() -> None:
    alpha_node = NodeID("coalesce::alpha")
    beta_node = NodeID("coalesce::beta")
    navigator = DAGNavigator(
        node_to_plugin={},
        node_to_next={alpha_node: None, beta_node: None},
        coalesce_node_ids={CoalesceName("alpha"): alpha_node, CoalesceName("beta"): beta_node},
        structural_node_ids=frozenset({alpha_node, beta_node}),
        coalesce_name_by_node_id={alpha_node: CoalesceName("alpha"), beta_node: CoalesceName("beta")},
        coalesce_on_success_map={},
        sink_names=frozenset(),
    )

    with pytest.raises(OrchestrationInvariantError, match="coalesce metadata mismatch"):
        WorkItemFactory(navigator).create(
            token=make_token_info(data={"value": 1}),
            current_node_id=beta_node,
            coalesce_name=CoalesceName("alpha"),
            coalesce_node_id=beta_node,
        )


def test_row_union_metadata_must_be_supplied_as_a_pair() -> None:
    with pytest.raises(OrchestrationInvariantError, match="row_union fields must be both set or both None"):
        WorkItem(
            token=make_token_info(data={"value": 1}),
            current_node_id=NodeID("row_union::merge"),
            row_union_name=RowUnionName("merge"),
        )


def test_work_item_cannot_target_two_barrier_kinds() -> None:
    with pytest.raises(OrchestrationInvariantError, match="cannot target both"):
        WorkItem(
            token=make_token_info(data={"value": 1}),
            current_node_id=NodeID("coalesce::merge"),
            coalesce_node_id=NodeID("coalesce::merge"),
            coalesce_name=CoalesceName("merge"),
            row_union_node_id=NodeID("row_union::merge"),
            row_union_name=RowUnionName("merge"),
        )


def test_factory_resolves_coalesce_name_from_node_id() -> None:
    node_id = NodeID("coalesce::merge")
    navigator = DAGNavigator(
        node_to_plugin={},
        node_to_next={node_id: None},
        coalesce_node_ids={CoalesceName("merge"): node_id},
        structural_node_ids=frozenset({node_id}),
        coalesce_name_by_node_id={node_id: CoalesceName("merge")},
        coalesce_on_success_map={},
        sink_names=frozenset(),
    )

    item = WorkItemFactory(navigator).create(
        token=make_token_info(data={"value": 1}),
        current_node_id=node_id,
        coalesce_node_id=node_id,
    )

    assert item.coalesce_name == CoalesceName("merge")


def test_factory_accepts_matching_coalesce_name_and_node_id() -> None:
    node_id = NodeID("coalesce::merge")
    navigator = DAGNavigator(
        node_to_plugin={},
        node_to_next={node_id: None},
        coalesce_node_ids={CoalesceName("merge"): node_id},
        structural_node_ids=frozenset({node_id}),
        coalesce_name_by_node_id={node_id: CoalesceName("merge")},
        coalesce_on_success_map={},
        sink_names=frozenset(),
    )

    item = WorkItemFactory(navigator).create(
        token=make_token_info(data={"value": 1}),
        current_node_id=node_id,
        coalesce_name=CoalesceName("merge"),
        coalesce_node_id=node_id,
    )

    assert item.coalesce_node_id == node_id


class TestResolveMergedBranchBarrier:
    """Unit coverage for resolve_merged_branch_barrier's four outcomes (elspeth-0bd2cde19a)."""

    def test_flat_case_returns_completed_coalesce_name_unchanged(self) -> None:
        """merged_branch_name is None (no enclosing frame) -> completed_coalesce_name verbatim.

        One contract for all three call sites (round-2 F1 correction:
        measured control-vs-patched byte-identical at
        _notify_coalesce_of_lost_branch too, scratchpad/p8_flat_lossmerge.py
        — that site's own earlier terminal/non-terminal split means this
        value is load-bearing only at complete_coalesce_merge/
        _fire_coalesce_merge's terminal-coalesce check, but is harmless
        everywhere, so all three sites pass the same thing).
        """
        assert resolve_merged_branch_barrier(
            None,
            completed_coalesce_name=CoalesceName("merge_outer"),
            branch_to_coalesce={},
            branch_to_row_union={},
        ) == (CoalesceName("merge_outer"), None)

    def test_nested_branch_bound_to_coalesce_resolves_fresh(self) -> None:
        result = resolve_merged_branch_barrier(
            "outer_a",
            completed_coalesce_name=CoalesceName("merge_inner"),
            branch_to_coalesce={BranchName("outer_a"): CoalesceName("merge_outer")},
            branch_to_row_union={},
        )
        assert result == (CoalesceName("merge_outer"), None)

    def test_nested_branch_bound_to_row_union_resolves_fresh(self) -> None:
        result = resolve_merged_branch_barrier(
            "outer_a",
            completed_coalesce_name=CoalesceName("merge_inner"),
            branch_to_coalesce={},
            branch_to_row_union={BranchName("outer_a"): RowUnionName("union_outer")},
        )
        assert result == (None, RowUnionName("union_outer"))

    def test_nested_branch_bound_to_neither_map_returns_none_none(self) -> None:
        """Reachable today (E2 made consumer-fed outer branches authorable, F3).

        A nested region inside an UNBOUND fork branch (fed by an ordinary
        consumer, spec §7 E2 — no enclosing barrier): the continuation
        carries no barrier context, distinct from BOTH the flat-default
        return AND a resolved barrier. See
        tests/integration/core/dag/test_nested_fork_coalesce.py's unbound-
        outer-branch case for the end-to-end witness.
        """
        result = resolve_merged_branch_barrier(
            "outer_a",
            completed_coalesce_name=CoalesceName("merge_inner"),
            branch_to_coalesce={},
            branch_to_row_union={},
        )
        assert result == (None, None)
