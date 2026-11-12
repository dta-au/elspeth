"""Regression tests for the P3 batch-transform sweep findings (20261111-plugins-review).

One class per finding; each test fails on the base tree at an assertion and
passes once that finding's fix lands.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, ClassVar

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.transforms import (
    batch_drift_compare,
    batch_outlier_annotator,
    batch_paired_preference,
    batch_rank,
)
from elspeth.testing import make_field, make_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed"}


def _make_row(data: dict[str, Any]) -> PipelineRow:
    """Create a PipelineRow with an OBSERVED contract for testing."""
    fields = tuple(
        make_field(key, type(value) if value is not None else object, original_name=key, required=False, source="inferred")
        for key, value in data.items()
    )
    contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
    return make_row(data, contract=contract)


@pytest.fixture
def ctx() -> PluginContext:
    return make_context()


class TestFinding20BatchRowCapIsAdvertised:
    """#20: the 4096-row flush cap must reach the composer/operator before a run.

    Each capped plugin refuses a flush above ``_MAX_BATCH_ROWS`` at run time.
    Its composer hints must name that same cap, read from the module constant,
    and tie it to the aggregation trigger, so a planner does not configure a
    trigger (or an end-of-source flush) that fails every batch.
    """

    @pytest.mark.parametrize(
        "module, plugin_cls_name",
        [
            pytest.param(batch_outlier_annotator, "BatchOutlierAnnotator", id="batch_outlier_annotator"),
            pytest.param(batch_rank, "BatchRank", id="batch_rank"),
            pytest.param(batch_paired_preference, "BatchPairedPreference", id="batch_paired_preference"),
            pytest.param(batch_drift_compare, "BatchDriftCompare", id="batch_drift_compare"),
        ],
    )
    def test_composer_hints_name_the_flush_cap(self, module: ModuleType, plugin_cls_name: str) -> None:
        plugin_cls = vars(module)[plugin_cls_name]
        cap = vars(module)["_MAX_BATCH_ROWS"]
        assistance = plugin_cls.get_agent_assistance()
        assert assistance is not None
        cap_hints = [hint for hint in assistance.composer_hints if str(cap) in hint and "trigger" in hint]
        assert cap_hints, f"{plugin_cls.name} composer_hints never state the {cap}-row flush cap against the trigger"


class TestFinding99BatchStatsIntegerSumOverflow:
    """#99: an exact-int ``sum`` outside the JSON safe range must be a recorded plugin error.

    Every input is inside ±(2**53-1), but the total is not. Canonical JSON
    refuses such an int, so the engine would blame the plugin with a
    PluginContractViolation. The plugin must instead return its own
    non-retryable overflow reason, like its float-overflow branch does.
    """

    _SAFE_MAX = 2**53 - 1

    @staticmethod
    def _transform() -> Any:
        from elspeth.plugins.transforms.batch_stats import BatchStats

        return BatchStats({"schema": DYNAMIC_SCHEMA, "value_field": "v"})

    @pytest.mark.parametrize(
        "values",
        [
            pytest.param([_SAFE_MAX, 1], id="just-above-max"),
            pytest.param([_SAFE_MAX, _SAFE_MAX], id="two-max"),
            pytest.param([-_SAFE_MAX, -1], id="just-below-min"),
        ],
    )
    def test_int_sum_beyond_safe_range_is_a_recorded_overflow(self, ctx: PluginContext, values: list[int]) -> None:
        result = self._transform().process([_make_row({"v": value}) for value in values], ctx)

        assert result.status == "error", "an int sum beyond ±(2**53-1) must not be emitted as success"
        assert result.reason is not None
        assert result.reason["reason"] == "float_overflow"
        assert result.reason["operation"] == "sum"
        assert result.reason["valid_count"] == len(values)
        assert result.retryable is False
        # Value-free: the total is row-derived content and stays out of the reason.
        assert str(sum(values)) not in repr(result.reason)

    def test_int_sum_at_safe_max_still_succeeds(self, ctx: PluginContext) -> None:
        result = self._transform().process([_make_row({"v": self._SAFE_MAX - 1}), _make_row({"v": 1})], ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["sum"] == self._SAFE_MAX
        assert type(result.row["sum"]) is int

    def test_mixed_int_float_sum_overflow_is_recorded_not_raised(self, ctx: PluginContext) -> None:
        # sum([huge_int, float]) converts the int to float and raises
        # OverflowError; only the mean was guarded.
        raised: BaseException | None = None
        result = None
        try:
            result = self._transform().process([_make_row({"v": 10**400}), _make_row({"v": 1.5})], ctx)
        except OverflowError as exc:
            raised = exc
        assert raised is None, "OverflowError escaped process() instead of a recorded float_overflow"
        assert result is not None
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "float_overflow"
        assert result.reason["operation"] == "sum"


def _admit(config_cls: Any, config: dict[str, Any], plugin_name: str) -> tuple[Any, Exception | None]:
    """Run config admission and return (config, refusal) so tests assert on both."""
    from elspeth.plugins.infrastructure.config_base import PluginConfigError

    try:
        return config_cls.from_dict(config, plugin_name=plugin_name), None
    except PluginConfigError as exc:
        return None, exc


def _variant_config(baseline: object, *, pair_field: bool = True) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema": DYNAMIC_SCHEMA,
        "variant_field": "variant",
        "score_field": "score",
        "baseline_variant": baseline,
    }
    if pair_field:
        config["pair_field"] = "case_id"
    return config


# Every scalar a YAML or composer config can carry as a variant label, each of
# which must reach the plugin with its own type (same_scalar_bucket_value is
# type-strict, so a coerced baseline never matches its variant).
_BASELINE_LABELS: tuple[object, ...] = ("control", "1", 1, 0, True, False, 0.2, 1.0, 2.5)


class TestFinding107PairedPreferenceFloatBaseline:
    """#107: a float baseline_variant must keep its type and match float variants."""

    def test_every_scalar_baseline_is_admitted_with_its_own_type(self) -> None:
        from elspeth.plugins.transforms.batch_paired_preference import BatchPairedPreferenceConfig

        for baseline in _BASELINE_LABELS:
            cfg, refused = _admit(BatchPairedPreferenceConfig, _variant_config(baseline), "batch_paired_preference")
            assert refused is None, f"baseline_variant {baseline!r} was refused: {refused}"
            assert type(cfg.baseline_variant) is type(baseline), f"{baseline!r} was coerced to {cfg.baseline_variant!r}"
            assert cfg.baseline_variant == baseline

    def test_float_baseline_matches_float_variants(self, ctx: PluginContext) -> None:
        from elspeth.plugins.transforms.batch_paired_preference import BatchPairedPreference

        transform = BatchPairedPreference(_variant_config(1.0))
        rows = [
            _make_row({"case_id": "p1", "variant": 2.0, "score": 0.9}),
            _make_row({"case_id": "p1", "variant": 1.0, "score": 0.4}),
            _make_row({"case_id": "p2", "variant": 2.0, "score": 0.7}),
            _make_row({"case_id": "p2", "variant": 1.0, "score": 0.5}),
        ]

        result = transform.process(rows, ctx)

        assert result.status == "success", result.reason
        assert result.row is not None
        assert type(result.row["baseline_variant"]) is float
        assert result.row["baseline_variant"] == 1.0
        assert result.row["variant"] == 2.0
        assert result.row["wins"] == 2

    def test_non_finite_baseline_is_refused_as_non_finite(self) -> None:
        from elspeth.plugins.transforms.batch_paired_preference import BatchPairedPreferenceConfig

        for baseline in (float("nan"), float("inf")):
            _cfg, refused = _admit(BatchPairedPreferenceConfig, _variant_config(baseline), "batch_paired_preference")
            assert refused is not None, f"baseline_variant {baseline!r} can never match a variant and must be refused"
            assert "baseline_variant must be finite" in str(refused), f"refusal does not name the non-finite baseline: {refused}"


