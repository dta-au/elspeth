"""ELSPETH LLM Gateway provider.

Defines :class:`GatewayConfig` (config + schema, Phase 2 Task 2) and
:class:`GatewayLLMProvider` (HTTP transport against the Phase 1 compatibility
gateway, Phase 2 Task 3).

Endpoint validation follows the design's contract:
- HTTPS is required, except the exact loopback form ``http://127.0.0.1:<port>/v1``.
  ``localhost`` and other loopback spellings (``::1``) are deliberately NOT
  treated as the accepted loopback form — only the literal ``127.0.0.1``
  address qualifies.
- Userinfo, query strings, and fragments are rejected outright (userinfo via
  the shared ``validate_credential_safe_https_url`` helper).
- The path must end with the versioned base ``/v1``, with no empty (doubled
  slash), ``.``, or ``..`` segments anywhere in the path — this rejects both
  a path that extends past the versioned base (e.g. ``/v1/extra``) and one
  that spells its way back to ``/v1`` through path tricks (e.g. ``//v1``,
  ``/v1/../v1``).
- The port, if present, must parse cleanly — a malformed port fails at
  config-validation time rather than at connect time.

``GatewayLLMProvider`` implements the current three-method ``LLMProvider``
protocol: it POSTs to ``{endpoint}/chat/completions`` with a static bearer
credential (resolved from ``GatewayConfig.credential_ref`` by
``LLMTransform._create_provider``, never held as a literal on the config
itself) and the ``X-ELSPETH-LLM-Gateway-Contract`` header, validates the
echoed contract header and the response envelope, and maps the gateway's
``error.code``-only error contract to ELSPETH's existing typed LLM error
hierarchy. ``runtime_preflight`` checks gateway readiness (contract major,
model alias, capabilities) THEN performs one bounded real completion — a
readiness document alone is never accepted as proof of health.
"""

from __future__ import annotations

