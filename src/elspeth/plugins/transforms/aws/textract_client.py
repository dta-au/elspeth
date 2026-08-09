"""Strict, audited adapters for Amazon Textract operations.

Two row-scoped adapters share one SDK-construction helper, one sanitized
error taxonomy, and one audit/telemetry discipline: ``TextractClient``
drives the asynchronous StartDocumentAnalysis/GetDocumentAnalysis pair,
and ``TextractInlineClient`` drives the synchronous AnalyzeDocument call
used by the inline (payload-store bytes) transform.
"""

from __future__ import annotations

import hashlib
import threading
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

_send_attempts = threading.local()


def _record_send_attempt(**_: Any) -> None:
    """Count one started HTTP attempt; registered on botocore's before-send event."""
    _send_attempts.count = getattr(_send_attempts, "count", 0) + 1


def _reset_send_attempts() -> None:
    _send_attempts.count = 0


def _observed_send_attempts() -> int:
    return max(1, getattr(_send_attempts, "count", 0))


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


class TextractSyncSDKClient(Protocol):
    """SDK operations used by the synchronous inline-analysis plugin."""

    def analyze_document(self, **kwargs: Any) -> object: ...

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


def _provider_error(error: Exception, *, observed_attempts: int) -> tuple[str, bool, int, bool]:
    from botocore.exceptions import ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

    if isinstance(error, (ConnectTimeoutError, ConnectionClosedError, EndpointConnectionError, ReadTimeoutError)):
        return "transport_error", True, observed_attempts, False
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
    read_timeout: float = 30.0,
) -> TextractSDKClient:
    """Build one shared SDK client with botocore as the sole retry owner."""
    import boto3
    from botocore.config import Config

    kwargs: dict[str, Any] = {
        "region_name": region,
        "config": Config(
            connect_timeout=10,
            read_timeout=read_timeout,
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
    client = boto3.client("textract", **kwargs)
    client.meta.events.register("before-send.textract", _record_send_attempt)
    return cast("TextractSDKClient", client)


def build_textract_sync_sdk_client(
    *,
    region: str,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
    read_timeout: float,
) -> TextractSyncSDKClient:
    """Build the shared SDK client typed for synchronous AnalyzeDocument use.

    The one boto3 Textract client implements both operation surfaces; the two
    protocols exist so each plugin depends only on the operations it calls.
    """
    return cast(
        "TextractSyncSDKClient",
        build_textract_sdk_client(
            region=region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            read_timeout=read_timeout,
        ),
    )


class _TextractAuditedClient(AuditedClientBase):
    """Shared audit/telemetry discipline for row-scoped Textract adapters."""

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        *,
        region: str,
        max_response_bytes: int,
        limiter: LimiterProtocol | None = None,
        token_id: str | None = None,
    ) -> None:
        super().__init__(execution, state_id, run_id, telemetry_emit, limiter=limiter, token_id=token_id)
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._region = region
        self._max_response_bytes = max_response_bytes

    def _bounded_semantic_response(
        self,
        response: Mapping[str, Any],
        *,
        exclude: frozenset[str],
    ) -> tuple[dict[str, Any], Mapping[str, Any]]:
        """Return the size-bounded semantic response and its frozen form."""
        semantic = {key: value for key, value in response.items() if key not in exclude}
        try:
            semantic_size = len(canonical_json(semantic).encode("utf-8"))
        except (TypeError, ValueError, RecursionError, UnicodeError) as error:
            raise TextractResponseError from error
        if semantic_size > self._max_response_bytes:
            raise TextractResponseError(
                "Amazon Textract response exceeded the maximum response size",
                category="response_too_large",
            )
        try:
            frozen = cast("Mapping[str, Any]", deep_freeze(semantic))
        except (TypeError, ValueError, RecursionError, UnicodeError) as error:
            raise TextractResponseError from error
        return semantic, frozen

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


class TextractClient(_TextractAuditedClient):
    """Row-scoped audited wrapper around one shared asynchronous Textract SDK client."""

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
        super().__init__(
            execution,
            state_id,
            run_id,
            telemetry_emit,
            region=region,
            max_response_bytes=max_response_bytes,
            limiter=limiter,
            token_id=token_id,
        )
        self._sdk_client = sdk_client

    def start_document_analysis(
        self,
        *,
        bucket: str,
        key: str,
        version: str | None,
        feature_types: tuple[str, ...],
        queries: Sequence[Mapping[str, Any]],
        client_request_token: str,
        audit_identity: Mapping[str, str] | None = None,
    ) -> StartAnalysisReceipt:
        # ``audit_identity`` substitutes the operator-safe location identity
        # (e.g. {"profile": alias, "key": relative_key}) for the literal
        # bucket/key pair in the persisted call record; the SDK request itself
        # always uses the real bucket and executable key.
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
        location_identity: Mapping[str, str | None] = {"bucket": bucket, "key": key} if audit_identity is None else dict(audit_identity)
        request_payload = RawCallPayload(
            {
                "operation": "start_document_analysis",
                "region": self._region,
                **location_identity,
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
            _reset_send_attempts()
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
            code, retryable, attempts, idempotency_mismatch = _provider_error(error, observed_attempts=_observed_send_attempts())
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
            _reset_send_attempts()
            raw_response = self._sdk_client.get_document_analysis(**sdk_request)
            response = _mapping(raw_response)
            request_id_present, attempts = _parse_response_metadata(response.get("ResponseMetadata"))
            raw_next_token = response.get("NextToken")
            returned_next_token = None if raw_next_token is None else _bounded_string(raw_next_token, maximum=_MAX_NEXT_TOKEN_LENGTH)
            semantic_response, frozen_semantic = self._bounded_semantic_response(
                response,
                exclude=frozenset({"ResponseMetadata", "NextToken"}),
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
            code, retryable, attempts, idempotency_mismatch = _provider_error(error, observed_attempts=_observed_send_attempts())
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


class TextractInlineClient(_TextractAuditedClient):
    """Row-scoped audited wrapper for one synchronous AnalyzeDocument call.

    The audited request never contains document bytes or base64: the
    payload SHA-256 already binds the call to the integrity-checked source
    content held in the payload store, so the hash plus byte length is the
    complete reproducible identity of what was sent.
    """

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        *,
        region: str,
        sdk_client: TextractSyncSDKClient,
        max_response_bytes: int,
        limiter: LimiterProtocol | None = None,
        token_id: str | None = None,
    ) -> None:
        super().__init__(
            execution,
            state_id,
            run_id,
            telemetry_emit,
            region=region,
            max_response_bytes=max_response_bytes,
            limiter=limiter,
            token_id=token_id,
        )
        self._sdk_client = sdk_client

    def analyze_document(
        self,
        *,
        document_bytes: bytes,
        document_sha256: str,
        document_format: str,
        feature_types: tuple[str, ...],
        queries: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Run one audited AnalyzeDocument call and return the frozen semantic response.

        AnalyzeDocument has no idempotency token, so botocore's standard
        retry policy may repeat a billable attempt; the recorded attempt
        count is the caller-visible evidence of that.
        """
        canonical_features = tuple(sorted(feature_types))
        query_requests = _query_request(queries)
        sdk_request: dict[str, Any] = {
            "Document": {"Bytes": document_bytes},
            "FeatureTypes": list(canonical_features),
        }
        if query_requests:
            sdk_request["QueriesConfig"] = {"Queries": query_requests}

        call_index = self._next_call_index()
        request_payload = RawCallPayload(
            {
                "operation": "analyze_document",
                "region": self._region,
                "document_sha256": document_sha256,
                "document_size_bytes": len(document_bytes),
                "document_format": document_format,
                "feature_types": canonical_features,
                "queries": query_requests,
            }
        )
        telemetry_request = RawCallPayload(
            {
                "operation": "analyze_document",
                "region": self._region,
                "document_size_bytes": len(document_bytes),
                "feature_count": len(canonical_features),
                "query_count": len(query_requests),
            }
        )
        started = time.perf_counter()
        terminal_error: Exception | None = None
        attempts = 1
        try:
            self._acquire_rate_limit()
            _reset_send_attempts()
            raw_response = self._sdk_client.analyze_document(**sdk_request)
            response = _mapping(raw_response)
            request_id_present, attempts = _parse_response_metadata(response.get("ResponseMetadata"))
            semantic_response, frozen_semantic = self._bounded_semantic_response(
                response,
                exclude=frozenset({"ResponseMetadata"}),
            )
            result: Mapping[str, Any] | None = frozen_semantic
            response_payload = RawCallPayload(
                {
                    "operation": "analyze_document",
                    "status": "success",
                    "attempts": attempts,
                    "request_id_present": request_id_present,
                    "semantic_response": semantic_response,
                }
            )
            call_status = CallStatus.SUCCESS
            error_payload = None
        except TextractResponseError as error:
            terminal_error = error
            result = None
            response_payload = RawCallPayload({"operation": "analyze_document", "status": error.category, "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": error.category, "retryable": False})
        except _textract_provider_exception_types() as error:
            # AnalyzeDocument sends no idempotency token, so the mismatch
            # classification cannot legitimately fire; if AWS returns it
            # anyway it is an ordinary non-retryable service error here.
            code, retryable, attempts, _idempotency_mismatch = _provider_error(error, observed_attempts=_observed_send_attempts())
            result = None
            terminal_error = TextractServiceError(code=code, retryable=retryable)
            response_payload = RawCallPayload({"operation": "analyze_document", "status": "service_error", "attempts": attempts})
            call_status = CallStatus.ERROR
            error_payload = RawCallPayload({"type": "service_error", "code": code, "retryable": retryable})

        latency_ms = (time.perf_counter() - started) * 1000
        telemetry_response = RawCallPayload(
            {
                "operation": "analyze_document",
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
        assert result is not None
        return result