class TestFinding108PairedPreferenceStudentTInterval:
    """#108: the paired 95% interval must use the Student-t critical value at n-1 df.

    Reference values are the 0.975 quantile of Student's t at df, solved
    independently with mpmath at 40 digits (root of 1 - I_x(df/2, 1/2)/2 with
    x = df/(df+t^2)) and pinned as literals; they agree with published tables
    (12.706, 4.303, 2.262, 1.984).
    """

    _T_975: ClassVar[dict[int, float]] = {
        1: 12.706204736174705,
        2: 4.302652729749464,
        9: 2.2621571627982053,
        99: 1.9842169515864174,
    }

    @pytest.mark.parametrize("pair_count", [2, 3, 10, 100])
    def test_interval_half_width_uses_t_critical_value(self, ctx: PluginContext, pair_count: int) -> None:
        import math
        import statistics

        from elspeth.plugins.transforms.batch_paired_preference import BatchPairedPreference

        transform = BatchPairedPreference(_variant_config("A"))
        rows: list[PipelineRow] = []
        deltas: list[float] = []
        for index in range(pair_count):
            baseline_score = 0.5
            candidate_score = 0.5 + ((index * 7) % 5 - 2) / 10
            deltas.append(float(candidate_score - baseline_score))
            rows.append(_make_row({"case_id": f"p{index}", "variant": "A", "score": baseline_score}))
            rows.append(_make_row({"case_id": f"p{index}", "variant": "B", "score": candidate_score}))

        result = transform.process(rows, ctx)

        assert result.status == "success", result.reason
        assert result.row is not None
        mean_delta = sum(deltas) / pair_count
        standard_error = statistics.stdev(deltas) / math.sqrt(pair_count)
        half_width = self._T_975[pair_count - 1] * standard_error
        assert result.row["standard_error_delta"] == pytest.approx(standard_error, rel=1e-12)
        assert result.row["confidence_95_low"] == pytest.approx(mean_delta - half_width, rel=1e-9, abs=1e-12)
        assert result.row["confidence_95_high"] == pytest.approx(mean_delta + half_width, rel=1e-9, abs=1e-12)


