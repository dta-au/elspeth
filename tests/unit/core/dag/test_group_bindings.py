"""GroupBindingRegistry — ONE registry, derived views, frame resolution (barrier-scopes spec §3)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.core.config import CoalesceSettings, GateSettings, RowUnionSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
from elspeth.core.dag.models import GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


def _fork_binding(
    *,
    closer_kind: CloserKind = CloserKind.COALESCE,
    opener: str = "g1",
    closer: str = "merge",
    member_roster: tuple[str, ...] = ("path_a", "path_b"),
) -> GroupBinding:
    return GroupBinding(
        kind=FrameKind.FORK,
        opener_node_id=NodeID(f"gate_{opener}_abc"),
        opener_name=opener,
        closer_node_id=NodeID(f"{closer_kind}_{closer}_def"),
        closer_name=closer,
        closer_kind=closer_kind,
        policy="require_all",
        on_group_failure=None,
        member_roster=member_roster,
    )


def _expand_binding(*, opener: str = "explode", closer: str = "page_stitcher") -> GroupBinding:
    return GroupBinding(
        kind=FrameKind.EXPAND,
        opener_node_id=NodeID(f"transform_{opener}_abc"),
        opener_name=opener,
        closer_node_id=NodeID(f"collector_{closer}_def"),
        closer_name=closer,
        closer_kind=CloserKind.COLLECTOR,
        policy="require_all",
        on_group_failure="quarantine",
        member_roster=(),
    )


class TestCloserKindWire:
    def test_members_are_their_strings(self) -> None:
        # StrEnum: every serialized surface (composer NodeSpec dicts, the
        # guidedDecoder wire shapes, audit JSON, GraphValidationError
        # component_type) keeps carrying plain strings unchanged.
        assert CloserKind.COALESCE == "coalesce"
        assert f"{CloserKind.ROW_UNION}" == "row_union"
        assert json.loads(json.dumps(CloserKind.COLLECTOR)) == "collector"


class TestDerivedViews:
    def test_branch_to_coalesce_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        assert reg.branch_to_coalesce() == {
            BranchName("path_a"): CoalesceName("merge"),
            BranchName("path_b"): CoalesceName("merge"),
        }
        assert reg.branch_to_row_union() == {}

    def test_branch_to_row_union_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(closer_kind=CloserKind.ROW_UNION, closer="union"),))
        assert reg.branch_to_row_union() == {
            BranchName("path_a"): RowUnionName("union"),
            BranchName("path_b"): RowUnionName("union"),
        }

    def test_expand_binding_contributes_no_branch_view(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        assert reg.branch_to_coalesce() == {}
        assert reg.branch_to_row_union() == {}


class TestExclusivity:
    def test_duplicate_opener_rejected(self) -> None:
        with pytest.raises(ValueError, match="binds at most one closer"):
            GroupBindingRegistry(bindings=(_fork_binding(), _fork_binding(closer="other")))

    def test_duplicate_closer_rejected(self) -> None:
        b1 = _fork_binding()
        b2 = _fork_binding(opener="g2")  # same closer node
        with pytest.raises(ValueError, match="closes at most one group"):
            GroupBindingRegistry(bindings=(b1, b2))

    def test_shared_roster_member_across_forks_rejected(self) -> None:
        # binding_for's FORK resolution keys on member_key (the branch name),
        # so roster membership must be a function. Branch names are
        # one-producer connections in the builder; the registry re-asserts it.
        b1 = _fork_binding()
        b2 = _fork_binding(opener="g2", closer="other", member_roster=("path_b", "path_c"))
        with pytest.raises(ValueError, match="appears in two bound forks"):
            GroupBindingRegistry(bindings=(b1, b2))


class TestBindingFor:
    """binding_for — the settle-member walk's frame resolver (spec §6.1; 2026-08-22 synthesis)."""

    def test_fork_frame_resolves_by_member_key(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg_runtime_1", member_key="path_a")
        assert reg.binding_for(frame) is reg.bindings[0]

    def test_fork_frame_outside_any_roster_is_inert(self) -> None:
        reg = GroupBindingRegistry(bindings=(_fork_binding(),))
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg_runtime_1", member_key="unbound_branch")
        assert reg.binding_for(frame) is None

    def test_expand_frame_resolves_after_mint_registration(self) -> None:
        # EXPAND group ids are runtime-minted (generate_id()), so the opener's
        # mint path registers each group; before registration the frame is
        # inert (exactly what an UNDECLARED expand stays forever).
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        frame = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_run_1", member_key="tok_child_1")
        assert reg.binding_for(frame) is None
        assert reg.register_expand_group("eg_run_1", opener_name="explode") is reg.bindings[0]
        assert reg.binding_for(frame) is reg.bindings[0]

    def test_undeclared_opener_registration_is_a_noop(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(),))
        assert reg.register_expand_group("eg_run_2", opener_name="plain_batch_transform") is None
        frame = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_run_2", member_key="tok_x")
        assert reg.binding_for(frame) is None

    def test_reregistering_group_to_a_different_opener_rejected(self) -> None:
        reg = GroupBindingRegistry(bindings=(_expand_binding(), _expand_binding(opener="explode2", closer="stitch2")))
        reg.register_expand_group("eg_run_3", opener_name="explode")
        assert reg.register_expand_group("eg_run_3", opener_name="explode") is reg.bindings[0]  # idempotent
        with pytest.raises(ValueError, match="already registered"):
            reg.register_expand_group("eg_run_3", opener_name="explode2")


