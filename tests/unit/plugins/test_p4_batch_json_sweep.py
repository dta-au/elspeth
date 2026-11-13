"""Plugin review 2026-11-11, P4 sweep: batch/aggregation and JSON/CSV transforms.

One section per finding (#53, #54, #101, #102, #105, #111, #120, #121, #135,
#136). #146 (truncate) is a maintainer decision and has no test here.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts import ExceptionResult, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_factory

OBSERVED = {"mode": "observed"}


# ---------------------------------------------------------------------------
# #53 / #54 blob_csv_expand: every routed error reason is exercised, and the
# parse-phase reasons carry the same source identity as the load-phase ones.
# ---------------------------------------------------------------------------


class _PayloadStore:
    def __init__(self, content_by_hash: dict[str, bytes]) -> None:
        self.content_by_hash = content_by_hash

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        content = self.content_by_hash[content_hash]
        return None if len(content) > max_bytes else content


def _csv_field_error(text: str, **options: Any) -> dict[str, Any]:
    from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

    transform = BlobCSVExpand({"schema": OBSERVED, "source": "field", "text_field": "content", **options})
    result = transform.process(make_pipeline_row({"content": text}), make_context())
    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    return dict(result.reason)


def _csv_blob_error(body: bytes, **options: Any) -> tuple[str, dict[str, Any]]:
    from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

    blob_ref = hashlib.sha256(body).hexdigest()
    transform = BlobCSVExpand({"schema": OBSERVED, "blob_ref_field": "blob_ref", **options})
    transform._payload_store = _PayloadStore({blob_ref: body})
    result = transform.process(make_pipeline_row({"blob_ref": blob_ref}), make_context())
    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    return blob_ref, dict(result.reason)


class TestBlobCSVExpandErrorTaxonomy:
    def test_blob_too_large_is_routed_with_identity(self) -> None:
        blob_ref, reason = _csv_blob_error(b"a,b\n1,2\n", max_blob_bytes=4)
        assert reason["reason"] == "blob_too_large"
        assert reason.get("field") == "blob_ref"
        assert reason.get("blob_ref") == blob_ref
        assert reason["max_blob_bytes"] == 4

    def test_decode_failed_is_routed_with_identity(self) -> None:
        blob_ref, reason = _csv_blob_error(b"a,b\n\xff\xfe,2\n")
        assert reason["reason"] == "decode_failed"
        assert reason.get("field") == "blob_ref"
        assert reason.get("blob_ref") == blob_ref
        assert reason["encoding"] == "utf-8"

    def test_exhausted_during_skip_rows_carries_identity(self) -> None:
        reason = _csv_field_error("a,b\n", skip_rows=3)
        assert reason["reason"] == "csv_exhausted_during_skip_rows"
        assert reason["skip_rows"] == 3
        assert reason["rows_skipped"] == 1
        assert reason.get("field") == "content"

    def test_parse_error_in_skip_rows_phase_carries_identity(self) -> None:
        reason = _csv_field_error('"x"y,z\na,b\n1,2\n', skip_rows=1)
        assert reason["reason"] == "csv_parse_error"
        assert reason["phase"] == "skip_rows"
        assert reason["row_number"] == 1
        assert reason.get("field") == "content"

    def test_parse_error_in_header_phase_carries_identity(self) -> None:
        reason = _csv_field_error('"a"b,c\n1,2\n')
        assert reason["reason"] == "csv_parse_error"
        assert reason["phase"] == "header"
        assert reason.get("field") == "content"

    def test_parse_error_in_data_phase_carries_blob_identity(self) -> None:
        blob_ref, reason = _csv_blob_error(b'a,b\n1,2\n"3"x,4\n')
        assert reason["reason"] == "csv_parse_error"
        assert reason["phase"] == "data"
        assert reason["row_number"] == 2
        assert reason.get("field") == "blob_ref"
        assert reason.get("blob_ref") == blob_ref

    def test_header_error_carries_identity(self) -> None:
        reason = _csv_field_error("a,a\n1,2\n")
        assert reason["reason"] == "csv_header_error"
        assert reason.get("field") == "content"

    def test_config_error_carries_identity(self) -> None:
        reason = _csv_field_error("a,b\n1,2\n", field_mapping={"zzz": "renamed"})
        assert reason["reason"] == "csv_config_error"
        assert reason.get("field") == "content"

    def test_field_collision_carries_identity(self) -> None:
        reason = _csv_field_error("content2,csv_row_index\n1,2\n")
        assert reason["reason"] == "field_collision"
        assert reason.get("field") == "content"

    def test_too_many_rows_carries_identity(self) -> None:
        reason = _csv_field_error("a\n1\n2\n3\n", max_output_rows=2)
        assert reason["reason"] == "too_many_rows"
        assert reason.get("field") == "content"

    def test_column_count_mismatch_carries_identity(self) -> None:
        blob_ref, reason = _csv_blob_error(b"a,b\n1,2,3\n")
        assert reason["reason"] == "csv_column_count_mismatch"
        assert reason.get("field") == "blob_ref"
        assert reason.get("blob_ref") == blob_ref


# ---------------------------------------------------------------------------
# #101 / #102 batch_drift_compare
# ---------------------------------------------------------------------------


class TestBatchDriftCompareSkippedIndices:
    def test_success_row_names_skipped_rows_by_batch_index(self) -> None:
        from elspeth.plugins.transforms.batch_drift_compare import BatchDriftCompare

        transform = BatchDriftCompare({"schema": OBSERVED, "cohort_field": "cohort", "value_field": "score"})
        rows = [
            make_pipeline_row({"cohort": "base", "score": 1.0}),
            make_pipeline_row({"cohort": "base", "score": None}),
            make_pipeline_row({"cohort": "cur", "score": float("nan")}),
            make_pipeline_row({"cohort": "cur", "score": 2.0}),
            make_pipeline_row({"cohort": "cur", "score": None}),
        ]
        result = transform.process(rows, make_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row["baseline_missing_count"] == 1
        emitted = result.row.to_dict()
        assert {
            "baseline_missing_indices",
            "baseline_non_finite_indices",
            "cohort_missing_indices",
            "cohort_non_finite_indices",
        } <= set(emitted)
        assert tuple(result.row["baseline_missing_indices"]) == (1,)
        assert tuple(result.row["baseline_non_finite_indices"]) == ()
        assert tuple(result.row["cohort_missing_indices"]) == (4,)
        assert tuple(result.row["cohort_non_finite_indices"]) == (2,)

    def test_index_fields_are_declared_created_fields(self) -> None:
        from elspeth.plugins.transforms.batch_drift_compare import BatchDriftCompare

        for value_type in ("numeric", "categorical"):
            transform = BatchDriftCompare({"schema": OBSERVED, "cohort_field": "cohort", "value_field": "score", "value_type": value_type})
            created = {field.name for field in transform.created_output_fields()}
            assert {
                "baseline_missing_indices",
                "baseline_non_finite_indices",
                "cohort_missing_indices",
                "cohort_non_finite_indices",
            } <= created


class TestBatchDriftCompareChiSquareConvention:
    def test_chi_square_baseline_support_convention_is_disclosed(self) -> None:
        from elspeth.plugins.transforms.batch_drift_compare import BatchDriftCompare

        assistance = BatchDriftCompare.get_agent_assistance()
        assert assistance is not None
        hints = [hint for hint in assistance.composer_hints if "chi_square_statistic" in hint]
        assert hints, "no composer hint discloses how chi_square_statistic treats new categories"
        assert any("new_categor" in hint for hint in hints)
        assert "chi_square_statistic" in BatchDriftCompare.usage_when_not_to_use


# ---------------------------------------------------------------------------
# #105 json_sink: the JSON-array baseline-append branch is unreachable
# ---------------------------------------------------------------------------


class TestJSONSinkArrayAppendBranchRemoved:
    def test_suffix_stripping_helper_is_gone(self) -> None:
        from elspeth.plugins.sinks import json_sink

        assert "_iter_file_without_suffix" not in vars(json_sink)


# ---------------------------------------------------------------------------
# #111 json_explode: configurable index_field
# ---------------------------------------------------------------------------


def _require_index_field_option() -> None:
    from elspeth.plugins.transforms.json_explode import JSONExplodeConfig

    assert "index_field" in JSONExplodeConfig.model_fields


class TestJSONExplodeIndexField:
    def test_index_field_is_a_naming_option(self) -> None:
        _require_index_field_option()
        from elspeth.plugins.transforms.json_explode import JSONExplode

        assert "index_field" in JSONExplode.output_naming_config_keys
        assert JSONExplode({"schema": OBSERVED, "array_field": "items"}).declared_output_fields == frozenset({"item", "item_index"})

    def test_nested_explode_with_distinct_index_field_succeeds(self) -> None:
        _require_index_field_option()
        from elspeth.plugins.transforms.json_explode import JSONExplode

        transform = JSONExplode({"schema": OBSERVED, "array_field": "subs", "output_field": "sub", "index_field": "sub_index"})
        result = transform.process(make_pipeline_row({"item_index": 3, "subs": ["x", "y"]}), make_context())

        assert result.status == "success"
        assert result.rows is not None
        assert [row.to_dict() for row in result.rows] == [
            {"item_index": 3, "sub": "x", "sub_index": 0},
            {"item_index": 3, "sub": "y", "sub_index": 1},
        ]
        assert {field.name for field in transform.created_output_fields()} == {"sub", "sub_index"}

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({"index_field": "items"}, id="index-equals-array"),
            pytest.param({"index_field": "item"}, id="index-equals-output"),
            pytest.param({"index_field": "not an identifier"}, id="non-identifier"),
            pytest.param({"index_field": "  "}, id="blank"),
        ],
    )
    def test_index_field_is_validated(self, options: dict[str, Any]) -> None:
        _require_index_field_option()
        from elspeth.plugins.transforms.json_explode import JSONExplode

        with pytest.raises(PluginConfigError):
            JSONExplode({"schema": OBSERVED, "array_field": "items", **options})

    def test_index_field_unchecked_when_index_disabled(self) -> None:
        _require_index_field_option()
        from elspeth.plugins.transforms.json_explode import JSONExplode

        transform = JSONExplode({"schema": OBSERVED, "array_field": "items", "include_index": False, "index_field": "items"})
        assert transform.declared_output_fields == frozenset({"item"})


# ---------------------------------------------------------------------------
# #120 BatchTransformMixin.evict_submission keeps tracking when the buffer refuses
# ---------------------------------------------------------------------------


def _wait_for(condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        time.sleep(0.01)


class TestEvictSubmissionRefusedKeepsTracking:
    def test_refused_eviction_of_completed_row_releases_real_result(self) -> None:
        from elspeth.contracts import Determinism
        from elspeth.plugins.infrastructure.base import BaseTransform
        from elspeth.plugins.infrastructure.batching import BatchTransformMixin
        from elspeth.plugins.infrastructure.batching.ports import CollectorOutputPort

        class _Harness(BaseTransform, BatchTransformMixin):
            name = "p4_evict_harness"
            determinism = Determinism.DETERMINISTIC

            def __init__(self) -> None:
                super().__init__({"schema": OBSERVED})

            def connect_output(self, output: Any, max_pending: int = 4) -> None:
                self.init_batch_processing(max_pending=max_pending, output=output, name=self.name)

            def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
                raise NotImplementedError

            def close(self) -> None:
                self.shutdown_batch_processing()

        release_a = threading.Event()
        b_done = threading.Event()

        def slow(row: PipelineRow, ctx: TransformContext) -> TransformResult:
            assert release_a.wait(timeout=5)
            return TransformResult.success(row, success_reason={"action": "a"})

        def fast(row: PipelineRow, ctx: TransformContext) -> TransformResult:
            b_done.set()
            return TransformResult.success(row, success_reason={"action": "b"})

        collector = CollectorOutputPort()
        transform = _Harness()
        transform.connect_output(collector)
        factory = make_factory()
        token_a = TokenInfo(row_id="A", token_id="A", row_data=make_pipeline_row({}))
        token_b = TokenInfo(row_id="B", token_id="B", row_data=make_pipeline_row({}))
        try:
            transform.accept_row(make_pipeline_row({}), make_context(landscape=factory, token=token_a, state_id="sA"), slow)
            transform.accept_row(make_pipeline_row({}), make_context(landscape=factory, token=token_b, state_id="sB"), fast)
            assert b_done.wait(timeout=5)
            # B is complete but held behind A: the buffer refuses the eviction.
            _wait_for(lambda: transform._batch_buffer.pending_count == 2)
            assert transform.evict_submission("B", "sB") is False
            release_a.set()
            _wait_for(lambda: len(collector.results) == 2)
        finally:
            release_a.set()
            transform.close()

        by_token = {token.token_id: (result, state_id) for token, result, state_id in collector.results}
        result_b, state_b = by_token["B"]
        assert state_b == "sB"
        assert not isinstance(result_b, ExceptionResult), f"B's real result was replaced by {result_b!r}"
        assert isinstance(result_b, TransformResult)
        assert result_b.status == "success"


# ---------------------------------------------------------------------------
# #121 batch_distribution_profile: overflow-safe percentile interpolation
# ---------------------------------------------------------------------------


class TestDistributionProfilePercentileOverflow:
    def test_extreme_opposite_sign_values_profile_finitely(self) -> None:
        from elspeth.plugins.transforms.batch_distribution_profile import BatchDistributionProfile

        transform = BatchDistributionProfile({"schema": OBSERVED, "value_field": "v"})
        rows = [make_pipeline_row({"v": -1e308}), make_pipeline_row({"v": 1e308})]
        result = transform.process(rows, make_context())

        assert result.status == "success", result.reason
        assert result.row is not None
        assert result.row["median"] == 0.0
        assert result.row["p25"] == pytest.approx(-5e307)
        assert result.row["p75"] == pytest.approx(5e307)

    def test_ordinary_interpolation_is_unchanged(self) -> None:
        from elspeth.plugins.transforms.batch_distribution_profile import BatchDistributionProfile

        # Byte-identical to the pre-fix formula whenever upper - lower is finite.
        assert BatchDistributionProfile._percentile([0.1, 0.2, 0.3, 0.4], 0.5) == 0.2 + (0.3 - 0.2) * 0.5
        assert BatchDistributionProfile._percentile([1, 3], 0.5) == 2.0
        assert BatchDistributionProfile._percentile([0.1, 0.7], 0.25) == 0.1 + (0.7 - 0.1) * 0.25


# ---------------------------------------------------------------------------
# #135 batch_threshold_summary: a bool / numeric string is not a threshold
# ---------------------------------------------------------------------------


class TestThresholdSpecStrictValue:
    @pytest.mark.parametrize(
        "bad", [pytest.param(True, id="bool-true"), pytest.param(False, id="bool-false"), pytest.param("0.5", id="str")]
    )
    def test_non_numeric_threshold_value_is_refused(self, bad: object) -> None:
        from elspeth.plugins.transforms.batch_threshold_summary import ThresholdSpec

        with pytest.raises(ValidationError):
            ThresholdSpec.model_validate({"name": "a", "operator": ">", "value": bad})

    @pytest.mark.parametrize("good", [pytest.param(1, id="int"), pytest.param(0.5, id="float")])
    def test_int_and_float_thresholds_still_admitted(self, good: float) -> None:
        from elspeth.plugins.transforms.batch_threshold_summary import ThresholdSpec

        spec = ThresholdSpec.model_validate({"name": "a", "operator": ">", "value": good})
        assert spec.value == float(good)


# ---------------------------------------------------------------------------
# #136 batch_top_k: datetime / Decimal values are counted like group keys
# ---------------------------------------------------------------------------


class TestBatchTopKScalarValues:
    def test_datetime_values_are_counted(self) -> None:
        from elspeth.plugins.transforms.batch_top_k import BatchTopK

        day1 = datetime(2026, 1, 1, tzinfo=UTC)
        day2 = datetime(2026, 1, 2, tzinfo=UTC)
        transform = BatchTopK({"schema": OBSERVED, "field": "day", "k": 2})
        rows = [make_pipeline_row({"day": value}) for value in (day1, day2, day2)]
        result = transform.process(rows, make_context())

        assert result.status == "success", result.reason
        assert result.row is not None
        top = [(entry["value"], entry["count"]) for entry in result.row["top_values"]]
        assert top == [(day2, 2), (day1, 1)]

    def test_decimal_values_are_counted_and_non_finite_decimal_skipped(self) -> None:
        from elspeth.plugins.transforms.batch_top_k import BatchTopK

        transform = BatchTopK({"schema": OBSERVED, "field": "code", "k": 5})
        rows = [make_pipeline_row({"code": value}) for value in (Decimal("1.5"), Decimal("1.5"), Decimal("NaN"), Decimal("2"))]
        result = transform.process(rows, make_context())

        assert result.status == "success", result.reason
        assert result.row is not None
        assert result.row["non_finite_count"] == 1
        top = [(entry["value"], entry["count"]) for entry in result.row["top_values"]]
        assert top == [(Decimal("1.5"), 2), (Decimal("2"), 1)]
