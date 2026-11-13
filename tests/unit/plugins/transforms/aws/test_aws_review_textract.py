"""Regression tests for the 2026-11-13 AWS transforms review, Textract findings.

Each section names the review finding it pins
(``docs/architecture/reviews/2026-11-13-aws-transforms-review.md``).
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

from sqlalchemy import select
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink, ListSource

from elspeth.contracts import RunStatus
from elspeth.contracts.errors import PluginRetryableError
from elspeth.contracts.payload_store import PayloadStore
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.config import ElspethSettings, RetrySettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape.schema import calls_table, node_states_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.aws.textract_bucket_region import (
    BucketRegionCoordinator,
    BucketRegionUnverifiedError,
    BucketRegionVerification,
    S3HeadBucketSDKClient,
)
from elspeth.plugins.transforms.aws.textract_client import (
    AnalysisResultPage,
    InlineAnalysisResult,
    StartAnalysisReceipt,
    TextractClient,
    TextractInlineClient,
    TextractSDKClient,
    TextractServiceError,
)
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysis
from elspeth.testing import make_pipeline_row

# --- shared fakes -------------------------------------------------------------


def _async_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "region": "ap-southeast-2",
        "bucket_field": "document_bucket",
        "key_field": "document_key",
        "feature_types": ["FORMS"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


def _blocks() -> list[dict[str, object]]:
    return [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1},
        {"BlockType": "LINE", "Id": "line-1", "Page": 1, "Text": "Document text", "Confidence": 99.0},
    ]


def _page(*, status: str = "SUCCEEDED", next_token: str | None = None) -> AnalysisResultPage:
    return AnalysisResultPage(
        semantic_response={
            "JobStatus": status,
            "DocumentMetadata": {"Pages": 1},
            "AnalyzeDocumentModelVersion": "1.0",
            "Blocks": _blocks() if status == "SUCCEEDED" else [],
        },
        next_token=next_token,
    )


class _FakeAsyncClient:
    """Row-client stand-in for the audited ``TextractClient``."""

    def __init__(self, *, pages: list[AnalysisResultPage | Exception], start: StartAnalysisReceipt | Exception | None = None) -> None:
        self.pages = list(pages)
        self.start_result: StartAnalysisReceipt | Exception = StartAnalysisReceipt(job_id="job-1") if start is None else start
        self.start_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def start_document_analysis(self, **kwargs: object) -> StartAnalysisReceipt:
        self.start_calls.append(kwargs)
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def get_document_analysis(self, **kwargs: object) -> AnalysisResultPage:
        self.get_calls.append(kwargs)
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeCoordinator:
    def __init__(self, outcome: BucketRegionVerification | Exception | None = None) -> None:
        self.outcome = outcome or BucketRegionVerification(
            region="ap-southeast-2", source="response_header", http_status=200, cache_status="live"
        )

    def verify(self, bucket: str, live_verify: object) -> BucketRegionVerification:
        del bucket, live_verify
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _async_transform(
    client: _FakeAsyncClient, coordinator: _FakeCoordinator | None = None, **overrides: object
) -> AWSTextractDocumentAnalysis:
    transform = AWSTextractDocumentAnalysis(_async_config(**overrides))
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    transform._poll_interval_seconds = 0.001
    transform._poll_max_interval_seconds = 0.001
    transform._row_clients["state-1"] = cast("TextractClient", client)
    transform._bucket_region_coordinator = cast("BucketRegionCoordinator", coordinator or _FakeCoordinator())
    return transform


def _row() -> PipelineRow:
    return make_pipeline_row({"document_bucket": "docs", "document_key": "invoice.pdf"})


def _outcome(call: Any) -> object:
    """The transform's return value, or the exception it raised."""
    try:
        return call()
    except Exception as error:
        return error


def _run_async(transform: AWSTextractDocumentAnalysis) -> object:
    return _outcome(lambda: transform._process_single_with_state(_row(), "state-1", ctx=make_context(run_id="run-1"), token_id="token-1"))


def _expect_result(outcome: object) -> TransformResult:
    assert isinstance(outcome, TransformResult), f"expected a row result, got {outcome!r}"
    return outcome


def _expect_retryable(outcome: object) -> PluginRetryableError:
    assert isinstance(outcome, PluginRetryableError), f"expected a raised PluginRetryableError, got {outcome!r}"
    assert outcome.retryable is True
    return outcome


# --- Finding 1 (P1): retryable Textract failures must reach the engine retry ---
#
# The engine retries only a RAISED PluginRetryableError (engine/processor.py,
# ``is_retryable``); ``TransformResult.error(retryable=True)`` is never read.


def test_f1_retryable_submit_error_raises_plugin_retryable_error() -> None:
    client = _FakeAsyncClient(pages=[], start=TextractServiceError(code="ThrottlingException", retryable=True))

    error = _expect_retryable(_run_async(_async_transform(client)))

    assert "ThrottlingException" in str(error)
    assert len(client.start_calls) == 1
    assert client.get_calls == []


