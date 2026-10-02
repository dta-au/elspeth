"""Tests for BatchEffectSize aggregation transform."""

from datetime import UTC, datetime
from typing import Any

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.testing import make_field, make_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed"}


def _make_row(data: dict[str, Any]):
    """Create a PipelineRow with OBSERVED contract for testing."""
    fields = tuple(
        make_field(key, type(value) if value is not None else object, original_name=key, required=False, source="inferred")
        for key, value in data.items()
    )
    contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
    return make_row(data, contract=contract)


class TestBatchEffectSize:
    @pytest.fixture
    def ctx(self) -> PluginContext:
        return make_context()

    def test_has_required_attributes(self) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        assert BatchEffectSize.name == "batch_effect_size"
        assert BatchEffectSize.is_batch_aware is True

    def test_computes_cohens_d_and_hedges_g(self, ctx: PluginContext) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize(
            {"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score", "baseline_variant": "control"}
        )
        rows = [
            _make_row({"variant": "control", "score": 1.0}),
            _make_row({"variant": "control", "score": 2.0}),
            _make_row({"variant": "control", "score": 3.0}),
            _make_row({"variant": "treatment", "score": 2.0}),
            _make_row({"variant": "treatment", "score": 3.0}),
            _make_row({"variant": "treatment", "score": 4.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline_variant"] == "control"
        assert result.row["variant"] == "treatment"
        assert result.row["baseline_mean"] == 2.0
        assert result.row["variant_mean"] == 3.0
        assert result.row["mean_delta"] == 1.0
        assert result.row["pooled_stdev"] == 1.0
        assert result.row["cohens_d"] == 1.0
        assert result.row["hedges_g"] == pytest.approx(0.8)

    def test_missing_and_non_finite_scores_are_skipped_and_reported(self, ctx: PluginContext) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "A", "score": 1.0}),
            _make_row({"variant": "A", "score": None}),
            _make_row({"variant": "B", "score": 2.0}),
            _make_row({"variant": "B", "score": float("nan")}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline_count"] == 1
        assert result.row["variant_count"] == 1
        assert result.row["baseline_missing_count"] == 1
        assert result.row["variant_non_finite_count"] == 1

    @pytest.mark.parametrize("variant_value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_variant_returns_error_before_success_output(self, ctx: PluginContext, variant_value: float) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "A", "score": 1.0}),
            _make_row({"variant": variant_value, "score": 2.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "validation_failed"
        assert result.reason["cause"] == "non_finite_variant"
        assert result.reason["field"] == "variant"
        assert result.reason["row_errors"] == [{"row_index": 1, "reason": "non_finite_variant"}]
        assert not result.retryable

    @pytest.mark.parametrize(
        ("bad_score", "found"),
        [
            ("SENTINEL-score-7f3a91", "str"),
            (True, "bool"),
            (datetime(2031, 7, 19, 13, 47, 11, tzinfo=UTC), "datetime"),
        ],
    )
    def test_wrong_typed_score_fails_the_whole_batch_with_a_recorded_reason(
        self, ctx: PluginContext, bad_score: object, found: str
    ) -> None:
        """A wrong-typed score fails the BATCH with a returned, value-free reason (elspeth-5887fb7928).

        The bad row sits in the second-seen group at batch index 3, so a
        group-local index (it is row 1 of group B) would be reported wrongly.
        """
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "A", "score": 1.0}),
            _make_row({"variant": "B", "score": 2.0}),
            _make_row({"variant": "A", "score": 3.0}),
            _make_row({"variant": "B", "score": bad_score}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.retryable is False
        assert result.row is None
        assert result.rows is None
        assert result.reason is not None
        assert result.reason["reason"] == "invalid_input"
        assert result.reason["error_type"] == "wrong_type"
        assert result.reason["field"] == "score"
        assert result.reason["expected"] == "numeric (int or float)"
        assert result.reason["actual_type"] == found
        assert "in row 3" in result.reason["error"]
        assert str(bad_score) not in repr(result.reason)
        assert repr(bad_score) not in repr(result.reason)

    def test_no_finite_score_reason_names_the_group_by_index_not_by_label(self, ctx: PluginContext) -> None:
        """The variant label is row data: the audit reason records row indices, never the label."""
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "A", "score": 1.0}),
            _make_row({"variant": "SENTINEL-label-5c2e", "score": None}),
            _make_row({"variant": "SENTINEL-label-5c2e", "score": float("nan")}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.retryable is False
        assert result.reason is not None
        assert result.reason["cause"] == "variant_has_no_finite_scores"
        assert result.reason["group_by"] == "variant"
        assert result.reason["field"] == "score"
        assert result.reason["row_errors"] == [
            {"row_index": 1, "reason": "missing_value"},
            {"row_index": 2, "reason": "non_finite_value"},
        ]
        assert "group_value" not in result.reason
        assert "SENTINEL-label-5c2e" not in repr(result.reason)

    def test_missing_baseline_reason_counts_the_batch_variants_without_naming_them(self, ctx: PluginContext) -> None:
        """The configured baseline is config and may be named; the variants present are row data and may not."""
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize(
            {"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score", "baseline_variant": "control"}
        )
        rows = [
            _make_row({"variant": "SENTINEL-label-a1", "score": 1.0}),
            _make_row({"variant": "SENTINEL-label-b2", "score": 2.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.retryable is False
        assert result.reason is not None
        assert result.reason["cause"] == "baseline_variant_missing"
        assert result.reason["expected"] == "control"
        assert result.reason["group_by"] == "variant"
        assert result.reason["count"] == 2
        assert "errors" not in result.reason
        assert "SENTINEL-label" not in repr(result.reason)

    def test_first_seen_variant_is_the_baseline_when_none_is_configured(self, ctx: PluginContext) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "B", "score": 3.0}),
            _make_row({"variant": "A", "score": 1.0}),
            _make_row({"variant": "B", "score": 4.0}),
            _make_row({"variant": "A", "score": 2.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline_variant"] == "B"
        assert result.row["variant"] == "A"
        assert result.row["mean_delta"] == -2.0

    def test_float_overflow_reason_names_groups_by_first_row_index_not_by_label(self, ctx: PluginContext) -> None:
        """An overflowing mean fails the batch; the reason points at rows, never at variant labels."""
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "SENTINEL-base-9d", "score": 1e308}),
            _make_row({"variant": "SENTINEL-base-9d", "score": 1e308}),
            _make_row({"variant": "SENTINEL-cand-4b", "score": 1.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "error"
        assert result.retryable is False
        assert result.reason is not None
        assert result.reason["reason"] == "float_overflow"
        assert result.reason["operation"] == "baseline_mean"
        assert result.reason["group_by"] == "variant"
        assert result.reason["field"] == "score"
        assert result.reason["error"] == (
            "overflow comparing the variant group first seen in row 2 with the baseline group first seen in row 0"
        )
        assert "group_value" not in result.reason
        assert "value" not in result.reason
        assert "SENTINEL" not in repr(result.reason)

    def test_single_value_group_reports_none_stdev(self, ctx: PluginContext) -> None:
        """n=1 stdev is undefined -- must emit None, never 0.0 (B4.5-a-effect_size-stdev)."""
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        transform = BatchEffectSize({"schema": DYNAMIC_SCHEMA, "variant_field": "variant", "score_field": "score"})
        rows = [
            _make_row({"variant": "A", "score": 5.0}),
            _make_row({"variant": "B", "score": 7.0}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline_count"] == 1
        assert result.row["variant_count"] == 1
        # stdev undefined at n=1 -- honest None, never 0.0
        assert result.row["baseline_stdev"] is None
        assert result.row["variant_stdev"] is None
        # Both groups n=1 -> pooled dispersion denominator (n1+n2-2) is 0, so the
        # pooled stdev is UNDEFINED, not zero. Emit None rather than a misleading
        # real 0.0 (B4.5-b-effect_size-pooled). cohens_d stays None.
        assert result.row["pooled_stdev"] is None
        assert result.row["cohens_d"] is None


class TestBatchEffectSizeConfig:
    @pytest.mark.parametrize(
        "config",
        [
            {"variant_field": "", "score_field": "score"},
            {"variant_field": "variant", "score_field": ""},
            {"variant_field": "score", "score_field": "score"},
        ],
    )
    def test_invalid_config_rejected_at_config_boundary(self, config: dict[str, Any]) -> None:
        from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize

        with pytest.raises(PluginConfigError):
            BatchEffectSize({"schema": DYNAMIC_SCHEMA, **config})