import json
import math
import time
from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from elspeth.contracts import CallStatus, CallType
from elspeth.contracts.audit_protocols import PluginAuditWriter
from elspeth.contracts.call_data import LLMCallError, LLMCallRequest, LLMCallResponse
from elspeth.contracts.token_usage import TokenUsage
from elspeth.contracts.value_source import ValueSource
from elspeth.core.llm_profiles import SECRET_REF_PATTERN
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from elspeth.plugins.infrastructure.clients.llm import (
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from elspeth.plugins.infrastructure.url_validation import validate_credential_safe_https_url
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.provider import LLMQueryResult, ParsedFinishReason, parse_finish_reason
from elspeth.plugins.transforms.llm.validation import reject_nonfinite_constant

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.clients.base import TelemetryEmitCallback

__all__ = ["GatewayConfig", "GatewayLLMProvider"]

#: The gateway's inbound/outbound contract-major header. Required on every
#: request; the gateway echoes the same header on every response (success or
#: error) so ELSPETH can detect a contract drift on the wire, not just at
#: config time.
_GATEWAY_CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"

#: Audit-safe static message used for EVERY exception this provider raises.
#: Per the binding error-mapping contract, ELSPETH maps gateway failures on
#: ``error.code`` only and never surfaces ``error.message`` or any other
#: agency/gateway-controlled text through an exception message — the full
#: response body remains available only through the audited HTTP payload.
_STATIC_GATEWAY_ERROR = "Gateway LLM request failed"

_GATEWAY_VERSIONED_BASE = "/v1"
_GATEWAY_LOOPBACK_HOST = "127.0.0.1"

# Closed capability vocabulary the Phase 1 gateway contract can report/require.
_SUPPORTED_GATEWAY_CAPABILITIES = frozenset({"text", "tools", "json_object", "json_schema", "seed", "usage"})

# The only gateway contract major ELSPETH currently speaks.
_SUPPORTED_GATEWAY_CONTRACT_MAJORS = frozenset({1})


def _validate_gateway_endpoint(value: str) -> str:
    """Apply the credential-safe HTTPS rule plus the gateway's stricter shape.

    ``validate_credential_safe_https_url`` treats any loopback spelling
    (``localhost``, ``127.0.0.1``, ``::1``) as an acceptable HTTP loopback
    host. The gateway design only accepts the literal ``127.0.0.1`` form, so
    that broader allowance is narrowed here.

    Two additional checks close review gaps from Task 2:

    - the path shape check rejects empty (doubled-slash), ``.``, and ``..``
      segments *before* the ``endswith`` check, so ``https://host//v1`` and
      ``https://host/v1/../v1`` cannot spell their way past a suffix-only
      comparison. A legitimate reverse-proxy sub-path mount such as
      ``https://host/gateway/v1`` is unaffected — it has no such segments.
    - ``urlsplit`` parses ``.port`` lazily; a malformed port
      (out-of-range, non-numeric) raises ``ValueError`` only when the
      attribute is actually read. Reading it here turns that into a clean,
      immediate config-validation error instead of a deferred connect-time
      failure.
    """
    validated = validate_credential_safe_https_url(value, field_name="endpoint", allow_http_loopback=True)
    parsed = urlsplit(validated)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"endpoint must have a valid port: {exc}") from exc
    if parsed.scheme == "http" and parsed.hostname != _GATEWAY_LOOPBACK_HOST:
        raise ValueError(f"endpoint must use HTTPS unless targeting the literal {_GATEWAY_LOOPBACK_HOST} loopback host")
    if parsed.query:
        raise ValueError("endpoint must not contain a query string")
    if parsed.fragment:
        raise ValueError("endpoint must not contain a fragment")
    path_segments = parsed.path.split("/")
    # path_segments[0] is always "" for an absolute path (leading '/'); only
    # interior/trailing segments are checked for doubled slashes and dot segments.
    if any(segment in ("", ".", "..") for segment in path_segments[1:]):
        raise ValueError("endpoint path must not contain empty, '.', or '..' segments")
    if not parsed.path.endswith(_GATEWAY_VERSIONED_BASE):
        raise ValueError(f"endpoint must end with the versioned base path {_GATEWAY_VERSIONED_BASE!r}")
    return validated


class GatewayConfig(LLMConfig):
    """Configuration for the ELSPETH LLM Gateway provider.

    The gateway fronts every real upstream agency behind one stable HTTP
    contract (see ``docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-
    integration-design.md``). ``model`` here is a *logical* alias the
    gateway resolves server-side — it is not a raw upstream model id, so
    unlike OpenRouter there is no authoritative local catalog to validate
    against. The LLM plugin is registered with the value-source walker, so
    every provider variant must still declare its participation contract.
    """

    VALUE_SOURCES: ClassVar[tuple[ValueSource, ...]] = ()

    provider: Literal["gateway"] = Field(default="gateway", description="LLM provider")
    model: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Logical model alias resolved server-side by the gateway",
    )
    endpoint: str = Field(..., description="Gateway base URL; must end with the versioned base path '/v1'")
    credential_ref: str = Field(..., description="Operator secret reference naming the gateway bearer credential")
    contract_major: int = Field(
        default=1,
        description="Gateway contract major version this configuration expects",
    )
    required_capabilities: tuple[str, ...] = Field(
        default=(),
        description="Gateway capabilities this configuration requires; closed set",
    )
    timeout_seconds: float = Field(default=60.0, gt=0, le=300, description="Request timeout")
    max_tokens: int | None = Field(default=None, gt=0, le=131072, description="Maximum tokens in response")
    tracing: dict[str, Any] | None = Field(default=None, description="Tier 2 tracing configuration")

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return _validate_gateway_endpoint(value)

    @field_validator("credential_ref")
    @classmethod
    def _validate_credential_ref(cls, value: str) -> str:
        if SECRET_REF_PATTERN.fullmatch(value) is None:
            raise ValueError("credential_ref must match the operator secret reference pattern")
        return value

    @field_validator("contract_major")
    @classmethod
    def _validate_contract_major(cls, value: int) -> int:
        if value not in _SUPPORTED_GATEWAY_CONTRACT_MAJORS:
            raise ValueError(f"contract_major {value} is not supported; supported majors: {sorted(_SUPPORTED_GATEWAY_CONTRACT_MAJORS)}")
        return value

    @field_validator("required_capabilities")
    @classmethod
    def _validate_required_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for capability in value:
            if capability not in _SUPPORTED_GATEWAY_CAPABILITIES:
                raise ValueError(f"unknown gateway capability {capability!r}; supported: {sorted(_SUPPORTED_GATEWAY_CAPABILITIES)}")
            if capability in seen:
                raise ValueError(f"duplicate gateway capability {capability!r}")
            seen.add(capability)
        return value