def test_f1_non_retryable_submit_error_stays_a_row_error() -> None:
    client = _FakeAsyncClient(pages=[], start=TextractServiceError(code="AccessDeniedException", retryable=False))

    result = _expect_result(_run_async(_async_transform(client)))

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "submit_failed"
    assert result.retryable is False


def test_f1_retryable_poll_error_is_retried_in_place_without_resubmitting() -> None:
    client = _FakeAsyncClient(
        pages=[
            _page(status="IN_PROGRESS"),
            TextractServiceError(code="ThrottlingException", retryable=True),
            TextractServiceError(code="InternalServerError", retryable=True),
            _page(),
        ]
    )

    result = _expect_result(_run_async(_async_transform(client)))

    assert result.status == "success", result.reason
    assert len(client.start_calls) == 1
    assert len(client.get_calls) == 4
    assert {call["job_id"] for call in client.get_calls} == {"job-1"}


def test_f1_retryable_poll_error_during_pagination_repeats_the_same_page_request() -> None:
    client = _FakeAsyncClient(
        pages=[
            _page(next_token="page-2"),
            TextractServiceError(code="ThrottlingException", retryable=True),
            _page(),
        ]
    )

    result = _expect_result(_run_async(_async_transform(client)))

    assert result.status == "success", result.reason
    assert [call["next_token"] for call in client.get_calls] == [None, "page-2", "page-2"]


def test_f1_retryable_poll_error_outlasting_the_deadline_escalates_to_engine_retry() -> None:
    client = _FakeAsyncClient(pages=[TextractServiceError(code="ThrottlingException", retryable=True)] * 100_000)
    transform = _async_transform(client)
    transform._poll_timeout_seconds = 0.05

    error = _expect_retryable(_run_async(transform))

    assert "ThrottlingException" in str(error)
    assert len(client.start_calls) == 1
    assert len(client.get_calls) >= 2


def test_f1_non_retryable_poll_error_stays_a_row_error() -> None:
    client = _FakeAsyncClient(pages=[TextractServiceError(code="AccessDeniedException", retryable=False)])

    result = _expect_result(_run_async(_async_transform(client)))

    assert result.reason is not None
    assert result.reason["reason"] == "poll_failed"
    assert result.retryable is False
    assert len(client.get_calls) == 1


def test_f1_poll_timeout_is_an_honest_permanent_row_error() -> None:
    """Decision: the author's poll deadline is final; the result no longer claims a retry."""
    client = _FakeAsyncClient(pages=[_page(status="IN_PROGRESS")] * 100_000)
    transform = _async_transform(client)
    transform._poll_timeout_seconds = 0.02

    result = _expect_result(_run_async(transform))

    assert result.reason is not None
    assert result.reason["reason"] == "poll_timeout"
    assert result.retryable is False


def test_f1_retryable_unverified_bucket_region_raises_plugin_retryable_error() -> None:
    client = _FakeAsyncClient(pages=[])
    coordinator = _FakeCoordinator(BucketRegionUnverifiedError(code="transport_error", retryable=True))

    error = _expect_retryable(_run_async(_async_transform(client, coordinator)))

    assert "transport_error" in str(error)
    assert client.start_calls == []


def test_f1_non_retryable_unverified_bucket_region_stays_a_row_error() -> None:
    client = _FakeAsyncClient(pages=[])
    coordinator = _FakeCoordinator(BucketRegionUnverifiedError(code="NoSuchBucket", retryable=False))

    result = _expect_result(_run_async(_async_transform(client, coordinator)))

    assert result.reason == {"reason": "bucket_region_unverified", "error_type": "NoSuchBucket"}
    assert result.retryable is False


# Inline transform.

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"inline-analysis-document"
_PNG_SHA256 = hashlib.sha256(_PNG_BYTES).hexdigest()


class _FakePayloadStore:
    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        assert content_hash == _PNG_SHA256
        return _PNG_BYTES if len(_PNG_BYTES) <= max_bytes else None


