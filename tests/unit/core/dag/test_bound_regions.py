"""Bound-region computation: membership, well-nestedness, depth cap, fixpoint bound."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import FrameKind, NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID, SinkName
from elspeth.core.config import CoalesceSettings, GateSettings, QueueSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.bound_regions import compute_bound_regions, derive_escalation_fixpoint_bound, validate_sese_regions
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


def _build_fork_coalesce_with_in_region_sink() -> ExecutionGraph:
    """Fully-bound, roster-equal fork (rule 2 is satisfied) whose 'path_a'
    branch carries an INNER conditional gate that can route straight to a
    sink instead of continuing to the coalesce. Rule 2 never sees this: it
    only checks that branch ALIASES resolve to one closer, not what an
    inner gate inside that branch's own chain does. Rule 4's forward walk
    is the only thing that catches it.
    """
    source = _BoundRegionMockSource()
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
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
        sinks={"out": _BoundRegionMockSink("out"), "leak": _BoundRegionMockSink("leak")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_gate",
                input="source_out",
                condition="'all'",
                routes={"all": "fork"},
                fork_to=["path_a", "path_b"],
            ),
            GateSettings(
                name="screen",
                input="path_a",
                condition="True",
                routes={"true": "path_a_out", "false": "leak"},
            ),
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


def _build_fork_coalesce_with_branch_on_error_sink() -> ExecutionGraph:
    """PINNED DECISION 1 control: a branch transform's on_error routes to an
    OUTSIDE sink (DIVERT mode). Rule 4's walks are success-path-only, so this
    must build — the loss fixtures' shape and the settlement system's input.
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
                    on_error="errors",
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
        sinks={"out": _BoundRegionMockSink("out"), "errors": _BoundRegionMockSink("errors")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_gate",
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


def _build_coalesce_with_external_branch_feed() -> None:
    """Direct predicate pin over the raw add_edge/GroupBinding surface (same
    escape hatch Task 5 used for the hardest well-nestedness case — see
    `_build_partially_overlapping_regions`).

    A settings-driven version cannot isolate this violation: any real branch
    the builder accepts must itself terminate at a sink or a real consumer
    (the namespace check rejects a truly dangling connection), and either
    choice trips rule 4's FORWARD checks first, never leaving room to reach
    the backward walk in isolation. The raw surface bypasses the builder's
    connection-namespace bookkeeping entirely, which is exactly what isolates
    the backward-walk predicate: a fork whose 'path_a' branch is declared in
    the roster but has NO outgoing edge at all, and whose closer's OTHER
    inbound edge comes from a wholly unrelated chain that never traverses
    the fork gate — the coalesce-side counterpart of the row_union
    chain-descent guard (`_trace_branch_endpoints`, builder.py, row_union
    only). Rule 2 (roster-alias equality) cannot see this: it never asks
    where a branch's data actually flows from.
    """
    graph = ExecutionGraph()
    for node_id, node_type, plugin_name in [
        ("opener", NodeType.GATE, "fork_gate"),
        ("branch_b_transform", NodeType.TRANSFORM, "branch_b_transform"),
        ("external_source", NodeType.SOURCE, "external_source"),
        ("external_transform", NodeType.TRANSFORM, "external_transform"),
        ("closer", NodeType.COALESCE, "coalesce"),
        ("out", NodeType.SINK, "json"),
    ]:
        graph.add_node(node_id, node_type=node_type, plugin_name=plugin_name)
    graph.add_edge("opener", "branch_b_transform", label="path_b", mode=RoutingMode.MOVE)
    graph.add_edge("branch_b_transform", "closer", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("external_source", "external_transform", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("external_transform", "closer", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("closer", "out", label="continue", mode=RoutingMode.MOVE)

    binding = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("opener"),
        opener_name="fork_gate",
        closer_node_id=NodeID("closer"),
        closer_name="coalesce",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("path_a", "path_b"),
    )
    registry = GroupBindingRegistry(bindings=(binding,))
    regions = compute_bound_regions(graph, registry, max_depth=5)
    validate_sese_regions(graph, regions)


def _build_mixed_closure_sink_plus_bound_sibling() -> ExecutionGraph:
    """Rule-2-before-rule-4 ordering regression: one branch goes direct to a
    sink (unbound), its sibling closes at a coalesce (bound) — MIXED closure.
    Rule 2 (Task 6, builder.py, runs before bound-region computation even
    starts) must reject this with its own message; rule 4 must never get a
    chance to run at all for this gate.
    """
    source = _BoundRegionMockSource()
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_c = _BoundRegionTransform(name="branch_c_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
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
            WiredTransform(
                plugin=branch_c,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_c_transform",
                    plugin=branch_c.name,
                    input="path_c",
                    on_success="path_c_out",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"leak": _BoundRegionMockSink("leak"), "out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_gate",
                input="source_out",
                condition="'all'",
                routes={"all": "fork"},
                fork_to=["leak", "path_b", "path_c"],
            )
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="coalesce",
                branches={"path_b": "path_b_out", "path_c": "path_c_out"},
                policy="require_all",
                merge="union",
                on_success="out",
            )
        ],
    )


def _build_natural_sink_via_intermediate_gate() -> ExecutionGraph:
    """Review F1, manifestation 1 (`probe_natural_sink.py`): the opener's OWN
    non-fork route ('false': side_conn) feeds an intermediate non-fork gate
    that re-enters the region (via a queue feeding the coalesce) before
    reaching a real sink. The label-only anchor missed this because the
    edge `fork_rows -> side_screen` carries label='false', not a roster
    branch name — `side_screen` was never walked at all, so its own leak
    route was invisible.
    """
    source = _BoundRegionMockSource()
    queued_leg = _BoundRegionTransform(name="queued_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    other_leg = _BoundRegionTransform(name="other_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="fork_input", options={})},
        transforms=[
            WiredTransform(
                plugin=queued_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="queued_leg",
                    plugin=queued_leg.name,
                    input="queued_path",
                    on_success="inbound",
                    on_error="discard",
                    options={},
                ),
            ),
            WiredTransform(
                plugin=other_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="other_leg",
                    plugin=other_leg.name,
                    input="other_path",
                    on_success="other_done",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out"), "leak": _BoundRegionMockSink("leak")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_rows",
                input="fork_input",
                condition="True",
                routes={"true": "fork", "false": "side_conn"},
                fork_to=["queued_path", "other_path"],
            ),
            GateSettings(
                name="side_screen",
                input="side_conn",
                condition="True",
                routes={"true": "inbound", "false": "leak"},
            ),
        ],
        queues={"inbound": QueueSettings()},
        coalesce_settings=[
            CoalesceSettings(
                name="merged",
                branches={"queued_path": "inbound", "other_path": "other_done"},
                policy="require_all",
                merge="union",
                on_success="out",
            )
        ],
    )