# ---------------------------------------------------------------------------
# GatewayLLMProvider — HTTP transport against the Phase 1 compatibility gateway
# ---------------------------------------------------------------------------


def _validate_contract_header(response: httpx.Response, contract_major: int) -> None:
    """Reject a response whose echoed contract header doesn't match ours.

    The gateway echoes ``X-ELSPETH-LLM-Gateway-Contract`` on every response,
    success or error. A missing/mismatched header means ELSPETH and the
    gateway disagree about the wire contract — that is never retryable
    (retrying the same request against the same mismatched contract cannot
    succeed).
    """
    if response.headers.get(_GATEWAY_CONTRACT_HEADER) != str(contract_major):
        raise LLMClientError(_STATIC_GATEWAY_ERROR, retryable=False)


def _extract_gateway_error_code(response: httpx.Response) -> str | None:
    """Read only ``error.code`` from a gateway error envelope.

    Never reads ``error.message`` (agency-adjacent free text) or any other
    field. Any parse/shape failure returns ``None`` — callers fail closed
    (non-retryable) rather than raise a second, unrelated exception.
    """
    try:
        data = json.loads(response.content, parse_constant=reject_nonfinite_constant)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


#: Gateway error codes that ELSPETH treats as a non-retryable configuration
#: failure — the request itself, the contract, the target model, or a
#: requested capability is wrong, and retrying the identical request cannot
#: change that.
_GATEWAY_NON_RETRYABLE_CONFIG_CODES = frozenset(
    {
        "invalid_request",
        "contract_mismatch",
        "model_not_allowed",
        "capability_unsupported",
        "upstream_unauthorized",
        "upstream_response_invalid",
        "internal_error",
    }
)

#: Gateway error codes that map to ELSPETH's existing retryable transport/
#: server failure — the gateway or its upstream OAuth/agency call hit a
#: transient condition that may clear on retry.
_GATEWAY_SERVER_ERROR_CODES = frozenset({"upstream_unavailable", "oauth_token_unavailable"})


def _gateway_error_for_code(code: str | None) -> LLMClientError:
    """Map a gateway ``error.code`` to the matching typed ELSPETH exception.

    Binding mapping (see the Phase 2 plan and gateway error contract):
    unknown or missing codes fail closed as non-retryable — a code outside
    the closed vocabulary is itself a contract violation, not a reason to
    guess at retryability.
    """
    if code in _GATEWAY_NON_RETRYABLE_CONFIG_CODES:
        return LLMClientError(_STATIC_GATEWAY_ERROR, retryable=False)
    if code == "context_length_exceeded":
        return ContextLengthError(_STATIC_GATEWAY_ERROR)
    if code == "content_policy_rejected":
        return ContentPolicyError(_STATIC_GATEWAY_ERROR)
    if code == "upstream_rate_limited":
        return RateLimitError(_STATIC_GATEWAY_ERROR)
    if code == "upstream_timeout":
        return NetworkError(_STATIC_GATEWAY_ERROR)
    if code in _GATEWAY_SERVER_ERROR_CODES:
        return ServerError(_STATIC_GATEWAY_ERROR)
    # Unknown / missing code: fail closed, non-retryable.
    return LLMClientError(_STATIC_GATEWAY_ERROR, retryable=False)


