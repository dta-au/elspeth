"""Audited HTTP client with automatic call recording.

Provides SSRF-safe HTTP methods via get_ssrf_safe() which uses IP pinning
to prevent DNS rebinding attacks. See core/security/web.py for details.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Network, IPv6Network
from threading import Lock
from typing import TYPE_CHECKING, Any

import httpx
import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.call_data import (
    CallPayload,
    HTTPCallError,
    HTTPCallRequest,
    HTTPCallResponse,
    HTTPRedirectReplayHop,
    HTTPResponseTransport,
    RawCallPayload,
)
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.token_usage import UNKNOWN_TOKEN_USAGE, TokenUsage
from elspeth.core.canonical import stable_hash
from elspeth.core.security.web import (
    SSRFSafeRequest,
    validate_url_for_ssrf,
)
from elspeth.plugins.infrastructure.clients.base import AuditedClientBase, TelemetryEmitCallback
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    filter_response_headers as _filter_response_headers,
)
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    fingerprint_headers as _fingerprint_headers,
)
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    fingerprint_params as _fingerprint_params,
)
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    fingerprint_url as _fingerprint_url,
)
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    is_sensitive_header as _is_sensitive_header_fn,
)
from elspeth.plugins.infrastructure.clients.json_utils import parse_json_strict as _parse_json_strict

logger = structlog.get_logger(__name__)

_HTTP_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)


def _sanitize_http_error_message(message: str) -> str:
    """Fingerprint URL candidates before an HTTP error enters the audit trail."""

    def sanitize_match(match: re.Match[str]) -> str:
        candidate = match.group(0)
        try:
            return _fingerprint_url(candidate)
        except (ValueError, UnicodeError, contract_errors.FrameworkBugError):
            # Audit persistence must neither leak an unparseable URL nor mask
            # the original transport exception with a sanitization failure.
            return "<redacted-http-url>"

    return _HTTP_URL_IN_TEXT.sub(sanitize_match, message)


if TYPE_CHECKING:
    from elspeth.contracts import Call
    from elspeth.contracts.audit_protocols import CallRecorder
    from elspeth.contracts.call_mode import CallModeSession, ReplayCallEvidence
    from elspeth.contracts.contexts import LimiterProtocol


class HTTPResponseBodyTooLargeError(httpx.HTTPError):
    """Raised when an audited HTTP response exceeds the configured streaming cap."""

    def __init__(
        self,
        *,
        url: str,
        body_size: int,
        max_body_bytes: int,
        response_payload: HTTPCallResponse,
    ) -> None:
        super().__init__(f"response body {body_size} bytes exceeds max_response_body_bytes {max_body_bytes} for {url}")
        self.url = url
        self.body_size = body_size
        self.max_body_bytes = max_body_bytes
        self.response_payload = response_payload
        self.response_data = response_payload.to_dict()


class AuditedHTTPClient(AuditedClientBase):
    """HTTP client that automatically records all calls to audit trail.

    Wraps httpx to ensure every HTTP call is recorded to the Landscape
    audit trail. Supports:
    - Automatic request/response recording
    - Auth header fingerprinting (HMAC fingerprint stored, not raw secrets)
    - Latency measurement
    - Error recording
    - Telemetry emission after successful audit recording
    - Rate limiting (when limiter provided)

    Example:
        client = AuditedHTTPClient(
            execution=execution,
            state_id=state_id,
            run_id=run_id,
            telemetry_emit=telemetry_emit,
            base_url="https://api.example.com",
            headers={"Authorization": "Bearer ..."},
            limiter=registry.get_limiter("api.example.com"),
        )

        response = client.post("/v1/process", json={"data": "value"})
        print(response.json())
    """

    def __init__(
        self,
        execution: CallRecorder,
        state_id: str | None,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        *,
        timeout: float = 30.0,
        base_url: str | None = None,
        headers: dict[str, str] | None = None,
        limiter: LimiterProtocol | None = None,
        token_id: str | None = None,
        operation_id: str | None = None,
        coordination_token: CoordinationToken | None = None,
        member_token: WorkerMembershipToken | None = None,
        work_item: TokenWorkItem | None = None,
        max_response_body_bytes: int | None = None,
        call_mode_session: CallModeSession | None = None,
        archived_auth_for_replay: bool = False,
        semantic_managed_identity_verify: bool = False,
    ) -> None:
        """Initialize audited HTTP client.

        Args:
            execution: CallRecorder for audit trail storage
            state_id: Node state ID to associate calls with
            run_id: Pipeline run ID for telemetry correlation
            telemetry_emit: Callback to emit telemetry events
            timeout: Request timeout in seconds (default: 30.0)
            base_url: Optional base URL to prepend to all requests
            headers: Default headers for all requests
            limiter: Optional rate limiter for throttling requests
            token_id: Optional token identity for telemetry correlation
            operation_id: Optional operation parent for source/sink/preflight calls
            max_response_body_bytes: Optional streaming cap for response bodies.
                When set, download aborts as soon as the observed body exceeds
                this many bytes and audit records a bounded truncation marker.
        """
        if max_response_body_bytes is not None and max_response_body_bytes <= 0:
            raise ValueError("max_response_body_bytes must be > 0 when configured")
        if archived_auth_for_replay and (call_mode_session is None or call_mode_session.mode is not RunMode.REPLAY):
            raise ValueError("archived_auth_for_replay requires a replay call-mode session")
        if semantic_managed_identity_verify and (call_mode_session is None or call_mode_session.mode is not RunMode.VERIFY):
            raise ValueError("semantic_managed_identity_verify requires a verify call-mode session")
        super().__init__(
            execution,
            state_id,
            run_id,
            telemetry_emit,
            operation_id=operation_id,
            limiter=limiter,
            token_id=token_id,
            coordination_token=coordination_token,
            member_token=member_token,
            work_item=work_item,
        )
        self._timeout = timeout
        self._base_url = base_url
        self._default_headers = headers or {}
        self._max_response_body_bytes = max_response_body_bytes
        self._call_mode_session = call_mode_session
        self._archived_auth_for_replay = archived_auth_for_replay
        self._semantic_managed_identity_verify = semantic_managed_identity_verify
        # Shared httpx.Client for connection pooling and TCP reuse.
        # httpx.Client is thread-safe; the internal pool handles concurrency.
        # Per-request timeouts override the default via timeout= kwarg.
        # follow_redirects=False: SSRF-safe methods manage redirects manually.
        # VERIFY waits until the first request has passed source-call admission.
        self._client_init_lock = Lock()
        self._client = (
            None
            if call_mode_session is not None and call_mode_session.mode in (RunMode.REPLAY, RunMode.VERIFY)
            else httpx.Client(timeout=self._timeout, follow_redirects=False)
        )

    # Delegate to shared module functions. Instance methods preserved for
    # call-site compatibility within this class.
    def _is_sensitive_header(self, header_name: str) -> bool:
        return _is_sensitive_header_fn(header_name)

    def _filter_request_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return _fingerprint_headers(headers)

    def _filter_response_headers(self, headers: dict[str, str]) -> dict[str, str]:
        return _filter_response_headers(headers)

    def _record_call(
        self,
        *,
        call_index: int,
        call_type: CallType,
        status: CallStatus,
        request_data: CallPayload,
        response_data: CallPayload | None = None,
        error: CallPayload | None = None,
        latency_ms: float | None = None,
        approved_prompt_artifact_hash: str | None = None,
        token_usage: TokenUsage = UNKNOWN_TOKEN_USAGE,
        llm_call_attempt: str | None = None,
        source_call_id: str | None = None,
    ) -> Call:
        """Sanitize HTTP error URLs at the shared audit persistence boundary."""
        if isinstance(error, HTTPCallError):
            error = HTTPCallError(
                type=error.type,
                message=_sanitize_http_error_message(error.message),
                status_code=error.status_code,
            )
        return super()._record_call(
            call_index=call_index,
            call_type=call_type,
            status=status,
            request_data=request_data,
            response_data=response_data,
            error=error,
            latency_ms=latency_ms,
            approved_prompt_artifact_hash=approved_prompt_artifact_hash,
            token_usage=token_usage,
            llm_call_attempt=llm_call_attempt,
            source_call_id=source_call_id,
        )

    def _extract_provider(self, url: str) -> str:
        """Extract provider (host) from URL for telemetry.

        SECURITY: This method MUST NOT leak credentials. URLs may contain
        embedded userinfo (e.g., https://user:pass@host/). We use hostname
        only, which strips the userinfo component per RFC 3986.

        Args:
            url: Full URL

        Returns:
            Host portion of URL (e.g., "api.example.com"), without credentials
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        # SECURITY: Use hostname (not netloc) to avoid leaking credentials.
        # netloc = [userinfo@]host[:port], hostname = just the host
        return parsed.hostname or "unknown"

    def close(self) -> None:
        """Close the underlying httpx client and release connections."""
        if self._client is not None:
            self._client.close()

    def _resolve_url(self, url: str) -> str:
        """Join base_url with path, handling slash combinations."""
        if self._base_url:
            base = self._base_url.rstrip("/")
            path = url.lstrip("/")
            return f"{base}/{path}"
        return url

    def _parse_response_body(self, response: httpx.Response, full_url: str) -> Any:
        """Parse response body at Tier 3 boundary with strict JSON validation.

        Handles JSON (with NaN/Infinity rejection), text, and binary content.
        """
        # Tier 3 external boundary: the content-type header is authored by the
        # remote server and may be absent. Absence is recorded as "" (honest: we
        # were told nothing about the type), which routes to the binary fallback
        # below — never fabricated into a concrete type.
        content_type = response.headers["content-type"] if "content-type" in response.headers else ""

        if "application/json" in content_type:
            parsed, error = _parse_json_strict(response.text)
            if error is not None:
                # JSON parse failure is captured in the audit trail via the
                # _json_parse_failed sentinel dict recorded by record_call().
                return {
                    "_json_parse_failed": True,
                    "_error": error,
                    "_raw_text": response.text[:10_000],
                }
            return parsed

        # Text content types: text/*, application/xml, application/x-www-form-urlencoded
        is_text_content = content_type.startswith("text/") or "xml" in content_type or "form-urlencoded" in content_type
        if is_text_content:
            return response.text

        # Binary content as base64 for JSON serialization
        return {"_binary": base64.b64encode(response.content).decode("ascii")}

    def _record_and_emit(
        self,
        *,
        call_index: int,
        full_url: str,
        request_data: Mapping[str, Any],
        response: httpx.Response | None,
        response_data: Mapping[str, Any] | None,
        error_data: CallPayload | None,
        latency_ms: float,
        call_status: CallStatus,
        request_payload: CallPayload,
        response_payload: CallPayload | None = None,
        token_id_override: str | None = None,
        source_call_id: str | None = None,
    ) -> Call:
        """Record call to audit trail and emit telemetry event.

        Args:
            request_payload: Typed DTO for telemetry (e.g., HTTPCallRequest).
            response_payload: Typed DTO for telemetry (e.g., HTTPCallResponse).
            token_id_override: Per-call token_id for telemetry. When provided,
                overrides the client-level token_id. Used by batch transforms
                where a single client serves multiple tokens.

        Returns:
            Call object from Landscape recording (contains request_ref and response_ref blob hashes).
        """
        if isinstance(error_data, HTTPCallError):
            error_data = HTTPCallError(
                type=error_data.type,
                message=_sanitize_http_error_message(error_data.message),
                status_code=error_data.status_code,
            )
        call = self._record_call(
            call_index=call_index,
            call_type=CallType.HTTP,
            status=call_status,
            request_data=request_payload,
            response_data=response_payload,
            error=error_data,
            latency_ms=latency_ms,
            source_call_id=source_call_id,
        )

        # Telemetry emitted AFTER successful Landscape recording
        self._emit_telemetry_after_audit(
            provider=self._extract_provider(full_url),
            call_status=call_status,
            latency_ms=latency_ms,
            request_data=request_data,
            response_data=response_data,
            request_payload=request_payload,
            response_payload=response_payload,
            token_id_override=token_id_override,
            call_type_label="http",
        )

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.verify_call(
                call_type=CallType.HTTP,
                request_data=request_data,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
                current_call_id=call.call_id,
                live_status=call_status,
                live_response_data=response_data,
                live_error_data=error_data.to_dict() if error_data is not None else None,
            )

        return call

    def _restore_replay_response(
        self, evidence: ReplayCallEvidence, *, method: str, expected_url: str | None, request_headers: dict[str, str]
    ) -> httpx.Response:
        """Parse retained transport as untrusted audit data before any egress."""
        payload = evidence.response_data
        if payload is None:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has no response payload")
        status_code = payload.get("status_code")
        body_size = payload.get("body_size")
        transport = payload.get("transport")
        if type(status_code) is not int or not 100 <= status_code <= 999:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has invalid status")
        if (body_size is not None and (type(body_size) is not int or body_size < 0)) or not isinstance(transport, Mapping):
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} lacks exact transport")
        encoded_body = transport.get("body_b64")
        header_pairs = transport.get("headers")
        recorded_url = transport.get("request_url")
        if type(encoded_body) is not str or type(recorded_url) is not str or (expected_url is not None and recorded_url != expected_url):
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has incomplete or divergent transport")
        if not isinstance(header_pairs, tuple | list):
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} lacks ordered headers")
        headers: list[tuple[str, str]] = []
        for pair in header_pairs:
            if not isinstance(pair, tuple | list) or len(pair) != 2 or type(pair[0]) is not str or type(pair[1]) is not str:
                raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has invalid headers")
            name, value = pair
            if self._filter_response_headers({name: value}) != {name: value} or name.lower() == "content-encoding":
                raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has unsafe transport headers")
            headers.append((name, value))
        try:
            body = base64.b64decode(encoded_body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has invalid body encoding") from exc
        if body_size is not None and len(body) != body_size:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has inconsistent body size")
        if _fingerprint_url(recorded_url) != recorded_url:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has unsafe transport URL")
        request = httpx.Request(method, recorded_url, headers=request_headers)
        response = httpx.Response(status_code, headers=headers, content=body, request=request)
        expected_status = CallStatus.SUCCESS if (200 <= status_code < 300 or 300 <= status_code < 400) else CallStatus.ERROR
        if evidence.status is not expected_status:
            raise AuditIntegrityError(f"HTTP replay call {evidence.source_call_id} has contradictory status")
        return response

    def _replay_request(
        self,
        *,
        method: str,
        full_url: str,
        request_dto: HTTPCallRequest,
        request_headers: dict[str, str],
        params: dict[str, str | int | float] | None,
        call_index: int,
        token_id: str | None,
    ) -> httpx.Response:
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.REPLAY:
            raise AuditIntegrityError("HTTP replay requested without a replay session")
        if self._archived_auth_for_replay:
            raise AuditIntegrityError("Archived HTTP credential replay requires the SSRF-safe request path")
        request_data = request_dto.to_dict()
        evidence = session.replay_call(
            call_type=CallType.HTTP,
            request_data=request_data,
            current_state_id=self._state_id,
            current_operation_id=self._operation_id,
            current_call_index=call_index,
        )
        expected_url = str(httpx.Request(method, full_url, params=params).url)
        response = self._restore_replay_response(evidence, method=method, expected_url=expected_url, request_headers=request_headers)
        response_payload = RawCallPayload(evidence.response_data or {})
        error_payload = RawCallPayload(evidence.error_data) if evidence.error_data is not None else None
        self._record_and_emit(
            call_index=call_index,
            full_url=full_url,
            request_data=request_data,
            response=response,
            response_data=evidence.response_data,
            error_data=error_payload,
            latency_ms=evidence.latency_ms or 0.0,
            call_status=evidence.status,
            request_payload=request_dto,
            response_payload=response_payload,
            token_id_override=token_id,
            source_call_id=evidence.source_call_id,
        )
        return response

    def _replay_ssrf_request(
        self,
        *,
        method: str,
        request: SSRFSafeRequest,
        request_dto: HTTPCallRequest,
        request_headers: dict[str, str],
        params: dict[str, str | int | float] | None,
        call_index: int,
        follow_redirects: bool,
        max_redirects: int,
    ) -> tuple[httpx.Response, str, Call]:
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.REPLAY:
            raise AuditIntegrityError("SSRF-safe HTTP replay requested without a replay session")
        request_data = request_dto.to_dict()
        replay_request_data: Mapping[str, Any] = request_data
        audit_request_data = request_data
        audit_request_payload: CallPayload = request_dto
        archived_request_id: str | None = None
        if self._archived_auth_for_replay:
            archived = session.archived_call_request(
                call_type=CallType.HTTP,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
            )
            archived_headers = archived.request_data.get("headers")
            current_headers = request_data.get("headers")
            if not isinstance(archived_headers, Mapping) or not isinstance(current_headers, Mapping):
                raise AuditIntegrityError("Archived HTTP request has invalid headers")
            archived_auth = [(key, value) for key, value in archived_headers.items() if type(key) is str and key.lower() == "authorization"]
            if len(archived_auth) != 1 or any(type(key) is str and key.lower() == "authorization" for key in current_headers):
                raise AuditIntegrityError("Archived HTTP request has ambiguous credential identity")
            fingerprint = archived_auth[0][1]
            if type(fingerprint) is not str or not fingerprint.startswith("<fingerprint:"):
                raise AuditIntegrityError("Archived HTTP credential has no trusted fingerprint")
            comparable = dict(archived.request_data)
            comparable["headers"] = {key: value for key, value in archived_headers.items() if key != archived_auth[0][0]}
            if comparable != request_data:
                raise AuditIntegrityError("Current HTTP request differs from archived non-auth fields")
            replay_request_data = archived.request_data
            archived_request_id = archived.source_call_id
            audit_request_data = {
                **archived.request_data,
                "replay_credential_origin": {
                    "source_run_id": session.source_run_id,
                    "source_call_id": archived.source_call_id,
                    "identity": "archived_fingerprint",
                },
            }
            audit_request_payload = RawCallPayload(audit_request_data)
        evidence = session.replay_call(
            call_type=CallType.HTTP,
            request_data=replay_request_data,
            current_state_id=self._state_id,
            current_operation_id=self._operation_id,
            current_call_index=call_index,
        )
        if archived_request_id is not None and evidence.source_call_id != archived_request_id:
            raise AuditIntegrityError("Archived HTTP credential does not belong to the replayed source call")
        payload = evidence.response_data
        transport = payload.get("transport") if payload is not None else None
        if not isinstance(transport, Mapping):
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} lacks transport")
        raw_hops = transport.get("redirect_hops", ())
        redirect_count = payload.get("redirect_count", 0) if payload is not None else 0
        if type(redirect_count) is not int or not isinstance(raw_hops, tuple | list) or redirect_count != len(raw_hops):
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} has incomplete redirects")
        if (not follow_redirects and redirect_count) or redirect_count > max_redirects:
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} exceeds requested redirect policy")
        last_hop_url: str | None = None
        last_hop_transport_url: str | None = None
        for raw_hop in raw_hops:
            if not isinstance(raw_hop, Mapping):
                raise AuditIntegrityError("SSRF-safe HTTP replay has malformed redirect evidence")
            hop_request = raw_hop.get("request")
            hop_response = raw_hop.get("response")
            if not isinstance(hop_request, Mapping) or not isinstance(hop_response, Mapping):
                raise AuditIntegrityError("SSRF-safe HTTP replay has missing redirect payloads")
            hop_index = self._next_call_index()
            hop_evidence = session.replay_call(
                call_type=CallType.HTTP_REDIRECT,
                request_data=hop_request,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=hop_index,
            )
            if hop_evidence.response_data != hop_response:
                raise AuditIntegrityError(f"SSRF-safe HTTP replay hop {hop_evidence.source_call_id} differs from parent archive")
            hop_method = hop_request.get("method")
            hop_transport = hop_response.get("transport")
            hop_url = hop_transport.get("request_url") if isinstance(hop_transport, Mapping) else None
            if type(hop_method) is not str or type(hop_url) is not str:
                raise AuditIntegrityError(f"SSRF-safe HTTP replay hop {hop_evidence.source_call_id} lacks transport URL")
            archived_hop_url = hop_request.get("url")
            if type(archived_hop_url) is not str:
                raise AuditIntegrityError(f"SSRF-safe HTTP replay hop {hop_evidence.source_call_id} lacks logical URL")
            last_hop_url = archived_hop_url
            last_hop_transport_url = hop_url
            self._restore_replay_response(hop_evidence, method=hop_method, expected_url=hop_url, request_headers=request_headers)
            hop_audit_request: Mapping[str, Any] = hop_request
            if self._archived_auth_for_replay:
                hop_headers = hop_request.get("headers")
                if not isinstance(hop_headers, Mapping):
                    raise AuditIntegrityError(f"SSRF-safe HTTP replay hop {hop_evidence.source_call_id} has invalid headers")
                hop_auth = [(key, value) for key, value in hop_headers.items() if type(key) is str and key.lower() == "authorization"]
                if len(hop_auth) > 1 or (hop_auth and (type(hop_auth[0][1]) is not str or not hop_auth[0][1].startswith("<fingerprint:"))):
                    raise AuditIntegrityError(f"SSRF-safe HTTP replay hop {hop_evidence.source_call_id} has invalid credential evidence")
                if hop_auth:
                    hop_audit_request = {
                        **hop_request,
                        "replay_credential_origin": {
                            "source_run_id": session.source_run_id,
                            "source_call_id": hop_evidence.source_call_id,
                            "identity": "archived_fingerprint",
                        },
                    }
            self._record_call(
                call_index=hop_index,
                call_type=CallType.HTTP_REDIRECT,
                status=hop_evidence.status,
                request_data=RawCallPayload(hop_audit_request),
                response_data=RawCallPayload(hop_response),
                error=RawCallPayload(hop_evidence.error_data) if hop_evidence.error_data is not None else None,
                latency_ms=hop_evidence.latency_ms,
                source_call_id=hop_evidence.source_call_id,
            )
        logical_url = transport.get("logical_url")
        if type(logical_url) is not str:
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} lacks logical URL")
        if _fingerprint_url(logical_url) != logical_url:
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} has unsafe logical URL")
        if redirect_count == 0 and logical_url != request.original_url:
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} changed URL")
        if redirect_count and (logical_url != last_hop_url or transport.get("request_url") != last_hop_transport_url):
            raise AuditIntegrityError(f"SSRF-safe HTTP replay call {evidence.source_call_id} has inconsistent final redirect")
        expected_url = str(httpx.Request(method, request.connection_url, params=params).url) if redirect_count == 0 else None
        response = self._restore_replay_response(
            evidence, method=method if redirect_count == 0 else "GET", expected_url=expected_url, request_headers=request_headers
        )
        call = self._record_and_emit(
            call_index=call_index,
            full_url=logical_url,
            request_data=audit_request_data,
            response=response,
            response_data=payload,
            error_data=RawCallPayload(evidence.error_data) if evidence.error_data is not None else None,
            latency_ms=evidence.latency_ms or 0.0,
            call_status=evidence.status,
            request_payload=audit_request_payload,
            response_payload=RawCallPayload(payload or {}),
            source_call_id=evidence.source_call_id,
        )
        return response, logical_url, call

    def _emit_telemetry_after_audit(
        self,
        *,
        provider: str,
        call_status: CallStatus,
        latency_ms: float,
        request_data: Mapping[str, Any],
        response_data: Mapping[str, Any] | None,
        request_payload: CallPayload,
        response_payload: CallPayload | None = None,
        token_id_override: str | None = None,
        call_type_label: str,
    ) -> None:
        """Emit HTTP telemetry after audit recording, crashing on programmer bugs."""

        effective_token_id = token_id_override if token_id_override is not None else self._telemetry_token_id()
        try:
            self._telemetry_emit(
                ExternalCallCompleted(
                    timestamp=datetime.now(UTC),
                    run_id=self._run_id,
                    call_type=CallType.HTTP,
                    provider=provider,
                    status=call_status,
                    latency_ms=latency_ms,
                    state_id=self._telemetry_state_id(),
                    operation_id=self._telemetry_operation_id(),
                    token_id=effective_token_id,
                    request_hash=stable_hash(request_data),
                    response_hash=stable_hash(response_data) if response_data else None,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    token_usage=None,
                )
            )
        except contract_errors.TIER_1_ERRORS:
            raise  # System bugs and audit integrity violations must crash
        except (TypeError, AttributeError, KeyError, NameError):
            raise  # Programming errors must crash
        except Exception as tel_err:
            logger.warning(
                "telemetry_emit_failed",
                error=str(tel_err),
                error_type=type(tel_err).__name__,
                run_id=self._run_id,
                state_id=self._telemetry_state_id(),
                operation_id=self._telemetry_operation_id(),
                call_type=call_type_label,
                exc_info=True,
            )

    def _verify_redirect_call(
        self,
        *,
        call: Call,
        call_index: int,
        request_data: HTTPCallRequest,
        status: CallStatus,
        response_data: HTTPCallResponse | None = None,
        error_data: HTTPCallError | None = None,
    ) -> None:
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.VERIFY:
            return
        session.verify_call(
            call_type=CallType.HTTP_REDIRECT,
            request_data=request_data.to_dict(),
            current_state_id=self._state_id,
            current_operation_id=self._operation_id,
            current_call_index=call_index,
            current_call_id=call.call_id,
            live_status=status,
            live_response_data=response_data.to_dict() if response_data is not None else None,
            live_error_data=error_data.to_dict() if error_data is not None else None,
        )

    def _admit_verify_call(self, *, call_type: CallType, request_data: Mapping[str, Any], call_index: int) -> None:
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.VERIFY:
            return
        session.admit_verify_call(
            call_type=call_type,
            request_data=request_data,
            current_state_id=self._state_id,
            current_operation_id=self._operation_id,
            current_call_index=call_index,
        )

    def _build_response_payload(
        self,
        response: httpx.Response,
        full_url: str,
        *,
        redirect_count: int = 0,
        logical_url: str | None = None,
        redirect_hops: tuple[HTTPRedirectReplayHop, ...] = (),
    ) -> tuple[HTTPCallResponse, dict[str, Any]]:
        """Build typed and dict response payloads from an HTTP response."""
        response_body = self._parse_response_body(response, full_url)
        response_dto = HTTPCallResponse(
            status_code=response.status_code,
            headers=self._filter_response_headers(dict(response.headers)),
            body_size=len(response.content),
            body=response_body,
            redirect_count=redirect_count,
            transport=self._build_replay_transport(response, logical_url=logical_url, redirect_hops=redirect_hops),
        )
        return response_dto, response_dto.to_dict()

    def _build_replay_transport(
        self, response: httpx.Response, *, logical_url: str | None = None, redirect_hops: tuple[HTTPRedirectReplayHop, ...] = ()
    ) -> HTTPResponseTransport | None:
        """Keep exact observable bytes and headers only when audit-safe.

        A filtered header, fingerprinted URL or compressed wire response has
        no exact reconstruction from the available httpx object. Such calls
        remain valid live audit records but are ineligible for replay.
        """
        if type(response.headers) is not httpx.Headers:
            return None
        raw_headers = dict(response.headers)
        if any(hop.response.transport is None for hop in redirect_hops):
            return None
        if self._filter_response_headers(raw_headers) != raw_headers:
            return None
        if "content-encoding" in response.headers:
            return None
        request_url = str(response.request.url)
        try:
            if _fingerprint_url(request_url) != request_url:
                return None
            if logical_url is not None and _fingerprint_url(logical_url) != logical_url:
                return None
        except (ValueError, UnicodeError, contract_errors.FrameworkBugError):
            return None
        return HTTPResponseTransport(
            body_b64=base64.b64encode(response.content).decode("ascii"),
            headers=tuple(response.headers.multi_items()),
            request_url=request_url,
            logical_url=logical_url,
            redirect_hops=redirect_hops,
        )

    def _build_truncated_response_payload(
        self,
        response: httpx.Response,
        *,
        full_url: str,
        observed_body_size: int,
        redirect_count: int = 0,
    ) -> HTTPCallResponse:
        """Build bounded audit metadata for a response aborted by the body cap."""
        response_dto = HTTPCallResponse(
            status_code=response.status_code,
            headers=self._filter_response_headers(dict(response.headers)),
            body_size=observed_body_size,
            body={
                "_truncated": True,
                "_reason": "body_too_large",
                "_captured_body": False,
                "_observed_body_size": observed_body_size,
                "_max_body_bytes": self._require_response_body_cap(full_url),
            },
            redirect_count=redirect_count,
        )
        return response_dto

    def _require_response_body_cap(self, full_url: str) -> int:
        if self._max_response_body_bytes is None:
            raise RuntimeError(f"response body cap requested for {full_url!r} but no cap is configured")
        return self._max_response_body_bytes

    def _consume_capped_response(
        self,
        response: httpx.Response,
        *,
        full_url: str,
        redirect_count: int = 0,
    ) -> httpx.Response:
        """Read a streaming response, aborting as soon as it exceeds the cap."""
        max_body_bytes = self._require_response_body_cap(full_url)
        chunks: list[bytes] = []
        observed_body_size = 0
        chunk_size = min(max_body_bytes + 1, 64 * 1024)
        for chunk in response.iter_bytes(chunk_size=chunk_size):
            observed_body_size += len(chunk)
            if observed_body_size > max_body_bytes:
                response_payload = self._build_truncated_response_payload(
                    response,
                    full_url=full_url,
                    observed_body_size=observed_body_size,
                    redirect_count=redirect_count,
                )
                raise HTTPResponseBodyTooLargeError(
                    url=full_url,
                    body_size=observed_body_size,
                    max_body_bytes=max_body_bytes,
                    response_payload=response_payload,
                )
            chunks.append(chunk)

        # ``iter_bytes()`` above has ALREADY applied content-decoding
        # (gzip/deflate/br) to the body. Reconstructing the Response with the
        # original ``Content-Encoding`` header makes httpx re-decode the
        # already-decoded body on read -> DecodingError ("incorrect header
        # check"). Drop the now-inaccurate content-encoding/length so the
        # reconstructed body is treated as identity. (iter_bytes is kept
        # deliberately: the body cap is measured on the DECODED size, which is
        # the decompression-bomb-relevant size.)
        decoded_headers = httpx.Headers(
            [(key, value) for key, value in response.headers.multi_items() if key.lower() not in ("content-encoding", "content-length")]
        )
        return httpx.Response(
            response.status_code,
            headers=decoded_headers,
            content=b"".join(chunks),
            request=response.request,
            extensions=response.extensions,
        )

    def _request_with_optional_body_cap(
        self,
        client: httpx.Client,
        method: str,
        full_url: str,
        *,
        headers: dict[str, str],
        timeout: float | None = None,
        json: Mapping[str, Any] | None = None,
        params: dict[str, str | int | float] | None = None,
        extensions: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request, streaming only when a response body cap is configured."""
        request_kwargs: dict[str, Any] = {"headers": headers}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        if json is not None or method == "POST":
            request_kwargs["json"] = json
        if params is not None or method == "GET":
            request_kwargs["params"] = params
        if extensions is not None:
            request_kwargs["extensions"] = extensions

        if self._max_response_body_bytes is None:
            if method == "GET":
                return client.get(full_url, **request_kwargs)
            if method == "POST":
                return client.post(full_url, **request_kwargs)
            return client.request(method, full_url, **request_kwargs)

        with client.stream(method, full_url, **request_kwargs) as response:
            return self._consume_capped_response(response, full_url=full_url)

    def _execute_request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str] | None,
        timeout: float | None,
        json: Mapping[str, Any] | None = None,
        params: dict[str, str | int | float] | None = None,
        audit_request_metadata: Mapping[str, Any] | None = None,
        token_id: str | None = None,
    ) -> httpx.Response:
        """Execute an HTTP request with audit recording and telemetry.

        Shared implementation for post() and get(). Handles URL resolution,
        header merging, response parsing, audit recording, and telemetry.

        Args:
            method: HTTP method ("POST" or "GET")
            url: URL path (appended to base_url if configured)
            headers: Additional headers for this request
            timeout: Request timeout override (uses client default if None)
            json: JSON body (POST only)
            params: Query parameters (GET only)
            audit_request_metadata: Audit-only metadata to include in the
                recorded request payload without sending it to the remote
                service.
            token_id: Per-call token_id for telemetry (overrides client default).
                Used by batch transforms where one client serves multiple tokens.

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: For network/HTTP errors
        """
        if self._semantic_managed_identity_verify:
            raise AuditIntegrityError("Managed identity verify requires the SSRF-safe request path")
        if self._call_mode_session is None or self._call_mode_session.mode is not RunMode.REPLAY:
            self._acquire_rate_limit()
        call_index = self._next_call_index()

        full_url = self._resolve_url(url)
        merged_headers = {**self._default_headers, **(headers or {})}
        effective_timeout = timeout if timeout is not None else self._timeout

        # Build request DTO for audit trail — dataclass handles method-specific
        # field inclusion via to_dict() (POST includes json, GET includes params).
        # DTO stays alive for typed telemetry payload; dict form used for Landscape hashing.
        request_dto = HTTPCallRequest(
            method=method,
            url=_fingerprint_url(full_url),
            headers=self._filter_request_headers(merged_headers),
            json=json,
            params=_fingerprint_params(params),
            audit_metadata=audit_request_metadata,
        )
        request_data = request_dto.to_dict()

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY:
            return self._replay_request(
                method=method,
                full_url=full_url,
                request_dto=request_dto,
                request_headers=merged_headers,
                params=params,
                call_index=call_index,
                token_id=token_id,
            )

        self._admit_verify_call(call_type=CallType.HTTP, request_data=request_data, call_index=call_index)

        start = time.perf_counter()
        response: httpx.Response | None = None

        try:
            if self._client is None:
                with self._client_init_lock:
                    if self._client is None:
                        if self._call_mode_session is None or self._call_mode_session.mode is not RunMode.VERIFY:
                            raise AuditIntegrityError("HTTP client absent outside admitted verify mode")
                        self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
            client = self._client
            if client is None:
                raise AuditIntegrityError("HTTP client absent after verify admission")
            # Dispatch to the correct httpx method
            response = self._request_with_optional_body_cap(
                client,
                method,
                full_url,
                json=json,
                params=params,
                headers=merged_headers,
                timeout=effective_timeout,
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            response_payload: HTTPCallResponse | None = None
            response_data: Mapping[str, Any] | None = None
            if isinstance(e, HTTPResponseBodyTooLargeError):
                response_payload = e.response_payload
                response_data = e.response_data

            self._record_and_emit(
                call_index=call_index,
                full_url=full_url,
                request_data=request_data,
                response=None,
                response_data=response_data,
                error_data=HTTPCallError(
                    type=type(e).__name__,
                    message=str(e),
                ),
                latency_ms=latency_ms,
                call_status=CallStatus.ERROR,
                request_payload=request_dto,
                response_payload=response_payload,
                token_id_override=token_id,
            )

            raise

        latency_ms = (time.perf_counter() - start) * 1000

        # 2xx = SUCCESS, 4xx/5xx = ERROR
        is_success = 200 <= response.status_code < 300
        call_status = CallStatus.SUCCESS if is_success else CallStatus.ERROR
        response_dto, response_data = self._build_response_payload(response, full_url)

        error_data: CallPayload | None = None
        if not is_success:
            error_data = HTTPCallError(
                type="HTTPError",
                message=f"HTTP {response.status_code}",
                status_code=response.status_code,
            )

        self._record_and_emit(
            call_index=call_index,
            full_url=full_url,
            request_data=request_data,
            response=response,
            response_data=response_data,
            error_data=error_data,
            latency_ms=latency_ms,
            call_status=call_status,
            request_payload=request_dto,
            response_payload=response_dto,
            token_id_override=token_id,
        )

        return response

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        audit_request_metadata: Mapping[str, Any] | None = None,
        token_id: str | None = None,
    ) -> httpx.Response:
        """Make POST request with automatic audit recording.

        Args:
            url: URL path (appended to base_url if configured)
            json: JSON body to send (optional)
            headers: Additional headers for this request
            timeout: Request timeout in seconds (uses client default if None)
            audit_request_metadata: Audit-only metadata to include in the
                recorded request payload without sending it to the remote
                service.
            token_id: Per-call token_id for telemetry (overrides client default).
                Used by batch transforms where one client serves multiple tokens.

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: For network/HTTP errors
        """
        return self._execute_request(
            method="POST",
            url=url,
            headers=headers,
            timeout=timeout,
            json=json,
            audit_request_metadata=audit_request_metadata,
            token_id=token_id,
        )

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str | int | float] | None = None,
        timeout: float | None = None,
        token_id: str | None = None,
    ) -> httpx.Response:
        """Make GET request with automatic audit recording.

        Args:
            url: URL path (appended to base_url if configured)
            headers: Additional headers for this request
            params: Query parameters to append to URL
            timeout: Request timeout in seconds (uses client default if None)
            token_id: Per-call token_id for telemetry (overrides client default).
                Used by batch transforms where one client serves multiple tokens.

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: For network/HTTP errors
        """
        return self._execute_request(
            method="GET",
            url=url,
            headers=headers,
            timeout=timeout,
            params=params,
            token_id=token_id,
        )

    def _send_ssrf_safe_request(
        self,
        client: httpx.Client,
        method: str,
        connection_url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, str] | None,
        json: Mapping[str, Any] | None,
        params: dict[str, str | int | float] | None,
    ) -> httpx.Response:
        """Send one IP-pinned request with method-specific httpx handling."""
        return self._request_with_optional_body_cap(
            client,
            method,
            connection_url,
            json=json,
            params=params,
            headers=headers,
            extensions=extensions,
        )

    def request_ssrf_safe(
        self,
        method: str,
        request: SSRFSafeRequest,
        *,
        headers: dict[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        params: dict[str, str | int | float] | None = None,
        follow_redirects: bool = False,
        max_redirects: int = 10,
        allowed_ranges: Sequence[IPv4Network | IPv6Network] = (),
        verify_source_call_id: str | None = None,
    ) -> tuple[httpx.Response, str, Call]:
        """HTTP request with SSRF-safe IP pinning and redirect validation.

        Connects to the pre-validated IP in the SSRFSafeRequest, setting the
        Host header and TLS SNI to the original hostname. Each redirect hop
        is independently validated against the SSRF blocklist.

        Args:
            method: HTTP method to send for the initial request.
            request: SSRFSafeRequest from validate_url_for_ssrf()
            headers: Additional headers for this request
            json: JSON body for POST requests.
            params: Query parameters for the request.
            follow_redirects: Whether to follow HTTP redirects (default: False)
            max_redirects: Maximum redirect hops when follow_redirects=True
            allowed_ranges: IP networks that may bypass the default SSRF
                blocklist when validating redirect targets. This must match
                the ranges used to create the initial SSRFSafeRequest so every
                redirect hop preserves the same caller-approved boundary.
            verify_source_call_id: Source HTTP call admitted before managed
                identity token acquisition in verify mode.

        Returns:
            Tuple of (httpx.Response, final hostname URL as string, Call).
            The hostname URL is the logical URL after all redirects —
            distinct from response.url which is IP-based due to SSRF pinning.
            When follow_redirects is False or no redirects occurred, this is
            the original request URL.
            The Call contains request_ref and response_ref blob hashes from
            the audit trail.

        Raises:
            httpx.HTTPError: For network/HTTP errors
            SSRFBlockedError: If redirect target resolves to blocked IP
        """
        method_upper = method.upper()
        if self._semantic_managed_identity_verify:
            if not verify_source_call_id:
                raise AuditIntegrityError("Managed identity verify requires a preflight source call")
            if follow_redirects:
                raise AuditIntegrityError("Managed identity verify does not support redirect following")
        elif verify_source_call_id is not None:
            raise AuditIntegrityError("Verify source call identity requires managed identity verify mode")
        if self._call_mode_session is None or self._call_mode_session.mode is not RunMode.REPLAY:
            self._acquire_rate_limit()

        call_index = self._next_call_index()

        merged_headers = {
            **self._default_headers,
            **(headers or {}),
            "Host": request.host_header,
        }

        connection_url = request.connection_url
        effective_timeout = self._timeout

        # TLS SNI: use original hostname for certificate verification
        extensions: dict[str, str] = {}
        if request.scheme == "https":
            extensions["sni_hostname"] = request.sni_hostname

        # Record original URL and resolved IP in audit trail.
        # DTO stays alive for typed telemetry payload; dict form used for Landscape hashing.
        request_dto = HTTPCallRequest(
            method=method_upper,
            url=_fingerprint_url(request.original_url),
            headers=self._filter_request_headers(merged_headers),
            json=json,
            params=_fingerprint_params(params),
            resolved_ip=request.resolved_ip,
        )
        request_data = request_dto.to_dict()

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY:
            return self._replay_ssrf_request(
                method=method_upper,
                request=request,
                request_dto=request_dto,
                request_headers=merged_headers,
                params=params,
                call_index=call_index,
                follow_redirects=follow_redirects,
                max_redirects=max_redirects,
            )

        if self._semantic_managed_identity_verify:
            session = self._call_mode_session
            if session is None or verify_source_call_id is None:
                raise AuditIntegrityError("Managed identity verify session or source call is missing")
            admitted_source_call_id = session.admit_verify_http_managed_identity(
                request_data=request_data,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
                source_call_id=verify_source_call_id,
            )
            if admitted_source_call_id != verify_source_call_id:
                raise AuditIntegrityError("Managed identity verify admitted a different source call")
        else:
            self._admit_verify_call(call_type=CallType.HTTP, request_data=request_data, call_index=call_index)

        start = time.perf_counter()
        response: httpx.Response | None = None
        replay_hops: list[HTTPRedirectReplayHop] = []

        try:
            # Ephemeral client for SSRF-safe requests: connection_url uses the
            # resolved IP (e.g. https://1.2.3.4:443/path), so all hostnames sharing
            # an IP would map to the same pool key. A shared client would reuse a
            # TLS connection established for hostname-A when requesting hostname-B,
            # silently skipping SNI negotiation and certificate verification.
            with httpx.Client(
                timeout=effective_timeout,
                follow_redirects=False,
            ) as ssrf_client:
                response = self._send_ssrf_safe_request(
                    ssrf_client,
                    method_upper,
                    connection_url,
                    headers=merged_headers,
                    extensions=extensions if extensions else None,
                    json=json,
                    params=params,
                )

            # Handle redirects with SSRF validation at each hop
            redirect_count = 0
            final_hostname_url = request.original_url
            if follow_redirects:
                response, redirect_count, final_hostname_url = self._follow_redirects_safe(
                    response,
                    max_redirects,
                    effective_timeout,
                    merged_headers,
                    original_url=request.original_url,
                    allowed_ranges=allowed_ranges,
                    replay_hops=replay_hops,
                )

            latency_ms = (time.perf_counter() - start) * 1000

            is_success = 200 <= response.status_code < 300
            call_status = CallStatus.SUCCESS if is_success else CallStatus.ERROR

            response_dto, response_data = self._build_response_payload(
                response,
                final_hostname_url,
                redirect_count=redirect_count,
                logical_url=final_hostname_url,
                redirect_hops=tuple(replay_hops),
            )

            error_data: CallPayload | None = None
            if not is_success:
                error_data = HTTPCallError(
                    type="HTTPError",
                    message=f"HTTP {response.status_code}",
                    status_code=response.status_code,
                )

        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000

            response_payload: HTTPCallResponse | None = None
            error_response_data: Mapping[str, Any] | None = None
            if isinstance(e, HTTPResponseBodyTooLargeError):
                response_payload = e.response_payload
                error_response_data = e.response_data
            elif response is not None:
                response_payload, error_response_data = self._build_response_payload(response, request.original_url)

            error_payload = HTTPCallError(
                type=type(e).__name__,
                message=_sanitize_http_error_message(str(e)),
            )
            failed_call = self._record_call(
                call_index=call_index,
                call_type=CallType.HTTP,
                status=CallStatus.ERROR,
                request_data=request_dto,
                response_data=response_payload,
                error=error_payload,
                latency_ms=latency_ms,
            )

            self._emit_telemetry_after_audit(
                provider=request.host_header,
                call_status=CallStatus.ERROR,
                latency_ms=latency_ms,
                request_data=request_data,
                response_data=error_response_data,
                request_payload=request_dto,
                response_payload=response_payload,
                call_type_label="http_ssrf_safe",
            )

            if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
                self._call_mode_session.verify_call(
                    call_type=CallType.HTTP,
                    request_data=request_data,
                    current_state_id=self._state_id,
                    current_operation_id=self._operation_id,
                    current_call_index=call_index,
                    current_call_id=failed_call.call_id,
                    live_status=CallStatus.ERROR,
                    live_response_data=error_response_data,
                    live_error_data=error_payload.to_dict(),
                )

            raise

        # Success path: record + emit OUTSIDE the network try block, mirroring
        # _execute_request (post/get). _emit_telemetry_after_audit re-raises
        # programmer bugs (TypeError/AttributeError/KeyError/NameError) and
        # Tier-1 errors; keeping it inside the try would let `except Exception`
        # record a SECOND ERROR call at the same call_index after the SUCCESS
        # record already committed — a duplicate-index audit write that also
        # misattributes a telemetry/programmer bug to the HTTP call
        # (elspeth-affb35a660). Out here, that re-raise propagates cleanly with
        # exactly one SUCCESS record written.
        call = self._record_call(
            call_index=call_index,
            call_type=CallType.HTTP,
            status=call_status,
            request_data=request_dto,
            response_data=response_dto,
            error=error_data,
            latency_ms=latency_ms,
        )

        self._emit_telemetry_after_audit(
            provider=request.host_header,
            call_status=call_status,
            latency_ms=latency_ms,
            request_data=request_data,
            response_data=response_data,
            request_payload=request_dto,
            response_payload=response_dto,
            call_type_label="http_ssrf_safe",
        )

        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.verify_call(
                call_type=CallType.HTTP,
                request_data=request_data,
                current_state_id=self._state_id,
                current_operation_id=self._operation_id,
                current_call_index=call_index,
                current_call_id=call.call_id,
                live_status=call_status,
                live_response_data=response_data,
                live_error_data=error_data.to_dict() if error_data is not None else None,
            )

        return response, final_hostname_url, call

    def get_ssrf_safe(
        self,
        request: SSRFSafeRequest,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = False,
        max_redirects: int = 10,
        allowed_ranges: Sequence[IPv4Network | IPv6Network] = (),
        verify_source_call_id: str | None = None,
    ) -> tuple[httpx.Response, str, Call]:
        """GET with SSRF-safe IP pinning and redirect validation.

        Compatibility wrapper around ``request_ssrf_safe()`` for existing
        callers that only fetch pages.
        """
        return self.request_ssrf_safe(
            "GET",
            request,
            headers=headers,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            allowed_ranges=allowed_ranges,
            verify_source_call_id=verify_source_call_id,
        )

    def _follow_redirects_safe(
        self,
        response: httpx.Response,
        max_redirects: int,
        timeout: float,
        original_headers: dict[str, str],
        original_url: str,
        *,
        allowed_ranges: Sequence[IPv4Network | IPv6Network] = (),
        replay_hops: list[HTTPRedirectReplayHop] | None = None,
    ) -> tuple[httpx.Response, int, str]:
        """Follow HTTP redirects with SSRF validation at each hop.

        Each redirect target is independently resolved and validated against
        the SSRF blocklist, preventing redirect-based SSRF attacks like:
        attacker.com -> 301 -> http://169.254.169.254/

        Args:
            response: Initial response (may be a redirect)
            max_redirects: Maximum number of redirect hops
            timeout: Request timeout for each hop
            original_headers: Headers from original request (minus Host, which is set per-hop)
            original_url: Hostname-based URL for resolving relative redirects.
                response.url is IP-based (from connection_url rewrite), so relative
                Location headers must resolve against the original hostname URL to
                preserve correct Host headers and TLS SNI.
            allowed_ranges: IP networks that may bypass the default SSRF
                blocklist when validating each redirect target. These ranges do
                not bypass the unconditional blocks enforced by
                validate_url_for_ssrf().

        Returns:
            Tuple of (final non-redirect response, number of redirects followed,
            final hostname URL as string). The hostname URL is the logical URL
            after all redirects — distinct from response.url which is IP-based
            due to SSRF pinning.

        Raises:
            SSRFBlockedError: If any redirect target resolves to a blocked IP
            httpx.TooManyRedirects: If redirect chain exceeds max_redirects
        """
        redirects_followed = 0
        # Track the logical hostname URL for resolving relative redirects.
        # response.url is IP-based (from connection_url), so relative Location
        # headers would resolve against the IP instead of the hostname.
        hostname_url = httpx.URL(original_url)

        while response.is_redirect and redirects_followed < max_redirects:
            # Tier 3 external boundary: the redirect Location header is authored
            # by the remote server and may be absent on a malformed redirect.
            # Absence is recorded as None and stops redirect following — never
            # fabricated into a target URL.
            location = response.headers["location"] if "location" in response.headers else None
            if not location:
                break

            # Capture the URL we're redirecting FROM (before updating hostname_url)
            redirect_from = str(hostname_url)

            # Resolve relative URLs against the hostname URL, NOT response.url
            redirect_url = str(hostname_url.join(location))

            hop_call_index = self._next_call_index()
            hop_number = redirects_followed + 1
            hop_start = time.perf_counter()

            hop_headers = {k: v for k, v in original_headers.items() if k.lower() != "host"}
            redirect_url_obj = httpx.URL(redirect_url)
            redirect_host = redirect_url_obj.host or "unknown"
            default_port = 443 if redirect_url_obj.scheme == "https" else 80
            redirect_port = redirect_url_obj.port or default_port
            hop_headers["Host"] = f"{redirect_host}:{redirect_port}" if redirect_port != default_port else redirect_host

            blocked_hop_request_dto = HTTPCallRequest(
                method="GET",
                url=_fingerprint_url(redirect_url),
                headers=self._filter_request_headers(hop_headers),
                hop_number=hop_number,
                redirect_from=_fingerprint_url(redirect_from),
            )

            # CRITICAL: Validate the redirect target for SSRF
            try:
                redirect_request = validate_url_for_ssrf(redirect_url, allowed_ranges=allowed_ranges)
            except contract_errors.TIER_1_ERRORS:
                raise
            except Exception as redirect_err:
                self._admit_verify_call(
                    call_type=CallType.HTTP_REDIRECT,
                    request_data=blocked_hop_request_dto.to_dict(),
                    call_index=hop_call_index,
                )
                hop_latency_ms = (time.perf_counter() - hop_start) * 1000
                error_data = HTTPCallError(
                    type=type(redirect_err).__name__,
                    message=_sanitize_http_error_message(str(redirect_err)),
                )
                blocked_call = self._record_call(
                    call_index=hop_call_index,
                    call_type=CallType.HTTP_REDIRECT,
                    status=CallStatus.ERROR,
                    request_data=blocked_hop_request_dto,
                    error=error_data,
                    latency_ms=hop_latency_ms,
                )
                self._verify_redirect_call(
                    call=blocked_call,
                    call_index=hop_call_index,
                    request_data=blocked_hop_request_dto,
                    status=CallStatus.ERROR,
                    error_data=error_data,
                )
                raise

            # Update hostname_url to the redirect target for the next iteration.
            # If this was an absolute redirect to a different host, hostname_url
            # now tracks that new host.
            hostname_url = httpx.URL(redirect_url)

            # Build headers for this hop (Host header for virtual hosting)
            hop_headers["Host"] = redirect_request.host_header

            # TLS SNI for this hop
            extensions: dict[str, str] = {}
            if redirect_request.scheme == "https":
                extensions["sni_hostname"] = redirect_request.sni_hostname

            # Acquire rate limit for each redirect hop — each hop is a separate
            # outbound network request that must be throttled independently.
            # Bug fix: redirect hops were bypassing the rate limiter.
            self._acquire_rate_limit()

            # Pre-allocate call index and request data BEFORE the hop so that
            # both success and failure paths can record the hop in the audit trail.
            hop_request_dto = HTTPCallRequest(
                method="GET",
                url=_fingerprint_url(redirect_url),
                headers=self._filter_request_headers(hop_headers),
                resolved_ip=redirect_request.resolved_ip,
                hop_number=hop_number,
                redirect_from=_fingerprint_url(redirect_from),
            )
            self._admit_verify_call(call_type=CallType.HTTP_REDIRECT, request_data=hop_request_dto.to_dict(), call_index=hop_call_index)
            redirects_followed += 1

            # Ephemeral client per redirect hop: same TLS/SNI isolation rationale
            # as the initial SSRF-safe request — IP-based connection_url would
            # cause the pool to reuse connections across different hostnames.
            try:
                with httpx.Client(
                    timeout=timeout,
                    follow_redirects=False,
                ) as hop_client:
                    response = self._request_with_optional_body_cap(
                        hop_client,
                        "GET",
                        redirect_request.connection_url,
                        headers=hop_headers,
                        extensions=extensions if extensions else None,
                    )
            except Exception as hop_err:
                hop_latency_ms = (time.perf_counter() - hop_start) * 1000
                hop_response_payload = hop_err.response_payload if isinstance(hop_err, HTTPResponseBodyTooLargeError) else None
                # Record the failed hop in the audit trail so lineage is complete
                error_data = HTTPCallError(
                    type=type(hop_err).__name__,
                    message=_sanitize_http_error_message(str(hop_err)),
                )
                failed_hop_call = self._record_call(
                    call_index=hop_call_index,
                    call_type=CallType.HTTP_REDIRECT,
                    status=CallStatus.ERROR,
                    request_data=hop_request_dto,
                    response_data=hop_response_payload,
                    error=error_data,
                    latency_ms=hop_latency_ms,
                )
                self._verify_redirect_call(
                    call=failed_hop_call,
                    call_index=hop_call_index,
                    request_data=hop_request_dto,
                    status=CallStatus.ERROR,
                    response_data=hop_response_payload,
                    error_data=error_data,
                )
                raise

            hop_latency_ms = (time.perf_counter() - hop_start) * 1000

            # Record this redirect hop in the audit trail.
            # Each hop is a real network call — it may hit a different server.
            hop_response_dto = HTTPCallResponse(
                status_code=response.status_code,
                headers=self._filter_response_headers(dict(response.headers)),
                transport=self._build_replay_transport(response),
            )
            if replay_hops is not None:
                replay_hops.append(HTTPRedirectReplayHop(request=hop_request_dto, response=hop_response_dto))

            hop_status = CallStatus.SUCCESS if response.status_code < 400 else CallStatus.ERROR
            hop_call = self._record_call(
                call_index=hop_call_index,
                call_type=CallType.HTTP_REDIRECT,
                status=hop_status,
                request_data=hop_request_dto,
                response_data=hop_response_dto,
                latency_ms=hop_latency_ms,
            )
            self._verify_redirect_call(
                call=hop_call,
                call_index=hop_call_index,
                request_data=hop_request_dto,
                status=hop_status,
                response_data=hop_response_dto,
            )

        if response.is_redirect and redirects_followed >= max_redirects:
            raise httpx.TooManyRedirects(
                f"Exceeded {max_redirects} redirects",
                request=response.request,
            )

        return response, redirects_followed, str(hostname_url)
