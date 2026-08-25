"""Bound-region computation: membership, well-nestedness, depth cap, fixpoint bound."""

from __future__ import annotations

from typing import Any, ClassVar, get_args

import pytest

from elspeth.contracts.enums import FrameKind, NodeType, RoutingMode
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import NodeID, SinkName
from elspeth.core.config import (
    AggregationSettings,
    CoalesceSettings,
    CollectorSettings,
    GateSettings,
    QueueSettings,
    RowUnionSettings,
    ScopeSettings,
    SourceSettings,
    TransformSettings,
)
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.bound_regions import (
    BoundRegion,
    compute_bound_regions,
    derive_escalation_fixpoint_bound,
    validate_openers_bound_in_region,
    validate_sese_regions,
)
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
    creates_tokens = False
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


class _BoundRegionMultiRowTransform:
    """A creates_tokens=True stub — a scope opener candidate (spec §7 rule 5)."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = "output"
    creates_tokens = True
    is_batch_aware = False
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


class _BoundRegionCollectorPlugin:
    """A batch-aware stub — the collector plugin closing a declared scope."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = None
    creates_tokens = False
    is_batch_aware = True
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


class _BoundRegionAggregationTransform:
    """A stub aggregation-node plugin (spec §7 rule 6, ruling 25)."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = None
    creates_tokens = False
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


def _build_same_gate_route_override_clean_chain(*, override: bool) -> ExecutionGraph:
    """Review N1 (BLOCKING, final re-review of `ec9109193`): a same-gate
    route override on a CLEAN branch chain — no sink inside the region, so
    nothing else fires. The A2 fix (drawing a fork-branch connection's edge
    unconditionally under the branch name) initially DELETED the
    route-labelled edge for this shape instead of drawing both: the route
    ('special' -> 'path_a', the SAME connection 'path_a' branch already
    produces) stays live in the route-resolution map at runtime regardless
    of what the graph's edges say, so dropping its edge removed rule 4's
    only witness that an unframed route re-enters this branch's connection
    — this shape BUILT at `ec9109193` where it was correctly REJECTED at
    `5a52184a5`. Fixed by drawing both edges (the branch-name edge AND the
    route-labelled edge) as two separate facts about the graph, restoring
    the F2 limb's witness with no `bound_regions.py` change at all.
    `override=False` is the CONTROL (must build); `override=True` must be
    rejected by the F2 limb.
    """
    source = _BoundRegionMockSource()
    ta = _BoundRegionTransform(name="ta", output_schema_config=SchemaConfig(mode="observed", fields=None))
    tb = _BoundRegionTransform(name="tb", output_schema_config=SchemaConfig(mode="observed", fields=None))

    routes = {"go": "fork"}
    if override:
        routes["special"] = "path_a"

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=ta,  # type: ignore[arg-type]
                settings=TransformSettings(name="ta", plugin=ta.name, input="path_a", on_success="a_out", on_error="discard", options={}),
            ),
            WiredTransform(
                plugin=tb,  # type: ignore[arg-type]
                settings=TransformSettings(name="tb", plugin=tb.name, input="path_b", on_success="b_out", on_error="discard", options={}),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(name="fork_gate", input="source_out", condition="'go'", routes=routes, fork_to=["path_a", "path_b"]),
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="coalesce", branches={"path_a": "a_out", "path_b": "b_out"}, policy="require_all", merge="union", on_success="out"
            ),
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


def _build_exclusion_attack(*, evil_route: bool) -> ExecutionGraph:
    """Review R1 (BLOCKING, re-review of `5a52184a5`): the fork's OWN branch
    connection name is made to equal the queue name it feeds ('inbound'), so
    the roster-labelled edge itself lands on the queue; a second, ordinary,
    non-'continue' route ('evil') targets that SAME queue. The broad form of
    the `legitimate_targets` exclusion skipped 'evil' on target-coincidence
    alone (its target is ALSO reached by the roster edge), never inspecting
    the label — walking straight through into the exact E1 unframed-entry
    hazard F2 exists to close. `evil_route=False` is the CONTROL (must
    build); `evil_route=True` is the ATTACK (must be rejected by the F2
    limb, not silently swallowed by the exclusion).
    """
    source = _BoundRegionMockSource()
    other_leg = _BoundRegionTransform(name="other_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    qleg = _BoundRegionTransform(name="qleg", output_schema_config=SchemaConfig(mode="observed", fields=None))

    routes = {"go": "fork"}
    if evil_route:
        routes["evil"] = "inbound"

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="fork_input", options={})},
        transforms=[
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
                plugin=qleg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="qleg",
                    plugin=qleg.name,
                    input="inbound",
                    on_success="qout",
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
                condition="'go'",
                routes=routes,
                fork_to=["inbound", "other_path"],
            ),
        ],
        queues={"inbound": QueueSettings()},
        coalesce_settings=[
            CoalesceSettings(
                name="merged",
                branches={"inbound": "qout", "other_path": "other_done"},
                policy="require_all",
                merge="union",
                on_success="out",
            )
        ],
    )


def _build_onehop_queue_backdoor() -> ExecutionGraph:
    """Review A1: the solution architect's one-hop discriminator. Same hazard
    as `_build_queue_backdoor` (opener's non-fork route re-entering the
    region through the queue a real branch also feeds), one hop shorter —
    the opener's 'false' route feeds the branch's queue DIRECTLY, with no
    intermediate transform. Option A (widen membership only) does NOT close
    this variant (`inbound` stays branch-reachable via `queued_leg`, so the
    backward walk still authorizes the edge); only Option B's F2 limb does,
    by rejecting the non-branch route before membership is even consulted.
    This is the cheapest witness against a future membership-level refactor
    silently reopening the hole.
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
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="fork_rows",
                input="fork_input",
                condition="True",
                routes={"true": "fork", "false": "inbound"},
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


