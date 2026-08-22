"""Bound-region computation: membership, well-nestedness, depth cap, fixpoint bound."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import FrameKind, NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID
from elspeth.core.config import CoalesceSettings, GateSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.bound_regions import compute_bound_regions, derive_escalation_fixpoint_bound
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform

# Graph-level cases use the shared stub-plugin builders defined in this file,
# modeled on tests/unit/core/dag/test_builder_validation.py (mock source/sink/
# transform classes).


class _BoundRegionMockSource:
    name = "mock_source"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_validation_failure = "discard"
    on_success = "source_out"
    _output_schema_config: SchemaConfig | None = None


class _BoundRegionMockSink:
    """A mock sink with a caller-chosen name, for graphs needing >1 sink."""

    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, name: str = "mock_sink") -> None:
        self.name = name

    def _reset_diversion_log(self) -> None:
        pass


class _BoundRegionTransform:
    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = "output"
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self, *, name: str, output_schema_config: SchemaConfig) -> None:
        self.name = name
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = output_schema_config


def _plugin_names(graph: ExecutionGraph, ids: frozenset[NodeID]) -> set[str]:
    return {graph.get_node_info(n).plugin_name for n in ids}


def _build_fork_coalesce_with_branch_transforms() -> ExecutionGraph:
    """source -> gate 'fork_to' [path_a, path_b] -> per-branch transform -> coalesce.

    Both branch transforms are region members; the gate (opener) and the
    coalesce (closer) are not — membership excludes opener/closer.
    """
    source = _BoundRegionMockSource()
    branch_a = _BoundRegionTransform(name="branch_a_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=branch_a,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_a_transform",
                    plugin=branch_a.name,
                    input="path_a",
                    on_success="path_a_out",
                    on_error="discard",
                    options={},
                ),
            ),
            WiredTransform(
                plugin=branch_b,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_b_transform",
                    plugin=branch_b.name,
                    input="path_b",
                    on_success="path_b_out",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="gate",
                input="source_out",
                condition="'all'",
                routes={"all": "fork"},
                fork_to=["path_a", "path_b"],
            )
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="coalesce",
                branches={"path_a": "path_a_out", "path_b": "path_b_out"},
                policy="require_all",
                merge="union",
                on_success="out",
            )
        ],
    )


def _build_nested_fork_in_fork(*, max_bound_region_depth: int) -> ExecutionGraph:
    """Outer fork [left, right] closed by outer coalesce; 'left' itself carries
    an inner fork [la, lb] closed by an inner coalesce whose (un-on_success'd)
    output feeds the outer coalesce's 'left' branch connection. 'right' is a
    plain transform chain into the outer coalesce.

    A coalesce's on_success may only name a sink or be omitted (in which case
    it produces a connection named after itself) — it can never name an
    arbitrary intermediate connection — so the inner coalesce is chained into
    the outer one by leaving its on_success unset and pointing the outer
    coalesce's 'left' branch at the inner coalesce's own name.
    """
    source = _BoundRegionMockSource()
    right_transform = _BoundRegionTransform(name="right_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=right_transform,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="right_transform",
                    plugin=right_transform.name,
                    input="right",
                    on_success="right_out",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="outer_gate",
                input="source_out",
                condition="'all'",
                routes={"all": "fork"},
                fork_to=["left", "right"],
            ),
            GateSettings(
                name="inner_gate",
                input="left",
                condition="'all'",
                routes={"all": "fork"},
                fork_to=["la", "lb"],
            ),
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="inner_coalesce",
                branches={"la": "la", "lb": "lb"},
                policy="require_all",
                merge="union",
                # on_success omitted: chains into outer_coalesce's 'left' branch
                # via the connection named after this coalesce itself.
            ),
            CoalesceSettings(
                name="outer_coalesce",
                branches={"left": "inner_coalesce", "right": "right_out"},
                policy="require_all",
                merge="union",
                on_success="out",
            ),
        ],
        max_bound_region_depth=max_bound_region_depth,
    )


def _build_settings_driven_partial_overlap() -> None:
    """Genuinely CROSSING bound regions, authorable through settings alone
    (review round 1, F1 — the direct settings-level counterpart of
    `_build_partially_overlapping_regions` below).

        source -> g2 fork [m, n]
          branch m -> g1 fork [a, b]
            branch a -> ta -> a_out
            branch b -> tb -> b_out
          branch n -> tn -> n_out
        c2 (closes g2) branches {m: a_out, n: n_out} -> connection "c2"
        c1 (closes g1) branches {a: c2,    b: b_out} -> sink out

    c2's 'm' branch is wired to listen on "a_out" — a connection produced
    INSIDE g1's region (downstream of g1) — while its 'n' branch listens on
    "n_out", produced OUTSIDE g1's region; c1 then consumes c2's own output
    for its 'a' branch. So open(g2) < open(g1) < close(c2) < close(c1): a
    textbook crossing where neither span contains the other.

    No earlier builder guard rejects this: the row_union chain-descent guard
    (`_trace_branch_endpoints`, row_union-only) does not apply to coalesce
    branches, and the general coalesce-side well-nestedness walk (spec rule
    4) is Task 7's, not yet built. It also survives Task 6 rule 2: both
    forks are fully bound to exactly one closer each, and both rosters match
    their closer's branches exactly (`g2.fork_to == c2.branches == {m, n}`;
    `g1.fork_to == c1.branches == {a, b}`) — so this `compute_bound_regions`
    check is the ONLY thing rejecting the shape today, and stays the only
    thing rejecting it after Task 6 lands.
    """
    source = _BoundRegionMockSource()
    ta = _BoundRegionTransform(name="ta", output_schema_config=SchemaConfig(mode="observed", fields=None))
    tb = _BoundRegionTransform(name="tb", output_schema_config=SchemaConfig(mode="observed", fields=None))
    tn = _BoundRegionTransform(name="tn", output_schema_config=SchemaConfig(mode="observed", fields=None))

    ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=ta,  # type: ignore[arg-type]
                settings=TransformSettings(name="ta", plugin=ta.name, input="a", on_success="a_out", on_error="discard", options={}),
            ),
            WiredTransform(
                plugin=tb,  # type: ignore[arg-type]
                settings=TransformSettings(name="tb", plugin=tb.name, input="b", on_success="b_out", on_error="discard", options={}),
            ),
            WiredTransform(
                plugin=tn,  # type: ignore[arg-type]
                settings=TransformSettings(name="tn", plugin=tn.name, input="n", on_success="n_out", on_error="discard", options={}),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(name="g2", input="source_out", condition="'all'", routes={"all": "fork"}, fork_to=["m", "n"]),
            GateSettings(name="g1", input="m", condition="'all'", routes={"all": "fork"}, fork_to=["a", "b"]),
        ],
        coalesce_settings=[
            CoalesceSettings(name="c2", branches={"m": "a_out", "n": "n_out"}, policy="require_all", merge="union"),
            CoalesceSettings(name="c1", branches={"a": "c2", "b": "b_out"}, policy="require_all", merge="union", on_success="out"),
        ],
    )


def _build_partially_overlapping_regions() -> None:
    """Direct pin of the well-nestedness predicate over the raw
    `add_edge`/`GroupBinding` surface — the same surface
    `build_group_binding_registry` and the builder's node/edge construction
    ultimately produce. `_build_settings_driven_partial_overlap` above is the
    real-world-authorable companion; this one stays as the cheapest possible
    pin on the predicate itself, independent of any settings-layer wiring.
    """
    graph = ExecutionGraph()
    for node_id, node_type, plugin_name in [
        ("open1", NodeType.GATE, "gate1"),
        ("open2", NodeType.GATE, "gate2"),
        ("close1", NodeType.COALESCE, "coalesce1"),
        ("close2", NodeType.COALESCE, "coalesce2"),
    ]:
        graph.add_node(node_id, node_type=node_type, plugin_name=plugin_name)
    graph.add_edge("open1", "open2", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("open2", "close1", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("close1", "close2", label="continue", mode=RoutingMode.MOVE)

    binding1 = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("open1"),
        opener_name="gate1",
        closer_node_id=NodeID("close1"),
        closer_name="coalesce1",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("a", "b"),
    )
    binding2 = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("open2"),
        opener_name="gate2",
        closer_node_id=NodeID("close2"),
        closer_name="coalesce2",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("c", "d"),
    )
    registry = GroupBindingRegistry(bindings=(binding1, binding2))
    compute_bound_regions(graph, registry, max_depth=5)


class TestFixpointBound:
    def test_depth_zero_keeps_base(self) -> None:
        assert derive_escalation_fixpoint_bound(0) == 1_000

    def test_bound_grows_with_depth(self) -> None:
        # THE one formula (2026-08-22 synthesis): 1_000 + 8 * depth.
        assert derive_escalation_fixpoint_bound(5) == 1_040
        assert derive_escalation_fixpoint_bound(1_000) == 9_000  # override-deep builds outgrow the old constant


class TestRegionMembership:
    def test_fork_coalesce_region_members(self) -> None:
        graph = _build_fork_coalesce_with_branch_transforms()
        regions = graph.get_bound_regions()
        assert len(regions) == 1
        region = regions[0]
        # Branch transforms are members; gate and coalesce are NOT.
        member_names = _plugin_names(graph, region.member_node_ids)
        assert member_names == {"branch_a_transform", "branch_b_transform"}
        assert region.depth == 1
        assert graph.get_max_bound_region_depth() == 1


class TestWellNestedness:
    def test_partial_overlap_rejected_via_settings(self) -> None:
        # The real-world case: a genuinely crossing pair of bound regions,
        # authorable through settings alone (review round 1, F1).
        with pytest.raises(GraphValidationError, match="partially overlap"):
            _build_settings_driven_partial_overlap()

    def test_partial_overlap_rejected(self) -> None:
        # The direct predicate pin over the raw add_edge/GroupBinding surface.
        with pytest.raises(GraphValidationError, match="partially overlap"):
            _build_partially_overlapping_regions()


class TestDepthCap:
    def test_depth_beyond_cap_rejected(self) -> None:
        # Two nested bound regions with max_bound_region_depth=1.
        with pytest.raises(GraphValidationError, match="nesting depth"):
            _build_nested_fork_in_fork(max_bound_region_depth=1)

    def test_depth_within_cap_builds_and_derives_bound(self) -> None:
        graph = _build_nested_fork_in_fork(max_bound_region_depth=5)
        assert max(r.depth for r in graph.get_bound_regions()) == 2
        assert graph.get_max_bound_region_depth() == 2
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 2