def _build_label_hole_bypass() -> ExecutionGraph:
    """Review F1, manifestation 2 (`probe_label_hole.py`): the fork gate has
    an EXTRA route ('special': path_a) targeting its OWN fork branch under a
    different label. `builder.py`'s MATCH-PRODUCERS-TO-CONSUMERS pass draws
    that edge with the route label 'special', not the branch name 'path_a',
    so `edge.label in member_roster` alone dropped it — the whole 'path_a'
    branch (including its own in-region leak) was invisible to the walk.
    Same fork/coalesce shape as `_build_fork_coalesce_with_in_region_sink`.
    """
    source = _BoundRegionMockSource()
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="fork_input", options={})},
        transforms=[
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
        sinks={"out": _BoundRegionMockSink("out"), "output": _BoundRegionMockSink("output"), "leak": _BoundRegionMockSink("leak")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_gate",
                input="fork_input",
                condition="'fork_route'",
                routes={"fork_route": "fork", "other_route": "output", "special": "path_a"},
                fork_to=["path_a", "path_b"],
            ),
            GateSettings(
                name="screen",
                input="path_a",
                condition="True",
                routes={"true": "path_a_out", "false": "leak"},
            ),
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


def _build_queue_backdoor() -> ExecutionGraph:
    """Review F2 (`probe_queue_backdoor.py`, maintainer-ruled 2026-08-23): the
    opener's own non-fork route ('false': side_leg_in) feeds a transform
    that publishes DIRECTLY to the SAME queue connection ('inbound') the
    'queued_path' branch legitimately feeds. The side-routed token carries
    no FORK frame — it never took a declared branch — yet arrives at the
    queue that satisfies 'queued_path' for the coalesce: the exact E1
    residual-risk shape (a queue inside a bound region adopting a barrier
    and settling silently) the no-queue-exemption ruling exists to close.
    """
    source = _BoundRegionMockSource()
    queued_leg = _BoundRegionTransform(name="queued_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    other_leg = _BoundRegionTransform(name="other_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    side_leg = _BoundRegionTransform(name="side_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="fork_input", options={})},
        transforms=[
            WiredTransform(
                plugin=queued_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="queued_leg",
                    plugin=queued_leg.name,
                    input="queued_path",
                    on_success="inbound",
                    on_error="discard",
                    options={},
                ),
            ),
            WiredTransform(
                plugin=other_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="other_leg",
                    plugin=other_leg.name,
                    input="other_path",
                    on_success="other_done",
                    on_error="discard",
                    options={},
                ),
            ),
            WiredTransform(
                plugin=side_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="side_leg",
                    plugin=side_leg.name,
                    input="side_leg_in",
                    on_success="inbound",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_rows",
                input="fork_input",
                condition="True",
                routes={"true": "fork", "false": "side_leg_in"},
                fork_to=["queued_path", "other_path"],
            ),
        ],
        queues={"inbound": QueueSettings()},
        coalesce_settings=[
            CoalesceSettings(
                name="merged",
                branches={"queued_path": "inbound", "other_path": "other_done"},
                policy="require_all",
                merge="union",
                on_success="out",
            )
        ],
    )


