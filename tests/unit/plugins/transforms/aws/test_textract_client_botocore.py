"""Offline real-botocore proof for the audited Amazon Textract adapter.

Every other Textract unit test substitutes a hand-written fake for the SDK
client. Because the boto3 client class is synthesised at runtime behind
``BaseClient.__getattr__``, such a fake accepts any keyword and returns any
key, so the kwargs ELSPETH builds have never been checked against botocore's
shipped service model. ``botocore.stub.Stubber`` wraps the real client built by
``build_textract_sdk_client`` and validates both directions, turning what would
otherwise be a ``ParamValidationError`` on a customer's first live document into
a unit-test failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from botocore.stub import Stubber

from elspeth.contracts import CallStatus
from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.plugins.transforms.aws.textract_client import (
    TextractClient,
    TextractIdempotencyInvariantError,
    TextractSDKClient,
    build_textract_sdk_client,
)

_REGION = "ap-southeast-2"
_TOKEN = "a" * 64


@dataclass
class RecordingExecution:
    """Named recorder standing in for the audit sink; not a mock."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str) -> int:
        assert state_id == "state-1"
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"call-{len(self.calls)}")


def _sdk() -> Any:
    """Build the production SDK client so the real Config and region apply."""
    return build_textract_sdk_client(
        region=_REGION,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        aws_session_token=None,
    )


def _client(sdk: Any) -> tuple[TextractClient, RecordingExecution, list[Any]]:
    execution = RecordingExecution()
    events: list[Any] = []
    client = TextractClient(
        execution=cast("CallRecorder", execution),
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=events.append,
        region=_REGION,
        sdk_client=cast("TextractSDKClient", sdk),
        max_response_bytes=100_000,
    )
    return client, execution, events


def _metadata() -> dict[str, Any]:
    return {"RequestId": "request-1", "HTTPStatusCode": 200, "HTTPHeaders": {}, "RetryAttempts": 0}


def test_start_document_analysis_request_matches_the_service_model() -> None:
    sdk = _sdk()
    client, execution, events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "start_document_analysis",
            {"JobId": "job-1", "ResponseMetadata": _metadata()},
            expected_params={
                "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf", "Version": "v1"}},
                "FeatureTypes": ["FORMS", "TABLES"],
                "ClientRequestToken": _TOKEN,
            },
        )
        receipt = client.start_document_analysis(
            bucket="docs",
            key="invoice.pdf",
            version="v1",
            feature_types=("TABLES", "FORMS"),
            queries=(),
            client_request_token=_TOKEN,
        )
        stubber.assert_no_pending_responses()

    assert receipt.job_id == "job-1"
    assert execution.calls[0]["status"] is CallStatus.SUCCESS
    assert events[0].provider == "aws-textract"
    assert _TOKEN not in repr(execution.calls[0])


def test_start_document_analysis_without_a_version_omits_the_s3_version_key() -> None:
    sdk = _sdk()
    client, _execution, _events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "start_document_analysis",
            {"JobId": "job-2", "ResponseMetadata": _metadata()},
            expected_params={
                "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf"}},
                "FeatureTypes": ["FORMS"],
                "ClientRequestToken": _TOKEN,
            },
        )
        assert (
            client.start_document_analysis(
                bucket="docs",
                key="invoice.pdf",
                version=None,
                feature_types=("FORMS",),
                queries=(),
                client_request_token=_TOKEN,
            ).job_id
            == "job-2"
        )
        stubber.assert_no_pending_responses()


def test_start_document_analysis_queries_config_matches_the_service_model() -> None:
    sdk = _sdk()
    client, _execution, _events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "start_document_analysis",
            {"JobId": "job-3", "ResponseMetadata": _metadata()},
            expected_params={
                "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf"}},
                "FeatureTypes": ["QUERIES"],
                "ClientRequestToken": _TOKEN,
                "QueriesConfig": {
                    "Queries": [
                        {"Text": "What is the invoice total?", "Alias": "total", "Pages": ["1", "2-*"]},
                        {"Text": "Who issued the invoice?"},
                    ]
                },
            },
        )
        assert (
            client.start_document_analysis(
                bucket="docs",
                key="invoice.pdf",
                version=None,
                feature_types=("QUERIES",),
                queries=(
                    {"text": "What is the invoice total?", "alias": "total", "pages": ("1", "2-*")},
                    {"text": "Who issued the invoice?"},
                ),
                client_request_token=_TOKEN,
            ).job_id
            == "job-3"
        )
        stubber.assert_no_pending_responses()


