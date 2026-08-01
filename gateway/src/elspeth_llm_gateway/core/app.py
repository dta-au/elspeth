"""The gateway's FastAPI app factory: middleware, routes, and readiness.

``create_app`` wires together everything the rest of ``core`` builds — auth,
config, the adapter, the upstream transport, and ``CompletionService`` — into
one ASGI app. Three concerns are enforced as middleware, in a deliberately
chosen order (see the module-level comment above ``create_app`` for why):
request-ID assignment/echo, the inbound contract-version header, and bearer
authentication. Every response, success or error, carries the contract
header and an ``X-Request-ID``; every error response is the same
``GatewayError`` envelope regardless of which layer produced it.
"""

import copy
import hashlib
import importlib.metadata
import inspect
import logging
import os
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette._utils import get_route_path
from starlette.middleware.base import BaseHTTPMiddleware

from elspeth_llm_gateway import ADAPTER_API_MAJOR, CONTRACT_MAJOR
from elspeth_llm_gateway.core.auth import check_bearer
from elspeth_llm_gateway.core.config import ConfigError, GatewayConfig, load_config
from elspeth_llm_gateway.core.contract import ChatRequest
from elspeth_llm_gateway.core.errors import GatewayError, GatewayErrorCode, error_envelope
from elspeth_llm_gateway.core.events import log_event
from elspeth_llm_gateway.core.oauth import TokenManager
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json
from elspeth_llm_gateway.core.service import CompletionService
from elspeth_llm_gateway.core.transport import UpstreamClient
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter
from elspeth_llm_gateway.sdk.protocol import AdapterProtocol

CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"
REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ADAPTER_ENTRY_POINT_GROUP = "elspeth_llm_gateway.adapters"
_REFERENCE_ADAPTER_NAME = "reference_v1_invoke"
_MAX_MODEL_TARGET_ERRORS = 10

logger = logging.getLogger("elspeth_llm_gateway")


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _gated_path(request: Request) -> str:
    """The path the router actually matches routes against.

    ``request.url.path`` is ``scope["path"]``, which still includes
    ``root_path`` when the app is mounted behind a reverse proxy that sets
    it (e.g. ASGI ``root_path="/gw"``). The router itself matches on
    ``get_route_path(scope)``, which strips that prefix. Gating the
    contract/auth middleware on ``request.url.path`` instead of this would
    mean neither middleware recognises "/gw/v1/..." as under "/v1/" and
    both skip their check, while the router still resolves and serves the
    route underneath -- a full auth+contract bypass under any non-empty
    ``root_path``. Both middlewares must gate on the same path the router
    uses.
    """
    return get_route_path(request.scope)


def _request_id_from_state(request: Request) -> str:
    """Best-effort request id for a handler that runs outside the normal chain.

    Every request that reaches any handler here has already passed through
    ``RequestIDMiddleware`` (it is the outermost layer), so ``request.state``
    always carries one in practice; the fallback exists only so an exception
    handler can never itself raise while trying to render an error envelope.
    """
    return getattr(request.state, "request_id", None) or _new_request_id()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Outermost layer: assigns/echoes the request id and stamps both headers.

    Being outermost, this wraps the *entire* rest of the pipeline — contract
    check, auth, routing, and exception handling — so it stamps
    ``X-Request-ID`` and the contract header onto every response this app
    ever produces, including a 401 from the auth layer or a 400 from the
    contract-header layer nested inside it.

    A non-``GatewayError`` exception raised anywhere below is also caught
    *here*, not via ``@app.exception_handler(Exception)``: Starlette hoists
    an ``Exception``/500 handler out of ``ExceptionMiddleware`` and installs
    it on ``ServerErrorMiddleware`` instead, which sits *outside* every
    ``add_middleware``-registered layer (including this one) and re-raises
    after responding -- so a handler registered that way would produce a
    500 response with neither this app's headers nor any chance to prevent
    the raw exception from propagating to the ASGI server. Catching it here
    keeps the internal_error envelope inside the layer that stamps headers,
    and stops the exception from propagating further.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        raw_id = request.headers.get(REQUEST_ID_HEADER)
        request_id = raw_id if raw_id is not None and _REQUEST_ID_RE.fullmatch(raw_id) else _new_request_id()
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except Exception as exc:
            # Logged with the traceback for diagnosis, but with no
            # request-derived text ever assembled into the log message
            # itself -- only the fixed message and the exception's own
            # traceback are passed to the logger.
            logger.error("unhandled exception in request pipeline", exc_info=exc)
            error = GatewayError(GatewayErrorCode.INTERNAL_ERROR)
            response = JSONResponse(status_code=error.status, content=error_envelope(error, request_id))

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CONTRACT_HEADER] = str(CONTRACT_MAJOR)
        return response