def _build_crosslimb_no_path_violation() -> None:
    """Review F4 (`probe_crosslimb.py`): the no-path-to-closer limb has no
    coverage anywhere in the suite despite being a live, parity-adjudicated
    raise site (`config/cicd/runtime_rejection_parity.yaml` key
    `6bbf64577ab07fbe`). Direct predicate pin over the raw
    add_edge/GroupBinding surface (same escape hatch as
    `_build_coalesce_with_external_branch_feed`): the connection-namespace
    check rejects most settings-authored dead ends before rule 4 ever runs,
    so a fork branch whose target has NO outgoing success edge at all is
    only reachable this way. 'b_leg' is forward-reachable from the opener
    (branch 'b') but has no path back to the closer at all.
    """
    graph = ExecutionGraph()
    for node_id, node_type, plugin_name in [
        ("opener", NodeType.GATE, "fork_gate"),
        ("a_leg", NodeType.TRANSFORM, "a_leg_transform"),
        ("b_leg", NodeType.TRANSFORM, "b_leg_transform"),
        ("closer", NodeType.COALESCE, "coalesce"),
        ("out", NodeType.SINK, "json"),
    ]:
        graph.add_node(node_id, node_type=node_type, plugin_name=plugin_name)
    graph.add_edge("opener", "a_leg", label="a", mode=RoutingMode.MOVE)
    graph.add_edge("opener", "b_leg", label="b", mode=RoutingMode.MOVE)
    graph.add_edge("a_leg", "closer", label="continue", mode=RoutingMode.MOVE)
    # 'b_leg' has NO outgoing edge at all — a dead end unreachable from any
    # settings-authored config (the connection-namespace check would reject
    # a dangling on_success first), reachable only on this raw surface.
    graph.add_edge("closer", "out", label="continue", mode=RoutingMode.MOVE)

    binding = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("opener"),
        opener_name="fork_gate",
        closer_node_id=NodeID("closer"),
        closer_name="coalesce",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        on_group_failure=None,
        member_roster=("a", "b"),
    )
    registry = GroupBindingRegistry(bindings=(binding,))
    regions = compute_bound_regions(graph, registry, max_depth=5)
    validate_sese_regions(graph, regions)