def test_start_document_analysis_reports_retry_attempts_from_response_metadata() -> None:
    sdk = _sdk()
    client, execution, _events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "start_document_analysis",
            {"JobId": "job-4", "ResponseMetadata": {**_metadata(), "RetryAttempts": 2}},
            expected_params={
                "DocumentLocation": {"S3Object": {"Bucket": "docs", "Name": "invoice.pdf"}},
                "FeatureTypes": ["FORMS"],
                "ClientRequestToken": _TOKEN,
            },
        )
        client.start_document_analysis(
            bucket="docs",
            key="invoice.pdf",
            version=None,
            feature_types=("FORMS",),
            queries=(),
            client_request_token=_TOKEN,
        )
        stubber.assert_no_pending_responses()

    assert execution.calls[0]["response_data"].to_dict() == {
        "operation": "start_document_analysis",
        "status": "success",
        "job_id": "job-4",
        "request_id_present": True,
        "attempts": 3,
    }


def test_start_document_analysis_idempotency_mismatch_is_a_real_botocore_client_error() -> None:
    sdk = _sdk()
    client, execution, _events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_client_error(
            "start_document_analysis",
            service_error_code="IdempotentParameterMismatchException",
            service_message="token reused with different parameters",
            http_status_code=400,
        )
        with pytest.raises(TextractIdempotencyInvariantError):
            client.start_document_analysis(
                bucket="docs",
                key="invoice.pdf",
                version=None,
                feature_types=("FORMS",),
                queries=(),
                client_request_token=_TOKEN,
            )
        stubber.assert_no_pending_responses()

    assert execution.calls[0]["status"] is CallStatus.ERROR
    assert execution.calls[0]["error"].to_dict() == {
        "type": "idempotency_invariant",
        "code": "IdempotentParameterMismatchException",
        "retryable": False,
    }


def test_get_document_analysis_request_and_block_response_match_the_service_model() -> None:
    sdk = _sdk()
    client, _execution, _events = _client(sdk)
    blocks = [
        {"BlockType": "PAGE", "Id": "11111111-1111-1111-1111-111111111111", "Page": 1},
        {"BlockType": "LINE", "Id": "22222222-2222-2222-2222-222222222222", "Text": "Total due", "Confidence": 99.5, "Page": 1},
    ]
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_document_analysis",
            {
                "JobStatus": "SUCCEEDED",
                "DocumentMetadata": {"Pages": 1},
                "Blocks": blocks,
                "NextToken": "page-2",
                "ResponseMetadata": _metadata(),
            },
            expected_params={"JobId": "job-1", "MaxResults": 1000},
        )
        page = client.get_document_analysis(job_id="job-1", next_token=None)
        stubber.assert_no_pending_responses()

    assert page.next_token == "page-2"
    assert "NextToken" not in page.semantic_response
    assert "ResponseMetadata" not in page.semantic_response
    assert page.semantic_response["JobStatus"] == "SUCCEEDED"
    assert [block["BlockType"] for block in page.semantic_response["Blocks"]] == ["PAGE", "LINE"]


def test_get_document_analysis_continuation_sends_the_next_token() -> None:
    sdk = _sdk()
    client, _execution, _events = _client(sdk)
    with Stubber(sdk) as stubber:
        stubber.add_response(
            "get_document_analysis",
            {"JobStatus": "SUCCEEDED", "Blocks": [], "ResponseMetadata": _metadata()},
            expected_params={"JobId": "job-1", "MaxResults": 1000, "NextToken": "page-2"},
        )
        page = client.get_document_analysis(job_id="job-1", next_token="page-2")
        stubber.assert_no_pending_responses()

    assert page.next_token is None