class ContractHeaderMiddleware(BaseHTTPMiddleware):
    """Middle layer: on ``/v1/*``, a *present* contract header must match ``"1"``.

    Phase 1 made this header mandatory -- absent was rejected the same as
    mismatched. That is a deliberate, operator-approved relaxation as of
    Phase 3: the gateway is an *affordance* over a trusted, operator-chosen
    upstream, not a required protocol, so any plain OpenAI-compatible client
    (the OpenAI SDK, LiteLLM, ELSPETH's own OpenRouter-style provider) must be
    able to speak to it with only a bearer token and no gateway-specific
    header. Three-way behaviour, do not "fix" this back to two-way:
    absent -> proceed; present and equal to ``CONTRACT_MAJOR`` -> proceed;
    present and anything else -> reject ``contract_mismatch`` (400), exactly
    as before. Auth (``AuthMiddleware``, nested inside this one) is enforced
    independently and is unaffected by this relaxation.
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        received = request.headers.get(CONTRACT_HEADER)
        if _gated_path(request).startswith("/v1/") and received is not None and received != str(CONTRACT_MAJOR):
            error = GatewayError(GatewayErrorCode.CONTRACT_MISMATCH)
            return JSONResponse(status_code=error.status, content=error_envelope(error, _request_id_from_state(request)))
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Innermost layer: on ``/v1/*``, a valid bearer token is required."""

    def __init__(self, app, config: GatewayConfig) -> None:
        super().__init__(app)
        self._expected = config.inbound_bearer.get_secret_value()

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        if _gated_path(request).startswith("/v1/") and not check_bearer(request.headers.get("authorization"), self._expected):
            error = GatewayError(GatewayErrorCode.INBOUND_AUTHENTICATION_FAILED)
            return JSONResponse(status_code=error.status, content=error_envelope(error, _request_id_from_state(request)))
        return await call_next(request)


def _resolve_adapter(config: GatewayConfig) -> AdapterProtocol:
    """Resolve the configured adapter by name: the reference adapter, or an entry point.

    ``"reference_v1_invoke"`` always resolves to the in-tree
    ``ReferenceV1InvokeAdapter`` without consulting entry points at all;
    anything else is looked up in the ``elspeth_llm_gateway.adapters`` entry
    point group. A name that matches neither raises ``ConfigError`` — an
    unresolvable adapter is a startup failure, not a deferred runtime one.
    """
    if config.adapter_name == _REFERENCE_ADAPTER_NAME:
        return ReferenceV1InvokeAdapter()

    entry_points = importlib.metadata.entry_points(group=_ADAPTER_ENTRY_POINT_GROUP)
    matches = [entry_point for entry_point in entry_points if entry_point.name == config.adapter_name]
    if not matches:
        raise ConfigError([f"unknown_adapter:{config.adapter_name}"])

    adapter_cls = matches[0].load()
    return adapter_cls()


