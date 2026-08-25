# tests/integration/plugins/llm/test_gateway_provider_e2e.py
"""End-to-end: the real ``GatewayLLMProvider`` driving the real Phase 1 gateway.

Everything up to Phase 2 Task 5 was unit-level with mocked HTTP (respx) on
the ELSPETH side. This is the first test in the plan that proves the whole
seam: ELSPETH's synchronous, HTTP-based ``GatewayLLMProvider`` talking real
HTTP to a real ``uvicorn`` server running the actual gateway ASGI app
(``elspeth_llm_gateway.core.app.create_app``), which in turn talks to the
in-tree mock OAuth token server and mock fictional upstream over an
in-process ``httpx.ASGITransport`` pair (the same host-routed-transport
pattern ``gateway/conformance/conftest.py`` uses).

Bridging sync ELSPETH to the async gateway
-------------------------------------------
``GatewayLLMProvider.execute_query`` is synchronous (built on
``AuditedHTTPClient``, itself a synchronous ``httpx.Client`` wrapper); the
gateway app is an ASGI application. Rather than reach for an in-memory
ASGI-transport shortcut (which would never open a real socket and would
leave the sync/async boundary untested), this module runs the gateway app in
a genuine ``uvicorn.Server`` bound to ``127.0.0.1`` on an OS-assigned
ephemeral port, inside a background thread, for the lifetime of the
fixture. The provider is then configured with a perfectly ordinary
``http://127.0.0.1:<port>/v1`` endpoint (the exact loopback form the
endpoint validator allows) and issues real, synchronous HTTP requests
against it from the test's (main) thread while the gateway's own async
request handling — including its single OAuth-refresh-and-replay on a
401 — runs on the uvicorn server thread's event loop. This exercises the
true HTTP path end to end, not an in-memory approximation of it.

``uvicorn.Server.capture_signals`` already special-cases "not the main
thread" (skips installing signal handlers), so no monkeypatching is needed
to run it in a background thread. Startup/shutdown are synchronized via
bounded polling only: first a tight loop on ``Server.started`` (a plain
attribute flag, near-instant), then a bounded ``/healthz`` poll loop over a
real ``httpx.Client`` (covers the gap between "asyncio loop marked started"
and "the OS listener is actually accepting connyections") — never a fixed
sleep whose duration could flake.

Stacks
------
- ``main_stack`` (module-scoped): the real ``ReferenceV1InvokeAdapter``, the
  real mock OAuth app, the real mock upstream app. Backs almost every test
  here: single-query text, multi-query JSON Schema/JSON object (criterion 4),
  finish-reason/usage-preserved (criterion 5), the "exactly one upstream call
  in the normal case" half of criterion 6, every criterion 9 error mapping,
  and both preflight cases.
- ``no_usage_stack`` (function-scoped): same reference adapter and mock
  upstream, but wrapped in a thin adapter that always strips
  ``usage`` from ``parse_success``'s result. The in-tree mock upstream's
  every success response includes ``accounting`` (there is no request shape
  that omits it), so this is the only way to prove
  "unavailable usage -> ``TokenUsage.unknown()``, never zeros" against a
  real round trip through the real pipeline rather than only at the unit
  level (which already covers it exhaustively via respx in
  ``test_provider_gateway.py::TestUsageHandling``). This mirrors a
  real-world shape (an agency/adapter pair that doesn't report usage for a
  given call) — not a shortcut around the design.
- ``forced_401_stack`` (function-scoped): its own independent mock OAuth
  counter and a mock upstream configured to accept only the *second*
  sequentially-issued token (``require_bearer_prefix="mock-token-2"``), so
  the first upstream call is deterministically rejected with 401, forcing
  exactly one token invalidate + refetch + replay inside the gateway's
  ``UpstreamClient`` — invisible to ELSPETH, which sees one successful
  ``execute_query()`` call.

Hermetic and deterministic: no network egress beyond loopback (the OAuth and
upstream "servers" are in-process ASGI apps reached via
``httpx.ASGITransport``, never real sockets); every mock response is a pure
function of its request; all waits are bounded polls, never a blind sleep.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn

# --- sys.path shim -----------------------------------------------------
# ``gateway/`` is not on the default ELSPETH import path (see
# tests/gateway_runtime/conftest.py for the same pattern, scoped to a
# different test directory). This file needs both ``gateway/src`` (for
# ``elspeth_llm_gateway``) and ``gateway`` itself (for the ``mock`` package).
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _entry in (_REPO_ROOT / "gateway" / "src", _REPO_ROOT / "gateway"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from elspeth_llm_gateway.core.app import create_app  # noqa: E402
from elspeth_llm_gateway.core.config import load_config  # noqa: E402
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter  # noqa: E402
from mock.oauth import create_mock_oauth_app  # noqa: E402
from mock.upstream import create_mock_upstream_app  # noqa: E402

from elspeth.contracts import CallStatus, CallType  # noqa: E402
from elspeth.contracts.chat_parts import ChatMessage  # noqa: E402
from elspeth.plugins.infrastructure.clients.llm import (  # noqa: E402
    ContentPolicyError,
    ContextLengthError,
    LLMClientError,
    ServerError,
)
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMAuditParent  # noqa: E402
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider  # noqa: E402

# ---------------------------------------------------------------------------
# Fixed, deterministic development-only values for the in-process mock stack
# this module builds itself -- never used against a real credential store.
# ---------------------------------------------------------------------------

_INBOUND_BEARER = "e2e-inbound-bearer-0123456789abcdef"  # secret-scan: allow-this-line
_OAUTH_CLIENT_ID = "e2e-oauth-client"
_OAUTH_CLIENT_SECRET = "e2e-oauth-client-secret-0123456789abcdef"  # secret-scan: allow-this-line
_MODEL_ALIAS = "gateway-e2e-model"
_MODEL_TARGET = "gateway-e2e-target"
_OAUTH_HOST = "oauth.e2e.mock"
_UPSTREAM_HOST = "upstream.e2e.mock"
_CONTRACT_HEADER = "X-ELSPETH-LLM-Gateway-Contract"
_RUN_ID = "e2e-run"


# ---------------------------------------------------------------------------
# Minimal audit-recorder / telemetry doubles (same shape as
# tests/unit/plugins/llm/test_provider_gateway.py's FakeAuditRecorder --
# audit plumbing is not what this test proves; the gateway wiring is).
# ---------------------------------------------------------------------------


@dataclass
class FakeAuditRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    _next_index: int = 0

    def allocate_call_index(self, state_id: str | None) -> int:
        self._next_index += 1
        return self._next_index - 1

    def allocate_operation_call_index(self, operation_id: str) -> int:
        self._next_index += 1
        return self._next_index - 1

    def record_call(self, **call: Any) -> SimpleNamespace:
        self.calls.append(call)
        return SimpleNamespace(request_ref=f"request-{len(self.calls)}", response_ref=f"response-{len(self.calls)}")

    def record_operation_call(self, **call: Any) -> SimpleNamespace:
        self.calls.append(call)
        return SimpleNamespace(request_ref=f"op-request-{len(self.calls)}", response_ref=f"op-response-{len(self.calls)}")


@dataclass
class FakeTelemetryEmit:
    events: list[Any] = field(default_factory=list)

    def __call__(self, event: Any) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Recording transport: counts calls and captures the Authorization header
# seen by whichever ASGI app it wraps -- used to observe, from outside, how
# many times the mock OAuth token endpoint / mock upstream endpoint were
# actually hit by the gateway's own internal HTTP client, and with what
# bearer token each time.
# ---------------------------------------------------------------------------


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.call_count = 0
        self.authorization_headers: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        self.authorization_headers.append(request.headers.get("authorization"))
        return await self._inner.handle_async_request(request)


class _HostRoutedTransport(httpx.AsyncBaseTransport):
    """Dispatches by request URL host to one of several registered transports."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._routes.get(host)
        if transport is None:
            raise RuntimeError(f"e2e router transport: no route registered for host {host!r}")
        return await transport.handle_async_request(request)