class TestSESEWalk:
    """Spec §7 rule 4 (bidirectional SESE, success-path edges only).

    Forward: every non-DIVERT path from a FORK binding's own branch entries
    reaches the closer before any sink. Backward: every non-DIVERT edge into
    a region member or the closer originates inside the region. DIVERT
    edges (on_error, __quarantine__, __failsink__) are excluded from both
    walks (pinned decision 1, RC-7): every fork-coalesce loss fixture
    terminates a branch in-region via on_error/discard, and that is the
    settlement system's input, not a leak this rule may reject.
    """

    def test_sink_inside_bound_region_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match=r"reaches sink .* before the region's closer"):
            _build_fork_coalesce_with_in_region_sink()

    def test_on_error_divert_inside_region_stays_legal(self) -> None:
        # F6 (review): pin something OBSERVABLE, not just "did not raise" —
        # the 'errors' sink must be genuinely absent from the region while
        # both branch transforms are genuinely present, or this test cannot
        # distinguish "DIVERT correctly excluded" from "region computed
        # wrongly but happened not to raise".
        graph = _build_fork_coalesce_with_branch_on_error_sink()
        regions = graph.get_bound_regions()
        assert len(regions) == 1
        member_names = _plugin_names(graph, regions[0].member_node_ids)
        assert member_names == {"branch_a_transform", "branch_b_transform"}
        errors_sink_id = graph.get_sink_id_map()[SinkName("errors")]
        assert errors_sink_id not in regions[0].member_node_ids

    def test_external_entry_into_region_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match="originates outside the bound region"):
            _build_coalesce_with_external_branch_feed()

    def test_mixed_closure_fires_before_sese_walk(self) -> None:
        # Rule 2 (mixed closure) must pre-empt rule 4 for this shape: the
        # gate is rejected before compute_bound_regions/validate_sese_regions
        # ever runs, so the error is rule 2's, never rule 4's sink-inside
        # message.
        with pytest.raises(GraphValidationError, match="mixed closure") as exc_info:
            _build_mixed_closure_sink_plus_bound_sibling()
        assert "reaches sink" not in str(exc_info.value)

    def test_natural_sink_via_intermediate_gate_rejected(self) -> None:
        # Review F1, manifestation 1: label-only anchor missed a non-roster
        # opener route re-entering the region through an intermediate gate.
        with pytest.raises(GraphValidationError, match=r"reaches sink .* before the region's closer"):
            _build_natural_sink_via_intermediate_gate()

    def test_label_hole_bypass_rejected(self) -> None:
        # Review F1, manifestation 2: a route targeting the gate's OWN fork
        # branch under a different label drew an edge the roster-label
        # filter alone could not see.
        with pytest.raises(GraphValidationError, match=r"reaches sink .* before the region's closer"):
            _build_label_hole_bypass()

    def test_queue_backdoor_rejected(self) -> None:
        # Review F2 (maintainer ruled, 2026-08-23): an unframed token via the
        # opener's own non-fork route must not be allowed to enter the
        # region through a queue a real branch also feeds.
        with pytest.raises(GraphValidationError, match="not one of the fork's declared branches"):
            _build_queue_backdoor()

    def test_no_path_to_closer_limb_rejected(self) -> None:
        # Review F4: the no-path-to-closer limb had no test anywhere despite
        # being a live, parity-adjudicated raise site.
        with pytest.raises(GraphValidationError, match="has no success path to"):
            _build_crosslimb_no_path_violation()


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
