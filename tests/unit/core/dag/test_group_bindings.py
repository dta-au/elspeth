"""GroupBindingRegistry — ONE registry, derived views, frame resolution (barrier-scopes spec §3)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema import SchemaConfig
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.core.config import CoalesceSettings, GateSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.group_bindings import CloserKind, GroupBinding, GroupBindingRegistry
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


class _GroupBindingsTransform:
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