class _NoUsageAdapter:
    """``ReferenceV1InvokeAdapter``, but ``parse_success`` always strips usage.

    The in-tree mock upstream's every success response includes
    ``accounting``, so there is no request shape that gets a "no usage
    reported" response through it. This wrapper simulates a legitimate
    real-world adapter/agency pairing that doesn't report usage for a given
    call, while still exercising the real gateway pipeline (canonicalisation,
    the real upstream call, real response validation) end to end -- the same
    "swap in a differently-configured but still real adapter" technique the
    Task 13 conformance kit's ``_TextOnlyAdapter`` already uses.
    """

    def __init__(self) -> None:
        self._delegate = ReferenceV1InvokeAdapter()

    def descriptor(self):
        return self._delegate.descriptor()

    def validate_configuration(self, options: dict[str, Any]) -> None:
        self._delegate.validate_configuration(options)

    def build_invoke(self, request):
        return self._delegate.build_invoke(request)

    def parse_success(self, body: dict[str, Any]):
        response = self._delegate.parse_success(body)
        return response.model_copy(update={"usage": None})

    def classify_error(self, failure):
        return self._delegate.classify_error(failure)


def _build_gateway_app(
    *,
    model_alias: str = _MODEL_ALIAS,
    model_target: str = _MODEL_TARGET,
    require_bearer_prefix: str = "mock-token-",
    adapter: Any = None,
) -> tuple[Any, _RecordingTransport, _RecordingTransport]:
    """Build one independent mock-oauth + mock-upstream + gateway-app stack.

    Returns the ASGI app plus the two recording transports so a test can
    inspect how many times (and with what bearer) the gateway's internal
    HTTP client actually hit the OAuth token endpoint / the upstream
    endpoint.
    """
    oauth_app = create_mock_oauth_app(client_id=_OAUTH_CLIENT_ID, client_secret=_OAUTH_CLIENT_SECRET)
    upstream_app = create_mock_upstream_app(require_bearer_prefix=require_bearer_prefix)

    oauth_transport = _RecordingTransport(httpx.ASGITransport(app=oauth_app))
    upstream_transport = _RecordingTransport(httpx.ASGITransport(app=upstream_app))
    router_transport = _HostRoutedTransport({_OAUTH_HOST: oauth_transport, _UPSTREAM_HOST: upstream_transport})

    # Never closed explicitly: it wraps two in-process ASGITransports (no
    # real sockets, no pooled connections to leak) and is used exclusively
    # from within the uvicorn server thread's own event loop for the
    # fixture's lifetime; it is garbage-collected once that thread and loop
    # shut down.
    #
    # Constructed here on the calling (test) thread but only ever awaited
    # from the uvicorn server thread's event loop (this function returns the
    # app to be handed to that server before any request is made). This is
    # safe only because httpx.AsyncClient defers all event-loop-bound state
    # (its internal connection pool / anyio backend) to lazy init on first
    # async use -- it does not bind to a loop at construction time. If a
    # future httpx upgrade changes that (e.g. binding a loop reference
    # eagerly in __init__), this would start failing with a
    # cross-event-loop error the first time the server thread touches the
    # client, which is the diagnostic signal to look for if this ever flakes.
    upstream_client = httpx.AsyncClient(transport=router_transport)

    env = {
        "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": _INBOUND_BEARER,
        "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
        "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": f"https://{_UPSTREAM_HOST}",
        "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": f"https://{_OAUTH_HOST}/token",
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": _OAUTH_CLIENT_ID,
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": _OAUTH_CLIENT_SECRET,
        "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
        "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
        "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
        "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "20000",
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
        "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": json.dumps({model_alias: {"target": model_target}}),
    }
    config = load_config(env)
    app = create_app(config, adapter=adapter, upstream_client=upstream_client)
    return app, oauth_transport, upstream_transport


