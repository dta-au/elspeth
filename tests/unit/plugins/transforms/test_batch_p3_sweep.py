"""Regression tests for the P3 batch-transform sweep findings (20261111-plugins-review).

One class per finding; each test fails on the base tree at an assertion and
passes once that finding's fix lands.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

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
