"""Shared asynchronous-analyze machinery for the Azure document transforms.

Azure AI Document Intelligence and Azure AI Content Understanding both expose
the same long-running-operation (LRO) shape: a POST that answers ``202`` with an
``Operation-Location`` header, then a GET polled until a terminal status. This
module owns that shape once so both transforms get the same security posture,
retry budget, audit projection and failure vocabulary:

- every HTTP call flows through ``AuditedHTTPClient`` (full request/response
  audit, header fingerprinting so the api-key is never stored raw, telemetry,
  rate limiting);
- the polled ``Operation-Location`` URL carries our api-key header, so it is
  followed only when its scheme is https and its host AND port match the
  configured endpoint;
- a document URL is recorded in the audit with credential-bearing query
  parameters (a SAS ``sig``) and userinfo fingerprinted, while the wire body
  carries the URL verbatim;
- 429/503 capacity responses are retried inside one per-row budget, honouring
  Azure's ``Retry-After`` but never past the budget;
- a malformed or failed analysis fails the ROW closed with a bounded reason,
  never the batch.

Subclasses supply the service specifics: the analyze URL, the request body for
a URL or base64 document, how a poll body maps onto a terminal status, and how a
succeeded result enriches the row.
"""

from __future__ import annotations

import re
import threading
import time
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Self
from urllib.parse import urlparse

import httpx
import structlog
from pydantic import Field, field_validator, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.errors import FrameworkBugError, PluginRetryableError, TransformErrorReason, is_capacity_error
from elspeth.contracts.events import TelemetryEvent
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.batching import BatchTransformMixin, OutputPort
from elspeth.plugins.infrastructure.clients.fingerprinting import fingerprint_url
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient, HTTPResponseBodyTooLargeError
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.telemetry import make_warn_telemetry_before_start
from elspeth.plugins.transforms.azure.analyze_result import (
    AnalyzeStatus,
    MalformedPollBodyError,
    azure_error_codes,
    operation_location_host_matches,
    parse_poll_body,
)

logger = structlog.get_logger(__name__)
_warn_telemetry_before_start = make_warn_telemetry_before_start(logger)

# Slack on top of the per-call request timeout when sizing the batch-mixin wait.
_HTTP_TIMEOUT_HEADROOM_SECONDS = 30.0

# Padded standard-alphabet base64: whole 4-character groups, the last one
# optionally ``xx==`` or ``xxx=``. Rejects whitespace, ``data:`` URI prefixes,
# the URL-safe alphabet and unpadded or mis-padded input up front, which Azure
# would otherwise reject only after a billed, audited submit.
_BASE64_PATTERN = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")


@dataclass(frozen=True, slots=True)
class AnalyzeRequest:
    """The analyze submit: the wire body, and the projection the audit records.

    Both are deep-frozen at construction and thawed back to plain JSON
    containers only at the HTTP call.
    """

    body: Mapping[str, Any]
    audit_body: Mapping[str, Any]

    def __post_init__(self) -> None:
        freeze_fields(self, "body", "audit_body")


class _RowFailed(Exception):
    """Carries a row's terminal error result out of the submit/poll helpers.

    Raised only inside ``_process_single_with_state``'s call tree and converted
    back to the result there, so one row's failure never escapes the row.
    """

    def __init__(self, result: TransformResult) -> None:
        super().__init__(result.reason)
        self.result = result


def validate_azure_ai_endpoint(value: str, *, host_suffixes: tuple[str, ...], service: str) -> str:
    """Validate that a credential-bearing endpoint is an origin on an Azure AI host."""
    from elspeth.core.url_validation import validate_credential_safe_https_url

    endpoint = validate_credential_safe_https_url(value, field_name="endpoint")
    parsed = urlparse(endpoint)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not any(hostname.endswith(suffix) and bool(hostname.removesuffix(suffix)) for suffix in host_suffixes):
        allowed = ", ".join(f"*{suffix}" for suffix in host_suffixes)
        raise ValueError(f"endpoint must be an Azure {service} resource endpoint ({allowed})")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint must use a valid HTTPS port") from exc
    if port not in (None, 443):
        raise ValueError("endpoint must use the standard HTTPS port 443")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be an origin URL without a path, parameters, query, or fragment")
    return endpoint