@contextmanager
def _running_gateway_server(app: Any) -> Iterator[str]:
    """Run ``app`` in a real ``uvicorn.Server`` on an ephemeral loopback port.

    Yields the server's base URL (``http://127.0.0.1:<port>``, no trailing
    slash). All synchronization is bounded polling: first a tight loop on
    the plain ``Server.started`` flag, then a bounded ``/healthz`` poll over
    a real ``httpx.Client`` -- never a fixed-duration sleep.
    """
    config = uvicorn.Config(app=app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("gateway e2e server did not report started within 10s")
        time.sleep(0.01)

    sockets = server.servers[0].sockets
    port = sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    with httpx.Client() as probe:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                response = probe.get(f"{base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("gateway e2e server did not become healthy within 10s")
            time.sleep(0.02)

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if thread.is_alive():
            raise RuntimeError(
                "gateway e2e server thread did not exit within 10s after "
                "should_exit=True -- it would otherwise leak a thread/socket "
                "silently for the rest of the suite"
            )


def _make_provider(
    base_url: str,
    *,
    model: str = _MODEL_ALIAS,
    required_capabilities: tuple[str, ...] = (),
) -> GatewayLLMProvider:
    return GatewayLLMProvider(
        endpoint=f"{base_url}/v1",
        api_key=_INBOUND_BEARER,
        contract_major=1,
        required_capabilities=required_capabilities,
        timeout_seconds=10.0,
        recorder=FakeAuditRecorder(),
        run_id=_RUN_ID,
        telemetry_emit=FakeTelemetryEmit(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def main_stack() -> Iterator[tuple[str, _RecordingTransport, _RecordingTransport]]:
    app, oauth_transport, upstream_transport = _build_gateway_app()
    with _running_gateway_server(app) as base_url:
        yield base_url, oauth_transport, upstream_transport


@pytest.fixture()
def main_provider(main_stack: tuple[str, _RecordingTransport, _RecordingTransport]) -> Iterator[GatewayLLMProvider]:
    base_url, _, _ = main_stack
    provider = _make_provider(base_url)
    try:
        yield provider
    finally:
        provider.close()


@pytest.fixture()
def no_usage_provider() -> Iterator[GatewayLLMProvider]:
    app, _, _ = _build_gateway_app(adapter=_NoUsageAdapter())
    with _running_gateway_server(app) as base_url:
        provider = _make_provider(base_url)
        try:
            yield provider
        finally:
            provider.close()


@pytest.fixture()
def forced_401_stack() -> Iterator[tuple[GatewayLLMProvider, _RecordingTransport, _RecordingTransport]]:
    app, oauth_transport, upstream_transport = _build_gateway_app(require_bearer_prefix="mock-token-2")
    with _running_gateway_server(app) as base_url:
        provider = _make_provider(base_url)
        try:
            yield provider, oauth_transport, upstream_transport
        finally:
            provider.close()


# ---------------------------------------------------------------------------
# Criterion 4 -- single-query TEXT and multi-query JSON Schema / JSON object
# ---------------------------------------------------------------------------


class TestCriterion4Shapes:
    def test_single_query_text(self, main_provider: GatewayLLMProvider) -> None:
        result = main_provider.execute_query(
            messages=[ChatMessage(role="user", content="hello gateway")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-text",
                token_id="token-text",
            ),
        )
        assert result.content == "MOCK:hello gateway"
        assert result.model == _MODEL_ALIAS
        assert result.finish_reason is FinishReason.STOP

    def test_multi_query_json_schema_matches_transform_shape(self, main_provider: GatewayLLMProvider) -> None:
        """Exact response_format shape MultiQueryStrategy._execute_one_query
        builds (transform.py:611-628) for a STRUCTURED response query."""
        properties = {"answer": {"type": "string"}}
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "test_query_output",
                "schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                },
            },
        }
        result = main_provider.execute_query(
            messages=[ChatMessage(role="user", content="structured please")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-schema",
                token_id="token-schema",
            ),
            response_format=response_format,
        )
        assert json.loads(result.content) == {"answer": "structured please"}
        assert result.finish_reason is FinishReason.STOP

    def test_multi_query_json_object(self, main_provider: GatewayLLMProvider) -> None:
        response_format = {"type": "json_object"}
        result = main_provider.execute_query(
            messages=[ChatMessage(role="user", content="object please")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-object",
                token_id="token-object",
            ),
            response_format=response_format,
        )
        assert json.loads(result.content) == {"echo": "object please"}
        assert result.finish_reason is FinishReason.STOP


# ---------------------------------------------------------------------------
# Criterion 5 -- usage/finish_reason preserved; unavailable usage -> unknown()
# ---------------------------------------------------------------------------


class TestCriterion5UsageAndFinishReason:
    def test_truncated_maps_to_length_with_preserved_usage(self, main_provider: GatewayLLMProvider) -> None:
        message = "TRIGGER_HALT truncated"
        result = main_provider.execute_query(
            messages=[ChatMessage(role="user", content=message)],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-halt-truncated",
                token_id="token-halt-truncated",
            ),
        )
        assert result.content == "MOCK:truncated"
        assert result.finish_reason is FinishReason.LENGTH
        expected_prompt = len(message) // 4
        expected_completion = len("MOCK:truncated") // 4
        assert result.usage.prompt_tokens == expected_prompt
        assert result.usage.completion_tokens == expected_completion

    def test_complete_maps_to_stop_with_preserved_usage(self, main_provider: GatewayLLMProvider) -> None:
        message = "TRIGGER_HALT complete"
        result = main_provider.execute_query(
            messages=[ChatMessage(role="user", content=message)],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-halt-complete",
                token_id="token-halt-complete",
            ),
        )
        assert result.content == "MOCK:complete"
        assert result.finish_reason is FinishReason.STOP
        assert result.usage.prompt_tokens == len(message) // 4
        assert result.usage.completion_tokens == len("MOCK:complete") // 4

    def test_screened_maps_to_content_policy_error(self, main_provider: GatewayLLMProvider) -> None:
        """halt="screened" salvages no text -> CanonicalResponse.text="" ->
        the gateway's rendered content is "" -> the provider's blank-content
        rule raises ContentPolicyError (never a successful, empty result)."""
        with pytest.raises(ContentPolicyError):
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="TRIGGER_HALT screened")],
                model=_MODEL_ALIAS,
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-halt-screened",
                    token_id="token-halt-screened",
                ),
            )

    def test_unavailable_usage_recorded_as_unknown_never_zeros(self, no_usage_provider: GatewayLLMProvider) -> None:
        result = no_usage_provider.execute_query(
            messages=[ChatMessage(role="user", content="no usage please")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-no-usage",
                token_id="token-no-usage",
            ),
        )
        assert result.content == "MOCK:no usage please"
        assert result.usage.prompt_tokens is None
        assert result.usage.completion_tokens is None
        assert result.usage.reported_total is None


