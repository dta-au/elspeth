"""Unit tests for the audited synchronous Amazon Textract adapter.

``TextractInlineClient`` shares the async adapter's audit/telemetry
discipline; these tests pin the AnalyzeDocument specifics: exact
``Document.Bytes`` construction, hash-not-bytes audit identity, bounded
semantic retention, and the sanitized error taxonomy without the
idempotency invariant (AnalyzeDocument sends no client token).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber

from elspeth.contracts import CallStatus
from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.plugins.transforms.aws.textract_client import (
    TextractInlineClient,
    TextractResponseError,
    TextractServiceError,
    build_textract_sync_sdk_client,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\x08" + b"inline-document-payload"
_PNG_SHA256 = hashlib.sha256(_PNG_BYTES).hexdigest()


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
class FakeSyncSDK:
    analyze_response: object = field(default_factory=lambda: _analyze_response())
    analyze_requests: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def analyze_document(self, **kwargs: Any) -> object:
        self.analyze_requests.append(kwargs)
        if isinstance(self.analyze_response, Exception):
            raise self.analyze_response
        return self.analyze_response

    def close(self) -> None:
        self.closed = True


def _metadata(*, retries: int = 0) -> dict[str, Any]:
    return {
        "RequestId": "request-1",
        "RetryAttempts": retries,
        "HTTPStatusCode": 200,
        "HTTPHeaders": {"provider-header": "discard"},
    }


def _analyze_response(*, retries: int = 0) -> dict[str, Any]:
    return {
        "DocumentMetadata": {"Pages": 1},
        "AnalyzeDocumentModelVersion": "1.0",
        "Blocks": [{"BlockType": "PAGE", "Id": "page-1", "Page": 1}],
        "ResponseMetadata": _metadata(retries=retries),
    }


def _client_error(code: str, *, message: str = "provider-private-message", retries: int = 0) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"RequestId": "request-error", "RetryAttempts": retries},
        },
        "AnalyzeDocument",
    )


def _client(
    sdk: FakeSyncSDK,
    *,
    fail_record: bool = False,
    max_response_bytes: int = 100_000,
) -> tuple[TextractInlineClient, FakeExecution, list[Any], FakeLimiter]:
    execution = FakeExecution(fail_record=fail_record)
    events: list[Any] = []
    limiter = FakeLimiter()
    client = TextractInlineClient(
        execution=cast("CallRecorder", execution),
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


def _analyze(client: TextractInlineClient, **overrides: Any):
    arguments: dict[str, Any] = {
        "document_bytes": _PNG_BYTES,
        "document_sha256": _PNG_SHA256,
        "document_format": "png",
        "feature_types": ("TABLES", "FORMS"),
        "queries": (),
    }
    arguments.update(overrides)
    return client.analyze_document(**arguments)


def test_analyze_records_before_telemetry_and_returns_frozen_semantic_response() -> None:
    sdk = FakeSyncSDK()
    client, recorder, events, limiter = _client(sdk)

    result = _analyze(client)

    assert result["DocumentMetadata"] == {"Pages": 1}
    assert "ResponseMetadata" not in result
    assert limiter.acquisitions == 1
    assert recorder.order == ["audit", "telemetry"]
    audited = recorder.calls[0]
    assert audited["status"] is CallStatus.SUCCESS
    assert events[0].provider == "aws-textract"
    with pytest.raises(TypeError):
        cast(Any, result)["injected"] = "value"


def test_analyze_sends_exact_bytes_and_sorted_features_without_base64() -> None:
    sdk = FakeSyncSDK()
    client, _recorder, _, _ = _client(sdk)

    _analyze(client)

    assert sdk.analyze_requests == [
        {
            "Document": {"Bytes": _PNG_BYTES},
            "FeatureTypes": ["FORMS", "TABLES"],
        }
    ]
    # The SDK receives the original bytes object — no copy, no base64.
    assert sdk.analyze_requests[0]["Document"]["Bytes"] is _PNG_BYTES


def test_analyze_builds_queries_config_and_audits_hash_not_bytes() -> None:
    sdk = FakeSyncSDK()
    client, recorder, _, _ = _client(sdk)

    _analyze(
        client,
        feature_types=("QUERIES", "TABLES"),
        queries=({"text": "What is the total?", "alias": "total", "pages": ["1"]},),
    )

    assert sdk.analyze_requests[0]["QueriesConfig"] == {"Queries": [{"Text": "What is the total?", "Alias": "total", "Pages": ["1"]}]}
    request_payload = recorder.calls[0]["request_data"].to_dict()
    assert request_payload == {
        "operation": "analyze_document",
        "region": "ap-southeast-2",
        "document_sha256": _PNG_SHA256,
        "document_size_bytes": len(_PNG_BYTES),
        "document_format": "png",
        "feature_types": ["QUERIES", "TABLES"],
        "queries": [{"Text": "What is the total?", "Alias": "total", "Pages": ["1"]}],
    }
    # Document content never enters the audit record in any encoding.
    audited_repr = repr(recorder.calls[0])
    assert "inline-document-payload" not in audited_repr
    assert "Bytes" not in audited_repr


def test_analyze_success_audit_retains_semantic_response_without_response_metadata() -> None:
    sdk = FakeSyncSDK()
    client, recorder, _, _ = _client(sdk)

    _analyze(client)

    response_payload = recorder.calls[0]["response_data"].to_dict()
    assert response_payload["status"] == "success"
    assert response_payload["attempts"] == 1
    assert response_payload["request_id_present"] is True
    assert "ResponseMetadata" not in response_payload["semantic_response"]
    assert response_payload["semantic_response"]["Blocks"] == [{"BlockType": "PAGE", "Id": "page-1", "Page": 1}]
    assert "provider-header" not in repr(recorder.calls[0])


def test_analyze_response_too_large_fails_closed() -> None:
    sdk = FakeSyncSDK()
    client, recorder, _, _ = _client(sdk, max_response_bytes=10)

    with pytest.raises(TextractResponseError) as exc_info:
        _analyze(client)

    assert exc_info.value.category == "response_too_large"
    audited = recorder.calls[0]
    assert audited["status"] is CallStatus.ERROR
    assert audited["response_data"].to_dict()["status"] == "response_too_large"
    assert audited["error"].to_dict() == {"type": "response_too_large", "retryable": False}


def test_analyze_non_mapping_response_is_malformed() -> None:
    sdk = FakeSyncSDK(analyze_response=["not", "a", "mapping"])
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractResponseError) as exc_info:
        _analyze(client)

    assert exc_info.value.category == "malformed_response"
    assert recorder.calls[0]["error"].to_dict() == {"type": "malformed_response", "retryable": False}


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("InternalServerError", True),
        ("ThrottlingException", True),
        ("ProvisionedThroughputExceededException", True),
        ("LimitExceededException", True),
        ("AccessDeniedException", False),
        ("BadDocumentException", False),
        ("UnsupportedDocumentException", False),
        ("DocumentTooLargeException", False),
        ("InvalidParameterException", False),
    ],
)
def test_analyze_client_error_codes_are_sanitized_and_classified(code: str, retryable: bool) -> None:
    sdk = FakeSyncSDK(analyze_response=_client_error(code, retries=2))
    client, recorder, events, _ = _client(sdk)

    with pytest.raises(TextractServiceError) as exc_info:
        _analyze(client)

    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    audited = recorder.calls[0]
    assert audited["response_data"].to_dict()["attempts"] == 3
    assert audited["error"].to_dict() == {"type": "service_error", "code": code, "retryable": retryable}
    assert "provider-private-message" not in repr((exc_info.value, recorder.calls, events))


def test_analyze_idempotency_mismatch_is_an_ordinary_service_error() -> None:
    """AnalyzeDocument sends no client token, so the invariant error cannot apply."""
    sdk = FakeSyncSDK(analyze_response=_client_error("IdempotentParameterMismatchException"))
    client, recorder, _, _ = _client(sdk)

    with pytest.raises(TextractServiceError) as exc_info:
        _analyze(client)

    assert exc_info.value.code == "IdempotentParameterMismatchException"
    assert exc_info.value.retryable is False
    assert recorder.calls[0]["error"].to_dict()["type"] == "service_error"


def test_analyze_transport_error_is_retryable_and_sanitized() -> None:
    sdk = FakeSyncSDK(analyze_response=EndpointConnectionError(endpoint_url="https://private.example.invalid"))
    client, recorder, events, _ = _client(sdk)

    with pytest.raises(TextractServiceError) as exc_info:
        _analyze(client)

    assert exc_info.value.code == "transport_error"
    assert exc_info.value.retryable is True
    assert "private.example.invalid" not in repr((exc_info.value, recorder.calls, events))


def test_analyze_audit_failure_suppresses_telemetry() -> None:
    sdk = FakeSyncSDK()
    client, recorder, events, _ = _client(sdk, fail_record=True)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _analyze(client)

    assert events == []
    assert recorder.order == ["audit"]


def test_analyze_document_request_matches_the_service_model() -> None:
    """Real botocore Stubber validation of the AnalyzeDocument request shape."""
    sdk = build_textract_sync_sdk_client(
        region="ap-southeast-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        aws_session_token=None,
        read_timeout=120.0,
    )
    execution = FakeExecution()
    events: list[Any] = []
    client = TextractInlineClient(
        execution=cast("CallRecorder", execution),
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=events.append,
        region="ap-southeast-2",
        sdk_client=sdk,
        max_response_bytes=100_000,
    )
    with Stubber(cast(Any, sdk)) as stubber:
        stubber.add_response(
            "analyze_document",
            {
                "DocumentMetadata": {"Pages": 1},
                "AnalyzeDocumentModelVersion": "1.0",
                "Blocks": [{"BlockType": "PAGE", "Id": "page-1", "Page": 1}],
                "ResponseMetadata": _metadata(),
            },
            expected_params={
                "Document": {"Bytes": _PNG_BYTES},
                "FeatureTypes": ["FORMS", "TABLES"],
            },
        )
        result = _analyze(client)
        stubber.assert_no_pending_responses()

    assert result["AnalyzeDocumentModelVersion"] == "1.0"
    assert execution.calls[0]["status"] is CallStatus.SUCCESS
    assert cast(Any, sdk).meta.config.read_timeout == 120.0
    assert cast(Any, sdk).meta.config.connect_timeout == 10
