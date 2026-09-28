"""What each node does to the names its rows carry, at build time (review-B2-template-residuals-r1 F1 + F5).

``FieldNameResolution.past_node`` follows a node's renames, the fields it
creates FRESH (``freshly_created_fields``) and the fields it provably drops.
Two consumers read the result in opposite directions, so both are pinned:

- the DECLARATION rule (``validate_declared_field_spellings``) refuses a
  spelling some leg resolves — a stale alias of a dropped field must not
  refuse an unrelated later field (F5), and a header spelling of a created
  field is still refused (no regression);
- the LOOKUP rule (``validate_spelled_row_lookups_reachable``) refuses a
  spelled lookup no leg a run-time lookup honours resolves — a created field
  records only its own name (F1), while a source header stays admitted and
  a fan-in with one header-carrying path admits.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.enums import NodeType, RoutingMode
from elspeth.contracts.field_spelling import (
    ANY_HEADER_CARRIERS,
    NO_HEADER_CARRIERS,
    NO_SOURCE_RENAMES,
    NORMALIZATION_ONLY,
    OWN_NAMES_ONLY,
    DeclaredName,
    FieldNameResolution,
    HeaderCarriers,
    SourceFieldRenames,
    freshly_created_fields,
    keeps_input_field_contracts,
    unreachable_spelled_lookups,
)
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.dag import schema_validation
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.core.dag.models import GraphValidationError, NodeInfo

_SOURCE_FIELDS = ["id: str", "name: str", "c: str"]


def _add_transform(graph: ExecutionGraph, node_id: str, **kwargs: Any) -> None:
    schema = kwargs.pop("schema", {"mode": "observed"})
    graph.add_node(node_id, node_type=NodeType.TRANSFORM, plugin_name=kwargs.pop("plugin", "t"), config={"schema": schema}, **kwargs)


def _chain(
    *transforms: tuple[str, dict[str, Any]],
    source_schema: dict[str, Any] | None = None,
    source_renames: SourceFieldRenames = NO_SOURCE_RENAMES,
    sink_reads: frozenset[str] = frozenset(),
) -> ExecutionGraph:
    """src -> each transform in order -> sink."""
    graph = ExecutionGraph()
    graph.add_node(
        "src",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        config={"schema": source_schema or {"mode": "fixed", "fields": _SOURCE_FIELDS}},
        field_renames=source_renames,
    )
    previous = "src"
    for node_id, kwargs in transforms:
        _add_transform(graph, node_id, **kwargs)
        graph.add_edge(previous, node_id, label="continue", mode=RoutingMode.MOVE)
        previous = node_id
    graph.add_node(
        "sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}}, declared_read_fields=sink_reads
    )
    graph.add_edge(previous, "sink", label="out", mode=RoutingMode.MOVE)
    return graph


# The builder assigns each transform its computed output schema; these tests
# set it directly as the output schema config the resolution reads.
def _fixed(*fields: str) -> dict[str, Any]:
    return {"mode": "fixed", "fields": list(fields)}


_RENAME_NAME_TO_B = (
    "a_rename",
    {
        "renamed_input_fields": {"name": "b"},
        "declared_output_fields": frozenset({"b"}),
        "forwards_input_fields": True,
        "removed_input_fields": frozenset({"name"}),
    },
)
_RENAME_C_TO_B = (
    "b_rename",
    {
        "renamed_input_fields": {"c": "b"},
        "declared_output_fields": frozenset({"b"}),
        "forwards_input_fields": True,
        "removed_input_fields": frozenset({"c"}),
    },
)
_CREATE_NAME = ("make_name", {"declared_created_fields": frozenset({"name"}), "passes_through_input": True})


def _llm_like(node_id: str, created: str, **extra: Any) -> tuple[str, dict[str, Any]]:
    """A pass-through enricher that adds ``created`` (the executor's collision preflight guards it)."""
    return (
        node_id,
        {
            "declared_output_fields": frozenset({created}),
            "declared_created_fields": frozenset({created}),
            "passes_through_input": True,
            **extra,
        },
    )


def _with_output_schema(graph: ExecutionGraph, node_id: str, schema: dict[str, Any]) -> None:
    """Assign ``node_id`` the output schema config the builder would compute for it."""
    info = graph.get_node_info(node_id)
    graph._graph.nodes[node_id]["info"] = replace(info, output_schema_config=SchemaConfig.from_dict(schema))


class TestAliasOfADroppedField:
    """F5: a rename's alias dies with the renamed field."""

    def _linear(self, *, drop_keeps_b: bool) -> ExecutionGraph:
        graph = _chain(_RENAME_NAME_TO_B, ("a_drop", {}), _RENAME_C_TO_B, _CREATE_NAME)
        _with_output_schema(graph, "a_drop", _fixed("id: str", "b: str", "c: str") if drop_keeps_b else _fixed("id: str", "c: str"))
        # The created name is checked against a PARTICIPATING vote that carries b.
        _with_output_schema(graph, "b_rename", _fixed("id: str", "b: str"))
        return graph

    def test_a_field_named_like_a_dropped_alias_builds(self) -> None:
        schema_validation.validate_declared_field_spellings(self._linear(drop_keeps_b=False))

    def test_the_alias_still_refuses_while_the_renamed_field_lives(self) -> None:
        """Control: a drop that keeps b keeps name's identity on it (the c -> b rename would then collide at run time)."""
        graph = _chain(_RENAME_NAME_TO_B, _CREATE_NAME)
        _with_output_schema(graph, "a_rename", _fixed("id: str", "b: str", "c: str"))
        with pytest.raises(GraphValidationError, match="'name' is a header spelling of the arriving field 'b'"):
            schema_validation.validate_declared_field_spellings(graph)

    def test_a_named_removal_drops_the_alias_too(self) -> None:
        drop = ("drop_b", {"forwards_input_fields": True, "removed_input_fields": frozenset({"b"})})
        graph = _chain(_RENAME_NAME_TO_B, drop, _RENAME_C_TO_B, _CREATE_NAME)
        _with_output_schema(graph, "b_rename", _fixed("id: str", "b: str"))
        schema_validation.validate_declared_field_spellings(graph)

    def test_an_open_output_keeps_every_alias(self) -> None:
        """An output that admits undeclared fields proves no drop: the alias stays (never under-approximate)."""
        graph = self._linear(drop_keeps_b=False)
        _with_output_schema(graph, "a_drop", {"mode": "flexible", "fields": ["id: str", "c: str"]})
        with pytest.raises(GraphValidationError, match="'name' is a header spelling of the arriving field 'b'"):
            schema_validation.validate_declared_field_spellings(graph)

    def test_an_optional_declared_field_is_not_dropped(self) -> None:
        """The closed bound is the DECLARED fields, optional ones included, not the guaranteed lower bound."""
        graph = self._linear(drop_keeps_b=False)
        _with_output_schema(graph, "a_drop", _fixed("id: str", "b: str?", "c: str"))
        with pytest.raises(GraphValidationError, match="'name' is a header spelling of the arriving field 'b'"):
            schema_validation.validate_declared_field_spellings(graph)


class TestLookupOfACreatedField:
    """F1: a spelled lookup of a field a transform created fresh can never resolve."""

    def _consumer(self, lookups: dict[str, str]) -> tuple[str, dict[str, Any]]:
        return ("reader", {"header_spelled_lookups": lookups, "passes_through_input": True})

    def test_a_header_spelling_of_a_created_field_is_refused(self) -> None:
        graph = _chain(_llm_like("llm_pre", "score_text"), self._consumer({"Score_Text": "score_text"}))
        with pytest.raises(GraphValidationError, match="Unreachable header spelling") as caught:
            schema_validation.validate_spelled_row_lookups_reachable(graph)
        message = str(caught.value)
        assert "'Score_Text' reads 'score_text'" in message
        assert (
            "or any field a statistics-style aggregation or collector emits, its own name), "
            "so a lookup of 'Score_Text' reads nothing. Read it as row['score_text']"
        ) in message
        assert "reader" in message

    def test_a_header_spelling_of_a_source_field_is_admitted(self) -> None:
        """S-02: a source header may carry the spelling; the row decides."""
        schema_validation.validate_spelled_row_lookups_reachable(
            _chain(_llm_like("llm_pre", "score_text"), self._consumer({"Name": "name"}))
        )

    def test_a_spelling_of_a_renamed_away_header_is_refused(self) -> None:
        """name -> b took the header 'Name' with it: 'Name' reads b, never a later 'name'."""
        graph = _chain(_RENAME_NAME_TO_B, self._consumer({"Name": "name"}))
        with pytest.raises(GraphValidationError, match="renames the field a header spelled 'Name' names away from 'name'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)

    def test_a_fresh_row_writer_creates_nothing_fresh(self) -> None:
        """A transform that builds a fresh row runs no collision preflight: an output of an input's name keeps its original.

        Behind an OPEN source the input may carry a 'Score_Text' header, so the row decides.
        """
        writer = ("writer", {"declared_output_fields": frozenset({"score_text"}), "declared_created_fields": frozenset({"score_text"})})
        schema_validation.validate_spelled_row_lookups_reachable(
            _chain(writer, self._consumer({"Score_Text": "score_text"}), source_schema={"mode": "observed"})
        )

    def test_a_rename_target_is_no_header_carrier(self) -> None:
        """It carries the renamed field's recorded original (the ``carried`` leg), never a header spelling of its own name."""
        graph = _chain(_RENAME_NAME_TO_B, source_schema={"mode": "observed"})
        resolution = schema_validation._output_name_resolution(graph, "a_rename", {})
        assert not resolution.header_carriers.carries("b")
        assert resolution.header_carriers.carries("name")

    def test_the_declaration_rule_still_refuses_a_header_spelling_of_a_created_field(self) -> None:
        """No regression: a sink declaring 'Score_Text' behind a closed output that carries score_text is refused."""
        graph = _chain(_llm_like("llm_pre", "score_text"), sink_reads=frozenset({"Score_Text"}))
        _with_output_schema(graph, "llm_pre", _fixed("id: str", "name: str", "c: str", "score_text: str"))
        with pytest.raises(GraphValidationError, match="'Score_Text' is a header spelling of 'score_text'"):
            schema_validation.validate_declared_field_spellings(graph)


class TestLookupOfAFieldNoHeaderReaches:
    """review-B2-fix-r1 M1: a field no source header can name reaches the node recorded under its own name only.

    Each shape validated green and failed every row; release refused each at config.
    """

    _LOOKUP = ("reader", {"header_spelled_lookups": {"Score_Text": "score_text"}, "passes_through_input": True})

    def test_behind_a_headerless_source_the_lookup_is_refused(self) -> None:
        """X1: a headerless source records each column as written, so no row carries 'Score_Text' for score_text."""
        graph = _chain(
            self._LOOKUP,
            source_schema=_fixed("id: str", "score_text: str"),
            source_renames=SourceFieldRenames(mapping={}, keys="as_written"),
        )
        with pytest.raises(GraphValidationError, match="'Score_Text' reads 'score_text'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)

    def test_behind_a_headered_source_the_same_lookup_is_admitted(self) -> None:
        """Control for X1: a header row may spell it 'Score_Text'; the row decides (S-02)."""
        schema_validation.validate_spelled_row_lookups_reachable(_chain(self._LOOKUP, source_schema=_fixed("id: str", "score_text: str")))

    def test_a_field_a_value_writer_adds_behind_a_closed_source_is_refused(self) -> None:
        """X2: value_transform creates nothing fresh (a target may overwrite), but a fixed source provably lacks score_text."""
        writer = ("vt", {"passes_through_input": True})
        graph = _chain(writer, self._LOOKUP, source_schema=_fixed("id: str", "name: str"))
        with pytest.raises(GraphValidationError, match="'Score_Text' reads 'score_text'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)

    def test_a_value_writer_behind_an_open_source_is_admitted(self) -> None:
        """Control for X2: an open source may carry a 'Score_Text' header the target overwrites, keeping its original."""
        writer = ("vt", {"passes_through_input": True})
        schema_validation.validate_spelled_row_lookups_reachable(_chain(writer, self._LOOKUP, source_schema={"mode": "observed"}))

    def test_an_aggregation_output_behind_a_closed_source_is_refused(self) -> None:
        """X3: batch_stats' mean is no field of the fixed source, so no header names it."""
        graph = ExecutionGraph()
        graph.add_node("src", node_type=NodeType.SOURCE, plugin_name="csv", config={"schema": _fixed("id: int", "amount: float")})
        graph.add_node("agg", node_type=NodeType.AGGREGATION, plugin_name="batch_stats", config={"schema": {"mode": "observed"}})
        _add_transform(graph, "reader", header_spelled_lookups={"Mean": "mean"}, passes_through_input=True)
        graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
        graph.add_edge("src", "agg", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("agg", "reader", label="continue", mode=RoutingMode.MOVE)
        graph.add_edge("reader", "sink", label="out", mode=RoutingMode.MOVE)
        with pytest.raises(GraphValidationError, match="'Mean' reads 'mean'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)

    def test_a_source_field_mapping_target_is_refused(self) -> None:
        """X5: {name: given} records the header of 'name' on given, so a 'Given' lookup reads nothing."""
        consumer = ("reader", {"header_spelled_lookups": {"Given": "given", "ID": "id"}, "passes_through_input": True})
        graph = _chain(
            consumer,
            source_schema={"mode": "flexible", "fields": ["id: int", "given: str"]},
            source_renames=SourceFieldRenames(mapping={"name": "given"}, keys="normalized", normalizes_external_names=True),
        )
        with pytest.raises(GraphValidationError, match="'Given' reads 'given'") as caught:
            schema_validation.validate_spelled_row_lookups_reachable(graph)
        assert "'ID'" not in str(caught.value)

    def test_a_closed_output_bounds_the_carriers(self) -> None:
        """A field outside a node's closed output is gone: a later writer of that name records its own name."""
        writer = ("vt", {"passes_through_input": True})
        graph = _chain(("shaper", {}), writer, self._LOOKUP, source_schema=_fixed("id: str", "score_text: str"))
        _with_output_schema(graph, "shaper", _fixed("id: str"))
        with pytest.raises(GraphValidationError, match="'Score_Text' reads 'score_text'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)

    def test_a_closed_output_that_keeps_the_field_keeps_its_header(self) -> None:
        """Control: the shaper keeps score_text, whose contract (and header original) it hands on."""
        writer = ("vt", {"passes_through_input": True})
        graph = _chain(("shaper", {}), writer, self._LOOKUP, source_schema=_fixed("id: str", "score_text: str"))
        _with_output_schema(graph, "shaper", _fixed("id: str", "score_text: str"))
        schema_validation.validate_spelled_row_lookups_reachable(graph)


def _batch_chain(node_type: NodeType, **batch: Any) -> ExecutionGraph:
    """OPEN headered src -> one batch node -> a reader of row['Mean'] under [mean] -> sink."""
    graph = ExecutionGraph()
    graph.add_node(
        "src",
        node_type=NodeType.SOURCE,
        plugin_name="csv",
        config={"schema": {"mode": "observed"}},
        field_renames=SourceFieldRenames(mapping={"name": "given"}, keys="normalized", normalizes_external_names=True),
    )
    graph.add_node("batch", node_type=node_type, plugin_name="batch_stats", config={"schema": {"mode": "observed"}}, **batch)
    _add_transform(graph, "reader", header_spelled_lookups={"Mean": "mean"}, passes_through_input=True)
    graph.add_node("sink", node_type=NodeType.SINK, plugin_name="json", config={"schema": {"mode": "observed"}})
    graph.add_edge("src", "batch", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("batch", "reader", label="continue", mode=RoutingMode.MOVE)
    graph.add_edge("reader", "sink", label="out", mode=RoutingMode.MOVE)
    return graph


class TestLookupPastABatchOutput:
    """A reductive batch output records every field under its own name, whatever its input carried.

    Behind an OPEN source (x4, and x7 whose csv even has a 'Mean' header) and
    past a collector (x6) the lookup validated green and failed every row;
    release refused each at config.
    """

    @pytest.mark.parametrize("node_type", [NodeType.AGGREGATION, NodeType.COLLECTOR])
    def test_a_reductive_batch_output_is_refused(self, node_type: NodeType) -> None:
        graph = _batch_chain(node_type)
        with pytest.raises(GraphValidationError, match="'Mean' reads 'mean'"):
            schema_validation.validate_spelled_row_lookups_reachable(graph)
        assert schema_validation._output_name_resolution(graph, "batch", {}) == OWN_NAMES_ONLY

    @pytest.mark.parametrize("node_type", [NodeType.AGGREGATION, NodeType.COLLECTOR])
    @pytest.mark.parametrize("carrying", [{"passes_through_input": True}, {"forwards_input_fields": True}])
    def test_a_batch_output_that_keeps_its_input_contracts_is_admitted(self, node_type: NodeType, carrying: dict[str, bool]) -> None:
        """Control: batch_replicate / batch_rank / batch_outlier_annotator merge their input contracts."""
        graph = _batch_chain(node_type, **carrying)
        schema_validation.validate_spelled_row_lookups_reachable(graph)
        assert ("given", "renamed") in list(schema_validation._output_name_resolution(graph, "batch", {}).resolve(DeclaredName.of("Name")))

    def test_every_leg_ends_at_a_reductive_batch_output(self) -> None:
        """The source's {name: given} identity does not survive it: 'Name' names only its own normalization, unreachably."""
        resolution = schema_validation._output_name_resolution(_batch_chain(NodeType.AGGREGATION), "batch", {})
        assert list(resolution.resolve(DeclaredName.of("Name"))) == [("name", "own_name")]

    @pytest.mark.parametrize(
        ("batch_output", "passes_through_input", "forwards_input_fields", "keeps"),
        [
            (False, False, False, True),  # a row-to-row transform narrows or extends its input contract
            (True, False, False, False),  # a reductive batch output builds its own
            (True, True, False, True),
            (True, False, True, True),
        ],
    )
    def test_keeps_input_field_contracts(
        self, batch_output: bool, passes_through_input: bool, forwards_input_fields: bool, keeps: bool
    ) -> None:
        assert (
            keeps_input_field_contracts(
                batch_output=batch_output, passes_through_input=passes_through_input, forwards_input_fields=forwards_input_fields
            )
            is keeps
        )


class TestHeaderCarriers:
    """The positive fact the normalization leg rests on: which fields a row may carry under a header's spelling."""

    def test_a_headerless_source_carries_none(self) -> None:
        assert HeaderCarriers.of_source(SourceFieldRenames(mapping={}, keys="as_written"), None) == NO_HEADER_CARRIERS

    def test_a_closed_headered_source_carries_its_fields_but_its_mapping_targets(self) -> None:
        carriers = HeaderCarriers.of_source(
            SourceFieldRenames(mapping={"name": "given"}, keys="normalized", normalizes_external_names=True), {"id", "given"}
        )
        assert carriers == HeaderCarriers(names=frozenset({"id"}), all_but=False)

    def test_an_open_headered_source_carries_all_but_its_mapping_targets(self) -> None:
        carriers = HeaderCarriers.of_source(
            SourceFieldRenames(mapping={"name": "given"}, keys="normalized", normalizes_external_names=True), None
        )
        assert carriers.carries("anything")
        assert not carriers.carries("given")

    @pytest.mark.parametrize(
        ("parts", "carried", "not_carried"),
        [
            # A bounded path and an open one: any name the open one may carry, or the bounded one does.
            (
                (HeaderCarriers(frozenset({"a"}), all_but=False), HeaderCarriers(frozenset({"a", "b"}), all_but=True)),
                {"a", "z"},
                {"b"},
            ),
            ((HeaderCarriers(frozenset({"a"}), all_but=False), HeaderCarriers(frozenset({"c"}), all_but=False)), {"a", "c"}, {"b"}),
            ((HeaderCarriers(frozenset({"a"}), all_but=True), HeaderCarriers(frozenset({"b"}), all_but=True)), {"a", "b", "z"}, set()),
            ((HeaderCarriers(frozenset({"a"}), all_but=True), HeaderCarriers(frozenset({"a"}), all_but=True)), {"z"}, {"a"}),
        ],
    )
    def test_union_carries_what_any_path_carries(self, parts: tuple[HeaderCarriers, ...], carried: set[str], not_carried: set[str]) -> None:
        united = HeaderCarriers.union(parts)
        for name in carried | not_carried:
            assert united.carries(name) == any(part.carries(name) for part in parts) == (name in carried)

    def test_no_path_carries_every_name(self) -> None:
        assert HeaderCarriers.union(()) == ANY_HEADER_CARRIERS

    def test_within_intersects_a_closed_bound_and_ignores_an_open_one(self) -> None:
        open_carriers = HeaderCarriers(frozenset({"x"}), all_but=True)
        assert open_carriers.within(None) is open_carriers
        assert open_carriers.within({"x", "y"}) == HeaderCarriers(frozenset({"y"}), all_but=False)
        assert HeaderCarriers(frozenset({"x", "y"}), all_but=False).within({"y", "z"}) == HeaderCarriers(frozenset({"y"}), all_but=False)

    def test_without_takes_names_away_either_way(self) -> None:
        assert not HeaderCarriers(frozenset(), all_but=True).without({"x"}).carries("x")
        assert not HeaderCarriers(frozenset({"x"}), all_but=False).without({"x"}).carries("x")


class TestResolutionAlgebra:
    """The ``own_name`` leg through union and renames."""

    _SOURCE = FieldNameResolution.of_source(
        SourceFieldRenames(mapping={}, keys="normalized", normalizes_external_names=True), carried_out=None
    )

    def _created(self, name: str) -> FieldNameResolution:
        return self._SOURCE.past_node(renamed={}, created={name}, removed=(), carried_out=None, keeps_input_contracts=True)

    def test_the_normalization_leg_names_a_created_field_with_the_own_name_leg(self) -> None:
        assert list(self._created("score_text").resolve(DeclaredName.of("Score_Text"))) == [("score_text", "own_name")]

    def test_a_fan_in_with_one_header_carrying_path_admits_the_lookup(self) -> None:
        """Created on one arm only: rows from the other arm may carry a 'Score_Text' header."""
        united = FieldNameResolution.union((self._created("score_text"), self._SOURCE))
        assert united.header_carriers.carries("score_text")
        assert unreachable_spelled_lookups({"Score_Text": "score_text"}, united) == ()

    def test_created_on_every_path_is_carried_by_none(self) -> None:
        united = FieldNameResolution.union((self._created("score_text"), self._created("score_text")))
        assert not united.header_carriers.carries("score_text")
        assert [spelling.literal for spelling in unreachable_spelled_lookups({"Score_Text": "score_text"}, united)] == ["Score_Text"]

    def test_renaming_a_created_field_carries_it_as_an_ordinary_field(self) -> None:
        renamed = self._created("score_text").past_node(
            renamed={"score_text": "st"}, created=(), removed=(), carried_out=None, keeps_input_contracts=True
        )
        assert ("st", "carried") in list(renamed.resolve(DeclaredName.of("score_text")))

    def test_normalization_only_carries_every_name(self) -> None:
        assert NORMALIZATION_ONLY.header_carriers == ANY_HEADER_CARRIERS

    @pytest.mark.parametrize(("passes", "forwards", "expected"), [(True, False, {"x"}), (False, True, {"x"}), (False, False, set())])
    def test_freshly_created_fields_follows_the_overwrite_capability(self, passes: bool, forwards: bool, expected: set[str]) -> None:
        assert freshly_created_fields(
            declared_output_fields={"x", "b"}, renamed_input_fields={"a": "b"}, passes_through_input=passes, forwards_input_fields=forwards
        ) == frozenset(expected)


def test_header_spelled_lookups_are_transform_only() -> None:
    with pytest.raises(GraphValidationError, match="header_spelled_lookups is only meaningful for TRANSFORM nodes"):
        NodeInfo(node_id="s", node_type=NodeType.SINK, plugin_name="json", header_spelled_lookups={"Name": "name"})