# ---------------------------------------------------------------------------
# Criterion 6 -- retries/pooling stay outside the gateway; single in-gateway
# OAuth-refresh replay on 401
# ---------------------------------------------------------------------------


class TestCriterion6RetriesStayOutside:
    def test_exactly_one_upstream_call_in_normal_case(
        self, main_provider: GatewayLLMProvider, main_stack: tuple[str, _RecordingTransport, _RecordingTransport]
    ) -> None:
        _, _, upstream_transport = main_stack
        before = upstream_transport.call_count
        main_provider.execute_query(
            messages=[ChatMessage(role="user", content="count me once")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-count-once",
                token_id="token-count-once",
            ),
        )
        assert upstream_transport.call_count - before == 1

    def test_forced_401_causes_one_refetch_and_one_replay(
        self, forced_401_stack: tuple[GatewayLLMProvider, _RecordingTransport, _RecordingTransport]
    ) -> None:
        provider, oauth_transport, upstream_transport = forced_401_stack

        result = provider.execute_query(
            messages=[ChatMessage(role="user", content="hello after replay")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-401-replay",
                token_id="token-401-replay",
            ),
        )

        # ELSPETH sees exactly one successful call -- the gateway's own
        # replay-on-401 is invisible to it.
        assert result.content == "MOCK:hello after replay"
        assert result.finish_reason is FinishReason.STOP

        # But under the hood: the first token ("mock-token-1") didn't carry
        # the required prefix ("mock-token-2"), so the mock upstream 401'd
        # it; the gateway invalidated its cache and fetched exactly one more
        # token, then replayed exactly once with the new bearer.
        assert oauth_transport.call_count == 2
        assert upstream_transport.call_count == 2
        first_bearer, second_bearer = upstream_transport.authorization_headers
        assert first_bearer != second_bearer
        assert first_bearer == "Bearer mock-token-1"
        assert second_bearer == "Bearer mock-token-2"


