"""``CompletionService``: the gateway's single request-to-response orchestration.

This is the only place the seven-step pipeline described in the phase-1
design lives: capability check, model-alias lookup, bounds check and
canonicalisation, adapter invocation, upstream call, response parsing and
validation, and response rendering. Every exit from this pipeline is either
a rendered OpenAI-shaped response dict or a ``GatewayError`` — never a raw
adapter/upstream exception and never free text derived from the request or
an upstream body (see ``core.errors`` and ``core.events``).

Adapter code is third-party and untrusted: both ``build_invoke`` and
``parse_success``/``classify_error`` are called inside a narrow
``try/except Exception`` that maps any adapter-raised exception to a fixed
``GatewayError`` code, so an adapter bug can never leak its own exception
message (which might echo request or upstream content) to a caller.
"""

import json
import logging
import time

from elspeth_llm_gateway import CONTRACT_MAJOR
from elspeth_llm_gateway.core.config import GatewayConfig
from elspeth_llm_gateway.core.contract import (
    ChatMessage,
    ChatRequest,
    ChatTool,
    ResponseFormat,
    bounds_check,
    build_completion_response,
)
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode
from elspeth_llm_gateway.core.events import canonical_hash, log_event
from elspeth_llm_gateway.core.transport import UpstreamClient
from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor, AdapterProtocol, UpstreamFailure
from elspeth_llm_gateway.sdk.types import (
    CanonicalMessage,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalToolCall,
    CanonicalToolDef,
    Capability,
    FinishReason,
    ResponseFormatSpec,
)


def _canonicalize_message(message: ChatMessage) -> CanonicalMessage:
    tool_calls = tuple(
        CanonicalToolCall(call_id=call.id, name=call.function.name, arguments_json=call.function.arguments)
        for call in (message.tool_calls or [])
    )
    return CanonicalMessage(
        role=message.role,
        content=message.content,
        tool_calls=tool_calls,
        tool_call_id=message.tool_call_id,
    )


def _canonicalize_tool(tool: ChatTool) -> CanonicalToolDef:
    return CanonicalToolDef(
        name=tool.function.name,
        description=tool.function.description,
        parameters_schema=tool.function.parameters or {},
    )


def _canonicalize_response_format(response_format: ResponseFormat | None) -> ResponseFormatSpec | None:
    if response_format is None:
        return None
    if response_format.type == "json_object":
        return ResponseFormatSpec(kind="json_object")
    schema = response_format.json_schema
    return ResponseFormatSpec(kind="json_schema", schema_name=schema.name, json_schema=schema.schema_definition, strict=schema.strict)


def _canonicalize_tool_choice(request: ChatRequest) -> tuple[str | None, str | None]:
    tool_choice = request.tool_choice
    if tool_choice is None:
        return None, None
    if isinstance(tool_choice, str):
        return tool_choice, None
    return "named", tool_choice.function.name


def to_canonical_request(request: ChatRequest, model_target: dict) -> CanonicalRequest:
    """Map an inbound, already-validated ``ChatRequest`` to a ``CanonicalRequest``.

    Field-for-field, per the phase-1 canonicalisation contract: tool calls
    map ``id`` -> ``call_id`` and ``function.arguments`` -> ``arguments_json``;
    string ``tool_choice`` values pass through verbatim; a named
    ``tool_choice`` becomes ``tool_choice="named"`` plus
    ``tool_choice_function``; ``response_format`` maps ``json_object`` /
    ``json_schema`` to the matching ``ResponseFormatSpec`` kind.
    """
    tool_choice, tool_choice_function = _canonicalize_tool_choice(request)
    return CanonicalRequest(
        model_target=model_target,
        model_alias=request.model,
        messages=tuple(_canonicalize_message(message) for message in request.messages),
        temperature=request.temperature,
        seed=request.seed,
        max_tokens=request.max_tokens,
        tools=tuple(_canonicalize_tool(tool) for tool in (request.tools or [])),
        tool_choice=tool_choice,
        tool_choice_function=tool_choice_function,
        response_format=_canonicalize_response_format(request.response_format),
    )


def _check_capabilities(request: ChatRequest, capabilities: frozenset[Capability]) -> None:
    """Step 1: reject a request that needs a capability the adapter doesn't declare.

    Checked before anything else in the pipeline touches configuration,
    bounds, or the network — a capability mismatch never causes an upstream
    call (or even a model-alias lookup).
    """
    if request.tools and Capability.TOOLS not in capabilities:
        raise GatewayError(GatewayErrorCode.CAPABILITY_UNSUPPORTED)

    response_format = request.response_format
    if response_format is not None:
        if response_format.type == "json_object" and Capability.JSON_OBJECT not in capabilities:
            raise GatewayError(GatewayErrorCode.CAPABILITY_UNSUPPORTED)
        if response_format.type == "json_schema" and Capability.JSON_SCHEMA not in capabilities:
            raise GatewayError(GatewayErrorCode.CAPABILITY_UNSUPPORTED)

    if request.seed is not None and Capability.SEED not in capabilities:
        raise GatewayError(GatewayErrorCode.CAPABILITY_UNSUPPORTED)


