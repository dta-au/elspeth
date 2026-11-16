"""Regression tests for plugin review #102 and #20 (batch transforms, run p5).

#102: batch_drift_compare's ``chi_square_statistic`` skipped every category
absent from the baseline, so drift into new categories was understated. It is
now Pearson's chi-square test of homogeneity over the 2 x k contingency table
of every category seen in EITHER cohort, where no expected cell can be zero.
The earlier "fix" only disclosed the defect (usage sentence, composer hint and
a +64 B planner-scaffold allowance); those are gone.

#20: the 4096-row flush cap of the capped batch transforms was enforced only
at flush, so an aggregation with ``trigger.count`` above it passed validation
and failed every batch. The cap is now one shared constant, declared on each
capped plugin class as ``max_batch_rows``, and both the runtime factory and the
composer refuse a count trigger above it before any row is read.
"""

from __future__ import annotations

import importlib
import importlib.util
from fractions import Fraction
from types import ModuleType
from typing import Any

import pytest

from elspeth.config_loading import load_settings_from_config_dict
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms import (
    batch_drift_compare,
    batch_duplicate_annotator,
    batch_outlier_annotator,
    batch_paired_preference,
    batch_rank,
)
from elspeth.testing import make_field, make_row
from tests.fixtures.factories import make_context

DYNAMIC_SCHEMA = {"mode": "observed"}
_SHARED_CAP_MODULE = "elspeth.plugins.transforms._batch_row_types"


def _make_row(data: dict[str, Any]) -> PipelineRow:
    fields = tuple(
        make_field(key, type(value) if value is not None else object, original_name=key, required=False, source="inferred")
        for key, value in data.items()
    )
    contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
    return make_row(data, contract=contract)


@pytest.fixture
def ctx() -> PluginContext:
    return make_context()


# ---------------------------------------------------------------------------
# #102 batch_drift_compare chi-square over the union of categories
# ---------------------------------------------------------------------------


def _pearson_homogeneity(baseline: list[str], current: list[str]) -> float:
    """Independent oracle: Pearson chi-square over the 2 x k table, exact arithmetic."""
    categories = sorted(set(baseline) | set(current))
    rows = [[baseline.count(c) for c in categories], [current.count(c) for c in categories]]
    row_totals = [sum(row) for row in rows]
    col_totals = [rows[0][j] + rows[1][j] for j in range(len(categories))]
    grand = sum(row_totals)
    statistic = Fraction(0)
    for i in range(2):
        for j in range(len(categories)):
            expected = Fraction(row_totals[i] * col_totals[j], grand)
            statistic += (rows[i][j] - expected) ** 2 / expected
    return float(statistic)


def _chi_square(ctx: PluginContext, baseline: list[str], current: list[str]) -> float:
    transform = batch_drift_compare.BatchDriftCompare(
        {"schema": DYNAMIC_SCHEMA, "cohort_field": "cohort", "value_field": "label", "value_type": "categorical"}
    )
    rows = [_make_row({"cohort": "baseline", "label": v}) for v in baseline]
    rows += [_make_row({"cohort": "current", "label": v}) for v in current]
    result = transform.process(rows, ctx)
    assert result.status == "success"
    assert result.row is not None
    statistic = result.row["chi_square_statistic"]
    assert isinstance(statistic, float)
    return statistic


class TestFinding102ChiSquareCountsNewCategories:
    @pytest.mark.parametrize(
        "baseline, current",
        [
            pytest.param(["a", "a", "b"], ["a", "b", "b", "c"], id="one-new-category"),
            pytest.param(["a", "a", "b", "b"], ["a", "a", "a", "c"], id="shift-into-new"),
            pytest.param(["a", "a", "b", "b"], ["a", "a", "a", "b"], id="shift-within-support"),
            pytest.param(["a", "a", "b"], ["a", "a", "b"], id="identical"),
            pytest.param(["a", "b", "c"], ["a"], id="cohort-on-subset"),
        ],
    )
    def test_statistic_is_pearson_homogeneity_over_the_category_union(
        self, ctx: PluginContext, baseline: list[str], current: list[str]
    ) -> None:
        assert _chi_square(ctx, baseline, current) == pytest.approx(_pearson_homogeneity(baseline, current))

    def test_worked_example_counts_the_new_category(self, ctx: PluginContext) -> None:
        # baseline a2 b1, current a1 b2 c1: 8/9 (baseline row) + 2/3 (current row).
        # The baseline-support-only statistic was 1.375 (new category 'c' added nothing).
        assert _chi_square(ctx, ["a", "a", "b"], ["a", "b", "b", "c"]) == pytest.approx(14 / 9)

    def test_complete_drift_into_a_new_category_is_maximal(self, ctx: PluginContext) -> None:
        # Fully separated cohorts give chi-square = N (Cramer's V = 1); the old
        # statistic reported only the baseline cells (3.0 here).
        assert _chi_square(ctx, ["a", "a"], ["b", "b", "b"]) == pytest.approx(5.0)

    def test_drift_into_a_new_category_is_not_understated(self, ctx: PluginContext) -> None:
        within = _chi_square(ctx, ["a", "a", "b", "b"], ["a", "a", "a", "b"])
        into_new = _chi_square(ctx, ["a", "a", "b", "b"], ["a", "a", "a", "c"])
        assert into_new > within

    def test_defect_disclosure_is_gone(self) -> None:
        cls = batch_drift_compare.BatchDriftCompare
        assert "omits categories absent from the baseline" not in cls.usage_when_not_to_use
        assistance = cls.get_agent_assistance()
        assert assistance is not None
        assert not [hint for hint in assistance.composer_hints if "absent from the baseline" in hint]

    def test_planner_scaffold_allowance_for_the_disclosure_is_gone(self) -> None:
        planner_tests = importlib.import_module("tests.unit.web.composer.test_pipeline_planner")
        assert "_DRIFT_COMPARE_CHI_SQUARE_DISCLOSURE_CANONICAL_BYTES" not in vars(planner_tests)


