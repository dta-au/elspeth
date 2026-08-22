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


def _build_partially_overlapping_regions() -> None:
    """Two bound regions whose spans cross: open1 < open2 < close1 < close2.

    Measured, not assumed: a settings-driven attempt at this shape (an outer
    fork/coalesce with an inner fork nested in one branch, where the inner
    gate's OTHER branch is wired as a direct branch of the OUTER coalesce —
    the natural way to make the inner region's closer trail the outer
    coalesce) still builds successfully and resolves to properly NESTED
    regions, never a crossing one. Two structural reasons, both load-bearing:
    ``build_group_binding_registry``'s "first bound branch wins" interim
    filters each gate's ``member_roster`` down to the branches that resolve
    to ONE closer, so a branch feeding the "wrong" closer is silently
    dropped from that gate's binding rather than creating an ambiguous
    frame source; and region membership is computed by graph reachability
    (forward/backward reach between opener and closer), which the
    registry's roster filtering does not gate — the inner region's span
    still lands entirely inside the outer region's reachability-derived
    members, because the "crossing" edge (the inner gate's direct branch
    into the outer coalesce) does not change which NODES lie on the path
    between either opener/closer pair, only which edges do. So a genuine
    partial-overlap graph does not appear to be constructible through the
    settings surface at all — well short of "the builder's existing guards
    fire first", the topology-normalizing effect of branch-roster
    resolution keeps every settings-driven fork/coalesce shape well-nested
    by construction. This helper instead exercises the region check
    directly against the `add_edge`/`GroupBinding` surface — the same
    surface `build_group_binding_registry` and the builder's node/edge
    construction ultimately produce — so the well-nestedness guard itself
    stays covered even though no known config can reach it.
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
    def test_partial_overlap_rejected(self) -> None:
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