class AzureAnalyzeConfig(TransformDataConfig):
    """Options shared by every Azure asynchronous-analyze transform.

    Subclasses add the service fields (model or analyzer id, output targets)
    and implement ``all_output_field_names``.
    """

    api_key: str = Field(..., repr=False, description="Resource API key (sent as Ocp-Apim-Subscription-Key).")

    source_mode: str = Field(..., pattern=r"^(url|base64)$", description="How the document reference is read: 'url' or 'base64'.")
    source_field: str = Field(..., description="Row field holding the document URL (url mode) or base64 string (base64 mode).")
    max_base64_chars: int = Field(8_000_000, gt=0, description="Max length of a base64 source string (bounds the audited request blob).")

    poll_interval_seconds: float = Field(1.0, gt=0, description="Initial poll delay.")
    poll_backoff_multiplier: float = Field(1.5, ge=1, description="Exponential poll backoff factor.")
    poll_max_interval_seconds: float = Field(10.0, gt=0, description="Poll backoff cap.")
    poll_timeout_seconds: float = Field(300.0, gt=0, description="Outer bound on the poll sequence.")
    request_timeout_seconds: float = Field(60.0, gt=0, description="Per HTTP call timeout.")
    max_response_body_bytes: int = Field(50_000_000, gt=0, description="Cap on the analyze result response body.")
    max_capacity_retry_seconds: int = Field(
        300, gt=0, description="Per-row 429/503 retry budget (default 300 bounds web-worker hold time)."
    )
    batch_wait_timeout_seconds: int = Field(3600, gt=0, description="Batch-mixin per-row wait timeout.")

    @field_validator("api_key")
    @classmethod
    def _reject_empty_api_key(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("api_key must not be empty")
        return v

    @field_validator("source_field")
    @classmethod
    def _reject_empty_source_field(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source_field must not be empty")
        return v

    @model_validator(mode="after")
    def _validate_shared_consistency(self) -> Self:
        names = self.all_output_field_names()
        if not names:
            raise ValueError(f"At least one output target must be configured: {self.output_target_description()}.")
        dupes = sorted({name for name in names if names.count(name) > 1})
        if dupes:
            raise ValueError(f"Duplicate output field names: {dupes}. Each output field must be unique.")
        if self.poll_max_interval_seconds < self.poll_interval_seconds:
            raise ValueError("poll_max_interval_seconds must be >= poll_interval_seconds.")
        return self

    def all_output_field_names(self) -> list[str]:
        """Every output row field the transform may add."""
        raise NotImplementedError

    def output_target_description(self) -> str:
        """The output options, phrased for the 'no output configured' error."""
        raise NotImplementedError

    @property
    def declared_input_fields(self) -> frozenset[str]:
        """Declare ``source_field`` as required input so a missing reference is caught at
        DAG/compose validation rather than per-row at runtime (mirrors web_scrape's
        url_field). That holds only where the upstream contract PARTICIPATES in the
        guarantee vote: against an observed producer both static surfaces abstain by
        design and the executor's per-row check stays the only enforcement."""
        return super().declared_input_fields | frozenset({self.source_field})


class AzureAnalyzeTransform(BaseTransform, BatchTransformMixin):
    """Row-pipelined Azure analyze transform: submit, poll to terminal, enrich.

    Each row supplies one document reference (URL or base64). Rows are in flight
    concurrently with FIFO output ordering (BatchTransformMixin).
    """

    determinism = Determinism.EXTERNAL_CALL
    # Human service name used in error messages and network-error text.
    service_label: str = "Azure AI"

    _CAPACITY_RETRY_INITIAL_DELAY_SECONDS = 0.05
    _CAPACITY_RETRY_MAX_DELAY_SECONDS = 1.0

    def _init_analyze(self, cfg: AzureAnalyzeConfig, *, endpoint: str) -> None:
        """Bind the shared options. Called by the subclass ``__init__`` after config parse."""
        self._endpoint = endpoint.rstrip("/")
        self._api_key = cfg.api_key
        self._source_mode = cfg.source_mode
        self._source_field = cfg.source_field
        # The document reference must arrive as a non-empty string or the row
        # fails closed — declare the string-type half of that contract so the
        # DAG rejects a provably non-string producer (elspeth-b19dfe41fb).
        self.declared_string_input_fields = frozenset({cfg.source_field})
        self._max_base64_chars = cfg.max_base64_chars
        self._request_timeout_seconds = cfg.request_timeout_seconds
        self._max_response_body_bytes = cfg.max_response_body_bytes
        self._max_capacity_retry_seconds = cfg.max_capacity_retry_seconds
        self._poll_interval_seconds = cfg.poll_interval_seconds
        self._poll_backoff_multiplier = cfg.poll_backoff_multiplier
        self._poll_max_interval_seconds = cfg.poll_max_interval_seconds
        self._poll_timeout_seconds = cfg.poll_timeout_seconds
        self._batch_wait_timeout_seconds = cfg.batch_wait_timeout_seconds
        # Worst case for one row: the capacity budget and the poll budget both
        # run from the row start, so the last call can START just before the
        # later of the two deadlines and then take a full request timeout.
        self._effective_batch_wait_timeout_seconds = max(
            float(self._batch_wait_timeout_seconds),
            float(self._poll_timeout_seconds)
            + float(self._max_capacity_retry_seconds)
            + float(self._request_timeout_seconds)
            + _HTTP_TIMEOUT_HEADROOM_SECONDS,
        )

        self._recorder: PluginAuditWriter | None = None
        self._run_id: str = ""
        self._telemetry_emit: Callable[[TelemetryEvent], None] = _warn_telemetry_before_start
        self._limiter: Any = None
        self._http_clients: dict[str, Any] = {}
        self._http_clients_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._batch_initialized = False

    # ── Service hooks ─────────────────────────────────────────────────────
    # Abstract, so plugin discovery skips this base (``inspect.isabstract``).

    @abstractmethod
    def _analyze_url(self) -> str:
        """The full analyze POST URL including the api-version query."""

    @abstractmethod
    def _url_request(self, document_url: str, audit_url: str) -> AnalyzeRequest:
        """The submit body for a document URL (``audit_url`` is the fingerprinted spelling)."""

    @abstractmethod
    def _base64_request(self, data: str) -> AnalyzeRequest:
        """The submit body for an inline base64 document."""

    # The service's poll vocabulary, read by the shared ``parse_poll_body``
    # boundary: raw status string -> neutral status, the succeeded body's
    # result member, and the malformed_response error_type when it is missing.
    _POLL_STATUSES: ClassVar[Mapping[str, AnalyzeStatus]]
    _RESULT_KEY: ClassVar[str]
    _MISSING_RESULT_ERROR: ClassVar[str]

    @abstractmethod
    def _build_enriched_result(self, row: PipelineRow, result: Mapping[str, Any], *, operation_url: str) -> TransformResult:
        """Enrich the row from a succeeded result."""

    @abstractmethod
    def _forward_invariant_probe_bodies(self) -> tuple[str, Mapping[str, Any]]:
        """``(operation_url, succeeded poll body)`` for the ADR-009 probe client."""

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _default_request_headers(self) -> dict[str, str]:
        """Auth headers for the audited client. Isolated so AAD bearer auth is a clean later add."""
        return {"Ocp-Apim-Subscription-Key": self._api_key}

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        self._recorder = ctx.landscape
        self._run_id = ctx.run_id
        self._telemetry_emit = ctx.telemetry_emit
        self._limiter = ctx.rate_limit_registry.get_limiter(self.name) if ctx.rate_limit_registry is not None else None

    def connect_output(self, output: OutputPort, max_pending: int = 30) -> None:
        if self._batch_initialized:
            raise RuntimeError("connect_output() already called")
        self.init_batch_processing(
            max_pending=max_pending,
            output=output,
            name=self.name,
            max_workers=max_pending,
            batch_wait_timeout=self._effective_batch_wait_timeout_seconds,
        )
        self._batch_initialized = True

    def accept(self, row: PipelineRow, ctx: TransformContext) -> None:
        if not self._batch_initialized:
            raise RuntimeError("connect_output() must be called before accept().")
        self.accept_row(row, ctx, self._process_row)

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        raise NotImplementedError(f"{self.__class__.__name__} uses row-level pipelining. Use accept() instead of process().")

    def _get_http_client(self, state_id: str, *, ctx: TransformContext, token_id: str | None = None) -> Any:
        """Get or create the audited HTTP client for a state_id (cached for call_index continuity)."""
        with self._http_clients_lock:
            if state_id not in self._http_clients:
                if self._recorder is None:
                    raise RuntimeError(f"{self.name}: recorder not initialized — call on_start() before processing")
                self._http_clients[state_id] = AuditedHTTPClient(
                    member_token=ctx.require_member_token(),
                    work_item=ctx.require_work_item(),
                    execution=self._recorder,
                    state_id=state_id,
                    run_id=self._run_id,
                    telemetry_emit=self._telemetry_emit,
                    timeout=self._request_timeout_seconds,
                    headers=self._default_request_headers(),
                    limiter=self._limiter,
                    token_id=token_id,
                    max_response_body_bytes=self._max_response_body_bytes,
                    call_mode_session=ctx.call_mode_session,
                )
            return self._http_clients[state_id]

    def close(self) -> None:
        self._shutdown.set()
        if self._batch_initialized:
            self.shutdown_batch_processing()
        with self._http_clients_lock:
            for client in self._http_clients.values():
                client.close()
            self._http_clients.clear()
        self._recorder = None

    # ── Per-row processing ────────────────────────────────────────────────

    def _process_row(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        state_id = ctx.state_id
        if state_id is None:
            raise RuntimeError("state_id is required for batch processing.")
        token_id = ctx.token.token_id if ctx.token is not None else None
        try:
            return self._process_single_with_state(row, state_id, token_id=token_id, ctx=ctx)
        finally:
            with self._http_clients_lock:
                client = self._http_clients.pop(state_id, None)
            if client is not None:
                client.close()

    def _process_single_with_state(
        self, row: PipelineRow, state_id: str, *, ctx: TransformContext, token_id: str | None = None
    ) -> TransformResult:
        """Submit the document, poll the LRO to completion, and enrich the row.

        ``capacity_deadline`` is a single per-row total-budget anchor shared by the
        submit POST and every poll GET (it is NOT a per-phase budget): the
        ``max_capacity_retry_seconds`` cap applies to total 429/503 wait across the
        whole row. Poll GETs further clamp it to ``min(capacity_deadline,
        poll_deadline)`` so capacity retries never outlive the outer poll budget.

        Every per-row failure below is raised as ``_RowFailed`` and returned here
        as the row's error result, so it never escapes past the executor.
        """
        try:
            request = self._request_for_row(row)
            started_at = time.monotonic()
            capacity_deadline = started_at + float(self._max_capacity_retry_seconds)
            operation_url, retry_after = self._submit(
                request, state_id, token_id=token_id, capacity_deadline=capacity_deadline, started_at=started_at, ctx=ctx
            )
            analyzed = self._poll(
                operation_url,
                state_id,
                ctx=ctx,
                token_id=token_id,
                retry_after=retry_after,
                capacity_deadline=capacity_deadline,
                started_at=started_at,
            )
        except _RowFailed as failed:
            return failed.result
        return self._build_enriched_result(row, analyzed, operation_url=operation_url)

    @staticmethod
    def _row_failure(reason: TransformErrorReason) -> _RowFailed:
        """The exception that ends the row with a non-retryable error result (caught in ``_process_single_with_state``)."""
        return _RowFailed(TransformResult.error(reason, retryable=False))

    def _request_for_row(self, row: PipelineRow) -> AnalyzeRequest:
        """Read and validate the document reference; build the submit body."""
        if self._source_field not in row:
            raise self._row_failure({"reason": "missing_field", "field": self._source_field})
        ref = row[self._source_field]
        # Tier 2: the row value is user data; a wrong type is rejected, never coerced.
        if type(ref) is not str or not ref.strip():
            raise self._row_failure({"reason": "non_string_field", "field": self._source_field, "actual_type": type(ref).__name__})

        if self._source_mode == "url":
            parsed = urlparse(ref)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise self._row_failure({"reason": "invalid_input", "field": self._source_field, "error_type": "invalid_document_url"})
            # A SAS ``sig`` (or any credential-shaped query value / userinfo) in the
            # document URL is a credential: Azure needs it verbatim, the audit
            # trail must not keep it raw.
            return self._url_request(ref, fingerprint_url(ref))

        if len(ref) > self._max_base64_chars:
            raise self._row_failure(
                {
                    "reason": "invalid_input",
                    "field": self._source_field,
                    "error_type": "base64_too_large",
                    "message": f"base64 source length {len(ref)} exceeds max {self._max_base64_chars}",
                }
            )
        if not self._is_valid_base64(ref):
            raise self._row_failure({"reason": "invalid_input", "field": self._source_field, "error_type": "invalid_base64"})
        return self._base64_request(ref)

    @staticmethod
    def _is_valid_base64(value: str) -> bool:
        """True iff ``value`` is padded standard-alphabet base64 (empty input is rejected earlier)."""
        return _BASE64_PATTERN.fullmatch(value) is not None

    def _api_error_result(self, *, error_type: str, response: httpx.Response) -> TransformResult:
        """A non-capacity HTTP failure, with Azure's bounded error codes when present.

        ``code`` is Azure's ``error.code`` (the provider error code, as the
        Textract transforms report it); ``cause`` is ``error.innererror.code``,
        the sub-cause within it.
        """
        reason: TransformErrorReason = {"reason": "api_error", "error_type": error_type, "status_code": response.status_code}
        text = response.text
        body, _parse_error = parse_json_strict(text) if type(text) is str else (None, "non_text_body")
        code, inner_code = azure_error_codes(body)
        if code is not None:
            reason["code"] = code
        if inner_code is not None:
            reason["cause"] = inner_code
        return TransformResult.error(reason, retryable=False)

    def _http_call_with_capacity_retry(self, do_call: Callable[[], Any], *, deadline: float, started_at: float, error_type: str) -> Any:
        """Run one HTTP call with capacity (429/503) retry and return the 2xx response.

        ``deadline`` is the total-budget anchor (``min(capacity_deadline, poll_deadline)``
        when called from ``_poll``). Azure's ``Retry-After`` on a capacity response
        lengthens the wait (never shortens our backoff) but never past ``deadline``.
        Non-capacity HTTP errors end the row in the ``api_error`` category with
        ``error_type`` as the sub-label. Raises PluginRetryableError on network
        errors (engine retries the row).
        """
        delay = self._CAPACITY_RETRY_INITIAL_DELAY_SECONDS
        while True:
            if self._shutdown.is_set():
                raise _RowFailed(self._shutdown_result(started_at))
            try:
                response = do_call()
                response.raise_for_status()
                return response
            except HTTPResponseBodyTooLargeError as e:
                # Distinct exception class (subclass of httpx.HTTPError, NOT HTTPStatusError /
                # RequestError). AuditedHTTPClient raises it when a response body exceeds
                # max_response_body_bytes. If it escaped here it would propagate past the
                # executor and cancel the WHOLE batch; convert to a per-row error instead.
                raise self._row_failure({"reason": "body_too_large", "body_size": e.body_size, "max_body_bytes": e.max_body_bytes}) from e
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                if not is_capacity_error(status_code):
                    raise _RowFailed(self._api_error_result(error_type=error_type, response=e.response)) from e
                now = time.monotonic()
                if now >= deadline:
                    raise _RowFailed(self._retry_timeout_result(status_code, started_at)) from e
                retry_after = self._parse_retry_after(e.response.headers["retry-after"] if "retry-after" in e.response.headers else None)
                wait = delay if retry_after is None else max(delay, retry_after)
                sleep_seconds = min(wait, deadline - now)
                if sleep_seconds > 0 and self._shutdown.wait(timeout=sleep_seconds):
                    raise _RowFailed(self._shutdown_result(started_at)) from e
                if time.monotonic() >= deadline:
                    raise _RowFailed(self._retry_timeout_result(status_code, started_at)) from e
                delay = min(delay * 2.0, self._CAPACITY_RETRY_MAX_DELAY_SECONDS)
            except httpx.RequestError as e:
                raise PluginRetryableError(f"{self.service_label} network error: {e}", retryable=True) from e

    def _submit(
        self,
        request: AnalyzeRequest,
        state_id: str,
        *,
        token_id: str | None,
        ctx: TransformContext,
        capacity_deadline: float,
        started_at: float,
    ) -> tuple[str, float | None]:
        client = self._get_http_client(state_id, token_id=token_id, ctx=ctx)
        url = self._analyze_url()
        wire_body = deep_thaw(request.body)
        audit_body = deep_thaw(request.audit_body)

        def do_call() -> Any:
            return client.post(url, json=wire_body, audit_json=audit_body, timeout=self._request_timeout_seconds)

        resp = self._http_call_with_capacity_retry(do_call, deadline=capacity_deadline, started_at=started_at, error_type="submit_rejected")
        if resp.status_code != 202:
            raise self._row_failure({"reason": "api_error", "error_type": "submit_rejected", "status_code": resp.status_code})
        operation_url = resp.headers["operation-location"] if "operation-location" in resp.headers else None
        if not operation_url:
            raise self._row_failure({"reason": "operation_location_missing"})
        if not operation_location_host_matches(operation_url, self._endpoint):
            raise self._row_failure({"reason": "operation_location_untrusted"})
        retry_after = self._parse_retry_after(resp.headers["retry-after"] if "retry-after" in resp.headers else None)
        return operation_url, retry_after

    def _poll(
        self,
        operation_url: str,
        state_id: str,
        *,
        token_id: str | None,
        ctx: TransformContext,
        retry_after: float | None,
        capacity_deadline: float,
        started_at: float,
    ) -> Mapping[str, Any]:
        client = self._get_http_client(state_id, token_id=token_id, ctx=ctx)
        poll_deadline = started_at + self._poll_timeout_seconds
        # Wait before the first GET. Honor Azure's Retry-After when present;
        # the configured poll cap bounds our backoff, not Azure's requested delay.
        first_delay = max(retry_after or 0.0, self._poll_interval_seconds)
        if first_delay > 0:
            now = time.monotonic()
            if now >= poll_deadline:
                raise _RowFailed(self._poll_timeout_result(started_at))
            if self._shutdown.wait(timeout=min(first_delay, poll_deadline - now)):
                raise _RowFailed(self._shutdown_result(started_at))

        # Floor subsequent intervals at poll_interval_seconds so a Tier-3
        # Retry-After: 0 cannot spin a hot poll loop; cap our backoff at poll_max.
        initial_interval = retry_after if retry_after is not None else self._poll_interval_seconds
        interval = min(max(initial_interval, self._poll_interval_seconds), self._poll_max_interval_seconds)

        while True:
            if self._shutdown.is_set():
                raise _RowFailed(self._shutdown_result(started_at))
            if time.monotonic() >= poll_deadline:
                raise _RowFailed(self._poll_timeout_result(started_at))

            def do_call() -> Any:
                return client.get(operation_url, timeout=self._request_timeout_seconds)

            resp = self._http_call_with_capacity_retry(
                do_call, deadline=min(capacity_deadline, poll_deadline), started_at=started_at, error_type="poll_request_failed"
            )
            if time.monotonic() >= poll_deadline:
                raise _RowFailed(self._poll_timeout_result(started_at))

            try:
                outcome, result = parse_poll_body(
                    self._parse_json_strict(resp),
                    statuses=self._POLL_STATUSES,
                    result_key=self._RESULT_KEY,
                    missing_result_error=self._MISSING_RESULT_ERROR,
                )
            except MalformedPollBodyError as e:
                raise self._row_failure({"reason": "malformed_response", "error_type": e.error_type}) from e
            if outcome.status is AnalyzeStatus.SUCCEEDED:
                if result is None:
                    raise FrameworkBugError(f"{self.name}: parse_poll_body returned a succeeded outcome without its result")
                return result
            if outcome.status in (AnalyzeStatus.FAILED, AnalyzeStatus.CANCELED):
                # Carry only the bounded Azure error.code token; never the raw error.message (Tier-3 egress).
                cause = outcome.cause if outcome.cause is not None else "unknown"
                raise self._row_failure({"reason": "analysis_failed", "error_type": outcome.status.value, "cause": cause})
            now = time.monotonic()
            if now >= poll_deadline:
                raise _RowFailed(self._poll_timeout_result(started_at))
            # A running poll's own Retry-After may ask for a longer wait than our
            # backoff; honour it (bounded by the poll deadline), never a shorter one.
            poll_retry_after = self._parse_retry_after(resp.headers["retry-after"] if "retry-after" in resp.headers else None)
            wait = interval if poll_retry_after is None else max(interval, poll_retry_after)
            sleep_seconds = min(wait, poll_deadline - now)
            if sleep_seconds > 0 and self._shutdown.wait(timeout=sleep_seconds):
                raise _RowFailed(self._shutdown_result(started_at))
            interval = min(interval * self._poll_backoff_multiplier, self._poll_max_interval_seconds)

    def _parse_json_strict(self, response: Any) -> Any:
        """The response body as strict JSON; a non-text, too-deep or invalid body ends the row."""
        text = response.text
        if type(text) is not str:
            raise self._row_failure({"reason": "malformed_response", "error_type": "non_text_body"})
        try:
            data, parse_error = parse_json_strict(text)
        except RecursionError as e:
            raise self._row_failure({"reason": "malformed_response", "error_type": "json_too_deep"}) from e
        if parse_error is not None:
            raise self._row_failure({"reason": "malformed_response", "error_type": "invalid_json"})
        return data

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        """Azure's Retry-After in seconds; None for an absent, non-numeric, negative or non-finite value."""
        if value is None:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        # ``nan >= 0`` is False; ``inf`` is rejected explicitly so it cannot pin a wait at the deadline.
        return seconds if 0 <= seconds < float("inf") else None

    def _shutdown_result(self, started_at: float) -> TransformResult:
        return TransformResult.error({"reason": "shutdown_requested", "elapsed_seconds": time.monotonic() - started_at}, retryable=False)

    def _poll_timeout_result(self, started_at: float) -> TransformResult:
        return TransformResult.error({"reason": "poll_timeout", "elapsed_seconds": time.monotonic() - started_at}, retryable=False)

    def _retry_timeout_result(self, status_code: int, started_at: float) -> TransformResult:
        return TransformResult.error(
            {
                "reason": "retry_timeout",
                "status_code": status_code,
                "elapsed_seconds": time.monotonic() - started_at,
                "max_seconds": float(self._max_capacity_retry_seconds),
            },
            retryable=False,
        )

    # ── ADR-009 forward invariant probe ──────────────────────────────────

    def forward_invariant_probe_rows(self, probe: Any) -> list[Any]:
        value = "https://probe.example/doc.pdf" if self._source_mode == "url" else "cHJvYmU="
        return [self._augment_invariant_probe_row(probe, field_name=self._source_field, value=value)]

    def execute_forward_invariant_probe(self, probe_rows: list[Any], ctx: Any) -> TransformResult:
        """Exercise the real LRO path with a local fake client.

        Inserts the fake directly into ``_http_clients[state_id]`` so
        ``_get_http_client`` returns it without creating a real client — correctness
        depends on ``_get_http_client`` checking the cache before construction.
        """
        if len(probe_rows) != 1:
            raise FrameworkBugError(
                f"{self.__class__.__name__}.execute_forward_invariant_probe() requires exactly 1 row, got {len(probe_rows)}."
            )
        op_url, succeeded_body = self._forward_invariant_probe_bodies()

        import json as _json

        class _ProbeResponse:
            def __init__(self, status_code: int, headers: dict[str, str], payload: Mapping[str, Any]) -> None:
                self.status_code = status_code
                self.headers = httpx.Headers(headers)
                self.text = _json.dumps(payload)
                self.content = self.text.encode()

            def raise_for_status(self) -> None:
                return None

        class _ProbeClient:
            def post(self, url: str, *, json: Any = None, audit_json: Any = None, timeout: float | None = None) -> Any:
                del url, json, audit_json, timeout
                return _ProbeResponse(202, {"operation-location": op_url}, {})

            def get(self, url: str, *, timeout: float | None = None) -> Any:
                del url, timeout
                return _ProbeResponse(200, {}, succeeded_body)

            def close(self) -> None:
                return None

        # op_url is built on the configured endpoint host, so the host-pin check passes.
        state_id = ctx.state_id or "invariant-probe-state"
        token_id = ctx.token.token_id if ctx.token is not None else None
        with self._http_clients_lock:
            self._http_clients[state_id] = _ProbeClient()
        try:
            return self._process_single_with_state(probe_rows[0], state_id, token_id=token_id, ctx=ctx)
        finally:
            with self._http_clients_lock:
                client = self._http_clients.pop(state_id, None)
            if client is not None:
                client.close()