def _classify_gateway_http_error(response: httpx.Response) -> LLMClientError:
    return _gateway_error_for_code(_extract_gateway_error_code(response))


def _validate_gateway_success_response(
    response: httpx.Response,
    *,
    usage_required: bool,
) -> tuple[dict[str, Any], str, TokenUsage, ParsedFinishReason, str]:
    """Parse and validate a gateway chat-completion success body.

    Mirrors OpenRouter's Tier 3 validation shape (structural diagnostics
    such as type/key names are safe to surface; response *content* is
    never echoed into an exception message).
    """
    try:
        data = json.loads(response.content, parse_constant=reject_nonfinite_constant)
    except (ValueError, TypeError) as e:
        raise LLMClientError(f"Gateway response is not valid JSON: {type(e).__name__}", retryable=False) from e

    if not isinstance(data, dict):
        raise LLMClientError(f"Gateway response is not a JSON object: {type(data).__name__}", retryable=False)

    choices = data.get("choices")
    if not choices:
        raise LLMClientError(f"Gateway response is missing 'choices': {list(data.keys())}", retryable=False)

    try:
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMClientError(f"Gateway response has a malformed choice structure: {type(e).__name__}", retryable=False) from e

    raw_finish_reason = choices[0].get("finish_reason") if isinstance(choices[0], dict) else None

    if content is None:
        raise ContentPolicyError("Gateway returned null content (likely content-filtered upstream)")
    if not isinstance(content, str):
        raise LLMClientError(f"Gateway response content is {type(content).__name__}, expected str", retryable=False)
    if not content.strip():
        if raw_finish_reason == "tool_calls":
            raise LLMClientError("Gateway returned a tool_calls response (not supported by ELSPETH)", retryable=False)
        raise ContentPolicyError(f"Gateway returned empty content (finish_reason={raw_finish_reason})")

    raw_usage = data.get("usage")
    if usage_required and raw_usage is None:
        raise LLMClientError(
            "Gateway response omitted usage data required by this profile's required_capabilities",
            retryable=False,
        )
    if isinstance(raw_usage, dict):
        for usage_key, usage_val in raw_usage.items():
            if isinstance(usage_val, float) and not math.isfinite(usage_val):
                raise LLMClientError(f"Non-finite value in gateway usage.{usage_key}", retryable=False)
    usage = TokenUsage.from_dict(raw_usage)

    finish_reason = parse_finish_reason(str(raw_finish_reason)) if raw_finish_reason is not None else None

    raw_model = data["model"] if "model" in data else None
    if not isinstance(raw_model, str) or not raw_model.strip():
        missing_desc = "missing" if raw_model is None else f"{type(raw_model).__name__}/empty"
        raise LLMClientError(
            f"Gateway response 'model' is {missing_desc}, expected non-empty str",
            retryable=False,
        )
    response_model = raw_model

    return data, content, usage, finish_reason, response_model


