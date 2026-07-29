"""Unit tests for the audited Amazon Textract SDK adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from elspeth.contracts import CallStatus
from elspeth.plugins.transforms.aws.textract_client import (
    TextractClient,
    TextractIdempotencyInvariantError,
    TextractResponseError,
    TextractServiceError,
    build_textract_sdk_client,
)


@dataclass
class FakeExecution:
    calls: list[dict[str, Any]] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    fail_record: bool = False

    def allocate_call_index(self, state_id: str) -> int:
        assert state_id == "state-1"
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.order.append("audit")
        if self.fail_record:
            raise RuntimeError("audit unavailable")
        self.calls.append(kwargs)
        return SimpleNamespace(id="call-1")


@dataclass
class FakeLimiter:
    acquisitions: int = 0

    def acquire(self, weight: int = 1, timeout: float | None = None) -> None:
        assert weight == 1
        assert timeout is None
        self.acquisitions += 1


@dataclass
class FakeSDK:
    start_response: object = field(default_factory=lambda: {"JobId": "job-1", "ResponseMetadata": _metadata()})
    get_responses: list[object] = field(default_factory=list)
    start_requests: list[dict[str, Any]] = field(default_factory=list)
    get_requests: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def start_document_analysis(self, **kwargs: Any) -> object:
        self.start_requests.append(kwargs)
        if isinstance(self.start_response, Exception):
            raise self.start_response
        return self.start_response

    def get_document_analysis(self, **kwargs: Any) -> object:
        self.get_requests.append(kwargs)
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _metadata(*, retries: int = 0) -> dict[str, Any]:
    return {
        "RequestId": "request-1",
        "RetryAttempts": retries,
        "HTTPStatusCode": 200,
        "HTTPHeaders": {"provider-header": "discard"},
    }


def _client(
    sdk: FakeSDK,
    *,
    fail_record: bool = False,
    max_response_bytes: int = 100_000,
) -> tuple[TextractClient, FakeExecution, list[Any], FakeLimiter]:
    execution = FakeExecution(fail_record=fail_record)
    events: list[Any] = []
    limiter = FakeLimiter()
    client = TextractClient(
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: (execution.order.append("telemetry"), events.append(event)),
        region="ap-southeast-2",
        sdk_client=sdk,
        max_response_bytes=max_response_bytes,
        limiter=limiter,
        token_id="token-1",
    )
    return client, execution, events, limiter


def _start(client: TextractClient):
    return client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version="v1",
        feature_types=("FORMS", "TABLES"),
        queries=(),
        client_request_token="a" * 64,
    )


def test_start_records_before_telemetry_and_returns_job_id() -> None:
    sdk = FakeSDK()
    client, recorder, events, limiter = _client(sdk)

    receipt = _start(client)

    assert receipt.job_id == "job-1"
    assert sdk.start_requests == [
        {
            "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf", "Version": "v1"}},
            "FeatureTypes": ["FORMS", "TABLES"],
            "ClientRequestToken": "a" * 64,
        }
    ]
    assert limiter.acquisitions == 1
    assert recorder.order == ["audit", "telemetry"]
    audited = recorder.calls[0]
    assert audited["status"] is CallStatus.SUCCESS
    request_payload = audited["request_data"].to_dict()
    assert request_payload["client_request_token_fingerprint"]
    assert "a" * 64 not in repr(recorder.calls[0])
    assert events[0].provider == "aws-textract"


def test_start_omits_version_and_queries_when_absent() -> None:
    sdk = FakeSDK()
    client, _, _, _ = _client(sdk)

    client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version=None,
        feature_types=("TABLES",),
        queries=(),
        client_request_token="b" * 64,
    )

    assert sdk.start_requests == [
        {
            "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf"}},
            "FeatureTypes": ["TABLES"],
            "ClientRequestToken": "b" * 64,
        }
    ]


def test_start_sorts_features_and_builds_queries_config() -> None:
    sdk = FakeSDK()
    client, recorder, _, _ = _client(sdk)

    client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version=None,
        feature_types=("TABLES", "QUERIES", "FORMS"),
        queries=(
            {"text": "What is the total?", "alias": "total", "pages": ["1-3"]},
            {"text": "Who approved it?", "alias": None, "pages": []},
        ),
        client_request_token="c" * 64,
    )

    assert sdk.start_requests[0]["FeatureTypes"] == ["FORMS", "QUERIES", "TABLES"]
    assert sdk.start_requests[0]["QueriesConfig"] == {
        "Queries": [
            {"Text": "What is the total?", "Alias": "total", "Pages": ["1-3"]},
            {"Text": "Who approved it?"},
        ]
    }
    assert recorder.calls[0]["request_data"].to_dict()["queries"] == [
        {"Text": "What is the total?", "Alias": "total", "Pages": ["1-3"]},
        {"Text": "Who approved it?"},
    ]


def test_get_replaces_raw_next_token_with_fingerprint_in_audit() -> None:
    marker = "provider-document-text"
    sdk = FakeSDK(
        get_responses=[
            {
                "JobStatus": "SUCCEEDED",
                "DocumentMetadata": {"Pages": 1},
                "AnalyzeDocumentModelVersion": "1.0",
                "Blocks": [{"BlockType": "LINE", "Id": "line-1", "Text": marker}],
                "NextToken": "opaque-next-token",
                "ResponseMetadata": _metadata(retries=2),
            }
        ]
    )
    client, recorder, events, limiter = _client(sdk)

    page = client.get_document_analysis(job_id="job-1", next_token=None)

    assert page.next_token == "opaque-next-token"
    assert page.semantic_response["Blocks"][0]["Text"] == marker
    assert sdk.get_requests == [{"JobId": "job-1", "MaxResults": 1000}]
    assert limiter.acquisitions == 1
    audited = recorder.calls[0]["response_data"].to_dict()
    assert audited["attempts"] == 3
    assert audited["next_token_present"] is True
    assert audited["next_token_fingerprint"]
    assert "opaque-next-token" not in repr(audited)
    assert marker in repr(audited)
    assert marker not in repr(events)
    assert "provider-header" not in repr(audited)


def test_get_supplies_raw_next_token_only_to_sdk() -> None:
    sdk = FakeSDK(
        get_responses=[
            {
                "JobStatus": "IN_PROGRESS",
                "DocumentMetadata": {"Pages": 1},
                "Blocks": [],
                "ResponseMetadata": _metadata(),
            }
        ]
    )
    client, recorder, _, _ = _client(sdk)

    page = client.get_document_analysis(job_id="job-1", next_token="private-token")

    assert sdk.get_requests == [{"JobId": "job-1", "MaxResults": 1000, "NextToken": "private-token"}]
    assert page.next_token is None
    assert "private-token" not in repr(recorder.calls)
    assert recorder.calls[0]["request_data"].to_dict()["next_token_present"] is True


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"JobId": ""},
        {"JobId": 7},
        {"JobId": "job-1", "ResponseMetadata": {"RetryAttempts": -1}},
    ],
)
def test_malformed_start_response_is_audited_and_fails_closed(response: object) -> None:
    sdk = FakeSDK(start_response=response)
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractResponseError):
        _start(client)

    assert recorder.calls[0]["status"] is CallStatus.ERROR
    assert recorder.calls[0]["error"].to_dict()["type"] == "malformed_response"


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"JobStatus": "SUCCEEDED", "Blocks": [], "NextToken": 7},
        {"JobStatus": "SUCCEEDED", "Blocks": [], "ResponseMetadata": {"RetryAttempts": 99}},
    ],
)
def test_malformed_get_response_is_audited_and_fails_closed(response: object) -> None:
    sdk = FakeSDK(get_responses=[response])
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractResponseError):
        client.get_document_analysis(job_id="job-1", next_token=None)

    assert recorder.calls[0]["status"] is CallStatus.ERROR


def test_unencodable_next_token_is_audited_before_failure() -> None:
    sdk = FakeSDK(
        get_responses=[
            {
                "JobStatus": "SUCCEEDED",
                "Blocks": [],
                "NextToken": "\ud800",
                "ResponseMetadata": _metadata(),
            }
        ]
    )
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractResponseError):
        client.get_document_analysis(job_id="job-1", next_token=None)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] is CallStatus.ERROR


def test_deep_provider_shape_is_audited_before_failure() -> None:
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(2_000):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    sdk = FakeSDK(
        get_responses=[
            {
                "JobStatus": "SUCCEEDED",
                "Blocks": [],
                "Deep": nested,
                "ResponseMetadata": _metadata(),
            }
        ]
    )
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractResponseError):
        client.get_document_analysis(job_id="job-1", next_token=None)

    assert len(recorder.calls) == 1
    assert recorder.calls[0]["status"] is CallStatus.ERROR


def test_oversized_semantic_response_is_audited_and_fails_closed() -> None:
    sdk = FakeSDK(
        get_responses=[
            {
                "JobStatus": "SUCCEEDED",
                "Blocks": [{"BlockType": "LINE", "Id": "line-1", "Text": "x" * 1000}],
                "ResponseMetadata": _metadata(),
            }
        ]
    )
    client, recorder, _, _ = _client(sdk, max_response_bytes=100)

    with pytest.raises(TextractResponseError, match="maximum response size"):
        client.get_document_analysis(job_id="job-1", next_token=None)

    assert recorder.calls[0]["error"].to_dict()["type"] == "response_too_large"
    assert "x" * 1000 not in repr(recorder.calls)


def _client_error(code: str, *, message: str = "provider-private-message", retries: int = 0) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"RequestId": "request-error", "RetryAttempts": retries},
        },
        "StartDocumentAnalysis",
    )


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("InternalServerError", True),
        ("ThrottlingException", True),
        ("ProvisionedThroughputExceededException", True),
        ("LimitExceededException", True),
        ("AccessDeniedException", False),
        ("InvalidS3ObjectException", False),
        ("BadDocumentException", False),
        ("UnsupportedDocumentException", False),
        ("DocumentTooLargeException", False),
        ("InvalidParameterException", False),
    ],
)
def test_client_error_codes_are_sanitized_and_classified(code: str, retryable: bool) -> None:
    sdk = FakeSDK(start_response=_client_error(code, retries=2))
    client, recorder, events, _ = _client(sdk)

    with pytest.raises(TextractServiceError) as exc_info:
        _start(client)

    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    audited = recorder.calls[0]
    assert audited["response_data"].to_dict()["attempts"] == 3
    assert audited["error"].to_dict() == {"type": "service_error", "code": code, "retryable": retryable}
    assert "provider-private-message" not in repr((exc_info.value, recorder.calls, events))


def test_transport_error_is_retryable_and_sanitized() -> None:
    sdk = FakeSDK(start_response=EndpointConnectionError(endpoint_url="https://private.example.invalid"))
    client, recorder, events, _ = _client(sdk)

    with pytest.raises(TextractServiceError) as exc_info:
        _start(client)

    assert exc_info.value.code == "transport_error"
    assert exc_info.value.retryable is True
    assert recorder.calls[0]["response_data"].to_dict()["attempts"] == 3
    assert "private.example.invalid" not in repr((exc_info.value, recorder.calls, events))


def test_idempotency_mismatch_raises_distinct_invariant_error_after_audit() -> None:
    sdk = FakeSDK(start_response=_client_error("IdempotentParameterMismatchException"))
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractIdempotencyInvariantError):
        _start(client)

    assert recorder.calls[0]["error"].to_dict()["type"] == "idempotency_invariant"


def test_audit_failure_suppresses_telemetry() -> None:
    sdk = FakeSDK()
    client, recorder, events, _ = _client(sdk, fail_record=True)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _start(client)

    assert recorder.order == ["audit"]
    assert events == []


def test_builder_uses_standard_retry_policy_and_explicit_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    sentinel = SimpleNamespace(close=lambda: None)

    def fake_client(service: str, **kwargs: Any) -> object:
        captured["service"] = service
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr("boto3.client", fake_client)

    built = build_textract_sdk_client(
        region="ap-southeast-2",
        aws_access_key_id="resolved-access-id",
        aws_secret_access_key="resolved-secret-key",
        aws_session_token="resolved-session-token",
    )

    assert built is sentinel
    assert captured["service"] == "textract"
    kwargs = captured["kwargs"]
    assert kwargs["region_name"] == "ap-southeast-2"
    assert kwargs["aws_access_key_id"] == "resolved-access-id"
    assert kwargs["aws_secret_access_key"] == "resolved-secret-key"
    assert kwargs["aws_session_token"] == "resolved-session-token"
    assert kwargs["config"].connect_timeout == 10
    assert kwargs["config"].read_timeout == 30
    assert kwargs["config"].retries == {"mode": "standard", "total_max_attempts": 3}


def test_builder_omits_all_explicit_credentials_for_default_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("boto3.client", fake_client)

    build_textract_sdk_client(
        region="ap-southeast-2",
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
    )

    assert "aws_access_key_id" not in captured
    assert "aws_secret_access_key" not in captured
    assert "aws_session_token" not in captured
