"""Bound-region (SESE) computation and structural validation (spec §7 rules 3, depth cap §6.3).

A bound region is the SESE span of one bound group: the nodes strictly
between its opener and its closer. Membership walks SUCCESS-PATH edges
only — RoutingMode.DIVERT edges (on_error, __quarantine__, __failsink__)
are failure semantics, not region topology (pinned decision 1 in the WS2
plan; §7 rule 9 treats in-region on_error as legal).

`BoundRegion.member_node_ids` is a "between" set (forward reach ∩ backward
reach, minus the opener/closer themselves) — it is NOT yet a verified SESE
interior. A member may still have an inbound non-DIVERT edge originating
outside the region, or an outbound edge that bypasses the closer; ruling
those out is spec §7 rule 4, scoped to Task 7. Consumers reading
`member_node_ids` before that lands must not assume single-entry/single-exit
has been proven.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.enums import RoutingMode
from elspeth.contracts.types import NodeID
from elspeth.core.dag.group_bindings import GroupBinding, GroupBindingRegistry
from elspeth.core.dag.models import GraphValidationError

if TYPE_CHECKING:
    from elspeth.core.dag.graph import ExecutionGraph

ESCALATION_ITERATIONS_PER_LEVEL = 8
_BASE_FLUSH_ITERATIONS = 1_000


def derive_escalation_fixpoint_bound(max_observed_depth: int) -> int:
    """Non-convergence bound for the EOF drain fixpoint, derived from build depth.

    Spec §6.3: "derived at build from the actual depth (+ margin), never a
    constant — today's MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000 would
    collide with an override-deep unwind."

    THE one fixpoint formula (2026-08-22 synthesis): 1_000 + 8 * depth —
    each bound nesting level adds at most a handful of
    escalate-notify-reevaluate rounds, so depth-5 stays at 1_040 and an
    override-depth-1000 unwind gets 9_000. WS3's
    `derive_end_of_input_flush_bound` aligns to exactly this formula
    (consuming `graph.get_max_bound_region_depth()`); competing formulas are
    deleted, never forked.
    """
    return _BASE_FLUSH_ITERATIONS + ESCALATION_ITERATIONS_PER_LEVEL * max_observed_depth


@dataclass(frozen=True)
class BoundRegion:
    """One bound group's SESE span. Opener and closer are EXCLUDED from membership."""

    binding: GroupBinding
    member_node_ids: frozenset[NodeID]
    depth: int


def _forward_reach(graph: ExecutionGraph, start: NodeID, stop: NodeID) -> set[NodeID]:
    """Nodes reachable from start via non-DIVERT edges, not expanding through stop."""
    seen: set[NodeID] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in graph.get_outgoing_edges(current):
            if edge.mode is RoutingMode.DIVERT:
                continue
            nxt = NodeID(edge.to_node)
            if nxt in seen or nxt == stop:
                if nxt == stop:
                    seen.add(nxt)
                continue
            seen.add(nxt)
            frontier.append(nxt)
    return seen


def _backward_reach(graph: ExecutionGraph, start: NodeID, stop: NodeID) -> set[NodeID]:
    """Nodes that reach start via non-DIVERT edges, not expanding through stop."""
    seen: set[NodeID] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in graph.get_incoming_edges(current):
            if edge.mode is RoutingMode.DIVERT:
                continue
            prev = NodeID(edge.from_node)
            if prev in seen or prev == stop:
                if prev == stop:
                    seen.add(prev)
                continue
            seen.add(prev)
            frontier.append(prev)
    return seen


def compute_bound_regions(
    graph: ExecutionGraph,
    registry: GroupBindingRegistry,
    *,
    max_depth: int,
) -> tuple[BoundRegion, ...]:
    """Compute the SESE span of every bound group, enforce well-nestedness and the depth cap.

    Raises:
        GraphValidationError: two regions partially overlap (spec §7 rule 3), or
            the deepest region's nesting exceeds ``max_depth`` (spec §6.3).
    """
    spans: list[tuple[GroupBinding, frozenset[NodeID]]] = []
    for binding in registry.bindings:
        forward = _forward_reach(graph, binding.opener_node_id, binding.closer_node_id)
        backward = _backward_reach(graph, binding.closer_node_id, binding.opener_node_id)
        members = frozenset((forward & backward) - {binding.opener_node_id, binding.closer_node_id})
        spans.append((binding, members))

    def _span(binding: GroupBinding, members: frozenset[NodeID]) -> frozenset[NodeID]:
        return members | {binding.opener_node_id, binding.closer_node_id}

    # Well-nestedness (spec §7 rule 3): regions fully contain or are disjoint.
    for i, (b1, m1) in enumerate(spans):
        for b2, m2 in spans[i + 1 :]:
            s1, s2 = _span(b1, m1), _span(b2, m2)
            if s1.isdisjoint(s2):
                continue
            if s2 <= m1 or s1 <= m2:
                continue  # strictly nested (inner span entirely inside outer MEMBERS)
            raise GraphValidationError(
                f"Bound regions '{b1.closer_name}' (opener '{b1.opener_name}') and "
                f"'{b2.closer_name}' (opener '{b2.opener_name}') partially overlap. "
                f"Bound regions must fully contain one another or be disjoint (spec §7 rule 3): "
                f"close the inner group before the outer closer, or separate the regions.",
                component_id=b2.closer_name,
                component_type=b2.closer_kind,
            )

    regions: list[BoundRegion] = []
    for b1, m1 in spans:
        s1 = _span(b1, m1)
        depth = 1 + sum(1 for b2, m2 in spans if b2 is not b1 and s1 <= m2)
        regions.append(BoundRegion(binding=b1, member_node_ids=m1, depth=depth))

    too_deep = [r for r in regions if r.depth > max_depth]
    if too_deep:
        worst = max(too_deep, key=lambda r: r.depth)
        raise GraphValidationError(
            f"Bound-region nesting depth {worst.depth} exceeds the configured maximum {max_depth} "
            f"(innermost closer: '{worst.binding.closer_name}'). Deeper nesting is model-correct but "
            f"unsupported beyond 5 layers (spec §6.3) — per-token audit churn scales with depth. Raise "
            f"max_bound_region_depth in settings to accept the churn knowingly.",
            component_id=worst.binding.closer_name,
            component_type=worst.binding.closer_kind,
        )
    return tuple(regions)