class GatewayLLMProvider:
    """ELSPETH LLM Gateway provider — HTTP transport with Tier 3 validation.

    Speaks the Phase 1 compatibility gateway's contract major 1: POSTs to
    ``{endpoint}/chat/completions`` with a static bearer credential and the
    ``X-ELSPETH-LLM-Gateway-Contract`` header, validates the echoed contract
    header and the OpenAI-shaped response envelope, and maps the gateway's
    stable error envelope (``error.code`` ONLY — never ``error.message`` or
    any other agency-adjacent text) to ELSPETH's existing typed LLM error
    hierarchy.

    Like OpenRouterLLMProvider, the underlying transport is HTTP, so
    ``AuditedHTTPClient`` records the raw transport row automatically; the
    semantic ``CallType.LLM`` row is recorded here so
    ``calls.resolved_prompt_template_hash`` remains attached only to
    ``CallType.LLM`` rows.

    ELSPETH owns all retry/pooling/row-level policy — this provider issues
    exactly one HTTP call per ``execute_query()`` and contains no retry loop.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        contract_major: int,
        required_capabilities: tuple[str, ...] = (),
        timeout_seconds: float = 60.0,
        recorder: PluginAuditWriter,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        limiter: Any = None,
        resolved_prompt_template_hash: str | None = None,
    ) -> None:
        # Re-validate defensively (mirrors OpenRouterLLMProvider): GatewayConfig
        # already enforces this shape at config-construction time, but this
        # provider can also be constructed directly (tests, future callers).
        self._base_url = _validate_gateway_endpoint(endpoint)
        self._contract_major = contract_major
        self._required_capabilities = required_capabilities
        self._usage_required = "usage" in required_capabilities
        # Pre-built auth + contract headers — avoids storing the raw bearer
        # token as a separately named attribute.
        self._request_headers = {
            "Authorization": f"Bearer {api_key}",
            _GATEWAY_CONTRACT_HEADER: str(contract_major),
        }
        self._timeout = timeout_seconds
        self._recorder = recorder
        self._run_id = run_id
        self._telemetry_emit = telemetry_emit
        self._limiter = limiter
        self._resolved_prompt_template_hash = resolved_prompt_template_hash

        # Client cache with reference counting for parallel multi-query safety
        # — same pattern as OpenRouterLLMProvider.
        self._http_clients: dict[str, AuditedHTTPClient] = {}
        self._http_client_refs: dict[str, int] = {}
        self._http_clients_lock = Lock()

    def execute_query(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        state_id: str,
        token_id: str,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult:
        """Execute one gateway chat-completion request.

        Raises:
            RateLimitError: gateway ``upstream_rate_limited`` (retryable)
            ServerError: gateway ``upstream_unavailable``/``oauth_token_unavailable`` (retryable)
            NetworkError: transport failure or gateway ``upstream_timeout`` (retryable)
            ContentPolicyError: gateway ``content_policy_rejected`` or blank content (not retryable)
            ContextLengthError: gateway ``context_length_exceeded`` (not retryable)
            LLMClientError: every other failure (not retryable)
        """
        snapshot_state_id = state_id
        llm_request_payload = self._build_llm_request_payload(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        logical_start = time.perf_counter()

        http_client = self._get_http_client(snapshot_state_id, token_id=token_id)
        try:
            request_body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if max_tokens is not None:
                request_body["max_tokens"] = max_tokens
            if response_format is not None:
                request_body["response_format"] = response_format

            response = self._post_chat_completion(http_client, request_body)

            data, content, usage, finish_reason, response_model = _validate_gateway_success_response(
                response, usage_required=self._usage_required
            )

            result = LLMQueryResult(
                content=content,
                usage=usage,
                model=response_model,
                finish_reason=finish_reason,
            )
            self._record_logical_llm_success(
                state_id=snapshot_state_id,
                started_at=logical_start,
                request_payload=llm_request_payload,
                content=content,
                model=response_model,
                usage=usage,
                raw_response=data,
            )
            return result
        except LLMClientError as exc:
            self._record_logical_llm_error(
                state_id=snapshot_state_id,
                started_at=logical_start,
                request_payload=llm_request_payload,
                exc=exc,
            )
            raise
        finally:
            self._release_http_client(snapshot_state_id)

    def _post_chat_completion(self, http_client: AuditedHTTPClient, request_body: dict[str, Any]) -> httpx.Response:
        """POST one request, mapping transport and gateway-envelope failures.

        ``httpx.TimeoutException`` is a subclass of ``httpx.RequestError`` —
        the timeout-specific except clause is listed first (mirroring the
        OpenRouter provider's transport handling) so a future refinement
        that gives timeouts distinct handling cannot be silently shadowed by
        the broader ``RequestError`` clause below it.
        """
        try:
            response = http_client.post(
                "/chat/completions",
                json=request_body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as e:
            raise NetworkError(_STATIC_GATEWAY_ERROR) from e
        except httpx.RequestError as e:
            raise NetworkError(_STATIC_GATEWAY_ERROR) from e

        # Contract-header verification applies to every response — success
        # or error — before any status-code or body classification.
        _validate_contract_header(response, self._contract_major)

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Maps on error.code ONLY; never touches error.message or the
            # HTTP status code itself (a buggy/malicious gateway could send
            # a misleading status alongside a correct code, or vice versa).
            raise _classify_gateway_http_error(e.response) from e

        return response

    def _build_llm_request_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
    ) -> LLMCallRequest:
        extra_kwargs: dict[str, Any] = {}
        if response_format is not None:
            extra_kwargs["response_format"] = response_format
        return LLMCallRequest(
            model=model,
            messages=messages,
            temperature=temperature,
            provider="gateway",
            max_tokens=max_tokens,
            extra_kwargs=extra_kwargs,
        )

    def _record_logical_llm_success(
        self,
        *,
        state_id: str,
        started_at: float,
        request_payload: LLMCallRequest,
        content: str,
        model: str,
        usage: TokenUsage,
        raw_response: dict[str, Any],
    ) -> None:
        """Record the semantic LLM call that the HTTP transport fulfilled."""
        call_index = self._recorder.allocate_call_index(state_id)
        self._recorder.record_call(
            state_id=state_id,
            call_index=call_index,
            call_type=CallType.LLM,
            status=CallStatus.SUCCESS,
            request_data=request_payload,
            response_data=LLMCallResponse(
                content=content,
                model=model,
                usage=usage,
                raw_response=raw_response,
            ),
            latency_ms=(time.perf_counter() - started_at) * 1000,
            resolved_prompt_template_hash=self._resolved_prompt_template_hash,
        )

    def _record_logical_llm_error(
        self,
        *,
        state_id: str,
        started_at: float,
        request_payload: LLMCallRequest,
        exc: LLMClientError,
    ) -> None:
        call_index = self._recorder.allocate_call_index(state_id)
        message = str(exc) or type(exc).__name__
        self._recorder.record_call(
            state_id=state_id,
            call_index=call_index,
            call_type=CallType.LLM,
            status=CallStatus.ERROR,
            request_data=request_payload,
            error=LLMCallError(
                type=type(exc).__name__,
                message=message,
                retryable=bool(getattr(exc, "retryable", False)),
            ),
            latency_ms=(time.perf_counter() - started_at) * 1000,
            resolved_prompt_template_hash=self._resolved_prompt_template_hash,
        )

    def runtime_preflight(self, *, operation_id: str, model: str) -> None:
        """Validate gateway readiness, THEN run one bounded real completion.

        A readyz document alone is never accepted as proof of health (an
        explicit design requirement) — readiness only gates whether the
        second half (an actual authenticated completion) runs at all.
        """
        self._check_readyz(operation_id=operation_id, model=model)
        self._smoke_test_completion(operation_id=operation_id, model=model)

    def _readyz_base_url(self) -> str:
        """The gateway root (``/readyz`` lives one level above ``/v1``)."""
        return self._base_url.removesuffix(_GATEWAY_VERSIONED_BASE)

    def _check_readyz(self, *, operation_id: str, model: str) -> None:
        http_client = AuditedHTTPClient(
            execution=self._recorder,
            state_id=None,
            operation_id=operation_id,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            timeout=self._timeout,
            base_url=self._readyz_base_url(),
            headers=self._request_headers,
            limiter=self._limiter,
        )
        try:
            try:
                response = http_client.get("/readyz")
            except httpx.TimeoutException as e:
                raise NetworkError(_STATIC_GATEWAY_ERROR) from e
            except httpx.RequestError as e:
                raise NetworkError(_STATIC_GATEWAY_ERROR) from e

            try:
                payload = json.loads(response.content, parse_constant=reject_nonfinite_constant)
            except (ValueError, TypeError) as e:
                raise LLMClientError("Gateway readiness response is not valid JSON", retryable=False) from e
            if not isinstance(payload, dict):
                raise LLMClientError("Gateway readiness response is not a JSON object", retryable=False)

            # A readiness document alone is not health — but an unready
            # document, or one that disagrees with our configuration, is
            # still a config-level failure worth surfacing distinctly from
            # "the actual completion failed" (checked next, in the caller).
            if response.status_code != 200 or payload.get("ready") is not True:
                raise LLMClientError("Gateway reports not ready", retryable=False)

            if payload.get("contract_major") != self._contract_major:
                raise LLMClientError("Gateway readiness contract_major does not match configuration", retryable=False)

            model_aliases = payload.get("model_aliases")
            if not isinstance(model_aliases, list) or model not in model_aliases:
                raise LLMClientError("Gateway readiness does not report the configured model alias", retryable=False)

            declared_capabilities = payload.get("capabilities")
            declared = set(declared_capabilities) if isinstance(declared_capabilities, list) else set()
            missing = set(self._required_capabilities) - declared
            if missing:
                raise LLMClientError(
                    f"Gateway readiness does not report {len(missing)} required capabilit(y/ies)",
                    retryable=False,
                )
        finally:
            http_client.close()

    def _smoke_test_completion(self, *, operation_id: str, model: str) -> None:
        """Run a minimal audited gateway completion under an operation parent."""
        http_client = AuditedHTTPClient(
            execution=self._recorder,
            state_id=None,
            operation_id=operation_id,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            timeout=self._timeout,
            base_url=self._base_url,
            headers=self._request_headers,
            limiter=self._limiter,
        )
        try:
            request_body: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": "This is a pre-flight smoke test. Please reply with ok."}],
                "temperature": 0.0,
                "max_tokens": 32,
            }
            response = self._post_chat_completion(http_client, request_body)
            _validate_gateway_success_response(response, usage_required=self._usage_required)
        finally:
            http_client.close()

    def _get_http_client(self, state_id: str, *, token_id: str | None = None) -> AuditedHTTPClient:
        """Get or create AuditedHTTPClient for a state_id (thread-safe).

        Increments reference count so parallel queries sharing a state_id
        keep the client alive until the last query releases it.
        """
        with self._http_clients_lock:
            if state_id not in self._http_clients:
                self._http_clients[state_id] = AuditedHTTPClient(
                    execution=self._recorder,
                    state_id=state_id,
                    run_id=self._run_id,
                    telemetry_emit=self._telemetry_emit,
                    timeout=self._timeout,
                    base_url=self._base_url,
                    headers=self._request_headers,
                    limiter=self._limiter,
                    token_id=token_id,
                )
                self._http_client_refs[state_id] = 0
            self._http_client_refs[state_id] += 1
            return self._http_clients[state_id]

    def _release_http_client(self, state_id: str) -> None:
        """Decrement reference count and close client when last user releases it."""
        client_to_close: AuditedHTTPClient | None = None
        with self._http_clients_lock:
            if state_id not in self._http_client_refs:
                raise RuntimeError(
                    f"_release_http_client called for unknown state_id={state_id!r}. "
                    f"This is a refcount underflow — _get_http_client() was never called "
                    f"for this state_id, or it was already fully released."
                )
            count = self._http_client_refs[state_id] - 1
            self._http_client_refs[state_id] = count
            if count <= 0:
                client_to_close = self._http_clients.pop(state_id, None)
                self._http_client_refs.pop(state_id, None)
        if client_to_close is not None:
            client_to_close.close()

    def close(self) -> None:
        """Release all cached clients."""
        with self._http_clients_lock:
            for client in self._http_clients.values():
                client.close()
            self._http_clients.clear()
            self._http_client_refs.clear()