# ===== Differential test: builder-produced registry vs. the legacy maps =====


class _GroupBindingsMockSource:
    name = "mock_source"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_validation_failure = "discard"
    on_success = "source_out"
    _output_schema_config: SchemaConfig | None = None


class _GroupBindingsMockSink:
    name = "mock_sink"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


class _GroupBindingsNamedMockSink:
    """A mock sink with a caller-chosen name, for graphs needing >1 sink."""

    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, name: str) -> None:
        self.name = name

    def _reset_diversion_log(self) -> None:
        pass


class _GroupBindingsTransform:
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


def _build_fork_coalesce_graph() -> ExecutionGraph:
    """Fork gate -> {direct branch, transform-chain branch} -> coalesce.

    Modeled on test_builder_validation.py's
    test_branch_info_carries_identity_and_transform_branch_plan.
    """
    source = _GroupBindingsMockSource()
    transform = _GroupBindingsTransform(
        name="slow_branch_transform",
        output_schema_config=SchemaConfig(mode="observed", fields=None),
    )

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[
            WiredTransform(
                plugin=transform,  # type: ignore[arg-type]
                settings=TransformSettings(
                    name="slow_branch_transform",
                    plugin=transform.name,
                    input="slow_branch",
                    on_success="slow_out",
                    on_error="discard",
                    options={},
                ),
            )
        ],
        sinks={"output": _GroupBindingsMockSink()},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="splitter",
                input="source_out",
                condition="True",
                routes={"true": "fork", "false": "output"},
                fork_to=["fast_branch", "slow_branch"],
            )
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="merge_results",
                branches={"fast_branch": "fast_branch", "slow_branch": "slow_out"},
                policy="require_all",
                merge="union",
                on_success="output",
            )
        ],
    )


def test_derived_views_reproduce_graph_maps_exactly() -> None:
    graph = _build_fork_coalesce_graph()
    registry = graph.get_group_bindings()
    assert registry.branch_to_coalesce() == graph.get_branch_to_coalesce_map()
    assert registry.branch_to_row_union() == graph.get_branch_to_row_union_map()


def _build_mixed_coalesce_sink_branch_graph() -> ExecutionGraph:
    """Fork gate mixing a coalesce-bound pair with a direct-to-sink branch.

    Was authorable when this fixture was written (builder.py resolved each
    fork_to branch independently against coalesce/row_union specs or a
    direct sink name — "option 3" is a branch name that matches a sink
    exactly, with no barrier at all). Regression fixture for review round 1
    finding 1 Case A. Task 6's whole-roster fork closure (spec §7 rule 2 /
    ruling 23) now rejects this exact shape at build time: 'fast_branch' and
    'slow_branch' close at a barrier while 'direct_branch' goes direct to a
    sink — a mixed fork. The fixture is kept (it still constructs the
    settings/graph inputs; the build call itself is expected to raise) so
    the rejection has a concrete regression test rather than only Task 6's
    own builder fixtures.
    """
    source = _GroupBindingsMockSource()

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[],
        sinks={
            "output": _GroupBindingsNamedMockSink("output"),
            "direct_branch": _GroupBindingsNamedMockSink("direct_branch"),
        },  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="splitter",
                input="source_out",
                condition="True",
                routes={"true": "fork", "false": "output"},
                fork_to=["fast_branch", "slow_branch", "direct_branch"],
            )
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="merge_results",
                branches={"fast_branch": "fast_branch", "slow_branch": "slow_branch"},
                policy="require_all",
                merge="union",
                on_success="output",
            )
        ],
    )


