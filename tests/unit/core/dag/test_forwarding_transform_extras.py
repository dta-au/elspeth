"""Build-time extras firewall across FIELD-FORWARDING transforms (elspeth-15c72686f2).

``passes_through_input`` is all-or-nothing: it demands that every input field
survive. A transform that forwards the whole row except one column it consumed
cannot declare it, so the guarantee walk used to STOP at those nodes and an
upstream producer's extras became invisible to
``_validate_locked_consumer_guaranteed_extras``. The reported graph —
llm source -> line_explode -> locked text sink — built green on both gates and
then killed every row at the sink's per-row input preflight on
``<response_field>_usage`` / ``_model``.

``forwards_input_fields`` / ``removed_input_fields`` is the weaker declaration
those plugins CAN make, and ``walk_definite_emitted_fields`` is the
extras-direction walk that consumes it. Deliberately built from REAL plugin
instances rather than hand-stamped ``NodeInfo``: the defect lived in the gap
between what a plugin does at runtime and what its declarations said, so a
hand-built graph could assert the walk while leaving that gap open.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.enums import NodeType
from elspeth.core.config import GateSettings, RowUnionSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.guarantees import get_definite_emitted_fields, get_effective_guaranteed_fields
from elspeth.core.dag.models import EdgeContractError
from elspeth.core.dag.wiring import WiredTransform
from elspeth.plugins.sinks.text_sink import TextSink
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.llm.source import LLMSource
from elspeth.plugins.transforms.field_mapper import FieldMapper
from elspeth.plugins.transforms.line_explode import LineExplode
from elspeth.plugins.transforms.llm.transform import LLMTransform

_RESPONSE_FIELD = "announcement"
_METADATA_EXTRAS = frozenset({f"{_RESPONSE_FIELD}_usage", f"{_RESPONSE_FIELD}_model"})


def _llm_source_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Write an announcement.",
        "response_field": _RESPONSE_FIELD,
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    options.update(overrides)
    return options


def _llm_transform_options() -> dict[str, Any]:
    return {
        "provider": "openrouter",
        "model": "openai/gpt-4o-mini",
        "api_key": "test-api-key",
        "prompt_template": "Write an announcement about {{ row.topic }}.",
        "response_field": _RESPONSE_FIELD,
        "required_input_fields": ["topic"],
        "schema": {"mode": "observed"},
    }


def _line_explode_options() -> dict[str, Any]:
    return {
        "source_field": _RESPONSE_FIELD,
        "output_field": "sentence",
        "include_index": False,
        "schema": {"mode": "observed"},
    }


def _locked_text_sink_options(field: str = "sentence") -> dict[str, Any]:
    return {
        "path": "outputs/announcement_sentences.txt",
        "field": field,
        "schema": {"mode": "fixed", "fields": [f"{field}: str"]},
        "mode": "write",
        "collision_policy": "auto_increment",
    }


def _wired(plugin: Any, *, name: str, plugin_name: str, input_conn: str, on_success: str, options: dict[str, Any]) -> WiredTransform:
    return WiredTransform(
        plugin=plugin,
        settings=TransformSettings(
            name=name,
            plugin=plugin_name,
            input=input_conn,
            on_success=on_success,
            on_error="discard",
            options=dict(options),
        ),
    )


def _expect_rejected(**kwargs: Any) -> EdgeContractError:
    """Build the graph and return the extras rejection it must raise.

    ``from_plugin_instances`` runs ``validate_edge_compatibility`` as part of
    construction, so the rejection surfaces at build rather than at a separate
    validate call — which is the point: an authored graph never reaches a run.
    The assertion is pinned to the extras arm specifically (``extra_fields``
    populated) so a graph rejected for some unrelated reason cannot pass as a
    fix for this defect.
    """
    with pytest.raises(EdgeContractError) as excinfo:
        _build_graph(**kwargs)
    error = excinfo.value
    assert error.compatibility_result is not None, f"not an extras rejection: {error}"
    assert error.compatibility_result.extra_fields, f"not an extras rejection: {error}"
    return error


def _build_graph(
    *,
    source_plugin: Any,
    source_plugin_name: str,
    source_options: dict[str, Any],
    source_connection: str,
    transforms: list[WiredTransform],
    sink: Any,
    sink_name: str,
    gates: list[GateSettings] | None = None,
    row_union_settings: list[RowUnionSettings] | None = None,
) -> ExecutionGraph:
    return ExecutionGraph.from_plugin_instances(
        sources={"in": source_plugin},
        source_settings_map={
            "in": SourceSettings(plugin=source_plugin_name, on_success=source_connection, options=dict(source_options)),
        },
        transforms=transforms,
        sinks={sink_name: sink},
        aggregations={},
        gates=gates or [],
        coalesce_settings=[],
        row_union_settings=row_union_settings or [],
    )


class TestLLMMetadataExtrasSurviveAForwardingTransform:
    """The reported graph and its transform-fed twin."""

    def test_llm_source_through_line_explode_into_locked_sink_is_rejected(self) -> None:
        """Decisive repro of the battery round-7 g11-s2 failure.

        Before the fix this built green and every row died at the sink's
        ``model_validate`` on ``announcement_usage`` / ``announcement_model``.
        """
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS

    def test_llm_transform_through_line_explode_into_locked_sink_is_rejected(self) -> None:
        """The same defect one hop earlier — and the far commoner authored shape.

        The reported ticket blamed the llm SOURCE and argued no earlier battery
        round could have found this because ``source:llm`` was unauthorable.
        That narrative is wrong: an llm TRANSFORM feeding the same exploder
        reaches the identical dead end, so the defect was always reachable.
        """
        error = _expect_rejected(
            source_plugin=CSVSource(
                {
                    "path": "data/in.csv",
                    "schema": {"mode": "fixed", "fields": ["topic: str"]},
                    "on_validation_failure": "discard",
                }
            ),
            source_plugin_name="csv",
            source_options={
                "path": "data/in.csv",
                "schema": {"mode": "fixed", "fields": ["topic: str"]},
                "on_validation_failure": "discard",
            },
            source_connection="rows",
            transforms=[
                _wired(
                    LLMTransform(_llm_transform_options()),
                    name="writer",
                    plugin_name="llm",
                    input_conn="rows",
                    on_success="written",
                    options=_llm_transform_options(),
                ),
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="written",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                ),
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        extras = frozenset(error.compatibility_result.extra_fields)
        # The csv column rides through the llm transform AND the exploder, so it
        # is a definite extra too — the walk is not special-cased to llm metadata.
        assert extras == _METADATA_EXTRAS | {"topic"}

    def test_consumed_source_field_is_not_reported_as_an_extra(self) -> None:
        """The removal set is load-bearing, not decoration.

        ``line_explode`` consumes ``announcement``; a walk that only unioned
        upstream emits would name it here and propose dropping a field the
        transform already removed — a false repair for an LLM authoring loop.
        """
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(_locked_text_sink_options()),
            sink_name="sentence_rows",
        )

        assert _RESPONSE_FIELD not in error.compatibility_result.extra_fields

    def test_sink_admitting_the_metadata_extras_still_builds(self) -> None:
        """The two green g11 samples: a consumer whose schema tolerates the trio.

        Pins that the fix rejects on ARRIVAL, not on the presence of an llm
        upstream — otherwise it would break every working llm pipeline. The
        sink stays LOCKED (``mode: fixed`` still means ``extra='forbid'``) and
        declares the metadata OPTIONAL, which is the boundary the extras check
        documents: admitted = every declared model field, required or optional.
        Declaring them required instead would trade this defect for the
        opposite one — a sink demanding fields ``line_explode`` never
        guarantees.
        """
        sink_options = {
            "path": "outputs/announcement_sentences.txt",
            "field": "sentence",
            "schema": {
                "mode": "fixed",
                "fields": ["sentence: str", f"{_RESPONSE_FIELD}_usage: any?", f"{_RESPONSE_FIELD}_model: str?"],
            },
            "mode": "write",
            "collision_policy": "auto_increment",
        }
        _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(sink_options),
            sink_name="sentence_rows",
        )


class TestDefiniteEmitsIsSeparateFromTheGuaranteeWalk:
    """The two walks answer different questions and must keep different answers."""

    def _explode_graph(self) -> ExecutionGraph:
        return _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="sentence_rows",
                    options=_line_explode_options(),
                )
            ],
            sink=TextSink(
                {
                    "path": "outputs/o.txt",
                    "field": "sentence",
                    "schema": {"mode": "flexible", "fields": ["sentence: str"]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                }
            ),
            sink_name="sentence_rows",
        )

    def _explode_node(self, graph: ExecutionGraph) -> str:
        return next(node_id for node_id in graph._graph.nodes if graph.get_node_info(node_id).plugin_name == "line_explode")

    def test_presence_walk_is_unchanged_at_a_forwarding_node(self) -> None:
        """The permissive direction must NOT widen.

        ``get_effective_guaranteed_fields`` feeds sink required-field
        clearance, ``check_compatibility``'s missing-arm forgiveness, and the
        forgiven-field type walk — all of which read a wider set as PERMISSION.
        Widening it to close this gap would have loosened three gates to fix one.
        """
        graph = self._explode_graph()
        explode_node = self._explode_node(graph)

        assert get_effective_guaranteed_fields(graph, explode_node) == frozenset({"sentence"})

    def test_extras_walk_sees_the_forwarded_metadata(self) -> None:
        graph = self._explode_graph()
        explode_node = self._explode_node(graph)

        assert get_definite_emitted_fields(graph, explode_node) == frozenset({"sentence"}) | _METADATA_EXTRAS

    def test_extras_walk_is_additive_over_the_presence_walk(self) -> None:
        """Additivity is what makes the fix incapable of un-rejecting a graph."""
        graph = self._explode_graph()

        for node_id in graph._graph.nodes:
            assert get_definite_emitted_fields(graph, node_id) >= get_effective_guaranteed_fields(graph, node_id)


class TestExtrasSurviveRoutingNodes:
    """Pure-routing nodes must not truncate the definite-emits walk.

    A gate (and a row_union barrier) changes which rows travel an edge, never
    which fields a row carries — the traversal rule the composer's
    ``_connection_definite_emits`` already applies. The DAG walk used to stop
    at these nodes (``forwards_input_fields`` is a transform-only
    declaration), so one interposed gate reopened the exact
    elspeth-15c72686f2 hole this file exists to close: build green, every row
    dead at the locked sink's per-row preflight.
    """

    def _gate_graph(self, sink: Any) -> ExecutionGraph:
        return _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    LineExplode(_line_explode_options()),
                    name="exploded",
                    plugin_name="line_explode",
                    input_conn="brief",
                    on_success="exploded_rows",
                    options=_line_explode_options(),
                )
            ],
            gates=[
                GateSettings(
                    name="quality_gate",
                    input="exploded_rows",
                    condition="True",
                    routes={"true": "sentence_rows", "false": "sentence_rows"},
                )
            ],
            sink=sink,
            sink_name="sentence_rows",
        )

    def test_gate_between_exploder_and_locked_sink_is_rejected(self) -> None:
        """The reported graph with one gate interposed must still be rejected."""
        with pytest.raises(EdgeContractError) as excinfo:
            self._gate_graph(TextSink(_locked_text_sink_options()))
        error = excinfo.value
        assert error.compatibility_result is not None, f"not an extras rejection: {error}"
        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS

    def test_definite_emits_walks_through_a_gate(self) -> None:
        graph = self._gate_graph(
            TextSink(
                {
                    "path": "outputs/o.txt",
                    "field": "sentence",
                    "schema": {"mode": "flexible", "fields": ["sentence: str"]},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                }
            )
        )
        gate_node = next(n.node_id for n in graph.get_nodes() if n.node_type is NodeType.GATE)

        assert get_definite_emitted_fields(graph, gate_node) == frozenset({"sentence"}) | _METADATA_EXTRAS
        # Additivity must survive the traversal: a gate's definite emits may
        # only ever widen the presence answer, never contradict it.
        for node_id in graph._graph.nodes:
            assert get_definite_emitted_fields(graph, node_id) >= get_effective_guaranteed_fields(graph, node_id)

    def test_row_union_unions_arm_definite_emits_into_a_locked_sink_rejection(self) -> None:
        """A field definitely arriving on ONE arm's rows definitely arrives.

        Two exploder arms feed a row_union: the presence walk correctly
        INTERSECTS arm guarantees ({sentence}), but the extras question is the
        opposite polarity — the llm metadata rides every arm's rows, so the
        locked sink downstream of the barrier is a definite per-row death.
        This mirrors ``_row_union_definite_emits`` on the composer side. A
        row_union may not release directly into a sink (v1 constraint), so a
        second exploder consumes the released stream — which also pins that
        the arm-union output keeps flowing through a downstream forwarding
        transform's removal set (``sentence`` is consumed, the metadata is not).
        """
        explode_a = _line_explode_options()
        explode_b = _line_explode_options()
        clause_explode = {
            "source_field": "sentence",
            "output_field": "clause",
            "include_index": False,
            "schema": {"mode": "observed"},
        }
        with pytest.raises(EdgeContractError) as excinfo:
            _build_graph(
                source_plugin=LLMSource(_llm_source_options()),
                source_plugin_name="llm",
                source_options=_llm_source_options(),
                source_connection="brief",
                transforms=[
                    _wired(
                        LineExplode(explode_a),
                        name="exploded_a",
                        plugin_name="line_explode",
                        input_conn="branch_a",
                        on_success="a_out",
                        options=explode_a,
                    ),
                    _wired(
                        LineExplode(explode_b),
                        name="exploded_b",
                        plugin_name="line_explode",
                        input_conn="branch_b",
                        on_success="b_out",
                        options=explode_b,
                    ),
                    _wired(
                        LineExplode(clause_explode),
                        name="clauses",
                        plugin_name="line_explode",
                        input_conn="union_out",
                        on_success="clause_rows",
                        options=clause_explode,
                    ),
                ],
                gates=[
                    GateSettings(
                        name="variant_fork",
                        input="brief",
                        condition="True",
                        routes={"true": "fork", "false": "fork"},
                        fork_to=["branch_a", "branch_b"],
                    )
                ],
                row_union_settings=[
                    RowUnionSettings(name="variant_union", branches={"branch_a": "a_out", "branch_b": "b_out"}, on_success="union_out"),
                ],
                sink=TextSink(_locked_text_sink_options(field="clause")),
                sink_name="clause_rows",
            )
        error = excinfo.value
        assert error.compatibility_result is not None, f"not an extras rejection: {error}"
        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS


class TestForwardingParityAcrossTheDeclaringClass:
    """line_explode is one of four; the walk must not be exploder-specific."""

    def test_field_mapper_without_select_only_forwards_upstream_extras(self) -> None:
        """``select_only: false`` deep-copies the whole row, so extras ride through.

        This is also the shape the extras error RECOMMENDS as a repair
        ("insert a field_mapper with select_only: true"), which makes getting
        the non-select_only case right load-bearing for the advice.
        """
        mapper_options = {
            "mapping": {_RESPONSE_FIELD: "body"},
            "select_only": False,
            "schema": {"mode": "observed"},
        }
        error = _expect_rejected(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    FieldMapper(mapper_options),
                    name="mapped",
                    plugin_name="field_mapper",
                    input_conn="brief",
                    on_success="body_rows",
                    options=mapper_options,
                )
            ],
            sink=TextSink(_locked_text_sink_options(field="body")),
            sink_name="body_rows",
        )

        assert frozenset(error.compatibility_result.extra_fields) == _METADATA_EXTRAS

    def test_field_mapper_with_select_only_still_drops_the_extras(self) -> None:
        """The documented repair must actually repair.

        ``select_only: true`` emits only the mapping targets, so it declares no
        forwarding at all and the graph builds — if this regressed, every extras
        error in the tree would be proposing a fix that does not work.
        """
        mapper_options = {
            "mapping": {_RESPONSE_FIELD: "body"},
            "select_only": True,
            "schema": {"mode": "observed"},
        }
        _build_graph(
            source_plugin=LLMSource(_llm_source_options()),
            source_plugin_name="llm",
            source_options=_llm_source_options(),
            source_connection="brief",
            transforms=[
                _wired(
                    FieldMapper(mapper_options),
                    name="mapped",
                    plugin_name="field_mapper",
                    input_conn="brief",
                    on_success="body_rows",
                    options=mapper_options,
                )
            ],
            sink=TextSink(_locked_text_sink_options(field="body")),
            sink_name="body_rows",
        )


class TestForwardingRespectsTheNodesOwnExtrasFirewall:
    """A forwarding node whose own output contract forbids extras stops the walk.

    The composer computes ``propagates = forwards and not extras_firewall``
    (``_producer_emit_profile``): a node with a ``mode: fixed`` output schema
    emits EXACTLY its declared fields — rows either match that set or die at
    the node's own preflight, never downstream of it — so unioning upstream
    arrivals past it predicts fields that provably cannot arrive, and the two
    authoring surfaces hand the same graph opposite verdicts. Hand-built
    graph: the declaring plugins cannot author this shape (their schema option
    feeds input and output alike), but the walk must hold for any NodeInfo
    the public ``add_node`` surface can stamp.
    """

    def _firewalled_forwarder_graph(self) -> ExecutionGraph:
        from elspeth.contracts.schema import SchemaConfig

        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="mock_source",
            output_schema_config=SchemaConfig.from_dict(
                {"mode": "observed", "guaranteed_fields": ["announcement", "announcement_usage", "announcement_model"]}
            ),
        )
        graph.add_node(
            "firewalled",
            node_type=NodeType.TRANSFORM,
            plugin_name="mock_forwarder",
            output_schema_config=SchemaConfig.from_dict({"mode": "fixed", "fields": ["sentence: str"]}),
            forwards_input_fields=True,
            removed_input_fields=frozenset({"announcement"}),
        )
        graph.add_edge("src", "firewalled", label="continue")
        return graph

    def test_firewalled_forwarder_does_not_propagate_upstream_extras(self) -> None:
        graph = self._firewalled_forwarder_graph()

        assert get_definite_emitted_fields(graph, "firewalled") == frozenset({"sentence"})

    def test_extras_allowing_forwarder_still_propagates(self) -> None:
        """Control: relax only the firewall and the same graph propagates."""
        from elspeth.contracts.schema import SchemaConfig

        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="mock_source",
            output_schema_config=SchemaConfig.from_dict(
                {"mode": "observed", "guaranteed_fields": ["announcement", "announcement_usage", "announcement_model"]}
            ),
        )
        graph.add_node(
            "forwarder",
            node_type=NodeType.TRANSFORM,
            plugin_name="mock_forwarder",
            output_schema_config=SchemaConfig.from_dict({"mode": "flexible", "fields": ["sentence: str"]}),
            forwards_input_fields=True,
            removed_input_fields=frozenset({"announcement"}),
        )
        graph.add_edge("src", "forwarder", label="continue")

        assert get_definite_emitted_fields(graph, "forwarder") == frozenset({"sentence", "announcement_usage", "announcement_model"})


class TestDefiniteEmitsWalkIsLinear:
    """Bulk validation must not re-walk ancestry once per node/edge.

    ``walk_effective_guaranteed_fields``'s own docstring prescribes a shared
    bulk-validation cache (the ``validate_sink_required_fields`` discipline);
    the definite-emits walk consumes the presence answer at EVERY visited
    node, so allocating a fresh presence cache per node makes build
    validation quadratic in chain depth — once per edge on top of that when
    each ``_validate_locked_consumer_guaranteed_extras`` call starts cold.
    """

    _CHAIN = 30

    def _chain_graph(self) -> ExecutionGraph:
        from elspeth.contracts.schema import SchemaConfig

        graph = ExecutionGraph()
        graph.add_node(
            "src",
            node_type=NodeType.SOURCE,
            plugin_name="mock_source",
            output_schema_config=SchemaConfig.from_dict({"mode": "observed", "guaranteed_fields": ["seed"]}),
        )
        previous = "src"
        for index in range(self._CHAIN):
            node_id = f"t{index}"
            graph.add_node(
                node_id,
                node_type=NodeType.TRANSFORM,
                plugin_name="mock_forwarder",
                output_schema_config=SchemaConfig.from_dict({"mode": "observed"}),
                # Both flags: passes_through makes the PRESENCE vote recurse
                # through the whole ancestry (the expensive walk), forwards
                # makes the DEFINITE walk visit every node — the combination
                # that exposes per-node presence re-walking as quadratic.
                passes_through_input=True,
                forwards_input_fields=True,
            )
            graph.add_edge(previous, node_id, label="continue")
            previous = node_id
        return graph

    def test_one_walk_computes_each_presence_vote_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.core.dag import guarantees

        graph = self._chain_graph()
        real_vote = guarantees.walk_effective_guarantee_vote
        misses = 0

        def counting_vote(*args: Any, **kwargs: Any) -> Any:
            nonlocal misses
            node_id, cache = args[1], args[2]
            if node_id not in cache:
                misses += 1
            return real_vote(*args, **kwargs)

        monkeypatch.setattr(guarantees, "walk_effective_guarantee_vote", counting_vote)

        assert get_definite_emitted_fields(graph, f"t{self._CHAIN - 1}") == frozenset({"seed"})
        # Each of the CHAIN+1 nodes' votes computed at most twice (once is the
        # target; the slack tolerates one wrapper layer) — quadratic re-walking
        # would be ~CHAIN^2/2 ≈ 465 misses here.
        assert misses <= 2 * (self._CHAIN + 1), f"presence walk recomputed: {misses} cache misses"

    def test_bulk_callers_can_share_a_cache_across_edges(self) -> None:
        from elspeth.core.dag import guarantees

        graph = self._chain_graph()
        caches = guarantees.DefiniteEmitsCaches()

        first = guarantees.get_definite_emitted_fields(graph, f"t{self._CHAIN - 1}", caches=caches)
        assert first == frozenset({"seed"})
        # A second bulk query anywhere on the chain resolves entirely from the
        # shared cache — the property the per-edge call sites rely on.
        assert f"t{self._CHAIN // 2}" in caches.fields
        assert guarantees.get_definite_emitted_fields(graph, f"t{self._CHAIN // 2}", caches=caches) == frozenset({"seed"})


class TestForwardingDeclarationsMatchPluginBehaviour:
    """Unit-level pins on the four declarations the walk trusts."""

    def test_line_explode_declares_its_consumed_source_field(self) -> None:
        transform = LineExplode(_line_explode_options())

        assert transform.forwards_input_fields is True
        assert transform.removed_input_fields == frozenset({_RESPONSE_FIELD})

    def test_field_mapper_abstains_on_an_unresolved_original_header(self) -> None:
        """An original header removes a name only ``resolve_name`` knows at runtime.

        Declaring forwarding with an unnameable removal would predict a field
        that never arrives — a FALSE build-time rejection of a working rename
        pipeline, the one failure direction over-declaration cannot produce.
        """
        transform = FieldMapper(
            {
                "mapping": {"First Name": "given_name"},
                "select_only": False,
                "schema": {"mode": "observed"},
            }
        )

        assert transform.forwards_input_fields is False

    def test_field_mapper_abstains_on_an_identity_mapped_original_header(self) -> None:
        """Identity mappings are not exempt from the original-header abstention.

        ``{"First Name": "First Name"}`` looks like a no-op, but process()
        resolves the source through the contract, deletes the NORMALIZED
        ``first_name`` key, and writes the literal ``"First Name"`` key — so a
        normalized input field is removed under a name only ``resolve_name``
        knows at runtime. Declaring forwarding here under-states the removal
        set, the one direction the design forbids (a FALSE build-time
        rejection of a working pipeline).
        """
        transform = FieldMapper(
            {
                "mapping": {"First Name": "First Name"},
                "select_only": False,
                "schema": {"mode": "observed"},
            }
        )

        assert transform.forwards_input_fields is False

    def test_field_mapper_dotted_source_removes_nothing(self) -> None:
        """A dotted source is a nested READ; its root field survives in the output."""
        transform = FieldMapper(
            {
                "mapping": {"meta.author": "author"},
                "select_only": False,
                "schema": {"mode": "observed"},
            }
        )

        assert transform.forwards_input_fields is True
        assert transform.removed_input_fields == frozenset()
