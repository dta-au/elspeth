"""Bound-region (SESE) computation and structural validation (spec §7 rules 3/4, depth cap §6.3).

A bound region is the SESE span of one bound group: the nodes strictly
between its opener and its closer. Membership walks SUCCESS-PATH edges
only — RoutingMode.DIVERT edges (on_error, __quarantine__, __failsink__)
are failure semantics, not region topology (pinned decision 1 in the WS2
plan; §7 rule 9 treats in-region on_error as legal).

`BoundRegion.member_node_ids` is a "between" set (forward reach ∩ backward
reach, minus the opener/closer themselves) — it is a verified SESE interior
ONLY once `validate_sese_regions` (spec §7 rule 4) has run: it rejects a
member with an outbound edge that reaches a sink before the closer, a member
with no success path back to the closer, a non-branch opener route that
re-enters the region (2026-08-23 fix round, ruled), and an inbound edge into
any in-region node that originates outside the region.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.enums import FrameKind, NodeType, RoutingMode
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


def _forward_reach_from(graph: ExecutionGraph, starts: set[NodeID], stop: NodeID) -> set[NodeID]:
    """Nodes reachable from any of ``starts`` via non-DIVERT edges, not expanding through stop.

    Generalizes :func:`_forward_reach` to multiple start nodes. Rule 4's
    forward walk (:func:`validate_sese_regions`) must NOT anchor at a FORK
    binding's opener node itself: the opener is an ordinary gate, and its
    OTHER routing labels (e.g. a plain `'false': <sink>` route sitting beside
    `'true': fork`) can lead straight to a sink with no fork branch ever
    taken — that is not a leak out of the region, because no lineage frame
    for this group was ever minted on that path. The true forward-walk
    anchor is the set of nodes the opener's *fork branch* edges lead to
    (``edge.label in binding.member_roster``), never the opener's full
    outgoing-edge set. Verified against every fork_coalesce/row_union_ab
    example: each carries exactly this "route to fork" + "route to sink"
    pair on its opener gate.
    """
    seen: set[NodeID] = set()
    frontier: list[NodeID] = []
    for start in starts:
        seen.add(start)
        if start != stop:
            frontier.append(start)
    while frontier:
        current = frontier.pop()
        for edge in graph.get_outgoing_edges(current):
            if edge.mode is RoutingMode.DIVERT:
                continue
            nxt = NodeID(edge.to_node)
            if nxt in seen:
                continue
            seen.add(nxt)
            if nxt != stop:
                frontier.append(nxt)
    return seen


def _fork_branch_starts(graph: ExecutionGraph, binding: GroupBinding) -> set[NodeID]:
    """The forward-walk anchor for one bound group: a FORK's own branch entries.

    For an EXPAND/scope binding (``member_roster`` empty — ruling 28, Task 8's
    concern), the opener is a plain multi-row transform with no sibling routing
    labels to filter out, so the opener node itself is the correct anchor.

    An opener out-edge qualifies if EITHER its label is in the declared
    roster OR its target is backward-reachable from the closer (review F1,
    2026-08-23 fix round). The label-only predicate under-admits two real
    shapes: (a) a route whose target lands back inside the region through a
    node the roster doesn't name directly (e.g. an intermediate non-fork
    gate re-entering the region before reaching the closer), and (b) a route
    whose target happens to be one of the SAME gate's own fork branches
    under a route label override (`builder.py`'s MATCH-PRODUCERS-TO-CONSUMERS
    pass draws that edge with the route label, not the branch name, so
    `edge.label in member_roster` misses it). Both are genuine forward-walk
    entries; the label check alone let both bypass rule 4 entirely.
    """
    if binding.kind is not FrameKind.FORK:
        return {binding.opener_node_id}
    inside = _backward_reach(graph, binding.closer_node_id, binding.opener_node_id)
    return {
        NodeID(edge.to_node)
        for edge in graph.get_outgoing_edges(binding.opener_node_id)
        if edge.mode is not RoutingMode.DIVERT and (edge.label in binding.member_roster or NodeID(edge.to_node) in inside)
    }


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


def validate_sese_regions(graph: ExecutionGraph, regions: tuple[BoundRegion, ...]) -> None:
    """§7 rule 4 — bidirectional SESE, success-path edges only (pinned decision 1).

    Forward: every non-DIVERT path from a FORK binding's own branch entries
    (or an EXPAND binding's opener) reaches the closer before any sink.
    A non-branch opener route must not re-enter the region at all (F2,
    maintainer ruled 2026-08-23). Backward: every non-DIVERT edge into a
    region member or the closer originates at the opener or another member.

    Raises:
        GraphValidationError: a bound region's interior reaches a sink
            before its closer, a member has no success path back to the
            closer, a non-branch opener route re-enters the region, or an
            in-region node's inbound edge originates outside the region.
    """
    for region in regions:
        binding = region.binding
        in_region = region.member_node_ids | {binding.opener_node_id, binding.closer_node_id}

        # Forward walk, anchored at the branch entries (see
        # _forward_reach_from's docstring for why the opener node itself is
        # the wrong anchor). member_node_ids is forward ∩ backward and
        # therefore already excludes anything with no path back to the
        # closer, so the sink/no-path checks run over the WIDER forward-only
        # set, not member_node_ids (a member reached but never returning to
        # the closer is exactly what "no path to closer" must catch).
        branch_starts = _fork_branch_starts(graph, binding)
        forward_only = _forward_reach_from(graph, branch_starts, binding.closer_node_id) - {binding.closer_node_id}
        closer_reachable = _backward_reach(graph, binding.closer_node_id, binding.opener_node_id)
        # sorted(): forward_only is a set[NodeID] (NodeID is str-derived), so
        # unordered iteration makes WHICH violation raises first depend on
        # PYTHONHASHSEED (review F3, demonstrated across 8 seeds — including
        # a flip between the sink-inside and no-path-to-closer LIMBS, which
        # carry different parity dispositions). Deterministic order restores
        # a single, reproducible verdict per topology.
        for member in sorted(forward_only):
            info = graph.get_node_info(member)
            if info.node_type is NodeType.SINK:
                raise GraphValidationError(
                    f"Bound region '{binding.closer_name}' (opener '{binding.opener_name}') reaches sink "
                    f"'{member}' before the region's closer. No token may leave a bound region except "
                    f"through its closer — sinks inside a bound region are rejected flat (spec §7 rule 4). "
                    f"Move the sink after the closer, or unbind the group.",
                    component_id=binding.closer_name,
                    component_type=binding.closer_kind,
                )
            if member not in closer_reachable:
                raise GraphValidationError(
                    f"Node '{member}' inside bound region '{binding.closer_name}' has no success path to "
                    f"the region's closer. Every path from the opener must reach the closer "
                    f"(spec §7 rule 4).",
                    component_id=binding.closer_name,
                    component_type=binding.closer_kind,
                )

        # F2 (maintainer ruled, 2026-08-23): a non-branch opener out-edge
        # that re-enters the region. Such a route carries no lineage frame
        # for this group — the token never took a fork branch — so it must
        # not be allowed to feed an in-region node at all, regardless of
        # where it leads from there (F1's widened forward walk alone does
        # not close this: it makes the target get WALKED, which is silent
        # when the target leads nowhere alarming, but does not stop the
        # target being treated as a legitimate in-region MEMBER, so the
        # backward walk below still authorizes the edge). This is exactly
        # the E1 residual-risk class the 2026-08-23 no-queue-exemption
        # ruling exists to foreclose: an unframed token silently entering a
        # bound region (e.g. through a queue a real branch also feeds) and
        # settling a branch it was never part of.
        #
        # legitimate_targets excludes a false positive discovered while
        # verifying this fix: builder.py's gate fallthrough bookkeeping
        # (`gate_default_continue_targets`) draws an ADDITIONAL edge labeled
        # "continue" to a gate's sole unambiguous processing consumer, even
        # when that consumer was already reached via a real, roster-labeled
        # branch edge (verified against the nested-fork-in-fork fixture:
        # `outer_fork`'s "outer_a" branch and its "continue" fallthrough
        # both target `inner_fork`). A non-roster edge whose target is ALSO
        # the target of a real branch edge from the same opener is a
        # harmless duplicate, not a backdoor — only a target unreachable via
        # any roster-labeled edge is a genuine unframed entry.
        if binding.kind is FrameKind.FORK:
            opener_edges = graph.get_outgoing_edges(binding.opener_node_id)
            legitimate_targets = {
                NodeID(edge.to_node) for edge in opener_edges if edge.mode is not RoutingMode.DIVERT and edge.label in binding.member_roster
            }
            for edge in sorted(opener_edges, key=lambda e: (e.label, e.to_node)):
                if edge.mode is RoutingMode.DIVERT:
                    continue
                if edge.label in binding.member_roster:
                    continue
                target = NodeID(edge.to_node)
                if target in legitimate_targets:
                    continue
                if target in in_region:
                    raise GraphValidationError(
                        f"Gate '{binding.opener_name}' route '{edge.label}' is not one of the fork's declared "
                        f"branches, but it feeds '{target}', which is inside bound region "
                        f"'{binding.closer_name}'. A route outside the fork's branch list carries no lineage "
                        f"frame for this group, so it must not enter the region (spec §7 rule 4). Route "
                        f"'{edge.label}' outside the region, or add it to the fork's declared branches.",
                        component_id=binding.closer_name,
                        component_type=binding.closer_kind,
                    )

        # Backward walk: every non-DIVERT edge into an in-region node (or the
        # closer itself) must originate at the opener or another member.
        entry_targets = region.member_node_ids | {binding.closer_node_id}
        for member in sorted(entry_targets):
            for edge in graph.get_incoming_edges(member):
                if edge.mode is RoutingMode.DIVERT:
                    continue
                origin = NodeID(edge.from_node)
                if origin not in in_region:
                    raise GraphValidationError(
                        f"Edge into '{member}' (bound region '{binding.closer_name}') originates outside "
                        f"the bound region at '{origin}'. Every path into an in-region node must "
                        f"originate at the opener '{binding.opener_name}' (spec §7 rule 4, backward walk; "
                        f"row_union precedent). Feed that input from inside the region, or move the "
                        f"consumer outside it.",
                        component_id=binding.closer_name,
                        component_type=binding.closer_kind,
                    )