class _FakeInlineClient:
    def __init__(self, result: InlineAnalysisResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def analyze_document(self, **kwargs: object) -> InlineAnalysisResult:
        del kwargs
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _run_inline(client: _FakeInlineClient, **overrides: object) -> object:
    config: dict[str, object] = {
        "region": "ap-southeast-2",
        "document_format": "png",
        "feature_types": ["FORMS"],
        "text_field": "textract_text",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    transform = AWSTextractInlineAnalysis(config)
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    transform._payload_store = cast("PayloadStore", _FakePayloadStore())
    transform._row_clients["state-1"] = cast("TextractInlineClient", client)
    row = make_pipeline_row({"blob_ref": _PNG_SHA256})
    return _outcome(lambda: transform._process_single_with_state(row, "state-1", ctx=make_context(run_id="run-1"), token_id="token-1"))


def test_f1_inline_retryable_service_error_raises_plugin_retryable_error() -> None:
    client = _FakeInlineClient(TextractServiceError(code="ThrottlingException", retryable=True))

    error = _expect_retryable(_run_inline(client))

    assert "ThrottlingException" in str(error)
    assert client.calls == 1


def test_f1_inline_non_retryable_service_error_stays_a_row_error() -> None:
    client = _FakeInlineClient(TextractServiceError(code="AccessDeniedException", retryable=False))

    result = _expect_result(_run_inline(client))

    assert result.reason is not None
    assert result.reason["reason"] == "analysis_failed"
    assert result.reason["code"] == "AccessDeniedException"
    assert result.retryable is False


def test_f1_inline_hint_states_engine_retries_are_conditional_on_retry_policy() -> None:
    assistance = AWSTextractInlineAnalysis.get_agent_assistance()
    assert assistance is not None
    [hint] = [hint for hint in assistance.composer_hints if "idempotency" in hint]

    assert "when the pipeline configures retry" in hint


# Real engine path: the throttled row is retried by RowProcessor through the
# batch waiter and re-attaches to the same job via the deterministic
# ClientRequestToken.


class _ThrottleOnceTextractSDK:
    def __init__(self) -> None:
        self.start_tokens: list[str] = []
        self.get_job_ids: list[str] = []

    def start_document_analysis(self, **kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.start_tokens.append(kwargs["ClientRequestToken"])
        if len(self.start_tokens) == 1:
            raise ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}, "ResponseMetadata": {"RetryAttempts": 2}},
                "StartDocumentAnalysis",
            )
        return {"JobId": "job-1", "ResponseMetadata": {"RetryAttempts": 0, "RequestId": "req-start"}}

    def get_document_analysis(self, **kwargs: Any) -> dict[str, Any]:
        self.get_job_ids.append(kwargs["JobId"])
        return {
            "JobStatus": "SUCCEEDED",
            "DocumentMetadata": {"Pages": 1},
            "AnalyzeDocumentModelVersion": "1.0",
            "Blocks": _blocks(),
            "ResponseMetadata": {"RetryAttempts": 0, "RequestId": "req-get"},
        }

    def close(self) -> None:
        return None


class _HeadBucketSDK:
    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"BucketRegion": "ap-southeast-2", "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}}

    def close(self) -> None:
        return None


def test_f1_engine_retries_a_throttled_submit_and_reattaches_to_the_same_job(tmp_path: Any) -> None:
    sdk = _ThrottleOnceTextractSDK()
    transform = AWSTextractDocumentAnalysis(_async_config())
    transform._sdk_client = cast("TextractSDKClient", sdk)
    transform._s3_sdk_client = cast("S3HeadBucketSDKClient", _HeadBucketSDK())
    transform.on_success = "output"
    transform.on_error = "quarantine"

    source_name = "documents"
    source = ListSource([{"document_bucket": "docs", "document_key": "invoice.pdf"}], name="document_list", on_success="documents_out")
    source_settings = SourceSettings(plugin=source.name, on_success="documents_out", options={})
    transform_settings = TransformSettings(
        name="textract",
        plugin=transform.name,
        input="documents_out",
        on_success="output",
        on_error="quarantine",
        options=_async_config(),
    )
    sinks = {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}
    graph = ExecutionGraph.from_plugin_instances(
        sources={source_name: as_source(source)},
        source_settings_map={source_name: source_settings},
        transforms=[WiredTransform(plugin=as_transform(transform), settings=transform_settings)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={},
        gates=[],
    )
    settings = ElspethSettings(
        sources={source_name: source_settings},
        transforms=[transform_settings],
        sinks={name: SinkSettings(plugin=sink.name, options={}, on_write_failure="discard") for name, sink in sinks.items()},
        retry=RetrySettings(max_attempts=3, initial_delay_seconds=0.01, max_delay_seconds=0.1, exponential_base=2.0),
    )
    config = PipelineConfig(
        sources={source_name: as_source(source)},
        transforms=[as_transform(transform)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
    )
    db = make_landscape_db()

    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )

    assert sinks["quarantine"].results == []
    assert len(sinks["output"].results) == 1
    assert sinks["output"].results[0]["textract_text"] == "Document text"
    assert result.status is RunStatus.COMPLETED
    # Two submits, one per engine attempt, carrying the same deterministic
    # token: AWS returns the same JobId instead of starting a second job.
    assert len(sdk.start_tokens) == 2
    assert sdk.start_tokens[0] == sdk.start_tokens[1]
    assert sdk.get_job_ids == ["job-1"]
    with db.engine.connect() as conn:
        states = conn.execute(
            select(node_states_table)
            .where(node_states_table.c.run_id == result.run_id)
            .where(node_states_table.c.node_id == transform.node_id)
            .order_by(node_states_table.c.attempt)
        ).all()
        calls = conn.execute(
            select(calls_table).where(calls_table.c.state_id.in_([state.state_id for state in states])).order_by(calls_table.c.call_index)
        ).all()
    assert [(state.attempt, state.status) for state in states] == [(0, "failed"), (1, "completed")]
    first_attempt_calls = [call for call in calls if call.state_id == states[0].state_id]
    # The failed attempt's throttled submit is in the audit trail before the retry.
    assert [call.status for call in first_attempt_calls] == ["success", "error"]  # HeadBucket, throttled submit
