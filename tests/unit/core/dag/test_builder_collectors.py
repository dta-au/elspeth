"""Builder tests for collector nodes and the scope binding node-config key."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.contracts.schema import SchemaConfig, get_raw_node_required_fields
from elspeth.contracts.types import CollectorName, NodeID
from elspeth.core.config import CoalesceSettings, CollectorSettings, GateSettings, ScopeSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.models import EdgeContractError, GraphValidationError
from elspeth.core.dag.wiring import WiredTransform


class _Source:
    name = "src"
    output_schema = None
    config: ClassVar[dict[str, Any]] = {"schema": {"mode": "observed"}}
    _on_validation_failure = "discard"
    on_success = "rows"
    _output_schema_config = None
    observed_value_type: str | None = None


class _Sink:
    name = "out"
    input_schema = None
    config: ClassVar[dict[str, Any]] = {}
    _on_write_failure = "discard"
    declared_required_fields: ClassVar[frozenset[str]] = frozenset()

    def _reset_diversion_log(self) -> None:
        pass


class _MultiRowTransform:
    """Stub multi-row transform (creates_tokens=True) — a scope opener."""

    input_schema = None
    output_schema = None
    creates_tokens = True
    is_batch_aware = False
    on_success: str | None = "pages"
    on_error: str | None = "discard"
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    preserves_input_values = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        # .name is the PLUGIN name (matches TransformSettings.plugin below via
        # WiredTransform.__post_init__'s identity check) — NOT the node's
        # config name, which is "explode" (TransformSettings.name).
        self.name = "json_explode"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


class _BatchTransform:
    """Stub batch-aware transform — the collector plugin."""

    input_schema = None
    output_schema = None
    creates_tokens = False
    is_batch_aware = True
    on_success: str | None = None
    on_error: str | None = None
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    preserves_input_values = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self) -> None:
        self.name = "stitch"
        self.config = {"schema": {"mode": "observed"}}
        self._output_schema_config = None


class _PlainTransform:
    """Stub plain transform for per-branch fork chains (not a scope opener/closer)."""

    input_schema = None
    output_schema = None
    on_error: str | None = None
    on_success: str | None = "output"
    creates_tokens = False
    declared_output_fields: ClassVar[frozenset[str]] = frozenset()
    declared_input_fields: ClassVar[frozenset[str]] = frozenset()
    declared_string_input_fields: ClassVar[frozenset[str]] = frozenset()
    passes_through_input = False
    preserves_input_values = False
    forwards_input_fields = False
    removed_input_fields = frozenset()

    def __init__(self, *, name: str) -> None:
        self.name = name
        self.config: dict[str, Any] = {"schema": {"mode": "observed"}}
        self._output_schema_config = SchemaConfig(mode="observed", fields=None)


def _wt(name: str, inp: str, out: str) -> WiredTransform:
    return WiredTransform(
        plugin=_PlainTransform(name=name),  # type: ignore[arg-type]
        settings=TransformSettings(name=name, plugin=name, input=inp, on_success=out, on_error="discard", options={}),
    )


def _plugin_names(graph: ExecutionGraph, ids: frozenset[NodeID]) -> set[str]:
    return {graph.get_node_info(n).plugin_name for n in ids}


def _source_settings() -> dict[str, SourceSettings]:
    return {"src": SourceSettings(plugin="csv", options={"path": "x.csv", "schema": {"mode": "observed"}}, on_success="rows")}


def _explode_settings() -> TransformSettings:
    return TransformSettings(name="explode", plugin="json_explode", input="rows", on_success="pages", on_error="discard")


def _collector_settings() -> CollectorSettings:
    # on_error deliberately omitted: None = derives-from-structure (spec §7 rule 9),
    # the canonical authored shape (2026-08-22 synthesis).
    return CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages", on_success="out")


def _scope_settings() -> ScopeSettings:
    return ScopeSettings(name="document_pages", opener="explode", closer="page_stitcher", policy="require_all")


def _build(**overrides: Any) -> ExecutionGraph:
    kwargs: dict[str, Any] = {
        "sources": {"src": _Source()},
        "source_settings_map": _source_settings(),
        "transforms": [WiredTransform(plugin=_MultiRowTransform(), settings=_explode_settings())],
        "sinks": {"out": _Sink()},
        "collectors": {"page_stitcher": (_BatchTransform(), _collector_settings())},
        "scope_settings": [_scope_settings()],
    }
    kwargs.update(overrides)
    return ExecutionGraph.from_plugin_instances(**kwargs)


def _build_expand_outer_fork_inner() -> ExecutionGraph:
    """EXPAND scope (explode -> page_stitcher) with a FORK region nested inside
    it — spec §7 rule 3's own parenthetical: "a fork inside a collector-bound
    group is a nested region and must close inside it".

        src -> explode (scope opener) -> pages
          inner_gate forks [p, q]
            p -> tp -> p_out
            q -> tq -> q_out
          coalesce 'merge' {p: p_out, q: q_out} -> connection "merge"
        collector page_stitcher (scope closer) input=merge -> out
    """
    return ExecutionGraph.from_plugin_instances(
        sources={"src": _Source()},
        source_settings_map=_source_settings(),
        transforms=[
            WiredTransform(plugin=_MultiRowTransform(), settings=_explode_settings()),
            _wt("tp", "p", "p_out"),
            _wt("tq", "q", "q_out"),
        ],
        sinks={"out": _Sink()},
        collectors={
            "page_stitcher": (
                _BatchTransform(),
                CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="merge", on_success="out"),
            )
        },
        scope_settings=[_scope_settings()],
        gates=[GateSettings(name="inner_gate", input="pages", condition="'all'", routes={"all": "fork"}, fork_to=["p", "q"])],
        coalesce_settings=[CoalesceSettings(name="merge", branches={"p": "p_out", "q": "q_out"}, policy="require_all", merge="union")],
    )


def _build_three_level_expand_fork_fork(*, max_bound_region_depth: int = 5) -> ExecutionGraph:
    """EXPAND ⊃ FORK ⊃ FORK: the only topology anywhere that exercises depth
    >= 3 — the entire reason the depth cap exists (spec §6.3).

        src -> explode (scope opener) -> pages
          g1 forks [a, b]
            a -> g2 forks [aa, ab]
              aa -> t_aa -> aa_out
              ab -> t_ab -> ab_out
            c2 {aa: aa_out, ab: ab_out} -> connection "c2"  (closes g2)
            b -> t_b -> b_out
          c1 {a: c2, b: b_out} -> connection "c1"           (closes g1)
        collector page_stitcher (scope closer) input=c1 -> out
    """
    return ExecutionGraph.from_plugin_instances(
        sources={"src": _Source()},
        source_settings_map=_source_settings(),
        transforms=[
            WiredTransform(plugin=_MultiRowTransform(), settings=_explode_settings()),
            _wt("t_aa", "aa", "aa_out"),
            _wt("t_ab", "ab", "ab_out"),
            _wt("t_b", "b", "b_out"),
        ],
        sinks={"out": _Sink()},
        collectors={
            "page_stitcher": (
                _BatchTransform(),
                CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="c1", on_success="out"),
            )
        },
        scope_settings=[_scope_settings()],
        gates=[
            GateSettings(name="g1", input="pages", condition="'all'", routes={"all": "fork"}, fork_to=["a", "b"]),
            GateSettings(name="g2", input="a", condition="'all'", routes={"all": "fork"}, fork_to=["aa", "ab"]),
        ],
        coalesce_settings=[
            CoalesceSettings(name="c2", branches={"aa": "aa_out", "ab": "ab_out"}, policy="require_all", merge="union"),
            CoalesceSettings(name="c1", branches={"a": "c2", "b": "b_out"}, policy="require_all", merge="union"),
        ],
        max_bound_region_depth=max_bound_region_depth,
    )


class TestCollectorNode:
    def test_collector_node_is_built_with_scope_binding_key(self) -> None:
        graph = _build()
        collector_ids = graph.get_collector_id_map()
        assert list(collector_ids) == [CollectorName("page_stitcher")]
        info = graph.get_node_info(collector_ids[CollectorName("page_stitcher")])
        assert info.node_type == NodeType.COLLECTOR
        assert info.config["scope"] == {
            "name": "document_pages",
            "opener": "explode",
            "policy": "require_all",
        }

    def test_collector_requires_batch_aware_plugin(self) -> None:
        non_batch = _MultiRowTransform()
        with pytest.raises(GraphValidationError, match="is_batch_aware"):
            _build(collectors={"page_stitcher": (non_batch, _collector_settings())})

    def test_scope_binding_key_is_never_present_on_non_collector_nodes(self) -> None:
        # Canonical-hash stability (spec §3): "scope" must not leak into any
        # other node's canonical config dict.
        graph = _build()
        for info in graph.get_nodes():
            if info.node_type is not NodeType.COLLECTOR:
                assert "scope" not in info.config

    def test_scope_bound_region_is_computed(self) -> None:
        # EXPAND-kind bound region (spec §7 rule 3, §6.3): the explode ->
        # page_stitcher scope opens and closes with nothing between them in
        # this fixture, so membership is empty at depth 1 — the shape
        # WS3's leader_drain fixpoint bound (graph.get_max_bound_region_depth())
        # actually consumes.
        graph = _build()
        regions = graph.get_bound_regions()
        assert len(regions) == 1
        region = regions[0]
        assert region.member_node_ids == frozenset()
        assert region.depth == 1
        assert graph.get_max_bound_region_depth() == 1
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 1

    def test_expand_outer_fork_inner_bound_regions(self) -> None:
        # Mixed-kind nesting (spec §7 rule 3's own parenthetical): a FORK
        # region nested inside an EXPAND (scope) region. Both depths and both
        # membership sets are pinned — this is where an `s2 <= m1` containment
        # check across two different FrameKinds is least obviously right.
        graph = _build_expand_outer_fork_inner()
        regions = {r.binding.kind.name: r for r in graph.get_bound_regions()}
        assert set(regions) == {"FORK", "EXPAND"}

        fork_region = regions["FORK"]
        assert fork_region.binding.opener_name == "inner_gate"
        assert fork_region.binding.closer_name == "merge"
        assert fork_region.depth == 2
        assert _plugin_names(graph, fork_region.member_node_ids) == {"tp", "tq"}

        expand_region = regions["EXPAND"]
        assert expand_region.binding.opener_name == "explode"
        assert expand_region.binding.closer_name == "page_stitcher"
        assert expand_region.depth == 1
        assert _plugin_names(graph, expand_region.member_node_ids) == {
            "coalesce:merge",
            "config_gate:inner_gate",
            "tp",
            "tq",
        }

        assert graph.get_max_bound_region_depth() == 2
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 2

    def test_three_level_expand_fork_fork_bound_regions(self) -> None:
        # The only topology anywhere that exercises depth >= 3 — the entire
        # reason the depth cap exists (spec §6.3).
        graph = _build_three_level_expand_fork_fork()
        regions = {(r.binding.opener_name, r.binding.closer_name): r for r in graph.get_bound_regions()}
        assert set(regions) == {("explode", "page_stitcher"), ("g1", "c1"), ("g2", "c2")}

        expand_region = regions[("explode", "page_stitcher")]
        assert expand_region.depth == 1
        assert _plugin_names(graph, expand_region.member_node_ids) == {
            "coalesce:c1",
            "coalesce:c2",
            "config_gate:g1",
            "config_gate:g2",
            "t_aa",
            "t_ab",
            "t_b",
        }

        outer_fork = regions[("g1", "c1")]
        assert outer_fork.depth == 2
        assert _plugin_names(graph, outer_fork.member_node_ids) == {
            "coalesce:c2",
            "config_gate:g2",
            "t_aa",
            "t_ab",
            "t_b",
        }

        inner_fork = regions[("g2", "c2")]
        assert inner_fork.depth == 3
        assert _plugin_names(graph, inner_fork.member_node_ids) == {"t_aa", "t_ab"}

        assert graph.get_max_bound_region_depth() == 3
        assert graph.escalation_fixpoint_bound == 1_000 + 8 * 3

    def test_three_level_depth_cap_rejects_below_three(self) -> None:
        with pytest.raises(GraphValidationError, match="nesting depth"):
            _build_three_level_expand_fork_fork(max_bound_region_depth=2)


class TestCollectorDeclaredRequiredFieldsAreEnforced:
    """A collector's declared ``schema.required_fields`` is a live build-time
    contract, not an inert declaration (elspeth-c3cbf5f4cd).

    ``builder.py`` gives a collector the SAME nested-``options`` node-config
    shape it gives an aggregation, but ``get_raw_node_required_fields``
    unwrapped that nesting only for ``node_type == "aggregation"``. The
    identical declaration therefore built clean on a collector and raised
    ``EdgeContractError`` on an aggregation — a contract the author declares,
    the system accepts, and nothing ever checks.

    This is NOT the ADR-013 runtime ``declared_input_fields`` contract, which
    correctly fails closed for batch-aware plugins at construction. This is
    the separate build-time DAG check that predates it (see
    ``plugins/infrastructure/base.py`` and elspeth-3790106260), and an
    aggregation carrying a batch-aware plugin already gets it today.
    """

    # The node-config shape builder.py writes at builder.py:709-712 — the
    # authored plugin options nested one level down under "options".
    _NESTED_NODE_CONFIG: ClassVar[dict[str, Any]] = {
        "options": {"schema": {"mode": "observed", "required_fields": ["totally_absent_field"]}},
        "input_schema": {"mode": "observed", "required_fields": ["totally_absent_field"]},
        "scope": {"name": "document_pages", "opener": "explode", "policy": "require_all"},
    }

    def test_collector_and_aggregation_read_the_same_nested_contract(self) -> None:
        """Mutation check: same config, ``node_type`` the only variable.

        Before the fix this returned ``frozenset()`` for "collector" and
        ``{'totally_absent_field'}`` for "aggregation".
        """
        by_node_type = {
            node_type: get_raw_node_required_fields(self._NESTED_NODE_CONFIG, owner="node:x", node_type=node_type)
            for node_type in ("collector", "aggregation")
        }
        assert by_node_type == {
            "collector": frozenset({"totally_absent_field"}),
            "aggregation": frozenset({"totally_absent_field"}),
        }

    def test_flat_contract_node_types_do_not_unwrap_a_plugin_option_named_options(self) -> None:
        """The unwrap is a closed membership set, not an unconditional one.

        Only aggregations and collectors get the nested shape from the
        builder. Every other node kind carries its contract flat, so a key
        named ``options`` sitting on a flat node config must NOT be mistaken
        for the wrapper. The reachable case is a planner-authored composer
        draft, whose ``node.options`` is not validated against the plugin's
        config model when ``validate()`` runs — see the comment on
        ``NESTED_CONTRACT_OPTIONS_NODE_TYPES``.
        """
        for node_type in ("transform", "gate", "coalesce", "row_union", "queue", "source", "sink", None):
            assert get_raw_node_required_fields(self._NESTED_NODE_CONFIG, owner="node:x", node_type=node_type) == frozenset()

    def test_collector_required_fields_reject_a_producer_that_cannot_supply_them(self) -> None:
        """End to end: the declared requirement now fails the build.

        Asserts the ``EdgeContractError`` on the opener -> collector edge, not
        merely that the unwrap returns a non-empty set.
        """
        contract = {"schema": {"mode": "observed", "required_fields": ["totally_absent_field"]}}
        collector_plugin = _BatchTransform()
        collector_plugin.config = dict(contract)

        with pytest.raises(EdgeContractError) as exc_info:
            _build(
                collectors={
                    "page_stitcher": (
                        collector_plugin,
                        CollectorSettings(
                            name="page_stitcher",
                            plugin="stitch_pages",
                            input="pages",
                            on_success="out",
                            options=dict(contract),
                        ),
                    )
                }
            )

        error = exc_info.value
        assert error.compatibility_result is not None
        assert error.compatibility_result.missing_fields == ("totally_absent_field",)
        assert error.component_type == "collector"
        assert "totally_absent_field" in str(error)


def test_collector_is_a_rule9_error_routable_closer_at_runtime() -> None:
    """Spec §7 rule 9 parity (integration item 18): the runtime-facing
    closer set the orchestrator validators and the dispatch classifier
    consume names the collector, exactly as the builder's own rule-9
    acceptance already did."""
    graph = _build()
    assert "page_stitcher" in graph.get_error_routable_closer_names()