# ---------------------------------------------------------------------------
# Criterion 9 -- failures map to the correct ELSPETH outcomes
# ---------------------------------------------------------------------------


class TestCriterion9ErrorMapping:
    def test_overloaded_is_retryable_server_error(self, main_provider: GatewayLLMProvider) -> None:
        with pytest.raises(ServerError) as exc_info:
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="TRIGGER_FAULT overloaded")],
                model=_MODEL_ALIAS,
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-fault-overloaded",
                    token_id="token-fault-overloaded",
                ),
            )
        assert exc_info.value.retryable is True
        assert str(exc_info.value) == "Gateway LLM request failed"

    def test_screening_is_content_policy_error(self, main_provider: GatewayLLMProvider) -> None:
        with pytest.raises(ContentPolicyError) as exc_info:
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="TRIGGER_FAULT screening")],
                model=_MODEL_ALIAS,
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-fault-screening",
                    token_id="token-fault-screening",
                ),
            )
        assert exc_info.value.retryable is False
        assert str(exc_info.value) == "Gateway LLM request failed"

    def test_too_long_is_context_length_error(self, main_provider: GatewayLLMProvider) -> None:
        with pytest.raises(ContextLengthError) as exc_info:
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="TRIGGER_FAULT too_long")],
                model=_MODEL_ALIAS,
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-fault-too-long",
                    token_id="token-fault-too-long",
                ),
            )
        assert exc_info.value.retryable is False
        assert str(exc_info.value) == "Gateway LLM request failed"

    def test_unmapped_model_alias_is_non_retryable_config_failure(self, main_provider: GatewayLLMProvider) -> None:
        with pytest.raises(LLMClientError) as exc_info:
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="hi")],
                model="not-a-published-alias",
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-unmapped-model",
                    token_id="token-unmapped-model",
                ),
            )
        assert type(exc_info.value) is LLMClientError
        assert exc_info.value.retryable is False
        assert str(exc_info.value) == "Gateway LLM request failed"

    def test_contract_major_mismatch_is_non_retryable(self, main_provider: GatewayLLMProvider) -> None:
        """Mutate the outbound contract header to something the live
        gateway's ContractHeaderMiddleware will reject -- a genuine round
        trip through the real middleware, not a mocked response."""
        main_provider._request_headers[_CONTRACT_HEADER] = "99"
        with pytest.raises(LLMClientError) as exc_info:
            main_provider.execute_query(
                messages=[ChatMessage(role="user", content="hi")],
                model=_MODEL_ALIAS,
                temperature=0.0,
                max_tokens=100,
                audit_parent=LLMAuditParent.for_row(
                    state_id="state-contract-mismatch",
                    token_id="token-contract-mismatch",
                ),
            )
        assert type(exc_info.value) is LLMClientError
        assert exc_info.value.retryable is False
        assert str(exc_info.value) == "Gateway LLM request failed"


