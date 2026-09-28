"""Tests for the BatchRank aggregation transform."""

from __future__ import annotations

import math
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_rank import BatchRank
from elspeth.testing import make_field, make_row
from tests.fixtures.factories import make_context

OBSERVED = {"mode": "observed"}


def _make_row(data: dict[str, Any]) -> PipelineRow:
    """A PipelineRow with an OBSERVED contract."""
    fields = tuple(
        make_field(key, type(value) if value is not None else object, original_name=key, required=False, source="inferred")
        for key, value in data.items()
    )
    return make_row(data, contract=SchemaContract(mode="OBSERVED", fields=fields, locked=True))


def _rank(values: list[Any], **options: Any) -> TransformResult:
    transform = BatchRank({"schema": OBSERVED, "value_field": "score", **options})
    return transform.process([_make_row({"id": index, "score": value}) for index, value in enumerate(values)], make_context())


def _column(result: TransformResult, name: str) -> list[Any]:
    assert result.status == "success"
    assert result.rows is not None
    return [row[name] for row in result.rows]


@pytest.fixture
def ctx() -> PluginContext:
    return make_context()


class TestDeclarations:
    def test_class_declarations(self) -> None:
        assert BatchRank.name == "batch_rank"
        assert BatchRank.is_batch_aware is True
        assert BatchRank.supports_row_mode_when_batch_aware is False
        assert BatchRank.flush_emits_one_row_per_buffered_row is True
        assert BatchRank.passes_through_input is True
        assert BatchRank.preserves_input_values is True
        assert BatchRank.creates_tokens is False

    def test_created_output_fields_are_typed_under_the_prefix(self) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score", "output_prefix": "judge"})
        created = {field.name: (field.field_type, field.nullable) for field in transform.created_output_fields()}
        assert created == {
            "judge_rank": ("int", True),
            "judge_percentile": ("float", True),
            "judge_ranked_count": ("int", False),
            "judge_batch_size": ("int", False),
        }
        assert transform.declared_output_fields == frozenset(created)

    def test_value_field_is_consumed_but_not_required(self) -> None:
        """An absent value_field is an unranked row, not a batch failure: it is not in required_fields."""
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        assert "score" not in transform.schema_required_input_fields()
        assert "score" in transform.consumed_input_fields


class TestRanking:
    def test_competition_ties_descending_by_default(self) -> None:
        result = _rank([7, 9, 9, 3])
        assert _column(result, "rank_rank") == [3, 1, 1, 4]

    def test_competition_ties_skip_the_next_rank(self) -> None:
        result = _rank([10, 8, 8, 5])
        assert _column(result, "rank_rank") == [1, 2, 2, 4]

    def test_dense_ties_do_not_skip(self) -> None:
        result = _rank([10, 8, 8, 5], ties="dense")
        assert _column(result, "rank_rank") == [1, 2, 2, 3]

    def test_ascending_ranks_the_lowest_first(self) -> None:
        assert _column(_rank([10, 8, 8, 5], order="ascending"), "rank_rank") == [4, 2, 2, 1]
        assert _column(_rank([10, 8, 8, 5], order="ascending", ties="dense"), "rank_rank") == [3, 2, 2, 1]

    def test_percentile_is_the_share_of_ranked_rows_strictly_worse(self) -> None:
        descending = _rank([10, 8, 8, 5])
        assert _column(descending, "rank_percentile") == [75.0, 25.0, 25.0, 0.0]
        ascending = _rank([10, 8, 8, 5], order="ascending")
        assert _column(ascending, "rank_percentile") == [0.0, 25.0, 25.0, 75.0]

    def test_percentile_does_not_depend_on_the_ties_mode(self) -> None:
        values = [4, 4, 2, 9, 2, 7]
        assert _column(_rank(values), "rank_percentile") == _column(_rank(values, ties="dense"), "rank_percentile")

    def test_int_and_float_are_compared_without_conversion(self) -> None:
        result = _rank([1, 1.0, 2.5])
        assert _column(result, "rank_rank") == [2, 2, 1]
        # The row's own value is carried exactly as it arrived.
        assert [type(value) for value in _column(result, "score")] == [int, float, float]

    def test_counts_on_every_row(self) -> None:
        result = _rank([3, None, 1])
        assert _column(result, "rank_ranked_count") == [2, 2, 2]
        assert _column(result, "rank_batch_size") == [3, 3, 3]