def _compute_adapter_fingerprint(adapter: AdapterProtocol) -> str:
    """``sha256`` of the adapter module's own source file, first 16 hex chars.

    Computed once at startup (not per-request): this is a static identity
    check for "which adapter code is actually running", not something that
    can change mid-process.
    """
    source_file = inspect.getsourcefile(type(adapter))
    if source_file is None:
        return "0" * 16
    with open(source_file, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()[:16]


def _model_target_validator(adapter: AdapterProtocol) -> Callable[[dict], None] | None:
    """The adapter's ``validate_model_target``, or ``None`` if it has none.

    Probed by name rather than with ``isinstance`` against
    ``ModelTargetValidator``: the hook is optional (see that protocol's
    docstring for why it is not a member of ``AdapterProtocol``), and a
    structural ``isinstance`` check would be no stronger here anyway — the
    call below is wrapped in ``try/except Exception`` regardless, exactly as
    every other call into third-party adapter code is.
    """
    candidate = getattr(adapter, "validate_model_target", None)
    return candidate if callable(candidate) else None


def _model_target_errors(validate: Callable[[dict], None], model_mappings: dict[str, dict]) -> list[str]:
    """Ask the adapter about every configured target; collect safe error codes.

    Two properties this deliberately holds:

    - **Nothing the adapter raises is published.** The exception is swallowed
      whole; only ``model_target_invalid:<alias>`` is emitted. The alias is
      operator configuration already published verbatim in the same payload's
      ``model_aliases``, and has already passed ``config``'s alias charset
      rule, so it is safe to echo — an adapter's exception message, which may
      quote the target, is not.
    - **The list is bounded.** The readiness document is a bounded response
      by design; a mapping with hundreds of bad aliases must not turn
      ``errors`` into an unbounded list, so entries past
      ``_MAX_MODEL_TARGET_ERRORS`` collapse into one ``:truncated`` marker.

    Each target is deep-copied before it is handed over: the adapter is
    third-party code and must not be able to mutate the live configuration
    the request pipeline later reads its model targets from.
    """
    failed: list[str] = []
    for alias in sorted(model_mappings):
        try:
            validate(copy.deepcopy(model_mappings[alias]))
        except Exception:
            failed.append(alias)

    errors = [f"model_target_invalid:{alias}" for alias in failed[:_MAX_MODEL_TARGET_ERRORS]]
    if len(failed) > _MAX_MODEL_TARGET_ERRORS:
        errors.append("model_target_invalid:truncated")
    return errors


async def _read_capped_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, streamed, rejecting anything over ``max_bytes``.

    Streamed rather than ``await request.body()`` so an oversized body is
    caught as soon as more than ``max_bytes`` have arrived, without ever
    buffering the whole (potentially much larger) body first.
    """
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise GatewayError(GatewayErrorCode.INVALID_REQUEST)
    return bytes(buffer)


def create_app(
    config: GatewayConfig,
    *,
    adapter: AdapterProtocol | None = None,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the gateway ASGI app.

    ``adapter`` and ``upstream_client`` are injectable for tests (a fake
    adapter, a client wired to a mock transport); when omitted, the adapter
    is resolved from ``config.adapter_name`` and a real ``httpx.AsyncClient``
    is created and owned by this app. A single ``upstream_client`` backs both
    the OAuth ``TokenManager`` and the ``UpstreamClient`` — one transport for
    both token and completion calls, on purpose.
    """
    resolved_adapter = adapter if adapter is not None else _resolve_adapter(config)
    owns_http_client = upstream_client is None
    http_client = upstream_client if upstream_client is not None else httpx.AsyncClient()

    token_manager = TokenManager(config, http_client)
    upstream = UpstreamClient(config, token_manager, http_client)
    service = CompletionService(config, resolved_adapter, upstream, logger)
    adapter_fingerprint = _compute_adapter_fingerprint(resolved_adapter)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_http_client:
                await http_client.aclose()

    # No /docs, /redoc, or /openapi.json: this is a closed API boundary with
    # a fixed, hand-specified route set, not a browsable API -- /docs in
    # particular would pull third-party CDN scripts onto this origin.
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> JSONResponse:
        request_id = _request_id_from_state(request)
        raw_body = await _read_capped_body(request, config.max_body_bytes)

        try:
            parsed = parse_strict_json(raw_body, max_bytes=config.max_body_bytes)
        except StrictJsonError:
            # parse_strict_json's own iterative depth pre-scan rejects a
            # deeply-nested-but-individually-tiny body with
            # StrictJsonError(reason="too_deep") before ever handing it to
            # json.loads, so a bare RecursionError can no longer escape this
            # call -- see core/parsing.py's module-level comment for why the
            # underlying json.loads call cannot be trusted to fail safely on
            # its own.
            raise GatewayError(GatewayErrorCode.INVALID_REQUEST) from None

        try:
            chat_request = ChatRequest.model_validate(parsed)
        except ValidationError:
            raise GatewayError(GatewayErrorCode.INVALID_REQUEST) from None

        response = await service.complete(chat_request, request_id)
        return JSONResponse(content=response)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(content={"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Static readiness: configuration, adapter, and model mappings.

        Makes no OAuth call and no upstream call -- everything checked here
        is answerable from process configuration plus purely computational
        calls into the adapter (``validate_configuration``, ``descriptor``,
        and, when the adapter implements it, ``validate_model_target`` for
        every configured target).

        That last check is what makes readiness a real admission gate for
        model mappings rather than a presence test: the mapping *value* is
        opaque to the core, so only the adapter can say whether it is one its
        own ``build_invoke`` can consume. Without it a deployment whose
        targets the adapter cannot read passed admission and then failed
        every completion with ``internal_error``.
        """
        errors: list[str] = []
        capabilities: list[str] = []
        descriptor = None

        try:
            resolved_adapter.validate_configuration({})
        except Exception:
            errors.append("adapter_configuration_invalid")

        try:
            descriptor = resolved_adapter.descriptor()
            capabilities = sorted(capability.value for capability in descriptor.capabilities)
        except Exception:
            errors.append("adapter_descriptor_invalid")

        if descriptor is not None and descriptor.adapter_api_major != ADAPTER_API_MAJOR:
            errors.append("adapter_api_incompatible")

        if not config.model_mappings:
            errors.append("empty_model_mappings")

        # An adapter without the (optional) hook is not an error: it was
        # written against this same adapter API major before the hook
        # existed. Its targets simply cannot be checked, and the payload says
        # so via ``validates_model_targets`` -- the conformance kit, not the
        # runtime, is what makes the hook mandatory for image qualification.
        validate_target = _model_target_validator(resolved_adapter)
        if validate_target is not None:
            errors.extend(_model_target_errors(validate_target, config.model_mappings))

        ready = not errors
        payload = {
            "ready": ready,
            "contract_major": CONTRACT_MAJOR,
            "adapter": {
                "name": descriptor.name if descriptor is not None else config.adapter_name,
                "version": descriptor.version if descriptor is not None else "unknown",
                "adapter_api_major": descriptor.adapter_api_major if descriptor is not None else 0,
                "fingerprint": adapter_fingerprint,
                "validates_model_targets": validate_target is not None,
            },
            "capabilities": capabilities,
            "model_aliases": sorted(config.model_mappings),
            "mapping_generation": config.mapping_generation,
            "oauth_fixed_lifetime": config.oauth_fixed_lifetime_seconds is not None,
            "errors": errors,
        }
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    # GatewayError is handled here (via ExceptionMiddleware, deep inside the
    # middleware stack) rather than a generic Exception handler -- see
    # RequestIDMiddleware's docstring for why a catch-all needs to live in
    # the outermost layer instead.
    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        request_id = _request_id_from_state(request)
        # Route-level rejections (oversized body, malformed JSON, an
        # unknown field) never reach CompletionService, so this is the only
        # place they get logged at all; a GatewayError raised inside
        # service.complete() is also logged there (event "completion") --
        # logging again here, safe-fields-only, is deliberate belt-and-braces
        # rather than an attempt to be the single source of truth.
        log_event(logger, "request_error", request_id=request_id, error_code=exc.code.value, status="error")
        return JSONResponse(status_code=exc.status, content=error_envelope(exc, request_id))

    # Starlette's add_middleware makes the LAST-added middleware OUTERMOST.
    # Execution order must be request-ID -> contract -> auth, so middleware
    # is added in the reverse of that order: auth first (innermost), then
    # contract, then request-ID last (outermost) -- see RequestIDMiddleware's
    # docstring for why the outermost layer is the one that must stamp
    # headers on every response, including ones produced by the two layers
    # nested inside it.
    app.add_middleware(AuthMiddleware, config=config)
    app.add_middleware(ContractHeaderMiddleware)
    app.add_middleware(RequestIDMiddleware)

    return app


def build() -> FastAPI:
    """``uvicorn --factory`` entry point: build the app from process environment.

    This is the sole function the container image's ``ENTRYPOINT`` names
    (``elspeth_llm_gateway.core.app:build``) -- it reads ``os.environ``
    through ``load_config`` (the same fail-closed, whole-namespace-policed
    path every other caller of ``load_config`` goes through) and hands the
    result straight to ``create_app`` with no injected adapter or upstream
    client, exactly as a real deployment runs. A ``ConfigError`` raised here
    propagates out of uvicorn's factory import and fails the container at
    startup, before it ever binds a port -- there is no fallback or partial
    app to serve.
    """
    return create_app(load_config(os.environ))
