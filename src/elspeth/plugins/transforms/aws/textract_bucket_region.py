"""Audited S3 HeadBucket proof that a Textract document bucket is co-regional."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.core.canonical import stable_hash
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase, TelemetryEmitCallback
from elspeth.plugins.transforms.aws.textract_regions import is_supported_textract_region

if TYPE_CHECKING:
    from elspeth.contracts.audit_protocols import CallRecorder

logger = structlog.get_logger(__name__)

_MAX_ERROR_CODE_LENGTH = 128
_RETRYABLE_SERVICE_CODES = frozenset({"InternalError", "ServiceUnavailable", "SlowDown", "Throttling"})

S3_HEAD_BUCKET_CONNECT_TIMEOUT_SECONDS = 10
S3_HEAD_BUCKET_READ_TIMEOUT_SECONDS = 30
S3_HEAD_BUCKET_TOTAL_MAX_ATTEMPTS = 3
S3_HEAD_BUCKET_SDK_ALLOWANCE_SECONDS = S3_HEAD_BUCKET_TOTAL_MAX_ATTEMPTS * (
    S3_HEAD_BUCKET_CONNECT_TIMEOUT_SECONDS + S3_HEAD_BUCKET_READ_TIMEOUT_SECONDS
)

BucketRegionProofSource = Literal["response_field", "response_header", "error_header"]
BucketRegionCacheStatus = Literal["live", "cached"]


@dataclass(frozen=True, slots=True)
class BucketRegionProof:
    region: str
    source: BucketRegionProofSource
    http_status: int
    provider_code: str | None = None


@dataclass(frozen=True, slots=True)
class BucketRegionVerification:
    region: str
    source: BucketRegionProofSource
    http_status: int
    cache_status: BucketRegionCacheStatus
    provider_code: str | None = None


class BucketRegionUnverifiedError(RuntimeError):
    """Sanitized HeadBucket outcome that did not prove one supported region."""

    def __init__(self, *, code: str, retryable: bool, http_status: int | None = None) -> None:
        super().__init__("Amazon S3 bucket region is unverified")
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class S3HeadBucketSDKClient(Protocol):
    def head_bucket(self, **kwargs: Any) -> object: ...

    def close(self) -> None: ...


def build_s3_head_bucket_sdk_client(
    *,
    region: str,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
) -> S3HeadBucketSDKClient:
    """Build the shared HeadBucket client with exact Textract credential/retry posture."""
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": region,
        "config": Config(
            connect_timeout=S3_HEAD_BUCKET_CONNECT_TIMEOUT_SECONDS,
            read_timeout=S3_HEAD_BUCKET_READ_TIMEOUT_SECONDS,
            retries={"mode": "standard", "total_max_attempts": S3_HEAD_BUCKET_TOTAL_MAX_ATTEMPTS},
        ),
    }
    if aws_access_key_id is not None:
        if aws_secret_access_key is None:
            raise ValueError("explicit AWS access and secret credentials must be provided together")
        kwargs["aws_access_key_id"] = aws_access_key_id
        kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token is not None:
            kwargs["aws_session_token"] = aws_session_token
    return cast("S3HeadBucketSDKClient", boto3.client("s3", **kwargs))


def _strict_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise BucketRegionUnverifiedError(code="malformed_response", retryable=False)
    return value


def _strict_region(value: object) -> str | None:
    return value if type(value) is str and is_supported_textract_region(value) else None


def _metadata(value: object) -> tuple[int | None, Mapping[str, Any], int]:
    metadata = _strict_mapping(value)
    raw_status = metadata.get("HTTPStatusCode")
    status = raw_status if type(raw_status) is int and 100 <= raw_status <= 599 else None
    raw_headers = metadata.get("HTTPHeaders", {})
    headers = _strict_mapping(raw_headers)
    raw_retries = metadata.get("RetryAttempts", 0)
    attempts = raw_retries + 1 if type(raw_retries) is int and 0 <= raw_retries <= 10 else 1
    return status, headers, attempts


def _success_proof(raw_response: object) -> tuple[BucketRegionProof, int]:
    response = _strict_mapping(raw_response)
    status, headers, attempts = _metadata(response.get("ResponseMetadata", {}))
    if status != 200:
        raise BucketRegionUnverifiedError(code="malformed_response", retryable=False, http_status=status)
    field_present = "BucketRegion" in response
    header_present = "x-amz-bucket-region" in headers
    field_region = _strict_region(response.get("BucketRegion")) if field_present else None
    header_region = _strict_region(headers.get("x-amz-bucket-region")) if header_present else None
    if field_present and field_region is None:
        raise BucketRegionUnverifiedError(code="malformed_region", retryable=False, http_status=status)
    if header_present and header_region is None:
        raise BucketRegionUnverifiedError(code="malformed_region", retryable=False, http_status=status)
    if field_region is not None and header_region is not None and field_region != header_region:
        raise BucketRegionUnverifiedError(code="region_disagreement", retryable=False, http_status=status)
    if field_region is not None:
        return BucketRegionProof(field_region, "response_field", status), attempts
    if header_region is not None:
        return BucketRegionProof(header_region, "response_header", status), attempts
    raise BucketRegionUnverifiedError(code="missing_region", retryable=False, http_status=status)


def _safe_provider_code(value: object) -> str:
    if (
        type(value) is str
        and 0 < len(value) <= _MAX_ERROR_CODE_LENGTH
        and all(character.isascii() and (character.isalnum() or character in "._-") for character in value)
    ):
        return value
    return "unknown"


def _provider_outcome(error: Exception) -> tuple[BucketRegionProof | None, BucketRegionUnverifiedError, int]:
    from botocore.exceptions import ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

    if isinstance(error, (ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError)):
        failure = BucketRegionUnverifiedError(code="transport_error", retryable=True)
        return None, failure, 1
    if not isinstance(error, ClientError):
        failure = BucketRegionUnverifiedError(code="botocore_error", retryable=False)
        return None, failure, 1

    response = error.response
    raw_error = response.get("Error")
    raw_code = raw_error.get("Code") if isinstance(raw_error, Mapping) else None
    code = _safe_provider_code(raw_code)
    try:
        status, headers, attempts = _metadata(response.get("ResponseMetadata", {}))
    except BucketRegionUnverifiedError:
        status, headers, attempts = None, {}, 1
    if status in {301, 403}:
        region = _strict_region(headers.get("x-amz-bucket-region"))
        if region is not None:
            proof = BucketRegionProof(region=region, source="error_header", http_status=status, provider_code=code)
            return proof, BucketRegionUnverifiedError(code=code, retryable=False, http_status=status), attempts
    retryable = code in _RETRYABLE_SERVICE_CODES or status == 429 or (status is not None and 500 <= status <= 599)
    return None, BucketRegionUnverifiedError(code=code, retryable=retryable, http_status=status), attempts


def _provider_exception_types() -> tuple[type[Exception], ...]:
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


class HeadBucketClient(AuditedClientBase):
    """Row-scoped audited adapter for one shared S3 HeadBucket SDK client."""

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        *,
        region: str,
        sdk_client: S3HeadBucketSDKClient,
        token_id: str | None = None,
    ) -> None:
        super().__init__(execution, state_id, run_id, telemetry_emit, token_id=token_id)
        self._region = region
        self._sdk_client = sdk_client

    def _emit(
        self,
        *,
        status: CallStatus,
        latency_ms: float,
        response_status: str,
        attempts: int,
        http_status: int | None,
        proof_source: BucketRegionProofSource | None,
        provider_code: str | None,
        observed_region: str | None,
    ) -> None:
        telemetry_request = RawCallPayload({"operation": "head_bucket_region", "configured_region": self._region})
        telemetry_response = RawCallPayload(
            {
                "operation": "head_bucket_region",
                "status": response_status,
                "configured_region": self._region,
                "observed_region": observed_region,
                "attempts": attempts,
                "http_status": http_status,
                "proof_source": proof_source,
                "provider_code": provider_code,
            }
        )
        try:
            self._telemetry_emit(
                ExternalCallCompleted(
                    timestamp=datetime.now(UTC),
                    run_id=self._run_id,
                    call_type=CallType.HTTP,
                    provider="aws-s3",
                    status=status,
                    latency_ms=latency_ms,
                    state_id=self._telemetry_state_id(),
                    token_id=self._telemetry_token_id(),
                    request_hash=stable_hash(telemetry_request.to_dict()),
                    response_hash=stable_hash(telemetry_response.to_dict()),
                    request_payload=telemetry_request,
                    response_payload=telemetry_response,
                    token_usage=None,
                )
            )
        except contract_errors.TIER_1_ERRORS:
            raise
        except (TypeError, AttributeError, KeyError, NameError):
            raise
        except Exception as error:
            logger.warning(
                "telemetry_emit_failed",
                error_type=type(error).__name__,
                run_id=self._run_id,
                state_id=self._telemetry_state_id(),
                call_type="aws_s3_head_bucket",
                exc_info=True,
            )

    def verify_bucket_region(self, bucket: str) -> BucketRegionProof:
        call_index = self._next_call_index()
        request_payload = RawCallPayload({"operation": "head_bucket_region", "configured_region": self._region, "bucket": bucket})
        started = time.perf_counter()
        proof: BucketRegionProof | None = None
        terminal_error: BucketRegionUnverifiedError | None = None
        attempts = 1
        try:
            raw_response = self._sdk_client.head_bucket(Bucket=bucket)
            proof, attempts = _success_proof(raw_response)
        except BucketRegionUnverifiedError as error:
            terminal_error = error
        except _provider_exception_types() as error:
            proof, terminal_error, attempts = _provider_outcome(error)

        latency_ms = (time.perf_counter() - started) * 1000
        http_status: int | None
        if proof is not None:
            status = CallStatus.SUCCESS
            response_status = "verified"
            response_payload = RawCallPayload(
                {
                    "operation": "head_bucket_region",
                    "status": response_status,
                    "observed_region": proof.region,
                    "proof_source": proof.source,
                    "provider_code": proof.provider_code,
                    "http_status": proof.http_status,
                    "attempts": attempts,
                }
            )
            error_payload = None
            http_status = proof.http_status
            proof_source = proof.source
            provider_code = proof.provider_code
            observed_region = proof.region
        else:
            assert terminal_error is not None
            status = CallStatus.ERROR
            response_status = "unverified"
            http_status = terminal_error.http_status
            response_payload = RawCallPayload(
                {
                    "operation": "head_bucket_region",
                    "status": response_status,
                    "http_status": http_status,
                    "attempts": attempts,
                }
            )
            error_payload = RawCallPayload(
                {"type": "bucket_region_unverified", "code": terminal_error.code, "retryable": terminal_error.retryable}
            )
            proof_source = None
            provider_code = terminal_error.code
            observed_region = None
        self._record_call(
            call_index=call_index,
            call_type=CallType.HTTP,
            status=status,
            request_data=request_payload,
            response_data=response_payload,
            error=error_payload,
            latency_ms=latency_ms,
        )
        self._emit(
            status=status,
            latency_ms=latency_ms,
            response_status=response_status,
            attempts=attempts,
            http_status=http_status,
            proof_source=proof_source,
            provider_code=provider_code,
            observed_region=observed_region,
        )
        if terminal_error is not None and proof is None:
            raise terminal_error
        assert proof is not None
        return proof


@dataclass(slots=True)
class _InFlight:
    completed: threading.Event
    proof: BucketRegionProof | None = None
    error: BaseException | None = None


class BucketRegionCoordinator:
    """Transform-scoped success cache and single-flight HeadBucket coordinator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, BucketRegionProof] = {}
        self._in_flight: dict[str, _InFlight] = {}

    def verify(self, bucket: str, live_verify: Callable[[], BucketRegionProof]) -> BucketRegionVerification:
        with self._lock:
            cached = self._cache.get(bucket)
            if cached is not None:
                return BucketRegionVerification(cached.region, cached.source, cached.http_status, "cached", cached.provider_code)
            flight = self._in_flight.get(bucket)
            leader = flight is None
            if flight is None:
                flight = _InFlight(completed=threading.Event())
                self._in_flight[bucket] = flight

        if leader:
            try:
                proof = live_verify()
            except BaseException as error:
                with self._lock:
                    flight.error = error
                    self._in_flight.pop(bucket, None)
                    flight.completed.set()
                raise
            with self._lock:
                self._cache[bucket] = proof
                flight.proof = proof
                self._in_flight.pop(bucket, None)
                flight.completed.set()
            return BucketRegionVerification(proof.region, proof.source, proof.http_status, "live", proof.provider_code)

        flight.completed.wait()
        if flight.error is not None:
            raise flight.error
        if flight.proof is None:
            raise RuntimeError("bucket region single-flight completed without an outcome")
        return BucketRegionVerification(
            flight.proof.region,
            flight.proof.source,
            flight.proof.http_status,
            "cached",
            flight.proof.provider_code,
        )
