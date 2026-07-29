"""Strict, audited adapter for asynchronous Amazon Textract operations."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.contracts.freeze import deep_freeze
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase, TelemetryEmitCallback

if TYPE_CHECKING:
    from elspeth.contracts.audit_protocols import CallRecorder
    from elspeth.contracts.contexts import LimiterProtocol

logger = structlog.get_logger(__name__)

_RETRYABLE_CODES = frozenset(
    {
        "InternalServerError",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "LimitExceededException",
    }
)
_IDEMPOTENCY_MISMATCH_CODE = "IdempotentParameterMismatchException"
_MAX_ERROR_CODE_LENGTH = 128
_MAX_JOB_ID_LENGTH = 64
_MAX_NEXT_TOKEN_LENGTH = 1024
_SDK_TOTAL_MAX_ATTEMPTS = 3


class TextractResponseError(ValueError):
    """Raised when provider data cannot be safely retained."""

    def __init__(self, message: str = "malformed Amazon Textract response", *, category: str = "malformed_response") -> None:
        super().__init__(message)
        self.category = category


class TextractServiceError(RuntimeError):
    """Sanitized terminal SDK failure."""

    def __init__(self, *, code: str, retryable: bool) -> None:
        super().__init__("Amazon Textract request failed")
        self.code = code
        self.retryable = retryable


class TextractIdempotencyInvariantError(RuntimeError):
    """Raised when AWS reports reuse of a token with different parameters."""

    def __init__(self) -> None:
        super().__init__("Amazon Textract idempotency invariant failed")


@dataclass(frozen=True, slots=True)
class StartAnalysisReceipt:
    job_id: str


@dataclass(frozen=True, slots=True)
class AnalysisResultPage:
    semantic_response: Mapping[str, Any]
    next_token: str | None


class TextractSDKClient(Protocol):
    """SDK operations used by the asynchronous document-analysis plugin."""

    def start_document_analysis(self, **kwargs: Any) -> object: ...

    def get_document_analysis(self, **kwargs: Any) -> object: ...

    def close(self) -> None: ...


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
        raise TextractResponseError
    return value


def _bounded_string(value: object, *, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise TextractResponseError
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TextractResponseError from error
    return value


def _parse_response_metadata(value: object) -> tuple[bool, int]:
    if value is None:
        return False, 1
    metadata = _mapping(value)
    retry_attempts: object = metadata.get("RetryAttempts", 0)
    if type(retry_attempts) is not int or not 0 <= retry_attempts <= 10:
        raise TextractResponseError
    request_id = metadata.get("RequestId")
    if request_id is not None and (type(request_id) is not str or not 1 <= len(request_id) <= 256):
        raise TextractResponseError
    return request_id is not None, retry_attempts + 1


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _query_request(queries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for query in queries:
        text = query.get("text")
        alias = query.get("alias")
        pages = query.get("pages", ())
        if type(text) is not str or not text:
            raise ValueError("Textract query text must be a non-empty string")
        if alias is not None and (type(alias) is not str or not alias):
            raise ValueError("Textract query alias must be a non-empty string when set")
        if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes, bytearray)):
            raise ValueError("Textract query pages must be a sequence")
        if any(type(page) is not str or not page for page in pages):
            raise ValueError("Textract query page selectors must be non-empty strings")
        request: dict[str, Any] = {"Text": text}
        if alias is not None:
            request["Alias"] = alias
        if pages:
            request["Pages"] = list(pages)
        result.append(request)
    return result


def _provider_error(error: Exception) -> tuple[str, bool, int, bool]:
    from botocore.exceptions import ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

    if isinstance(error, (ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError)):
        return "transport_error", True, _SDK_TOTAL_MAX_ATTEMPTS, False
    if not isinstance(error, ClientError):
        return "botocore_error", False, 1, False

    error_data = error.response.get("Error")
    code = "unknown"
    if isinstance(error_data, Mapping):
        raw_code = error_data.get("Code")
        if type(raw_code) is str and raw_code:
            code = raw_code[:_MAX_ERROR_CODE_LENGTH]
    attempts = 1
    response_metadata = error.response.get("ResponseMetadata")
    if isinstance(response_metadata, Mapping):
        raw_retries = response_metadata.get("RetryAttempts", 0)
        if type(raw_retries) is int and 0 <= raw_retries <= 10:
            attempts = raw_retries + 1
    return code, code in _RETRYABLE_CODES, attempts, code == _IDEMPOTENCY_MISMATCH_CODE


def _textract_provider_exception_types() -> tuple[type[Exception], ...]:
    from botocore.exceptions import BotoCoreError, ClientError

    return (BotoCoreError, ClientError)


def build_textract_sdk_client(
    *,
    region: str,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
) -> TextractSDKClient:
    """Build one shared SDK client with botocore as the sole retry owner."""
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": region,
        "config": Config(
            connect_timeout=10,
            read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": _SDK_TOTAL_MAX_ATTEMPTS},
        ),
    }
    if aws_access_key_id is not None:
        if aws_secret_access_key is None:
            raise ValueError("explicit AWS access and secret credentials must be provided together")
        kwargs["aws_access_key_id"] = aws_access_key_id
        kwargs["aws_secret_access_key"] = aws_secret_access_key
        if aws_session_token is not None:
            kwargs["aws_session_token"] = aws_session_token
    return cast("TextractSDKClient", boto3.client("textract", **kwargs))


class TextractClient(AuditedClientBase):
    """Row-scoped audited wrapper around one shared Textract SDK client."""

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        *,
        region: str,
        sdk_client: TextractSDKClient,
        max_response_bytes: int,
        limiter: LimiterProtocol | None = None,
        token_id: str | None = None,
    ) -> None:
        super().__init__(execution, state_id, run_id, telemetry_emit, limiter=limiter, token_id=token_id)
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._region = region
        self._sdk_client = sdk_client
        self._max_response_bytes = max_response_bytes

    def _emit_after_audit(
        self,
        *,
        status: CallStatus,
        latency_ms: float,
        request_payload: RawCallPayload,
        response_payload: RawCallPayload,
        telemetry_request: RawCallPayload,
        telemetry_response: RawCallPayload,
    ) -> None:
        try:
            self._telemetry_emit(
                ExternalCallCompleted(
                    timestamp=datetime.now(UTC),
                    run_id=self._run_id,
                    call_type=CallType.HTTP,
                    provider="aws-textract",
                    status=status,
                    latency_ms=latency_ms,
                    state_id=self._telemetry_state_id(),
                    token_id=self._telemetry_token_id(),
                    request_hash=stable_hash(request_payload.to_dict()),
                    response_hash=stable_hash(response_payload.to_dict()),
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
                call_type="aws_textract",
                exc_info=True,
            )

    def start_document_analysis(
        self,
        *,
        bucket: str,
        key: str,
        version: str | None,
        feature_types: tuple[str, ...],
        queries: Sequence[Mapping[str, Any]],
        client_request_token: str,
    ) -> StartAnalysisReceipt:
        canonical_features = tuple(sorted(feature_types))
        query_requests = _query_request(queries)
        s3_object: dict[str, str] = {"Bucket": bucket, "Name": key}
        if version is not None:
            s3_object["Version"] = version
        sdk_request: dict[str, Any] = {
            "DocumentLocation": {"S3Object": s3_object},
            "FeatureTypes": list(canonical_features),
            "ClientRequestToken": client_request_token,
        }
        if query_requests:
            sdk_request["QueriesConfig"] = {"Queries": query_requests}

        call_index = self._next_call_index()
        request_payload = RawCallPayload(
            {
                "operation": "start_document_analysis",
                "region": self._region,
                "bucket": bucket,
                "key": key,
                "version": version,
                "feature_types": canonical_features,
                "queries": query_requests,
                "client_request_token_fingerprint": _fingerprint(client_request_token),
            }
        )
        telemetry_request = RawCallPayload(
            {
                "operation": "start_document_analysis",
                "region": self._region,
                "feature_count": len(canonical_features),
                "query_count": len(query_requests),
            }
        )
        started = time.perf_counter()
        terminal_error: Exception | None = None
        attempts = 1
        try:
            self._acquire_rate_limit()
            raw_response = self._sdk_client.start_document_analysis(**sdk_request)
            response = _mapping(raw_response)
            request_id_present, attempts = _parse_response_metadata(response.get("ResponseMetadata"))
            job_id = _bounded_string(response.get("JobId"), maximum=_MAX_JOB_ID_LENGTH)
            receipt: StartAnalysisReceipt | None = StartAnalysisReceipt(job_id=job_id)
            response_payload = RawCallPayload(
                {
                    "operation": "start_document_analysis",
                    "status": "success",
                    "job_id": job_id,
                    "request_id_present": request_id_present,
                    "attempts": attempts,
                }
            )
            call_status = CallStatus.SUCCESS
            error_payload = None
        except TextractResponseError as error:
            terminal_error = error
            receipt = None
            response_payload = RawCallPayload({"operation": "start_document_analysis", "status": error.category, "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": error.category, "retryable": False})
        except _textract_provider_exception_types() as error:
            code, retryable, attempts, idempotency_mismatch = _provider_error(error)
            receipt = None
            if idempotency_mismatch:
                terminal_error = TextractIdempotencyInvariantError()
                error_type = "idempotency_invariant"
            else:
                terminal_error = TextractServiceError(code=code, retryable=retryable)
                error_type = "service_error"
            response_payload = RawCallPayload({"operation": "start_document_analysis", "status": error_type, "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": error_type, "code": code, "retryable": retryable})

        latency_ms = (time.perf_counter() - started) * 1000
        telemetry_response = RawCallPayload(
            {
                "operation": "start_document_analysis",
                "status": response_payload.to_dict()["status"],
                "attempts": attempts,
            }
        )
        self._record_call(
            call_index=call_index,
            call_type=CallType.HTTP,
            status=call_status,
            request_data=request_payload,
            response_data=response_payload,
            error=error_payload,
            latency_ms=latency_ms,
        )
        self._emit_after_audit(
            status=call_status,
            latency_ms=latency_ms,
            request_payload=request_payload,
            response_payload=response_payload,
            telemetry_request=telemetry_request,
            telemetry_response=telemetry_response,
        )
        if terminal_error is not None:
            raise terminal_error
        assert receipt is not None
        return receipt

    def get_document_analysis(self, *, job_id: str, next_token: str | None) -> AnalysisResultPage:
        sdk_request: dict[str, Any] = {"JobId": job_id, "MaxResults": 1000}
        next_token_fingerprint = None
        if next_token is not None:
            sdk_request["NextToken"] = next_token
            next_token_fingerprint = _fingerprint(next_token)
        call_index = self._next_call_index()
        request_payload = RawCallPayload(
            {
                "operation": "get_document_analysis",
                "job_id": job_id,
                "next_token_present": next_token is not None,
                "next_token_fingerprint": next_token_fingerprint,
                "max_results": 1000,
            }
        )
        telemetry_request = RawCallPayload(
            {
                "operation": "get_document_analysis",
                "next_token_present": next_token is not None,
                "max_results": 1000,
            }
        )
        started = time.perf_counter()
        terminal_error: Exception | None = None
        attempts = 1
        try:
            self._acquire_rate_limit()
            raw_response = self._sdk_client.get_document_analysis(**sdk_request)
            response = _mapping(raw_response)
            request_id_present, attempts = _parse_response_metadata(response.get("ResponseMetadata"))
            raw_next_token = response.get("NextToken")
            returned_next_token = None if raw_next_token is None else _bounded_string(raw_next_token, maximum=_MAX_NEXT_TOKEN_LENGTH)
            semantic_response = {key: value for key, value in response.items() if key not in {"ResponseMetadata", "NextToken"}}
            try:
                semantic_size = len(canonical_json(semantic_response).encode("utf-8"))
                frozen_semantic = cast("Mapping[str, Any]", deep_freeze(semantic_response))
            except (TypeError, ValueError, RecursionError, UnicodeError) as error:
                raise TextractResponseError from error
            if semantic_size > self._max_response_bytes:
                raise TextractResponseError(
                    "Amazon Textract response exceeded the maximum response size",
                    category="response_too_large",
                )
            result_page: AnalysisResultPage | None = AnalysisResultPage(
                semantic_response=frozen_semantic,
                next_token=returned_next_token,
            )
            response_payload = RawCallPayload(
                {
                    "operation": "get_document_analysis",
                    "status": "success",
                    "attempts": attempts,
                    "request_id_present": request_id_present,
                    "next_token_present": returned_next_token is not None,
                    "next_token_fingerprint": None if returned_next_token is None else _fingerprint(returned_next_token),
                    "semantic_response": semantic_response,
                }
            )
            call_status = CallStatus.SUCCESS
            error_payload = None
        except TextractResponseError as error:
            terminal_error = error
            result_page = None
            returned_next_token = None
            response_payload = RawCallPayload({"operation": "get_document_analysis", "status": error.category, "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": error.category, "retryable": False})
        except _textract_provider_exception_types() as error:
            code, retryable, attempts, idempotency_mismatch = _provider_error(error)
            result_page = None
            returned_next_token = None
            if idempotency_mismatch:
                terminal_error = TextractIdempotencyInvariantError()
                error_type = "idempotency_invariant"
            else:
                terminal_error = TextractServiceError(code=code, retryable=retryable)
                error_type = "service_error"
            response_payload = RawCallPayload({"operation": "get_document_analysis", "status": error_type, "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": error_type, "code": code, "retryable": retryable})

        latency_ms = (time.perf_counter() - started) * 1000
        telemetry_response = RawCallPayload(
            {
                "operation": "get_document_analysis",
                "status": response_payload.to_dict()["status"],
                "attempts": attempts,
                "next_token_present": returned_next_token is not None,
            }
        )
        self._record_call(
            call_index=call_index,
            call_type=CallType.HTTP,
            status=call_status,
            request_data=request_payload,
            response_data=response_payload,
            error=error_payload,
            latency_ms=latency_ms,
        )
        self._emit_after_audit(
            status=call_status,
            latency_ms=latency_ms,
            request_payload=request_payload,
            response_payload=response_payload,
            telemetry_request=telemetry_request,
            telemetry_response=telemetry_response,
        )
        if terminal_error is not None:
            raise terminal_error
        assert result_page is not None
        return result_page