# ---------------------------------------------------------------------------
# #20 batch row cap: one authority, refused against trigger.count before a run
# ---------------------------------------------------------------------------

_CAPPED = [
    pytest.param(batch_outlier_annotator, "BatchOutlierAnnotator", id="batch_outlier_annotator"),
    pytest.param(batch_rank, "BatchRank", id="batch_rank"),
    pytest.param(batch_paired_preference, "BatchPairedPreference", id="batch_paired_preference"),
    pytest.param(batch_drift_compare, "BatchDriftCompare", id="batch_drift_compare"),
    pytest.param(batch_duplicate_annotator, "BatchDuplicateAnnotator", id="batch_duplicate_annotator"),
]


def _shared_cap() -> int:
    assert importlib.util.find_spec(_SHARED_CAP_MODULE) is not None
    module = importlib.import_module(_SHARED_CAP_MODULE)
    assert "MAX_BATCH_ROWS" in vars(module), "the batch row cap needs one shared authority"
    cap = vars(module)["MAX_BATCH_ROWS"]
    assert cap == 4096
    return int(cap)


class TestFinding20BatchRowCapIsOneDeclaredAuthority:
    def test_base_transform_declares_no_cap_by_default(self) -> None:
        assert "max_batch_rows" in vars(BaseTransform)
        assert vars(BaseTransform)["max_batch_rows"] is None

    @pytest.mark.parametrize("module, cls_name", _CAPPED)
    def test_each_capped_plugin_declares_the_shared_cap(self, module: ModuleType, cls_name: str) -> None:
        cap = _shared_cap()
        cls = vars(module)[cls_name]
        assert "max_batch_rows" in vars(cls), f"{cls.name} does not declare max_batch_rows"
        assert vars(cls)["max_batch_rows"] == cap
        # One authority: no per-module copy of the number left to drift.
        assert "_MAX_BATCH_ROWS" not in vars(module)

    def test_flush_refusal_reads_the_class_declaration(self, ctx: PluginContext, monkeypatch: pytest.MonkeyPatch) -> None:
        cls = batch_rank.BatchRank
        assert "max_batch_rows" in vars(cls)
        monkeypatch.setattr(cls, "max_batch_rows", 2)
        transform = cls({**cls.probe_config()})
        rows = [_make_row({"batch_rank_probe_value": float(i)}) for i in range(3)]
        result = transform.process(rows, ctx)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["cause"] == "batch_too_large"


_BASE_DOC: dict[str, object] = {
    "sources": {
        "main": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "in.csv", "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
    },
    "sinks": {"out": {"plugin": "json", "options": {"path": "out.json", "schema": {"mode": "observed"}}, "on_write_failure": "discard"}},
}


def _rank_aggregation_settings(count: int) -> Any:
    return load_settings_from_config_dict(
        {
            **_BASE_DOC,
            "aggregations": [
                {
                    "name": "ranked",
                    "plugin": "batch_rank",
                    "input": "rows",
                    "on_success": "out",
                    "on_error": "discard",
                    "trigger": {"count": count},
                    "output_mode": "transform",
                    "options": batch_rank.BatchRank.probe_config(),
                }
            ],
        }
    )


class TestFinding20TriggerCountAboveCapIsRefusedBeforeARun:
    def test_runtime_factory_refuses_count_above_cap(self) -> None:
        cap = _shared_cap()
        with pytest.raises(ValueError, match=r"Aggregation 'ranked' uses 'batch_rank'") as excinfo:
            instantiate_plugins_from_config(_rank_aggregation_settings(cap + 1), preflight_mode=True)
        message = str(excinfo.value)
        assert str(cap + 1) in message
        assert str(cap) in message

    def test_runtime_factory_admits_count_at_cap(self) -> None:
        cap = _shared_cap()
        bundle = instantiate_plugins_from_config(_rank_aggregation_settings(cap), preflight_mode=True)
        assert "ranked" in bundle.aggregations

    def test_composer_refuses_count_above_cap(self) -> None:
        from elspeth.web.composer.state import _batch_aware_placement_error

        cap = _shared_cap()
        message = _batch_aware_placement_error("ranked", "aggregation", "batch_rank", "transform", {"count": cap + 1})
        assert message is not None
        assert str(cap) in message
        assert _batch_aware_placement_error("ranked", "aggregation", "batch_rank", "transform", {"count": cap}) is None
        # An uncapped batch plugin is not affected.
        assert _batch_aware_placement_error("stats", "aggregation", "batch_stats", "transform", {"count": cap + 1}) is None