def test_mixed_coalesce_and_sink_branch_roster_excludes_sink_branch() -> None:
    """Review round 1 finding 1 Case A pinned that member_roster must be
    filtered to only the branches that resolve to the WINNING closer, never
    the gate's whole fork_to — a sink-bound sibling branch must never join
    the roster. Task 6's whole-roster fork closure (spec §7 rule 2 / ruling
    23) closes that interim era: the mixed shape this fixture builds is now
    a build-time rejection ("mixed closure"), so the roster-filtering
    question this test asked can no longer arise — the graph that would
    need it never builds."""
    with pytest.raises(GraphValidationError, match="mixed closure"):
        _build_mixed_coalesce_sink_branch_graph()


def _build_mixed_coalesce_row_union_graph() -> ExecutionGraph:
    """One gate mixing a coalesce-bound pair with a row_union-bound pair.

    Same-gate mixing is unconstrained by builder.py's ancestor/descendant and
    unrelated-fork-origin row_union checks (those only guard branches
    arriving from DIFFERENT gates), so this topology was authorable when
    this fixture was written — review round 1 finding 1 Case B. Task 6's
    whole-roster fork closure (spec §7 rule 2 / ruling 23) is exactly what
    was pending: a fork whose branches split across two different closers
    (here a coalesce and a row_union) is now a build-time rejection ("closes
    at multiple barriers"). The "first bound branch wins" interim this
    docstring used to describe is retired — this shape never reaches the
    group-bindings registry any more.
    """
    source = _GroupBindingsMockSource()

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[],
        sinks={"output": _GroupBindingsNamedMockSink("output")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="splitter",
                input="source_out",
                condition="True",
                routes={"true": "fork", "false": "output"},
                fork_to=["fast_branch", "slow_branch", "ru_a", "ru_b"],
            ),
            GateSettings(
                name="route_union_out",
                input="ru_out",
                condition="True",
                routes={"true": "output", "false": "output"},
            ),
        ],
        coalesce_settings=[
            CoalesceSettings(
                name="merge_results",
                branches={"fast_branch": "fast_branch", "slow_branch": "slow_branch"},
                policy="require_all",
                merge="union",
                on_success="output",
            )
        ],
        row_union_settings=[
            RowUnionSettings(
                name="variant_union",
                branches={"ru_a": "ru_a", "ru_b": "ru_b"},
                on_success="ru_out",
            )
        ],
    )


def test_mixed_coalesce_and_row_union_same_gate_is_pinned_interim() -> None:
    """Formerly a PINNED interim (review round 1 finding 1 Case B): a
    one-binding-per-gate registry could not represent two different closers
    for one gate, so "first bound branch wins" left the row_union branches
    unbound rather than mis-attributed. That interim era ended here: Task
    6's whole-roster fork closure (spec §7 rule 2 / ruling 23) now rejects
    this exact shape at build time — a fork closing at two different
    barriers ("closes at multiple barriers") — so the registry never sees
    it and the interim's own question (what does the registry do with the
    unbound half) no longer applies.
    """
    with pytest.raises(GraphValidationError, match="closes at multiple barriers"):
        _build_mixed_coalesce_row_union_graph()


def _build_fork_row_union_graph() -> ExecutionGraph:
    """Homogeneous fork gate -> row_union (review round 1 finding 2: the
    row_union half of the differential-equality claim needs a non-vacuous
    fixture; the only prior differential graph declared no row_union at
    all, so `branch_to_row_union() == {}` on both sides proved nothing)."""
    source = _GroupBindingsMockSource()

    return ExecutionGraph.from_plugin_instances(
        sources={"primary": source},  # type: ignore[arg-type]
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
        transforms=[],
        sinks={"output": _GroupBindingsNamedMockSink("output")},  # type: ignore[dict-item]
        aggregations={},
        gates=[
            GateSettings(
                name="splitter",
                input="source_out",
                condition="True",
                routes={"true": "fork", "false": "output"},
                fork_to=["ru_a", "ru_b"],
            ),
            GateSettings(
                name="route_union_out",
                input="ru_out",
                condition="True",
                routes={"true": "output", "false": "output"},
            ),
        ],
        coalesce_settings=[],
        row_union_settings=[
            RowUnionSettings(
                name="variant_union",
                branches={"ru_a": "ru_a", "ru_b": "ru_b"},
                on_success="ru_out",
            )
        ],
    )


def test_derived_views_reproduce_graph_maps_exactly_for_row_union() -> None:
    graph = _build_fork_row_union_graph()
    registry = graph.get_group_bindings()
    assert registry.branch_to_row_union() == graph.get_branch_to_row_union_map()
    assert registry.branch_to_row_union() == {
        BranchName("ru_a"): RowUnionName("variant_union"),
        BranchName("ru_b"): RowUnionName("variant_union"),
    }
    assert registry.branch_to_coalesce() == {}
