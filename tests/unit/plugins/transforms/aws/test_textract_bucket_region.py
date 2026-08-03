"""Audited S3 HeadBucket region proof for Amazon Textract."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from elspeth.contracts import CallStatus
from elspeth.plugins.transforms.aws.textract_bucket_region import (
    BucketRegionCoordinator,
    BucketRegionProof,
    BucketRegionUnverifiedError,
    HeadBucketClient,
    build_s3_head_bucket_sdk_client,
)


@dataclass
class FakeExecution:
    calls: list[dict[str, Any]] = field(default_factory=list)
    order: list[str] = field(default_factory=list)

    def allocate_call_index(self, state_id: str) -> int:
        assert state_id == "state-1"
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.order.append("audit")
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"call-{len(self.calls)}")


@dataclass
class FakeS3SDK:
    response: object
    requests: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def head_bucket(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self) -> None:
        self.closed = True


def _client(response: object) -> tuple[HeadBucketClient, FakeExecution, list[Any], FakeS3SDK]:
    execution = FakeExecution()
    events: list[Any] = []
    sdk = FakeS3SDK(response)
    client = HeadBucketClient(
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda event: (execution.order.append("telemetry"), events.append(event)),
        region="ap-southeast-2",
        sdk_client=sdk,
        token_id="token-1",
    )
    return client, execution, events, sdk


def _client_error(status: int, code: str, *, region: object = "ap-southeast-2") -> ClientError:
    headers = {} if region is None else {"x-amz-bucket-region": region}
    return ClientError(
        {
            "Error": {"Code": code, "Message": "private provider body must disappear"},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": headers,
                "RetryAttempts": 1,
            },
        },
        "HeadBucket",
    )


@pytest.mark.parametrize(
    ("response", "source"),
    [
        (
            {"BucketRegion": "ap-southeast-2", "ResponseMetadata": {"HTTPStatusCode": 200, "RetryAttempts": 0}},
            "response_field",
        ),
        (
            {
                "ResponseMetadata": {
                    "HTTPStatusCode": 200,
                    "HTTPHeaders": {"x-amz-bucket-region": "ap-southeast-2"},
                    "RetryAttempts": 0,
                }
            },
            "response_header",
        ),
        (_client_error(301, "PermanentRedirect"), "error_header"),
        (_client_error(403, "AccessDenied"), "error_header"),
    ],
)
def test_head_bucket_accepts_only_semantic_region_proofs(response: object, source: str) -> None:
    client, execution, events, sdk = _client(response)

    proof = client.verify_bucket_region("private-documents")

    expected_code = response.response["Error"]["Code"] if source == "error_header" else None  # type: ignore[union-attr]
    assert proof == BucketRegionProof(
        region="ap-southeast-2",
        source=source,
        http_status=200 if source != "error_header" else response.response["ResponseMetadata"]["HTTPStatusCode"],  # type: ignore[union-attr]
        provider_code=expected_code,
    )
    assert sdk.requests == [{"Bucket": "private-documents"}]
    assert execution.order == ["audit", "telemetry"]
    assert execution.calls[0]["status"] is CallStatus.SUCCESS
    assert execution.calls[0]["response_data"].to_dict()["observed_region"] == "ap-southeast-2"
    assert events[0].provider == "aws-s3"
    if source == "error_header":
        assert execution.calls[0]["response_data"].to_dict()["provider_code"] == expected_code
        assert events[0].response_payload.to_dict()["provider_code"] == expected_code
        assert events[0].response_payload.to_dict()["proof_source"] == "error_header"
    assert "private-documents" not in repr(events[0])
    assert "private provider body" not in repr(execution.calls)


def test_head_bucket_sanitizes_provider_code_before_audit_and_telemetry() -> None:
    client, execution, events, _sdk = _client(_client_error(403, "Access\nDenied"))

    proof = client.verify_bucket_region("private-documents")

    assert proof.provider_code == "unknown"
    assert execution.calls[0]["response_data"].to_dict()["provider_code"] == "unknown"
    assert events[0].response_payload.to_dict()["provider_code"] == "unknown"
    assert "Access\nDenied" not in repr(execution.calls)
    assert "Access\nDenied" not in repr(events)


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (_client_error(400, "BadRequest"), "BadRequest", False),
        (_client_error(404, "NoSuchBucket"), "NoSuchBucket", False),
        (_client_error(403, "AccessDenied", region=None), "AccessDenied", False),
        (_client_error(403, "AccessDenied", region="moon-east-1"), "AccessDenied", False),
        (_client_error(403, "AccessDenied", region="AP-SOUTHEAST-2"), "AccessDenied", False),
        (_client_error(503, "ServiceUnavailable"), "ServiceUnavailable", True),
        (EndpointConnectionError(endpoint_url="https://s3.invalid"), "transport_error", True),
    ],
)
def test_head_bucket_rejects_unverified_region_without_retaining_provider_message(
    response: object,
    code: str,
    retryable: bool,
) -> None:
    client, execution, events, _sdk = _client(response)

    with pytest.raises(BucketRegionUnverifiedError) as exc_info:
        client.verify_bucket_region("private-documents")

    assert exc_info.value.code == code
    assert exc_info.value.retryable is retryable
    assert execution.calls[0]["status"] is CallStatus.ERROR
    assert execution.order == ["audit", "telemetry"]
    assert "private-documents" not in repr(events[0])
    assert "private provider body" not in repr(execution.calls)


def test_success_response_rejects_missing_disagreeing_or_malformed_region() -> None:
    responses = (
        {"ResponseMetadata": {"HTTPStatusCode": 200}},
        {"BucketRegion": "", "ResponseMetadata": {"HTTPStatusCode": 200}},
        {
            "BucketRegion": "ap-southeast-2",
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
                "HTTPHeaders": {"x-amz-bucket-region": "us-east-1"},
            },
        },
    )

    for response in responses:
        client, _execution, _events, _sdk = _client(response)
        with pytest.raises(BucketRegionUnverifiedError, match="unverified"):
            client.verify_bucket_region("documents")


def test_bucket_region_coordinator_single_flight_cache_and_failure_retry() -> None:
    coordinator = BucketRegionCoordinator()
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def live() -> BucketRegionProof:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return BucketRegionProof(region="ap-southeast-2", source="response_header", http_status=200)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(coordinator.verify, "documents", live) for _ in range(4)]
        assert entered.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert sorted(result.cache_status for result in results) == ["cached", "cached", "cached", "live"]
    assert coordinator.verify("documents", live).cache_status == "cached"
    assert calls == 1

    failures = 0

    def fail() -> BucketRegionProof:
        nonlocal failures
        failures += 1
        raise BucketRegionUnverifiedError(code="transport_error", retryable=True)

    for _ in range(2):
        with pytest.raises(BucketRegionUnverifiedError):
            coordinator.verify("other-documents", fail)
    assert failures == 2


def test_bucket_region_coordinator_caches_per_bucket() -> None:
    coordinator = BucketRegionCoordinator()
    calls: list[str] = []

    for bucket in ("first", "second", "first"):
        result = coordinator.verify(
            bucket,
            lambda bucket=bucket: calls.append(bucket) or BucketRegionProof("ap-southeast-2", "response_field", 200),
        )
        assert result.region == "ap-southeast-2"

    assert calls == ["first", "second"]


def test_s3_builder_uses_textract_retry_timeout_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    sdk = SimpleNamespace()

    def fake_client(service: str, **kwargs: Any) -> object:
        captured["service"] = service
        captured.update(kwargs)
        return sdk

    monkeypatch.setattr("boto3.client", fake_client)

    assert (
        build_s3_head_bucket_sdk_client(
            region="eu-west-1",
            aws_access_key_id="access",
            aws_secret_access_key="secret",
            aws_session_token="session",
        )
        is sdk
    )
    assert captured["service"] == "s3"
    assert captured["region_name"] == "eu-west-1"
    assert captured["aws_access_key_id"] == "access"
    assert captured["aws_secret_access_key"] == "secret"
    assert captured["aws_session_token"] == "session"
    config = captured["config"]
    assert config.connect_timeout == 10
    assert config.read_timeout == 30
    assert config.retries["mode"] == "standard"
    assert config.retries["total_max_attempts"] == 3