def _build_nested_opener_queue_backdoor(*, leak: bool) -> ExecutionGraph:
    """Review A5.4: FORK-inside-FORK with the non-branch-route hazard on the
    INNER opener, not the outer one. `outer_gate` forks [left, right];
    `left` enters `inner_gate`, which forks [la_path, lb_path]; `inner_gate`
    ALSO carries a non-branch route ('leak') that feeds the SAME queue
    ('la_queue') `la_path`'s own transform legitimately feeds — the exact
    F2 hazard shape, one nesting level down. The F2 limb is guarded
    `if binding.kind is FrameKind.FORK` and checks only its OWN opener's
    immediate out-edges, so nothing in its structure guarantees it fires
    for an INNER binding rather than only the outer one; this is the first
    test that actually exercises a nested opener. `leak=False` is the
    CONTROL (must build); `leak=True` must be rejected pinned to the INNER
    region ('inner_coalesce'), proving the limb generalizes rather than
    only ever having been exercised at nesting depth 1.
    """
    source = _BoundRegionMockSource()
    right_transform = _BoundRegionTransform(name="right_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
    la_leg = _BoundRegionTransform(name="la_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    lb_leg = _BoundRegionTransform(name="lb_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    leak_leg = _BoundRegionTransform(name="leak_leg", output_schema_config=SchemaConfig(mode="observed", fields=None))

    inner_routes = {"go": "fork"}
    if leak:
        inner_routes["leak"] = "inner_leak_in"

    transforms = [
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
        WiredTransform(
            plugin=la_leg,  # type: ignore[arg-type]
            settings=TransformSettings(
                name="la_leg", plugin=la_leg.name, input="la_path", on_success="la_queue", on_error="discard", options={}
            ),
        ),
        WiredTransform(
            plugin=lb_leg,  # type: ignore[arg-type]
            settings=TransformSettings(
                name="lb_leg", plugin=lb_leg.name, input="lb_path", on_success="lb_out", on_error="discard", options={}
            ),
        ),
    ]
    if leak:
        transforms.append(
            WiredTransform(
                plugin=leak_leg,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="leak_leg",
                    plugin=leak_leg.name,
                    input="inner_leak_in",
                    on_success="la_queue",
                    on_error="discard",
                    options={},
                ),
            )
        )

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=transforms,
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(name="outer_gate", input="source_out", condition="'all'", routes={"all": "fork"}, fork_to=["left", "right"]),
            GateSettings(name="inner_gate", input="left", condition="'go'", routes=inner_routes, fork_to=["la_path", "lb_path"]),
        ],
        queues={"la_queue": QueueSettings()},
        coalesce_settings=[
            CoalesceSettings(
                name="inner_coalesce", branches={"la_path": "la_queue", "lb_path": "lb_out"}, policy="require_all", merge="union"
            ),
            CoalesceSettings(
                name="outer_coalesce",
                branches={"left": "inner_coalesce", "right": "right_out"},
                policy="require_all",
                merge="union",
                on_success="out",
            ),
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

    def test_exclusion_attack_control_builds(self) -> None:
        # Review R1: the CONTROL half of the discriminator — no evil route,
        # must build clean. Proves the narrowed exclusion still suppresses
        # the genuine builder bookkeeping artifact (the "continue"
        # fallthrough) rather than breaking this legitimate topology.
        _build_exclusion_attack(evil_route=False)

    def test_exclusion_attack_rejected(self) -> None:
        # Review R1 (BLOCKING): the broad `legitimate_targets` exclusion
        # skipped ANY non-roster edge whose target coincided with a roster
        # edge's target, regardless of label — this is the only witness
        # that discriminates that broad form from the narrowed
        # `label == "continue"` form. A fork branch's own connection name
        # is made to equal the queue it feeds, so the roster edge lands on
        # the queue too; a second, ordinary 'evil' route to that same queue
        # must still be rejected by the F2 limb, not silently excluded.
        with pytest.raises(GraphValidationError, match="not one of the fork's declared branches"):
            _build_exclusion_attack(evil_route=True)

    def test_nested_opener_queue_backdoor_control_builds(self) -> None:
        # Review A5.4 CONTROL: the nested fork-in-fork topology with no leak
        # route must build clean.
        _build_nested_opener_queue_backdoor(leak=False)

    def test_nested_opener_queue_backdoor_rejected(self) -> None:
        # Review A5.4: the F2 limb is guarded per-binding
        # (`if binding.kind is FrameKind.FORK`) and checks only its own
        # opener's immediate out-edges; nothing in its structure guarantees
        # it fires for a NESTED (inner) opener rather than only ever having
        # been exercised at nesting depth 1. This pins that it does.
        with pytest.raises(GraphValidationError, match="not one of the fork's declared branches") as exc_info:
            _build_nested_opener_queue_backdoor(leak=True)
        assert "inner_coalesce" in str(exc_info.value)

    def test_same_gate_route_override_clean_chain_control_builds(self) -> None:
        # Review N1 CONTROL: no route override, must build clean.
        _build_same_gate_route_override_clean_chain(override=False)

    def test_same_gate_route_override_clean_chain_rejected(self) -> None:
        # Review N1 (BLOCKING): the A2 builder fix (drawing a fork-branch
        # edge unconditionally under the branch name) initially DELETED the
        # route-labelled edge instead of drawing both, silently re-opening
        # an unframed-entry hazard on a CLEAN branch chain (no sink to
        # trigger any other limb) that `5a52184a5` correctly rejected. This
        # is the only witness that discriminates "edge deleted" from "edge
        # kept alongside the branch-name edge".
        with pytest.raises(GraphValidationError, match="not one of the fork's declared branches"):
            _build_same_gate_route_override_clean_chain(override=True)

    def test_onehop_queue_backdoor_rejected(self) -> None:
        # Review A1: the solution architect's one-hop discriminator — the
        # opener's non-fork route feeds the branch's queue DIRECTLY, no
        # intermediate transform. Option A (widen membership only) does NOT
        # close this variant; only Option B's F2 limb does. The cheapest
        # witness against a future membership-level refactor reopening the
        # hole.
        with pytest.raises(GraphValidationError, match="not one of the fork's declared branches"):
            _build_onehop_queue_backdoor()


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


def _build_fork_coalesce_with_undeclared_expand_in_branch() -> ExecutionGraph:
    """source -> gate 'fork_to' [path_a, path_b] -> coalesce; path_a IS a
    creates_tokens=True transform with no scope declared. Legal today
    (binding-survives-expansion posture, token_traversal.py:254-262) — a
    GraphValidationError under spec §7 rule 5 (ruling 28): a shape change
    inside a bound region must itself be a declared group.
    """
    source = _BoundRegionMockSource()
    branch_a = _BoundRegionMultiRowTransform(name="branch_a_expand", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=branch_a,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_a_expand",
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


def _build_fork_coalesce_with_scoped_expand_in_branch() -> ExecutionGraph:
    """Same shape as the undeclared case, but branch_a's expand is a
    declared scope whose collector closes BEFORE the coalesce
    (batch-in-fork-line — legal under ruling 28): source -> gate
    'fork_to' [path_a, path_b] -> coalesce; path_a is
    branch_a_expand (opener) -> page_stitcher (collector, closer) ->
    feeds the coalesce's 'path_a' branch connection.

    The scope's binding nests inside the outer FORK->coalesce region
    (depth 2), so an enclosing bound group (the coalesce) exists at depth 1
    and a failed inner group escalates to it structurally (ADR-042).
    """
    source = _BoundRegionMockSource()
    branch_a = _BoundRegionMultiRowTransform(name="branch_a_expand", output_schema_config=SchemaConfig(mode="observed", fields=None))
    stitcher = _BoundRegionCollectorPlugin(name="page_stitcher", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=branch_a,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_a_expand",
                    plugin=branch_a.name,
                    input="path_a",
                    on_success="pages",
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
        collectors={
            "page_stitcher": (
                stitcher,  # type: ignore[dict-item]
                CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="path_a_out"),
            )
        },
        scope_settings=[
            ScopeSettings(
                name="branch_a_pages",
                opener="branch_a_expand",
                closer="page_stitcher",
                policy="require_all",
            )
        ],
    )


def _build_top_level_scope() -> ExecutionGraph:
    """A standalone scope (opener -> collector), no fork/coalesce anywhere.

    No enclosing bound region exists, so this scope's own binding is the
    ONLY bound group in the graph — depth 1, outermost: a group failure
    here is terminal (nowhere to escalate to, ADR-042 structural handling).
    """
    source = _BoundRegionMockSource()
    opener = _BoundRegionMultiRowTransform(name="expand", output_schema_config=SchemaConfig(mode="observed", fields=None))
    collector = _BoundRegionCollectorPlugin(name="stitcher", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=opener,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="expand",
                    plugin=opener.name,
                    input="source_out",
                    on_success="pages",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        collectors={
            "stitcher": (
                collector,  # type: ignore[dict-item]
                CollectorSettings(name="stitcher", plugin="stitch_pages", input="pages", on_success="out"),
            )
        },
        scope_settings=[
            ScopeSettings(
                name="doc_pages",
                opener="expand",
                closer="stitcher",
                policy="require_all",
            )
        ],
    )


def _build_scope_closing_outside_region() -> None:
    """Direct predicate pin over the raw ``BoundRegion``/``GroupBinding``
    surface (same escape hatch Task 7 used for its backward-walk isolation
    case, ``_build_coalesce_with_external_branch_feed``).

    This shape CANNOT OCCUR through ``compute_bound_regions`` at all, for
    ANY caller — not merely "hard to author via settings". Whenever a
    scope's opener is genuinely a member of a FORK region (this rule's own
    precondition), the EXPAND region's own SPAN necessarily shares that
    opener node with the FORK region's span, so ``compute_bound_regions``
    returning without raising rule 3's partial-overlap error forces the
    ONLY surviving well-nestedness arm — the EXPAND span nested entirely
    inside the FORK's members — which puts the EXPAND's own closer inside
    the FORK region's members too (see
    ``config/cicd/runtime_rejection_parity.yaml``, key
    ``3956713c3d4e81ba``, disposition ``structural``, for the full proof).
    So ``validate_openers_bound_in_region``'s "closes outside" limb is a
    defensive invariant that ``compute_bound_regions`` already guarantees
    on every real build, and hand-building the region set below is the
    ONLY way to exercise the predicate directly — it pins that the check
    is CORRECT if it ever ran, not that it protects a reachable build.
    """
    graph = ExecutionGraph()
    for node_id, node_type, plugin_name in [
        ("fork_open", NodeType.GATE, "fork_gate"),
        ("branch_a", NodeType.TRANSFORM, "branch_a_expand"),
        ("branch_b", NodeType.TRANSFORM, "branch_b_transform"),
        ("fork_close", NodeType.COALESCE, "coalesce"),
        ("scope_close", NodeType.COLLECTOR, "page_stitcher"),
        ("out", NodeType.SINK, "json"),
    ]:
        graph.add_node(node_id, node_type=node_type, plugin_name=plugin_name)
    graph.add_edge("fork_open", "branch_a", label="path_a", mode=RoutingMode.MOVE)
    graph.add_edge("fork_open", "branch_b", label="path_b", mode=RoutingMode.MOVE)
    graph.add_edge("branch_a", "fork_close", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("branch_b", "fork_close", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("fork_close", "out", label="continue", mode=RoutingMode.MOVE)

    fork_binding = GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID("fork_open"),
        opener_name="fork_gate",
        closer_node_id=NodeID("fork_close"),
        closer_name="coalesce",
        closer_kind=CloserKind.COALESCE,
        policy="require_all",
        member_roster=("path_a", "path_b"),
    )
    scope_binding = GroupBinding(
        kind=FrameKind.EXPAND,
        opener_node_id=NodeID("branch_a"),
        opener_name="branch_a_expand",
        closer_node_id=NodeID("scope_close"),
        closer_name="page_stitcher",
        closer_kind=CloserKind.COLLECTOR,
        policy="require_all",
        member_roster=(),
    )
    registry = GroupBindingRegistry(bindings=(fork_binding, scope_binding))
    fork_region = BoundRegion(
        binding=fork_binding,
        member_node_ids=frozenset({NodeID("branch_a"), NodeID("branch_b")}),
        depth=1,
    )
    multi_row_node_ids = {NodeID("branch_a"): "branch_a_expand"}
    validate_openers_bound_in_region(graph, (fork_region,), registry, multi_row_node_ids, frozenset())


def _build_plain_expand_pipeline() -> ExecutionGraph:
    """source -> creates_tokens=True transform (no scope) -> sink, no
    fork/coalesce anywhere. Inert expands OUTSIDE bound regions are the
    batch posture (spec §7 rule 5 applies only INSIDE a bound region) —
    untouched.
    """
    source = _BoundRegionMockSource()
    expand = _BoundRegionMultiRowTransform(name="explode", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=expand,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="explode",
                    plugin=expand.name,
                    input="source_out",
                    on_success="out",
                    on_error="discard",
                    options={},
                ),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
    )


class TestRule5OpenersBoundInRegion:
    """Spec §7 rule 5 (ruling 28): every token-creating node inside a bound
    region must be a declared scope opener whose closer is ALSO inside
    that region.
    """

    def test_undeclared_expand_inside_coalesce_branch_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match=r"Multi-row transform .* inside bound region"):
            _build_fork_coalesce_with_undeclared_expand_in_branch()

    def test_declared_scope_closing_in_region_is_legal(self) -> None:
        # This is NOT the corpus's first depth-2 unit-test region set —
        # test_builder_collectors.py::test_expand_outer_fork_inner_bound_regions
        # (Task 5) already pins depth 2 for an EXPAND-outer/FORK-inner
        # nesting. What IS new here is the REVERSE mixed-kind nesting
        # (FORK outer, EXPAND/scope inner — a scope entirely inside one
        # fork branch), which no existing fixture exercised.
        graph = _build_fork_coalesce_with_scoped_expand_in_branch()
        regions = graph.get_bound_regions()
        assert {r.binding.closer_kind for r in regions} == {CloserKind.COALESCE, CloserKind.COLLECTOR}
        assert max(r.depth for r in regions) == 2

    def test_scoped_expand_whose_collector_sits_outside_region_rejected(self) -> None:
        with pytest.raises(GraphValidationError, match="closes outside"):
            _build_scope_closing_outside_region()

    def test_top_level_undeclared_expand_stays_legal(self) -> None:
        graph = _build_plain_expand_pipeline()
        assert graph.get_bound_regions() == ()

    def test_scope_opener_has_no_route_surface_for_the_f2_style_hazard(self) -> None:
        # Addendum A5.5 (task-7-review.md), commissioned as a Task 8
        # checklist obligation: rule 4's F2 limb (validate_sese_regions,
        # bound_regions.py) is guarded `if binding.kind is FrameKind.FORK`
        # and rejects a FORK opener's own non-branch route that re-enters
        # the region. The analogous hazard for an EXPAND (scope) opener
        # would be a non-on_success route on the opener transform that
        # re-enters the region unframed. That hazard is UNAUTHORABLE, not
        # merely untested: TransformSettings (the only settings class that
        # can name a scope's `opener:`, per ScopeSettings.opener) has no
        # `routes`/`fork_to` field at all — a scope opener is a plain
        # transform with exactly one on_success connection and one on_error
        # DIVERT connection (already excluded from every walk). There is no
        # second edge for an unframed token to travel, so no limb is needed
        # here; the guard is structural, at the config-schema level.
        assert "routes" not in TransformSettings.model_fields
        assert "fork_to" not in TransformSettings.model_fields

        # Truth pin on top of the declaration pin above (the config schema
        # ruling out routes:/fork_to: proves nothing about edges the BUILDER
        # itself draws — Task 7's F2/R1/A2/N1 sequence was entirely about
        # exactly that class of builder-drawn edge no config field predicts).
        # Build a REAL scope opener and assert its actual graph node has
        # exactly one non-DIVERT outgoing edge.
        graph = _build_fork_coalesce_with_scoped_expand_in_branch()
        registry = graph.get_group_bindings()
        expand_binding = next(b for b in registry.bindings if b.kind is FrameKind.EXPAND)
        opener_edges = [edge for edge in graph.get_outgoing_edges(expand_binding.opener_node_id) if edge.mode is not RoutingMode.DIVERT]
        assert len(opener_edges) == 1

    def test_scoped_expand_nested_inside_a_fork_inside_a_fork(self) -> None:
        # A5.4-style generalization check (originally commissioned against
        # rule 4's F2 limb; here pinned against rule 5): a scope nested TWO
        # bound-region levels deep (FORK -> FORK -> EXPAND) must still be
        # recognized as a legal declared opener/closer pair by
        # validate_openers_bound_in_region, which loops over EVERY region
        # independently rather than only the outermost one.
        source = _BoundRegionMockSource()
        outer_b = _BoundRegionTransform(name="outer_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
        inner_a = _BoundRegionMultiRowTransform(name="inner_a_expand", output_schema_config=SchemaConfig(mode="observed", fields=None))
        stitcher = _BoundRegionCollectorPlugin(name="page_stitcher", output_schema_config=SchemaConfig(mode="observed", fields=None))
        inner_b = _BoundRegionTransform(name="inner_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

        graph = ExecutionGraph.from_plugin_instances(
            sources={"primary": source},  # type: ignore[arg-type]
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
            transforms=[
                WiredTransform(
                    plugin=outer_b,  # type: ignore[arg-type]
                    settings=TransformSettings(
                        name="outer_b_transform",
                        plugin=outer_b.name,
                        input="outer_b",
                        on_success="outer_b_out",
                        on_error="discard",
                        options={},
                    ),
                ),
                WiredTransform(
                    plugin=inner_a,  # type: ignore[arg-type]
                    settings=TransformSettings(
                        name="inner_a_expand", plugin=inner_a.name, input="inner_a", on_success="pages", on_error="discard", options={}
                    ),
                ),
                WiredTransform(
                    plugin=inner_b,  # type: ignore[arg-type]
                    settings=TransformSettings(
                        name="inner_b_transform",
                        plugin=inner_b.name,
                        input="inner_b",
                        on_success="inner_b_out",
                        on_error="discard",
                        options={},
                    ),
                ),
            ],
            sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
            aggregations={},
            gates=[
                GateSettings(
                    name="outer_gate", input="source_out", condition="'all'", routes={"all": "fork"}, fork_to=["outer_a", "outer_b"]
                ),
                GateSettings(name="inner_gate", input="outer_a", condition="'all'", routes={"all": "fork"}, fork_to=["inner_a", "inner_b"]),
            ],
            coalesce_settings=[
                CoalesceSettings(
                    name="inner_coalesce",
                    branches={"inner_a": "pages_out", "inner_b": "inner_b_out"},
                    policy="require_all",
                    merge="union",
                ),
                CoalesceSettings(
                    name="outer_coalesce",
                    branches={"outer_a": "inner_coalesce", "outer_b": "outer_b_out"},
                    policy="require_all",
                    merge="union",
                    on_success="out",
                ),
            ],
            collectors={
                "page_stitcher": (
                    stitcher,  # type: ignore[dict-item]
                    CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="pages_out"),
                )
            },
            scope_settings=[ScopeSettings(name="inner_a_pages", opener="inner_a_expand", closer="page_stitcher", policy="require_all")],
        )
        regions = graph.get_bound_regions()
        assert {r.binding.closer_kind for r in regions} == {CloserKind.COALESCE, CloserKind.COLLECTOR}
        assert max(r.depth for r in regions) == 3

    def test_creates_tokens_aggregation_inside_region_rejected_by_rule_5_first(self) -> None:
        # Task 8/9 rider (2026-08-23 review finding): ruling 28 is
        # node-kind-agnostic, so the builder's multi_row_node_ids census
        # also covers aggregation plugins with creates_tokens=True (not
        # reachable on any shipped plugin today, but the ruling draws no
        # node-kind exception). Rule 6 (validate_no_aggregations_in_regions)
        # ALSO independently rejects any in-region aggregation regardless of
        # creates_tokens — both rejections are correct for this shape. Rule
        # 5 runs FIRST in the builder (builder.py), so ITS message
        # ("not a declared scope opener") wins the overlap, not rule 6's
        # ("Aggregators are banned"). Pins that ordering, not just that
        # *a* rejection happens.
        source = _BoundRegionMockSource()
        branch_a_agg = _BoundRegionAggregationTransform(
            name="branch_a_agg", output_schema_config=SchemaConfig(mode="observed", fields=None)
        )
        branch_a_agg.creates_tokens = True  # not otherwise reachable — see docstring above
        branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

        def _build() -> ExecutionGraph:
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
                sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
                aggregations={
                    "branch_a_agg": (
                        branch_a_agg,  # type: ignore[dict-item]
                        AggregationSettings(name="branch_a_agg", plugin="agg", input="path_a", on_success="path_a_out", on_error="discard"),
                    )
                },
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

        with pytest.raises(GraphValidationError, match=r"Multi-row transform .* inside bound region") as exc_info:
            _build()
        assert "not a declared scope opener" in str(exc_info.value)
        assert "banned inside all bound regions" not in str(exc_info.value)

    def test_creates_tokens_collector_inside_region_rejected_flat(self) -> None:
        # Task 8/9 rider: a creates_tokens=True COLLECTOR is a closer, not
        # an opener — the widened census still names it in
        # multi_row_node_ids, and it can never be a KEY in
        # registry.by_opener_node() (that index is keyed by OPENER node ids
        # only), so it is rejected flat by the SAME "not a declared scope
        # opener" limb — the correct fail-closed outcome, not a special case.
        source = _BoundRegionMockSource()
        branch_a_expand = _BoundRegionMultiRowTransform(
            name="branch_a_expand", output_schema_config=SchemaConfig(mode="observed", fields=None)
        )
        stitcher = _BoundRegionCollectorPlugin(name="page_stitcher", output_schema_config=SchemaConfig(mode="observed", fields=None))
        stitcher.creates_tokens = True  # not otherwise reachable — see docstring above
        branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

        with pytest.raises(GraphValidationError, match=r"Multi-row transform 'page_stitcher' inside bound region"):
            ExecutionGraph.from_plugin_instances(
                sources={"primary": source},  # type: ignore[arg-type]
                source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
                transforms=[
                    WiredTransform(
                        plugin=branch_a_expand,  # type: ignore[arg-type]
                        settings=TransformSettings(
                            name="branch_a_expand",
                            plugin=branch_a_expand.name,
                            input="path_a",
                            on_success="pages",
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
                sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[arg-type]
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
                collectors={
                    "page_stitcher": (
                        stitcher,  # type: ignore[dict-item]
                        CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="path_a_out"),
                    )
                },
                scope_settings=[
                    ScopeSettings(name="branch_a_pages", opener="branch_a_expand", closer="page_stitcher", policy="require_all")
                ],
            )


def _build_fork_coalesce_with_branch_aggregation(*, output_mode: str) -> ExecutionGraph:
    """fork [path_a, path_b] -> coalesce; path_a IS an aggregation node.

    Both output_mode: transform and output_mode: passthrough must be
    rejected (spec §7 rule 6, ruling 25) — the pre-existing row_union-only
    backward walk in builder.py only rejects output_mode: transform feeding
    a row_union branch; this rule is a flat ban regardless of mode, and
    covers coalesce regions the row_union-only walk never inspected.
    """
    source = _BoundRegionMockSource()
    branch_a_agg = _BoundRegionAggregationTransform(name="branch_a_agg", output_schema_config=SchemaConfig(mode="observed", fields=None))
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
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={
            "branch_a_agg": (
                branch_a_agg,  # type: ignore[dict-item]
                AggregationSettings(
                    name="branch_a_agg",
                    plugin="agg",
                    input="path_a",
                    on_success="path_a_out",
                    on_error="discard",
                    output_mode=output_mode,  # type: ignore[arg-type]
                ),
            )
        },
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


def _build_top_level_aggregation_pipeline() -> ExecutionGraph:
    """source -> aggregation -> sink, no fork/coalesce anywhere.

    Outside any bound region no roster is watching (ADR-020 posture) —
    untouched by rule 6.
    """
    source = _BoundRegionMockSource()
    agg = _BoundRegionAggregationTransform(name="top_agg", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={
            "top_agg": (
                agg,  # type: ignore[dict-item]
                AggregationSettings(name="top_agg", plugin="agg", input="source_out", on_success="out", on_error="discard"),
            )
        },
    )


def _build_aggregation_after_coalesce_release() -> ExecutionGraph:
    """fork [path_a, path_b] -> coalesce -> aggregation -> sink.

    The aggregation sits strictly AFTER the coalesce's release (consuming
    the coalesce's own self-named output connection) — legal under rule 6,
    same as the top-level case: the region's roster already released before
    the aggregation ever sees a row.
    """
    source = _BoundRegionMockSource()
    branch_a = _BoundRegionTransform(name="branch_a_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))
    agg = _BoundRegionAggregationTransform(name="after_agg", output_schema_config=SchemaConfig(mode="observed", fields=None))

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
        aggregations={
            "after_agg": (
                agg,  # type: ignore[dict-item]
                AggregationSettings(name="after_agg", plugin="agg", input="coalesce", on_success="out", on_error="discard"),
            )
        },
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
            # on_success omitted: publishes under the coalesce's own id
            # ("coalesce"), which the aggregation below consumes as `input`.
            CoalesceSettings(
                name="coalesce",
                branches={"path_a": "path_a_out", "path_b": "path_b_out"},
                policy="require_all",
                merge="union",
            )
        ],
    )


def _build_aggregation_alongside_nested_scope_in_a_branch() -> ExecutionGraph:
    """FORK[path_a, path_b] -> coalesce; path_a chains a declared scope
    (opener -> collector) followed by a SEPARATE aggregation node, both
    before reaching the coalesce.

    Adversarial nested-region probe (Task 8 checklist precedent, carried
    into Task 9 per the controller's dispatch): the scope opener/closer are
    both legally bound (rule 5 has nothing to say — they nest correctly),
    but the aggregation is a member of the OUTER FORK region only, not the
    EXPAND region. Proves `validate_no_aggregations_in_regions`'s per-region
    membership check correctly attributes the aggregation to its enclosing
    FORK region even when a DIFFERENT, properly-nested EXPAND region also
    exists in the same branch — the depth machinery must not confuse "this
    node is in some region" with "this node is in THIS region".
    """
    source = _BoundRegionMockSource()
    branch_a_expand = _BoundRegionMultiRowTransform(name="branch_a_expand", output_schema_config=SchemaConfig(mode="observed", fields=None))
    stitcher = _BoundRegionCollectorPlugin(name="page_stitcher", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_a_agg = _BoundRegionAggregationTransform(name="branch_a_agg", output_schema_config=SchemaConfig(mode="observed", fields=None))
    branch_b = _BoundRegionTransform(name="branch_b_transform", output_schema_config=SchemaConfig(mode="observed", fields=None))

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=branch_a_expand,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="branch_a_expand",
                    plugin=branch_a_expand.name,
                    input="path_a",
                    on_success="pages",
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
        aggregations={
            "branch_a_agg": (
                branch_a_agg,  # type: ignore[dict-item]
                AggregationSettings(name="branch_a_agg", plugin="agg", input="stitched", on_success="path_a_out", on_error="discard"),
            )
        },
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
        collectors={
            "page_stitcher": (
                stitcher,  # type: ignore[dict-item]
                CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="stitched"),
            )
        },
        scope_settings=[ScopeSettings(name="branch_a_pages", opener="branch_a_expand", closer="page_stitcher", policy="require_all")],
    )


class TestRule6AggregatorBan:
    """Spec §7 rule 6 (ruling 25): aggregators are windows, not closers —
    banned inside every bound region, both output modes, every closer kind.
    """

    def test_aggregation_inside_coalesce_branch_rejected_both_modes(self) -> None:
        for output_mode in ("transform", "passthrough"):
            with pytest.raises(GraphValidationError, match=r"Aggregation .* inside bound region"):
                _build_fork_coalesce_with_branch_aggregation(output_mode=output_mode)

    def test_aggregation_outside_regions_stays_legal(self) -> None:
        assert _build_top_level_aggregation_pipeline() is not None
        assert _build_aggregation_after_coalesce_release() is not None

    def test_aggregation_alongside_nested_scope_in_a_branch_rejected(self) -> None:
        # The scope opener/closer are legally bound (rule 5 stays silent);
        # only the sibling aggregation is rejected, attributed to the OUTER
        # FORK region specifically, not the (legal) inner EXPAND region.
        with pytest.raises(GraphValidationError, match="Aggregation 'branch_a_agg' is inside bound region 'coalesce'"):
            _build_aggregation_alongside_nested_scope_in_a_branch()


class TestGroupFailureHandlingIsStructural:
    """Group-failure handling is structural (ADR-042): a failed group
    escalates iff an enclosing bound group exists; the outermost group is
    terminal. The former ``on_group_failure`` field is deleted — depth is
    the only input, pinned here at both depths over REAL computed regions.
    """

    def test_nested_scope_computes_depth_2(self) -> None:
        # A REAL nested region (not a fixture with hand-set depth): the
        # scope's own binding computes to depth 2 via compute_bound_regions
        # because it is genuinely nested inside the outer fork->coalesce
        # region's span — an enclosing bound group exists to escalate to.
        graph = _build_fork_coalesce_with_scoped_expand_in_branch()
        regions = graph.get_bound_regions()
        scope_region = next(r for r in regions if r.binding.closer_kind == CloserKind.COLLECTOR)
        assert scope_region.depth == 2

    def test_top_level_scope_computes_depth_1(self) -> None:
        graph = _build_top_level_scope()
        (region,) = graph.get_bound_regions()
        assert region.depth == 1


class TestRule7RosterAuthorityIsStructural:
    """Spec §7 rule 7 (standing ruling): require_all is legal exactly where
    a roster authority exists — declared branches (coalesce/row_union) or
    a bound EXPAND group (scope). No new runtime raise: the policy
    vocabularies are already closed per closer kind at the pydantic-model
    level (spec §2); a runtime check here could never fire.
    """

    def test_aggregation_settings_has_no_policy_field(self) -> None:
        # Declaration pin: an aggregator has no roster authority at all
        # (it consumes a batch, not a group), so it stays policy-free —
        # asserting the field's ABSENCE is the right shape here.
        assert "policy" not in AggregationSettings.model_fields

    def test_scope_and_coalesce_policy_literals_include_require_all(self) -> None:
        # Truth pin, not just a declaration pin: confirm require_all is
        # actually IN the Literal vocabulary for the two closer kinds whose
        # roster authority is genuine — ScopeSettings (a bound EXPAND
        # group) and CoalesceSettings (declared fork branches) — rather
        # than only pinning the aggregation half's absence.
        assert "require_all" in get_args(ScopeSettings.model_fields["policy"].annotation)
        assert "require_all" in get_args(CoalesceSettings.model_fields["policy"].annotation)

    def test_row_union_has_no_configurable_policy_hardcoded_require_all(self) -> None:
        # row_union's roster authority (declared fork branches) is so
        # structurally confined there is no policy field to diverge at
        # all: group_bindings.py hardcodes policy="require_all" for every
        # ROW_UNION binding, and RowUnionSettings carries no policy field.
        assert "policy" not in RowUnionSettings.model_fields


# ---------------------------------------------------------------------------
# §7 rule 5 FORK arm (META-38 commit 3): an UNBOUND fork inside a bound region
# ---------------------------------------------------------------------------


def _build_unbound_fork_inside_scope(fork_to: list[str]) -> ExecutionGraph:
    """The META-38 structural counterexample (falsifier S1): source -> expand
    (declared scope opener) -> gate fork_to ``fork_to`` UNBOUND -> one
    ordinary transform per branch -> declared queue 'merged' -> collector
    (the scope's closer) -> sink. Rule 2 needs a bound branch, E2 lets an
    unbound branch feed a transform, rule 4's sink-inside never fires and the
    builder's rule-5 census excludes gates — so before the FORK arm this
    BUILT, in both the 2-branch shape (roster N, arrivals 2N) and the
    1-branch shape (roster N == arrivals N, provenance silently dropped)."""
    obs = SchemaConfig(mode="observed", fields=None)
    source = _BoundRegionMockSource()
    opener = _BoundRegionMultiRowTransform(name="expand", output_schema_config=obs)
    collector = _BoundRegionCollectorPlugin(name="stitcher", output_schema_config=obs)
    transforms = [
        WiredTransform(
            plugin=opener,  # type: ignore[arg-type]
            settings=TransformSettings(
                name="expand", plugin=opener.name, input="source_out", on_success="pages", on_error="discard", options={}
            ),
        )
    ]
    for branch in fork_to:
        plugin = _BoundRegionTransform(name=f"t_{branch}", output_schema_config=obs)
        transforms.append(
            WiredTransform(
                plugin=plugin,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name=f"t_{branch}", plugin=plugin.name, input=branch, on_success="merged", on_error="discard", options={}
                ),
            )
        )
    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[dict-item]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=transforms,
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[GateSettings(name="fanout", input="pages", condition="'all'", routes={"all": "fork"}, fork_to=fork_to)],
        queues={"merged": QueueSettings()},
        collectors={"stitcher": (collector, CollectorSettings(name="stitcher", plugin="stitch_pages", input="merged", on_success="out"))},  # type: ignore[dict-item]
        scope_settings=[ScopeSettings(name="doc_pages", opener="expand", closer="stitcher", policy="require_all")],
    )