class TestUnrankedRows:
    @pytest.mark.parametrize("unranked", [None, math.inf, -math.inf, math.nan], ids=["null", "inf", "-inf", "nan"])
    def test_null_or_non_finite_value_passes_through_unranked(self, unranked: float | None) -> None:
        result = _rank([5, unranked, 9])
        assert _column(result, "id") == [0, 1, 2]
        assert _column(result, "rank_rank") == [2, None, 1]
        assert _column(result, "rank_percentile") == [0.0, None, 50.0]
        assert _column(result, "rank_ranked_count") == [2, 2, 2]
        assert _column(result, "rank_batch_size") == [3, 3, 3]

    def test_absent_value_field_passes_through_unranked(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [_make_row({"id": 0, "score": 2}), _make_row({"id": 1, "note": "no score"}), _make_row({"id": 2, "score": 4})]

        result = transform.process(rows, ctx)

        assert _column(result, "rank_rank") == [2, None, 1]
        assert result.rows is not None
        assert "score" not in result.rows[1]
        assert result.rows[1]["note"] == "no score"

    def test_all_unranked_batch_still_emits_every_row(self) -> None:
        result = _rank([None, math.nan])
        assert _column(result, "rank_rank") == [None, None]
        assert _column(result, "rank_percentile") == [None, None]
        assert _column(result, "rank_ranked_count") == [0, 0]


class TestShape:
    def test_one_row_batch_is_a_success_multi(self) -> None:
        result = _rank([42])
        assert result.status == "success"
        assert result.is_multi_row
        assert result.row is None
        assert _column(result, "rank_rank") == [1]
        assert _column(result, "rank_percentile") == [0.0]

    def test_no_quarantined_indices_are_declared(self) -> None:
        result = _rank([1, None, 3])
        assert result.success_reason is not None
        assert "metadata" not in result.success_reason

    def test_empty_batch_returns_error(self, ctx: PluginContext) -> None:
        result = BatchRank({"schema": OBSERVED, "value_field": "score"}).process([], ctx)
        assert result.status == "error"
        assert result.reason == {"reason": "empty_batch"}

    def test_excessive_batch_is_refused(self, ctx: PluginContext, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("elspeth.plugins.transforms.batch_rank._MAX_BATCH_ROWS", 2)
        rows = [_make_row({"score": value}) for value in (1, 2, 3)]
        result = BatchRank({"schema": OBSERVED, "value_field": "score"}).process(rows, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["cause"] == "batch_too_large"
        assert result.reason["batch_size"] == 3

    def test_batch_of_exactly_the_cap_is_ranked(self, ctx: PluginContext, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("elspeth.plugins.transforms.batch_rank._MAX_BATCH_ROWS", 2)
        rows = [_make_row({"score": value}) for value in (1, 2)]
        result = BatchRank({"schema": OBSERVED, "value_field": "score"}).process(rows, ctx)
        assert _column(result, "rank_rank") == [2, 1]

    @settings(max_examples=150, deadline=None)
    @given(
        values=st.lists(
            st.one_of(st.none(), st.integers(min_value=-(10**20), max_value=10**20), st.floats(allow_nan=True, allow_infinity=True)),
            min_size=1,
            max_size=25,
        ),
        order=st.sampled_from(["descending", "ascending"]),
        ties=st.sampled_from(["competition", "dense"]),
    )
    def test_one_row_out_per_row_in_with_every_input_field_preserved(self, values: list[Any], order: str, ties: str) -> None:
        inputs = [{"id": index, "score": value, "tag": f"t{index}"} for index, value in enumerate(values)]
        transform = BatchRank({"schema": OBSERVED, "value_field": "score", "order": order, "ties": ties})

        result = transform.process([_make_row(dict(data)) for data in inputs], make_context())

        assert result.status == "success"
        assert result.is_multi_row
        assert result.rows is not None
        assert len(result.rows) == len(inputs)
        ranked_count = sum(1 for value in values if value is not None and math.isfinite(value))
        for data, emitted in zip(inputs, result.rows, strict=True):
            out = emitted.to_dict()
            for key, value in data.items():
                assert key in out
                assert out[key] is value or out[key] == value
            assert out["rank_batch_size"] == len(values)
            assert out["rank_ranked_count"] == ranked_count
            ranked = data["score"] is not None and math.isfinite(data["score"])
            assert (out["rank_rank"] is None) is (not ranked)
            assert (out["rank_percentile"] is None) is (not ranked)
            if ranked:
                assert 1 <= out["rank_rank"] <= ranked_count
                assert 0.0 <= out["rank_percentile"] < 100.0


def _row_with(data: dict[str, Any], *fields: FieldContract) -> PipelineRow:
    """A PipelineRow whose contract is exactly ``fields`` (one producer's truthful description of its row)."""
    return make_row(data, contract=SchemaContract(mode="OBSERVED", fields=fields, locked=True))


class TestCarriedFieldContracts:
    """Every emitted row's contract describes that row's own carried values.

    Buffered rows can come from different producers (two sources on one queue,
    row_union branches), each typing a carried field truthfully for its own
    rows. The one shared output contract must describe all of them: an
    emitted row's contract never rejects the value it carries.
    """

    @staticmethod
    def _violations(result: TransformResult) -> list[list[str]]:
        assert result.status == "success"
        assert result.rows is not None
        return [[type(violation).__name__ for violation in row.contract.validate(row.to_dict())] for row in result.rows]

    def test_int_then_float_rank_field(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [_row_with({"score": 7}, make_field("score", int)), _row_with({"score": 2.5}, make_field("score", float))]

        result = transform.process(rows, ctx)

        assert _column(result, "rank_rank") == [1, 2]
        assert self._violations(result) == [[], []]

    def test_carried_non_rank_field_typed_differently(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [
            _row_with({"score": 1, "note": "kept"}, make_field("score", int), make_field("note", str)),
            _row_with({"score": 2, "note": 3}, make_field("score", int), make_field("note", int)),
        ]

        result = transform.process(rows, ctx)

        assert self._violations(result) == [[], []]
        assert result.rows is not None
        # Two producers typing one field differently are described as 'any', never as either one's type.
        assert result.rows[0].contract.get_field("note").python_type is object

    def test_carried_field_nullable_on_one_producer_only(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [
            _row_with({"score": 1, "note": "kept"}, make_field("score", int), make_field("note", str, required=True)),
            _row_with({"score": 2, "note": None}, make_field("score", int), make_field("note", str, required=True, nullable=True)),
        ]

        assert self._violations(transform.process(rows, ctx)) == [[], []]

    def test_carried_field_absent_from_one_producer(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [
            _row_with({"score": 1, "note": "kept"}, make_field("score", int), make_field("note", str, required=True)),
            _row_with({"score": 2}, make_field("score", int)),
        ]

        result = transform.process(rows, ctx)

        assert self._violations(result) == [[], []]
        assert result.rows is not None
        assert "note" not in result.rows[1]
        # A field some buffered row lacks is never claimed required on the rows that share the contract.
        assert result.rows[1].contract.get_field("note").required is False

    def test_one_producer_batch_keeps_its_exact_carried_types(self, ctx: PluginContext) -> None:
        """Negative control: describing several producers never widens a batch from ONE producer."""
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        contract = SchemaContract(
            mode="OBSERVED", fields=(make_field("score", int, required=True), make_field("note", str, nullable=True)), locked=True
        )
        rows = [make_row({"score": 7, "note": "a"}, contract=contract), make_row({"score": 3, "note": None}, contract=contract)]

        result = transform.process(rows, ctx)

        assert result.rows is not None
        emitted = result.rows[0].contract
        assert (emitted.get_field("score").python_type, emitted.get_field("score").required) == (int, True)
        assert (emitted.get_field("note").python_type, emitted.get_field("note").nullable) == (str, True)
        assert self._violations(result) == [[], []]


class TestBatchFailures:
    @pytest.mark.parametrize(
        ("bad_value", "type_name"),
        [("SENTINEL-str-9f2", "str"), (True, "bool")],
        ids=["str", "bool-is-not-a-number"],
    )
    def test_present_non_numeric_value_fails_the_whole_batch_value_free(self, bad_value: object, type_name: str) -> None:
        result = _rank([10, None, bad_value, 12])

        assert result.status == "error"
        assert result.retryable is False
        assert result.rows is None and result.row is None
        assert result.reason is not None
        assert result.reason["reason"] == "invalid_input"
        assert result.reason["error_type"] == "wrong_type"
        assert result.reason["field"] == "score"
        assert result.reason["expected"] == "numeric (int or float)"
        assert result.reason["actual_type"] == type_name
        assert result.reason["error"] == f"must be numeric (int or float), got {type_name} in row 2"
        assert "SENTINEL-str-9f2" not in repr(sorted(result.reason.items()))

    def test_runtime_annotation_field_collision_fails_the_whole_batch_value_free(self, ctx: PluginContext) -> None:
        transform = BatchRank({"schema": OBSERVED, "value_field": "score"})
        rows = [_make_row({"score": 1}), _make_row({"score": 2, "rank_rank": "SENTINEL-prior-rank-31"})]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "field_collision"
        assert result.reason["collisions"] == ["rank_rank"]
        assert result.reason["error"] == "would overwrite existing input fields ['rank_rank'] in row 1"
        assert "SENTINEL-prior-rank-31" not in repr(sorted(result.reason.items()))


class TestConfig:
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_value_field_rejected(self, blank: str) -> None:
        with pytest.raises(PluginConfigError, match="value_field must not be empty"):
            BatchRank({"schema": OBSERVED, "value_field": blank})

    def test_output_prefix_must_be_an_identifier(self) -> None:
        with pytest.raises(PluginConfigError, match="valid Python identifier"):
            BatchRank({"schema": OBSERVED, "value_field": "score", "output_prefix": "not-an-identifier"})

    def test_value_field_must_not_collide_with_an_output_field(self) -> None:
        with pytest.raises(PluginConfigError, match="collides with a batch_rank output field"):
            BatchRank({"schema": OBSERVED, "value_field": "rank_rank"})

    @pytest.mark.parametrize(("option", "value"), [("order", "desc"), ("ties", "average")])
    def test_order_and_ties_are_closed_choices(self, option: str, value: str) -> None:
        with pytest.raises(PluginConfigError):
            BatchRank({"schema": OBSERVED, "value_field": "score", option: value})

    def test_explicit_schema_declaring_an_output_field_is_refused(self) -> None:
        with pytest.raises(PluginConfigError, match=r"would overwrite them"):
            BatchRank({"schema": {"mode": "flexible", "fields": ["score: float", "rank_percentile: float"]}, "value_field": "score"})