# ---------------------------------------------------------------------------
# runtime_preflight -- readyz + a real completion; fails when the configured
# model alias is not published by the gateway
# ---------------------------------------------------------------------------


class TestRuntimePreflight:
    def test_preflight_succeeds_against_live_gateway(self, main_provider: GatewayLLMProvider) -> None:
        main_provider.runtime_preflight(operation_id="op-preflight-ok", model=_MODEL_ALIAS)

    def test_preflight_fails_when_model_alias_not_published(self, main_provider: GatewayLLMProvider) -> None:
        with pytest.raises(LLMClientError):
            main_provider.runtime_preflight(operation_id="op-preflight-bad-model", model="never-published-alias")


# ---------------------------------------------------------------------------
# Sanity: two audit rows recorded through the real round trip (transport +
# semantic), same discipline the unit-level provider tests already assert --
# a light end-to-end check that the audit plumbing engages at all here.
# ---------------------------------------------------------------------------


def test_success_records_two_audit_rows_end_to_end(main_stack: tuple[str, _RecordingTransport, _RecordingTransport]) -> None:
    base_url, _, _ = main_stack
    recorder = FakeAuditRecorder()
    provider = GatewayLLMProvider(
        endpoint=f"{base_url}/v1",
        api_key=_INBOUND_BEARER,
        contract_major=1,
        timeout_seconds=10.0,
        recorder=recorder,
        run_id=_RUN_ID,
        telemetry_emit=FakeTelemetryEmit(),
    )
    try:
        provider.execute_query(
            messages=[ChatMessage(role="user", content="audit me")],
            model=_MODEL_ALIAS,
            temperature=0.0,
            max_tokens=100,
            audit_parent=LLMAuditParent.for_row(
                state_id="state-audit",
                token_id="token-audit",
            ),
        )
    finally:
        provider.close()

    assert len(recorder.calls) == 2
    call_types = [call["call_type"] for call in recorder.calls]
    assert CallType.HTTP in call_types
    assert CallType.LLM in call_types
    llm_call = next(call for call in recorder.calls if call["call_type"] == CallType.LLM)
    assert llm_call["status"] == CallStatus.SUCCESS
