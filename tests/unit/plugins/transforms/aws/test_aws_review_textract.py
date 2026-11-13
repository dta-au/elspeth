"""Regression tests for the 2026-11-13 AWS transforms review, Textract findings.

Each section names the review finding it pins
(``docs/architecture/reviews/2026-11-13-aws-transforms-review.md``).
"""

from __future__ import annotations

import hashlib
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

from sqlalchemy import select
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.mock_audit import mock_item_audit_authority
from tests.fixtures.plugins import CollectSink, ListSource
from tests.unit.engine.orchestrator.test_call_mode_session import _CURRENT_TOKEN
from tests.unit.engine.orchestrator.test_call_mode_session import _factory as _session_factory

from elspeth.contracts import CallType, RunMode, RunStatus
from elspeth.contracts.call_mode import ArchivedCallRequestEvidence, CallModeSession
from elspeth.contracts.errors import AuditIntegrityError, PluginRetryableError
from elspeth.contracts.payload_store import PayloadStore
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import stable_hash
from elspeth.core.config import ElspethSettings, RetrySettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape.schema import calls_table, node_states_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.call_mode_session import AuditedCallModeSession
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.aws.replay_sdk import ReplayOnlySDK
from elspeth.plugins.transforms.aws.textract_bucket_region import (
    BucketRegionCoordinator,
    BucketRegionProof,
    BucketRegionUnverifiedError,
    BucketRegionVerification,
    HeadBucketClient,
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


def _page(*, status: str = "SUCCEEDED", next_token: str | None = None, blocks: list[dict[str, object]] | None = None) -> AnalysisResultPage:
    return AnalysisResultPage(
        semantic_response={
            "JobStatus": status,
            "DocumentMetadata": {"Pages": 1},
            "AnalyzeDocumentModelVersion": "1.0",
            "Blocks": blocks if blocks is not None else (_blocks() if status == "SUCCEEDED" else []),
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
            _page(blocks=[]),
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


# --- Findings 2 and 3 (P2): verify / replay determinism -------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def allocate_call_index(self, _state_id: str, **_kwargs: object) -> int:
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(call_id=f"new-{len(self.calls)}")


class _VerifySession:
    """Verify session whose admission is hash-exact like AuditedCallModeSession."""

    mode = RunMode.VERIFY
    source_run_id = "source-run"

    def __init__(self, archive: dict[int, dict[str, Any]]) -> None:
        self.archive = archive
        self.admissions: list[dict[str, Any]] = []
        self.verifications: list[dict[str, Any]] = []

    def archived_call_request(
        self, *, call_type: CallType, current_state_id: str | None, current_operation_id: str | None, current_call_index: int
    ) -> ArchivedCallRequestEvidence:
        del call_type, current_state_id, current_operation_id
        if current_call_index not in self.archive:
            raise AuditIntegrityError("Exact source request is missing")
        return ArchivedCallRequestEvidence(source_call_id=f"source-{current_call_index}", request_data=self.archive[current_call_index])

    def admit_verify_call(self, **kwargs: Any) -> str:
        self.admissions.append(kwargs)
        archived = self.archive.get(kwargs["current_call_index"])
        if archived is None or stable_hash(archived) != stable_hash(kwargs["request_data"]):
            raise AuditIntegrityError("No exact source http call at parent-local index")
        return f"source-{kwargs['current_call_index']}"

    def verify_call(self, **kwargs: Any) -> None:
        self.verifications.append(kwargs)


class _PollSDK:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)

    def get_document_analysis(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self.responses.pop(0)

    def start_document_analysis(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("not used")

    def close(self) -> None:
        return None


def _textract_client(session: object, sdk: object, recorder: _Recorder) -> TextractClient:
    return TextractClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=cast("TextractSDKClient", sdk),
        max_response_bytes=100_000,
        call_mode_session=cast("CallModeSession", session),
    )


def _poll_request(*, job_id: str, next_token: str | None) -> dict[str, Any]:
    return {
        "operation": "get_document_analysis",
        "job_id": job_id,
        "next_token_present": next_token is not None,
        "next_token_fingerprint": None if next_token is None else hashlib.sha256(next_token.encode("utf-8")).hexdigest(),
        "max_results": 1000,
    }


def _sdk_page(*, status: str = "SUCCEEDED", next_token: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {"JobStatus": status, "Blocks": [], "ResponseMetadata": {"RetryAttempts": 0, "RequestId": "req"}}
    if next_token is not None:
        page["NextToken"] = next_token
    return page


def test_f2_verify_poll_admission_substitutes_the_source_job_id_and_next_token_fingerprint() -> None:
    session = _VerifySession(
        {
            0: _poll_request(job_id="source-job", next_token=None),
            1: _poll_request(job_id="source-job", next_token="source-token"),
        }
    )
    recorder = _Recorder()
    client = _textract_client(session, _PollSDK(_sdk_page(next_token="live-token"), _sdk_page()), recorder)

    first = _outcome(lambda: client.get_document_analysis(job_id="live-job", next_token=None))
    assert not isinstance(first, Exception), first
    second = _outcome(lambda: client.get_document_analysis(job_id="live-job", next_token="live-token"))
    assert not isinstance(second, Exception), second

    assert [admission["request_data"] for admission in session.admissions] == [session.archive[0], session.archive[1]]
    assert [verification["request_data"] for verification in session.verifications] == [session.archive[0], session.archive[1]]
    # The persisted call record keeps the live identity it actually sent.
    assert [call["request_data"].to_dict()["job_id"] for call in recorder.calls] == ["live-job", "live-job"]


def test_f2_verify_poll_refuses_a_source_job_id_that_changes_within_the_row() -> None:
    session = _VerifySession(
        {
            0: _poll_request(job_id="source-job", next_token=None),
            1: _poll_request(job_id="other-source-job", next_token=None),
        }
    )
    client = _textract_client(session, _PollSDK(_sdk_page(status="IN_PROGRESS"), _sdk_page()), _Recorder())

    first = _outcome(lambda: client.get_document_analysis(job_id="live-job", next_token=None))
    assert not isinstance(first, Exception), first
    second = _outcome(lambda: client.get_document_analysis(job_id="live-job", next_token=None))

    assert isinstance(second, AuditIntegrityError), second
    assert len(session.admissions) == 1


def test_f2_verify_poll_refuses_an_archived_request_of_another_operation() -> None:
    archived = _poll_request(job_id="source-job", next_token=None)
    archived["operation"] = "start_document_analysis"
    session = _VerifySession({0: archived})
    client = _textract_client(session, _PollSDK(_sdk_page()), _Recorder())

    outcome = _outcome(lambda: client.get_document_analysis(job_id="live-job", next_token=None))

    assert isinstance(outcome, AuditIntegrityError), outcome
    assert session.admissions == []


class _ReplayExistenceSession:
    mode = RunMode.REPLAY
    source_run_id = "source-run"

    def __init__(self, existing: dict[int, dict[str, Any]]) -> None:
        self.existing = existing
        self.queries: list[dict[str, Any]] = []

    def has_source_call(self, **kwargs: Any) -> bool:
        self.queries.append(kwargs)
        index = kwargs["current_call_index"]
        return index in self.existing and stable_hash(self.existing[index]) == stable_hash(kwargs["request_data"])


def test_f3_session_answers_source_call_existence_without_reading_payloads() -> None:
    assert "has_source_call" in vars(AuditedCallModeSession)
    factory, call = _session_factory()
    session = AuditedCallModeSession(
        factory, current_run_id="current", source_run_id="source", mode=RunMode.REPLAY, coordination_token=_CURRENT_TOKEN
    )
    request = {"operation": "get_document_analysis"}

    factory.execution.find_call_for_current_parent.return_value = call
    assert (
        session.has_source_call(
            call_type=CallType.HTTP, request_data=request, current_state_id="state", current_operation_id=None, current_call_index=3
        )
        is True
    )
    lookup = factory.execution.find_call_for_current_parent.call_args.kwargs
    assert lookup["request_hash"] == stable_hash(request)
    assert lookup["current_call_index"] == 3
    factory.execution.find_call_for_current_parent.return_value = None
    assert (
        session.has_source_call(
            call_type=CallType.HTTP, request_data=request, current_state_id="state", current_operation_id=None, current_call_index=4
        )
        is False
    )
    factory.execution.get_call_request_data.assert_not_called()


def test_f3_replay_client_reports_whether_the_source_made_the_next_poll() -> None:
    assert "source_has_next_poll" in vars(TextractClient)
    session = _ReplayExistenceSession({1: _poll_request(job_id="job-1", next_token=None)})
    recorder = _Recorder()
    recorder.calls.append({"call_index": 0})  # the replayed start call
    client = _textract_client(session, _PollSDK(), recorder)
    client._last_call_index = 0

    assert client.source_has_next_poll(job_id="job-1", next_token=None) is True
    assert client.source_has_next_poll(job_id="job-1", next_token="other") is False
    assert [query["current_call_index"] for query in session.queries] == [1, 1]


class _ReplayRowClient(_FakeAsyncClient):
    def __init__(self, *, pages: list[AnalysisResultPage | Exception], source_polls: int) -> None:
        super().__init__(pages=pages)
        self.source_polls = source_polls

    def source_has_next_poll(self, *, job_id: str, next_token: str | None) -> bool:
        del job_id, next_token
        return len(self.get_calls) < self.source_polls


def _replay_transform(client: _ReplayRowClient) -> AWSTextractDocumentAnalysis:
    transform = _async_transform(client)
    transform._run_mode = RunMode.REPLAY
    transform._poll_interval_seconds = 30.0
    transform._poll_max_interval_seconds = 30.0

    def _no_sleep(timeout: float) -> bool:
        raise AssertionError(f"replay slept {timeout}s between polls")

    transform._wait_for_shutdown = _no_sleep  # replay must never wait on the clock
    return transform


def test_f3_replay_poll_loop_ends_from_the_archive_without_sleeping() -> None:
    client = _ReplayRowClient(pages=[_page(status="IN_PROGRESS")] * 10, source_polls=3)
    transform = _replay_transform(client)
    started = time.monotonic()

    result = _expect_result(_run_async(transform))

    assert time.monotonic() - started < 5.0
    assert result.reason is not None
    assert result.reason["reason"] == "poll_timeout"
    assert len(client.get_calls) == 3


def test_f3_replay_poll_loop_reaches_success_without_real_sleeps() -> None:
    client = _ReplayRowClient(pages=[_page(status="IN_PROGRESS"), _page(status="IN_PROGRESS"), _page()], source_polls=3)
    transform = _replay_transform(client)
    started = time.monotonic()

    result = _expect_result(_run_async(transform))

    assert time.monotonic() - started < 5.0
    assert result.status == "success", result.reason


def test_f3_replay_poll_loop_reproduces_a_transient_escalation_at_archive_end() -> None:
    client = _ReplayRowClient(pages=[TextractServiceError(code="ThrottlingException", retryable=True)] * 5, source_polls=2)
    transform = _replay_transform(client)
    started = time.monotonic()

    error = _expect_retryable(_run_async(transform))

    assert time.monotonic() - started < 5.0
    assert "ThrottlingException" in str(error)
    assert len(client.get_calls) == 2


def _proof() -> BucketRegionProof:
    return BucketRegionProof(region="ap-southeast-2", source="response_header", http_status=200)


def test_f3_replay_follower_waits_for_the_source_leader_even_when_it_arrives_first() -> None:
    assert "verify_replay" in vars(BucketRegionCoordinator)
    coordinator = BucketRegionCoordinator()
    outcomes: list[object] = []
    follower = threading.Thread(
        target=lambda: outcomes.append(
            _outcome(lambda: coordinator.verify_replay("docs", lambda: _proof(), source_led=False, wait_seconds=10.0))
        )
    )
    follower.start()
    time.sleep(0.05)
    leader = coordinator.verify_replay("docs", lambda: _proof(), source_led=True, wait_seconds=10.0)
    follower.join(timeout=10.0)

    assert leader.cache_status == "live"
    [follower_outcome] = outcomes
    assert isinstance(follower_outcome, BucketRegionVerification), follower_outcome
    assert follower_outcome.cache_status == "cached"
    assert follower_outcome.region == "ap-southeast-2"


def test_f3_replay_source_leader_always_replays_its_own_head_bucket_call() -> None:
    assert "verify_replay" in vars(BucketRegionCoordinator)
    coordinator = BucketRegionCoordinator()
    calls: list[str] = []
    coordinator.verify_replay("docs", lambda: calls.append("first") or _proof(), source_led=True, wait_seconds=1.0)

    second = coordinator.verify_replay("docs", lambda: calls.append("second") or _proof(), source_led=True, wait_seconds=1.0)

    assert calls == ["first", "second"]
    assert second.cache_status == "live"


def test_f3_replay_follower_inherits_the_source_leader_failure() -> None:
    assert "verify_replay" in vars(BucketRegionCoordinator)
    coordinator = BucketRegionCoordinator()

    def failing() -> BucketRegionProof:
        raise BucketRegionUnverifiedError(code="NoSuchBucket", retryable=False, http_status=404)

    leader = _outcome(lambda: coordinator.verify_replay("docs", failing, source_led=True, wait_seconds=1.0))
    follower = _outcome(lambda: coordinator.verify_replay("docs", lambda: _proof(), source_led=False, wait_seconds=1.0))

    assert isinstance(leader, BucketRegionUnverifiedError) and leader.inherited is False
    assert isinstance(follower, BucketRegionUnverifiedError), follower
    assert follower.inherited is True
    assert follower.code == "NoSuchBucket"


def test_f3_replay_follower_without_a_source_leader_fails_closed() -> None:
    assert "verify_replay" in vars(BucketRegionCoordinator)
    coordinator = BucketRegionCoordinator()

    outcome = _outcome(lambda: coordinator.verify_replay("docs", lambda: _proof(), source_led=False, wait_seconds=0.05))

    assert isinstance(outcome, AuditIntegrityError), outcome


def test_f3_head_bucket_client_reads_the_leader_role_from_the_archive() -> None:
    assert "source_made_head_bucket_call" in vars(HeadBucketClient)
    request = {"operation": "head_bucket_region", "configured_region": "ap-southeast-2", "bucket": "docs"}
    session = _ReplayExistenceSession({0: request})
    client = HeadBucketClient(
        **mock_item_audit_authority("run-1"),
        execution=_Recorder(),
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=cast("S3HeadBucketSDKClient", ReplayOnlySDK()),
        call_mode_session=cast("CallModeSession", session),
    )

    assert client.source_made_head_bucket_call("docs") is True
    assert client.source_made_head_bucket_call("other-bucket") is False
    assert [query["current_call_index"] for query in session.queries] == [0, 0]