def _validate_canonical_response(response: CanonicalResponse, request: ChatRequest) -> None:
    """Step 5's post-``parse_success`` validation.

    ``finish_reason`` is already pydantic-typed as ``FinishReason`` on
    ``CanonicalResponse``, so this ``isinstance`` check is defense-in-depth
    against an adapter that satisfies ``AdapterProtocol`` structurally
    (it is a ``runtime_checkable`` ``Protocol``, not an enforced base class)
    without actually going through normal construction. Tool-call responses
    are rejected unless the request itself declared tools.
    """
    if not isinstance(response.finish_reason, FinishReason):
        raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID)
    if response.tool_calls and not request.tools:
        raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID)


class CompletionService:
    """Orchestrates one chat-completion request end to end."""

    def __init__(self, config: GatewayConfig, adapter: AdapterProtocol, upstream: UpstreamClient, logger: logging.Logger) -> None:
        self._config = config
        self._adapter = adapter
        self._upstream = upstream
        self._logger = logger

    async def complete(self, request: ChatRequest, request_id: str) -> dict:
        start = time.monotonic()
        descriptor = self._adapter.descriptor()
        request_hash = canonical_hash(request.model_dump(mode="json"))

        try:
            canonical_response = await self._run_pipeline(request, descriptor.capabilities)
        except GatewayError as exc:
            self._log_completion(descriptor, request_id, request.model, request_hash, start, status="error", error_code=exc.code.value)
            raise

        response = build_completion_response(
            response_id="gwcmpl-" + request_id,
            created=int(time.time()),
            model_alias=request.model,
            canonical=canonical_response,
        )
        self._log_completion(
            descriptor,
            request_id,
            request.model,
            request_hash,
            start,
            status="success",
            # Hash the *canonical* response, not the rendered envelope: the
            # envelope embeds response_id ("gwcmpl-" + request_id) and
            # created (int(time.time())), both unique per call, so hashing
            # it would make two otherwise-identical completions never hash
            # equal -- defeating canonical_hash's own equality-comparison
            # purpose. request_hash above is stable for the same reason.
            response_hash=canonical_hash(canonical_response.model_dump(mode="json")),
            response_bytes=len(json.dumps(response).encode("utf-8")),
        )
        return response

    async def _run_pipeline(self, request: ChatRequest, capabilities: frozenset[Capability]) -> CanonicalResponse:
        # Step 1: capability check, before any configuration lookup or network call.
        _check_capabilities(request, capabilities)

        # Step 2: model-alias lookup.
        if request.model not in self._config.model_mappings:
            raise GatewayError(GatewayErrorCode.MODEL_NOT_ALLOWED)

        # Step 3: bounds check, then canonicalisation.
        bounds_check(request, self._config.bounds)
        canonical_request = to_canonical_request(request, self._config.model_mappings[request.model])

        # Step 4: adapter build_invoke, then the upstream call.
        try:
            plan = self._adapter.build_invoke(canonical_request)
        except Exception:
            raise GatewayError(GatewayErrorCode.INTERNAL_ERROR) from None

        result = await self._upstream.invoke(plan)

        if 200 <= result.status < 300:
            # Step 5: parse_success, then response validation.
            try:
                canonical_response = self._adapter.parse_success(result.body if result.body is not None else {})
            except Exception:
                raise GatewayError(GatewayErrorCode.UPSTREAM_RESPONSE_INVALID) from None
            _validate_canonical_response(canonical_response, request)
            return canonical_response

        # Step 6: classify_error.
        failure = UpstreamFailure(status=result.status, body=result.body)
        try:
            classification = self._adapter.classify_error(failure)
        except Exception:
            raise GatewayError(GatewayErrorCode.INTERNAL_ERROR) from None
        raise GatewayError(GatewayErrorCode(classification.code))

    def _log_completion(
        self,
        descriptor: AdapterDescriptor,
        request_id: str,
        model_alias: str,
        request_hash: str,
        start: float,
        *,
        status: str,
        **extra,
    ) -> None:
        latency_ms = int((time.monotonic() - start) * 1000)
        log_event(
            self._logger,
            "completion",
            request_id=request_id,
            request_hash=request_hash,
            contract_major=CONTRACT_MAJOR,
            adapter_name=descriptor.name,
            adapter_version=descriptor.version,
            adapter_api_major=descriptor.adapter_api_major,
            model_alias=model_alias,
            mapping_generation=self._config.mapping_generation,
            status=status,
            latency_ms=latency_ms,
            **extra,
        )