class TestFinding113ClassifierPositiveLabelTypeMismatch:
    """#113: a positive_label equal by value but not by type to a present label fails the batch.

    Otherwise the label is treated as absent and every actual positive is
    counted as a true negative, which reads exactly like an all-negative batch.
    """

    _ROWS: ClassVar[tuple[dict[str, int], ...]] = ({"a": 1, "p": 1}, {"a": 1, "p": 0}, {"a": 0, "p": 0})

    @pytest.mark.parametrize(
        "positive_label, configured_type",
        [pytest.param("1", "str", id="quoted-one"), pytest.param(True, "bool", id="true-vs-one")],
    )
    def test_value_equal_label_of_another_type_fails_value_free(
        self, ctx: PluginContext, positive_label: object, configured_type: str
    ) -> None:
        from elspeth.plugins.transforms.batch_classifier_metrics import BatchClassifierMetrics

        transform = BatchClassifierMetrics(
            {"schema": DYNAMIC_SCHEMA, "actual_field": "a", "predicted_field": "p", "positive_label": positive_label}
        )

        result = transform.process([_make_row(row) for row in self._ROWS], ctx)

        assert result.status == "error", f"labels of another type were silently counted as true negatives: {result.row}"
        assert result.reason is not None
        assert result.reason["reason"] == "validation_failed"
        assert result.reason["cause"] == "positive_label_type_mismatch"
        assert result.reason["expected"] == configured_type
        assert result.reason["actual_type"] == "int"
        assert result.retryable is False

    def test_genuinely_absent_positive_label_still_succeeds(self, ctx: PluginContext) -> None:
        from elspeth.plugins.transforms.batch_classifier_metrics import BatchClassifierMetrics

        transform = BatchClassifierMetrics({"schema": DYNAMIC_SCHEMA, "actual_field": "a", "predicted_field": "p", "positive_label": 7})

        result = transform.process([_make_row(row) for row in self._ROWS], ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["binary_tp"] == 0
        assert result.row["binary_tn"] == 3

    def test_float_positive_label_is_refused_not_coerced(self) -> None:
        from elspeth.plugins.transforms.batch_classifier_metrics import BatchClassifierMetricsConfig

        config = {"schema": DYNAMIC_SCHEMA, "actual_field": "a", "predicted_field": "p", "positive_label": 1.0}
        cfg, refused = _admit(BatchClassifierMetricsConfig, config, "batch_classifier_metrics")
        coerced = None if cfg is None else cfg.positive_label
        assert refused is not None, f"positive_label 1.0 was coerced to {coerced!r}; data labels refuse float"


def _two_arm_rows(baseline_arm: object, candidate_arm: object) -> list[PipelineRow]:
    # The candidate arm arrives first, so only a configured baseline (never
    # first-seen order) can select ``baseline_arm``.
    return [
        _make_row({"variant": candidate_arm, "score": 0.9}),
        _make_row({"variant": baseline_arm, "score": 0.4}),
        _make_row({"variant": candidate_arm, "score": 0.7}),
        _make_row({"variant": baseline_arm, "score": 0.5}),
    ]


class TestFinding115ExperimentCompareNonStringBaseline:
    """#115: batch_experiment_compare must accept int and bool baselines, matched by type."""

    def test_every_scalar_baseline_is_admitted_with_its_own_type(self) -> None:
        from elspeth.plugins.transforms.batch_experiment_compare import BatchExperimentCompareConfig

        for baseline in _BASELINE_LABELS:
            cfg, refused = _admit(BatchExperimentCompareConfig, _variant_config(baseline, pair_field=False), "batch_experiment_compare")
            assert refused is None, f"baseline_variant {baseline!r} was refused: {refused}"
            assert type(cfg.baseline_variant) is type(baseline), f"{baseline!r} was coerced to {cfg.baseline_variant!r}"

    @pytest.mark.parametrize(
        "baseline_arm, candidate_arm",
        [pytest.param(1, 2, id="int-arms"), pytest.param(False, True, id="bool-arms"), pytest.param(0.2, 0.7, id="float-arms")],
    )
    def test_configured_non_string_baseline_is_selected(self, ctx: PluginContext, baseline_arm: object, candidate_arm: object) -> None:
        from elspeth.plugins.infrastructure.config_base import PluginConfigError
        from elspeth.plugins.transforms.batch_experiment_compare import BatchExperimentCompare

        transform = None
        refused: PluginConfigError | None = None
        try:
            transform = BatchExperimentCompare(_variant_config(baseline_arm, pair_field=False))
        except PluginConfigError as exc:
            refused = exc
        assert refused is None, f"baseline_variant {baseline_arm!r} was refused: {refused}"
        assert transform is not None

        result = transform.process(_two_arm_rows(baseline_arm, candidate_arm), ctx)

        assert result.status == "success", result.reason
        assert result.row is not None
        assert type(result.row["baseline_variant"]) is type(baseline_arm)
        assert result.row["baseline_variant"] == baseline_arm
        assert result.row["variant"] == candidate_arm

    def test_blank_and_non_finite_baselines_are_still_refused(self) -> None:
        from elspeth.plugins.transforms.batch_experiment_compare import BatchExperimentCompareConfig

        for baseline in ("   ", float("nan")):
            _cfg, refused = _admit(BatchExperimentCompareConfig, _variant_config(baseline, pair_field=False), "batch_experiment_compare")
            assert refused is not None, f"baseline_variant {baseline!r} must be refused"
            assert "baseline_variant must" in str(refused), f"refusal does not name the baseline rule: {refused}"