def _build_bound_fork_inside_scope() -> ExecutionGraph:
    """The well-nested sibling: the same fork closes at an IN-REGION coalesce
    before the collector — legal, must keep building."""
    obs = SchemaConfig(mode="observed", fields=None)
    source = _BoundRegionMockSource()
    opener = _BoundRegionMultiRowTransform(name="expand", output_schema_config=obs)
    t_pa = _BoundRegionTransform(name="t_pa", output_schema_config=obs)
    t_pb = _BoundRegionTransform(name="t_pb", output_schema_config=obs)
    collector = _BoundRegionCollectorPlugin(name="stitcher", output_schema_config=obs)
    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[dict-item]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=opener,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="expand", plugin=opener.name, input="source_out", on_success="pages", on_error="discard", options={}
                ),
            ),
            WiredTransform(
                plugin=t_pa,  # type: ignore[arg-type]
                settings=TransformSettings(name="t_pa", plugin=t_pa.name, input="pa", on_success="pa_out", on_error="discard", options={}),
            ),
            WiredTransform(
                plugin=t_pb,  # type: ignore[arg-type]
                settings=TransformSettings(name="t_pb", plugin=t_pb.name, input="pb", on_success="pb_out", on_error="discard", options={}),
            ),
        ],
        sinks={"out": _BoundRegionMockSink("out")},  # type: ignore[dict-item]
        aggregations={},
        gates=[GateSettings(name="fanout", input="pages", condition="'all'", routes={"all": "fork"}, fork_to=["pa", "pb"])],
        coalesce_settings=[CoalesceSettings(name="merge", branches={"pa": "pa_out", "pb": "pb_out"}, policy="require_all", merge="union")],
        # The collector consumes the coalesce BY NAME (the oracle's
        # section_merge -> page_stitcher shape).
        collectors={"stitcher": (collector, CollectorSettings(name="stitcher", plugin="stitch_pages", input="merge", on_success="out"))},  # type: ignore[dict-item]
        scope_settings=[ScopeSettings(name="doc_pages", opener="expand", closer="stitcher", policy="require_all")],
    )


class TestRule5ForkArm:
    @pytest.mark.parametrize("fork_to", [["pa", "pb"], ["pa"]], ids=["two-branches", "single-branch"])
    def test_unbound_fork_inside_a_bound_region_is_refused(self, fork_to: list[str]) -> None:
        with pytest.raises(GraphValidationError, match=r"Fork gate 'fanout' inside bound region 'stitcher' is unbound") as excinfo:
            _build_unbound_fork_inside_scope(fork_to)
        assert (excinfo.value.component_id, excinfo.value.component_type) == ("fanout", "gate")

    def test_bound_fork_inside_a_bound_region_still_builds(self) -> None:
        graph = _build_bound_fork_inside_scope()
        regions = graph.get_bound_regions()
        assert {r.binding.closer_name for r in regions} == {"stitcher", "merge"}
