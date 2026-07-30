"""Phase 3 Task 3: prove the Web Composer holds a real tool-calling
conversation through the reference gateway.

This is design acceptance criterion 8 and the criterion the whole
"endpoint affordance" phase exists for: an ordinary LiteLLM call, pointed at
``composer_endpoint_base_url``, must be indistinguishable in shape from any
other OpenAI-compatible provider call, and Composer's real compose loop must
be able to drive a full turn against it.

Harness: reuses the proven pattern from
``tests/integration/plugins/llm/test_gateway_provider_e2e.py`` -- a real
``uvicorn.Server`` on an ephemeral loopback port, hosting the real gateway
ASGI app (reference adapter + mock OAuth + mock upstream wired through one
``httpx.AsyncClient`` with a host-routing transport), with bounded-poll
readiness and a teardown that fails loudly if the server thread does not
exit. This module does not import that file directly (test modules are not a
library surface); it re-derives the same mechanism, sized for composer
traffic rather than the low-level ``GatewayLLMProvider`` traffic the other
suite drives.

Driving criterion 8 originally surfaced two genuine, independent defects,
both of which are now FIXED; this module is the end-to-end proof of both.

1. ``elspeth.web.composer.tool_batch._admit_tool_batch`` validated provider
   tool calls with ``runtime_checkable`` ``Protocol`` ``isinstance()``
   checks. Since Python 3.12 those resolve members via
   ``inspect.getattr_static``, which bypasses ``__getattr__`` -- and
   LiteLLM's ``ChatCompletionMessageToolCall`` declares no pydantic model
   fields at all, so ``id``/``function`` resolve only through
   ``BaseModel.__getattr__``. The admission guard therefore rejected every
   genuine tool call from every provider (elspeth-9ea866438b). The unit-level
   regression lives in
   ``tests/unit/web/composer/test_compose_loop_tool_call_cap.py``; the
   full round trip is ``test_composer_tool_round_trip_against_gateway``
   below.
2. The gateway's inbound ``ChatRequest`` (``extra="forbid"``) had no
   ``max_completion_tokens`` field, while LiteLLM's ``openai`` provider path
   *translates* any caller-supplied ``max_tokens`` into that wire field and
   drops the original key. ``elspeth.web.composer.boot_probe`` always sends
   ``max_tokens=16`` and ``app.py`` re-raises ``ComposerBootConfigError``, so
   the web app failed to boot whenever ``composer_endpoint_base_url``
   pointed at this gateway. The gateway now accepts the alias;
   ``test_boot_probe_succeeds_against_gateway`` below is the proof at the
   boot-probe level.

Note which paths each defect touched, because the asymmetry explains why the
text round trip passed all along: the primary role's ordinary compose-loop
turns never set ``max_tokens`` (``_call_llm``/``_call_text_llm``), so only
the boot probe and the advisor role (which sets
``composer_advisor_max_completion_tokens``) were affected by defect 2.

What is proven here, durably:

- The gateway's tool-call response shape satisfies the freeform loop's HARD
  attribute access (``response.choices[0].message.tool_calls[i].id`` /
  ``.function.name`` / ``.function.arguments``, ``finish_reason``) when
  consumed via a real ``litellm.acompletion`` call.
- **Criterion 8**: a real tool-call / tool-result round trip through
  Composer's actual compose loop against the live gateway -- the provider's
  tool call is admitted, dispatched, and its result recorded.
- A real text (non-tool) round trip through the same loop.
- The composer boot probe succeeds against the live gateway with default
  settings.
- No gateway/agency internals (the inbound bearer, the OAuth client
  secret) leak into persisted chat history, log output, or a raised
  exception's message on a real gateway-side failure.
- Advisor/primary role independence: pointing the primary role at a real
  gateway must not leak into an unconfigured advisor call's kwargs.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

# --- sys.path shim ------------------------------------------------------
# ``gateway/`` is not on the default ELSPETH import path (see
# tests/gateway_runtime/conftest.py and
# tests/integration/plugins/llm/test_gateway_provider_e2e.py for the same
# pattern, scoped to different test directories).
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _entry in (_REPO_ROOT / "gateway" / "src", _REPO_ROOT / "gateway"):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from elspeth_llm_gateway.core.app import create_app  # noqa: E402
from elspeth_llm_gateway.core.config import load_config  # noqa: E402
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter  # noqa: E402
from mock.oauth import create_mock_oauth_app  # noqa: E402
from mock.upstream import create_mock_upstream_app  # noqa: E402

import elspeth.web.composer.service as composer_service_module  # noqa: E402
from elspeth.web.catalog.protocol import CatalogService  # noqa: E402
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary  # noqa: E402
from elspeth.web.composer.service import ComposerServiceImpl  # noqa: E402
from elspeth.web.config import WebSettings  # noqa: E402
from elspeth.web.sessions.engine import create_session_engine  # noqa: E402
from elspeth.web.sessions.models import chat_messages_table, sessions_table  # noqa: E402
from elspeth.web.sessions.schema import initialize_session_schema  # noqa: E402
from elspeth.web.sessions.service import SessionServiceImpl  # noqa: E402
from elspeth.web.sessions.telemetry import build_sessions_telemetry  # noqa: E402

# ---------------------------------------------------------------------------
# Fixed, deterministic development-only values for the in-process mock stack
# this module builds itself -- never used against a real credential store.
# ---------------------------------------------------------------------------

_INBOUND_BEARER = "composer-gw-e2e-inbound-bearer-0123456789abcdef"  # secret-scan: allow-this-line
_OAUTH_CLIENT_ID = "composer-gw-e2e-oauth-client"
_OAUTH_CLIENT_SECRET = "composer-gw-e2e-oauth-client-secret-0123456789abcdef"  # secret-scan: allow-this-line
_MODEL_ALIAS = "gpt-5.5"  # matches WebSettings.composer_model's default -- no override needed
_MODEL_TARGET = "composer-gw-e2e-target"
_OAUTH_HOST = "oauth.composer-gw-e2e.mock"
_UPSTREAM_HOST = "upstream.composer-gw-e2e.mock"

# Bounds sized for real Composer traffic (measured empirically against the
# trained-operator tool catalog + system skill prompt): 43 registered tools
# totalling ~39KB of tool-def JSON (largest single tool ~7.3KB), and a
# ~68KB system message. The Phase 2 e2e suite's bounds (10 tools / 20000
# chars) are sized for the low-level GatewayLLMProvider's synthetic
# messages and are far too small for a real Composer request.
_MAX_MESSAGES = "50"
_MAX_TOOLS = "60"
_MAX_STRING_CHARS = "100000"
_MAX_SCHEMA_BYTES = "65536"
_MAX_SCHEMA_DEPTH = "15"


class _HostRoutedTransport(httpx.AsyncBaseTransport):
    """Dispatches by request URL host to one of several registered transports."""

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._routes.get(host)
        if transport is None:
            raise RuntimeError(f"composer-gateway e2e router transport: no route registered for host {host!r}")
        return await transport.handle_async_request(request)


def _build_gateway_app() -> Any:
    """Build one gateway ASGI app backed by the reference adapter + mock OAuth + mock upstream."""
    oauth_app = create_mock_oauth_app(client_id=_OAUTH_CLIENT_ID, client_secret=_OAUTH_CLIENT_SECRET)
    upstream_app = create_mock_upstream_app()
    router_transport = _HostRoutedTransport(
        {
            _OAUTH_HOST: httpx.ASGITransport(app=oauth_app),
            _UPSTREAM_HOST: httpx.ASGITransport(app=upstream_app),
        }
    )
    # Never closed explicitly: wraps two in-process ASGITransports (no real
    # sockets, nothing pooled to leak) and is used exclusively from the
    # uvicorn server thread's own event loop for the fixture's lifetime --
    # see test_gateway_provider_e2e.py's identical note for why constructing
    # it here (on the calling thread) before handing it to that thread is
    # safe.
    upstream_client = httpx.AsyncClient(transport=router_transport)

    env = {
        "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": _INBOUND_BEARER,
        "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
        "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": f"https://{_UPSTREAM_HOST}",
        "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": f"https://{_OAUTH_HOST}/token",
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": _OAUTH_CLIENT_ID,
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": _OAUTH_CLIENT_SECRET,
        "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
        "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": _MAX_MESSAGES,
        "ELSPETH_LLM_GATEWAY_MAX_TOOLS": _MAX_TOOLS,
        "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": _MAX_STRING_CHARS,
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": _MAX_SCHEMA_BYTES,
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": _MAX_SCHEMA_DEPTH,
        "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": json.dumps({_MODEL_ALIAS: {"target": _MODEL_TARGET}}),
    }
    config = load_config(env)
    return create_app(config, adapter=ReferenceV1InvokeAdapter(), upstream_client=upstream_client)


@contextmanager
def _running_gateway_server(app: Any) -> Iterator[str]:
    """Run ``app`` in a real ``uvicorn.Server`` on an ephemeral loopback port.

    Yields the server's base URL. All synchronization is bounded polling:
    first a tight loop on the plain ``Server.started`` flag, then a bounded
    ``/healthz`` poll over a real ``httpx.Client`` -- never a fixed-duration
    sleep. Teardown fails loudly (raises) if the server thread does not
    exit within the bound, rather than silently leaking a thread/socket for
    the rest of the suite.
    """
    config = uvicorn.Config(app=app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("composer-gateway e2e server did not report started within 10s")
        time.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
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
                raise RuntimeError("composer-gateway e2e server did not become healthy within 10s")
            time.sleep(0.02)

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        if thread.is_alive():
            raise RuntimeError(
                "composer-gateway e2e server thread did not exit within 10s after "
                "should_exit=True -- it would otherwise leak a thread/socket silently "
                "for the rest of the suite"
            )


@pytest.fixture(scope="module")
def gateway_base_url() -> Iterator[str]:
    """One gateway stack shared across every test in this module.

    The mock upstream and mock OAuth app are both stateless pure functions
    of their request (see their module docstrings), so sharing one running
    instance across tests is safe and keeps this module's total wall-clock
    cost to one server startup.
    """
    app = _build_gateway_app()
    with _running_gateway_server(app) as base_url:
        yield base_url


# ---------------------------------------------------------------------------
# Composer fixtures -- self-contained (this directory does not share
# tests/unit/web/composer/conftest.py; that tree is a different test scope).
# ---------------------------------------------------------------------------


def _mock_catalog() -> MagicMock:
    """Same shape as tests/unit/web/composer/conftest.py's ``_mock_catalog``:
    the minimum a real compose-loop turn touches while building the dynamic
    context and dispatching ``list_sources``."""
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[]),
    ]
    catalog.list_transforms.return_value = [
        PluginSummary(name="passthrough", description="Passthrough", plugin_type="transform", config_fields=[]),
    ]
    catalog.list_sinks.return_value = [
        PluginSummary(name="csv", description="CSV sink", plugin_type="sink", config_fields=[]),
    ]
    catalog.get_schema.return_value = PluginSchemaInfo(
        name="csv",
        plugin_type="source",
        description="CSV source",
        json_schema={"title": "Config", "properties": {}},
        knob_schema={"fields": []},
    )
    return catalog


def _settings(tmp_path: Path, *, endpoint_base_url: str, endpoint_api_key: str, **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
        "composer_endpoint_base_url": endpoint_base_url,
        "composer_endpoint_api_key": endpoint_api_key,
    }
    values.update(overrides)
    return WebSettings(**values)


def _build_sessions_service(tmp_path: Path) -> SessionServiceImpl:
    """Real SQLite-backed sessions service (StaticPool in-memory), same
    schema-bootstrap path production uses -- not a bare metadata.create_all()."""
    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=__import__("structlog").get_logger("test.composer.gateway_e2e.sessions"),
    )


def _insert_session_row(sessions_service: SessionServiceImpl, session_id: str) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with sessions_service._engine.begin() as conn:
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id="gateway-e2e-user",
                auth_provider_type="local",
                title="Composer-against-gateway e2e session",
                trust_mode="auto_commit",
                density_default="high",
                created_at=now,
                updated_at=now,
            )
        )


def _build_service(tmp_path: Path, base_url: str, sessions_service: SessionServiceImpl, **settings_overrides: Any) -> ComposerServiceImpl:
    settings = _settings(tmp_path, endpoint_base_url=f"{base_url}/v1", endpoint_api_key=_INBOUND_BEARER, **settings_overrides)
    return ComposerServiceImpl.for_trained_operator(
        catalog=_mock_catalog(),
        settings=settings,
        sessions_service=sessions_service,
    )


# ---------------------------------------------------------------------------
# The durable response-shape proof (design acceptance criterion 8, gateway
# side). Drives a real ``litellm.acompletion`` call -- not the low-level
# GatewayLLMProvider, not a hand-rolled HTTP client -- against the live
# gateway, and inspects the exact HARD attribute-access chain the freeform
# loop uses (``service.py``'s ``_call_model`` and ``tool_batch.py``'s
# ``_admit_tool_batch``: ``tc.id`` / ``tc.function.name`` /
# ``tc.function.arguments``).
# ---------------------------------------------------------------------------


class TestGatewayResponseShapeSatisfiesFreeformAttributeAccess:
    @pytest.mark.asyncio
    async def test_tool_call_response_shape(self, gateway_base_url: str) -> None:
        """USE_TOOL trigger -> real litellm.acompletion -> dotted attribute
        access on tool_calls, exactly as the freeform loop performs it.

        The GATEWAY's response shape is fully consumable via
        ``response.choices[0].message.tool_calls[i].id`` /
        ``.function.name`` / ``.function.arguments`` -- no dict-key access,
        and no gateway-side fix was ever needed. Defect 1 in the module
        docstring lived in Composer's OWN tool-batch admission code, never
        here; this test is the evidence that separated the two, and stays as
        the narrow proof of the gateway half.
        """
        import litellm

        response = await litellm.acompletion(
            model=_MODEL_ALIAS,
            api_base=f"{gateway_base_url}/v1",
            api_key=_INBOUND_BEARER,
            messages=[{"role": "user", "content": "USE_TOOL list_sources {}"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "list_sources",
                        "description": "List available source plugins.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
        )

        message = response.choices[0].message
        assert message.content is None
        assert response.choices[0].finish_reason == "tool_calls"
        assert message.tool_calls is not None
        assert len(message.tool_calls) == 1
        tool_call = message.tool_calls[0]
        assert tool_call.id == "mock-call-ref-1"
        assert tool_call.function.name == "list_sources"
        assert tool_call.function.arguments == "{}"
        assert json.loads(tool_call.function.arguments) == {}

    @pytest.mark.asyncio
    async def test_text_response_shape(self, gateway_base_url: str) -> None:
        """The non-tool-call companion: ``message.content`` is a plain string."""
        import litellm

        response = await litellm.acompletion(
            model=_MODEL_ALIAS,
            api_base=f"{gateway_base_url}/v1",
            api_key=_INBOUND_BEARER,
            messages=[{"role": "user", "content": "hello gateway"}],
        )

        assert response.choices[0].message.content == "MOCK:hello gateway"
        assert response.choices[0].message.tool_calls is None
        assert response.choices[0].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Text (non-tool) round trip through the real compose loop.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composer_text_round_trip_against_gateway(tmp_path: Path, gateway_base_url: str) -> None:
    """Drive one real compose-loop turn (``_run_one_turn_for_test``, the
    dedicated test-only driver that still exercises the real
    ``_compose_loop`` body) with ``llm=None`` so the REAL ``self._call_llm``
    -> real ``litellm.acompletion`` -> live gateway path runs, end to end,
    for a plain text turn."""
    sessions_service = _build_sessions_service(tmp_path)
    session_id = str(uuid4())
    _insert_session_row(sessions_service, session_id)
    service = _build_service(tmp_path, gateway_base_url, sessions_service)

    result = await service._run_one_turn_for_test(session_id=session_id, message="hello composer-against-gateway")

    # ``raw_assistant_content`` is threaded through only for specific
    # handoff scenarios (interpretation-review repair prose); it is None on
    # this plain terminal path even though the provider's message.content
    # was a real string -- ``assistant_message`` is the field that carries
    # the final rendered text on this path, and IS asserted below.
    assert result.assistant_message == "MOCK:hello composer-against-gateway"
    assert result.tool_outcomes == ()
    assert result.tool_invocations == ()


# ---------------------------------------------------------------------------
# Design acceptance criterion 8 -- the tool-call / tool-result round trip
# through the real compose loop. This is the criterion the whole "endpoint
# affordance" phase exists for, and the one that was blocked by the
# ``_admit_tool_batch`` Protocol-isinstance defect (elspeth-9ea866438b):
# every genuine LiteLLM tool call was rejected at admission with
# "Composer tool batch is missing a provider tool-call ID" before any
# dispatch could happen.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composer_tool_round_trip_against_gateway(tmp_path: Path, gateway_base_url: str) -> None:
    """Provider tool call -> admission -> dispatch -> tool result -> next turn.

    The mock upstream keys on the LAST conversation entry, so this drives a
    genuine two-turn exchange: turn 1's ``USE_TOOL list_sources {}`` yields a
    real provider tool call, Composer admits and dispatches it, and turn 2
    sees the tool result as the last entry and terminates with plain text.

    Nothing here is stubbed on the provider side -- ``llm=None`` means the
    real ``self._call_llm`` -> real ``litellm.acompletion`` -> live gateway
    path runs, so the objects reaching ``_admit_tool_batch`` are genuine
    ``litellm.types.utils.ChatCompletionMessageToolCall`` instances whose
    fields resolve through ``__getattr__``.
    """
    sessions_service = _build_sessions_service(tmp_path)
    session_id = str(uuid4())
    _insert_session_row(sessions_service, session_id)
    service = _build_service(tmp_path, gateway_base_url, sessions_service)

    result = await service._run_one_turn_for_test(session_id=session_id, message="USE_TOOL list_sources {}")

    # The tool call was admitted and dispatched -- the defect made this an
    # AuditIntegrityError before the first await, so an empty tuple here is
    # exactly the regression.
    assert len(result.tool_outcomes) == 1
    outcome = result.tool_outcomes[0]
    assert outcome.call.id == "mock-call-ref-1"
    assert outcome.call.function.name == "list_sources"
    assert outcome.call.function.arguments == "{}"
    assert outcome.error_class is None
    assert outcome.error_message is None
    assert outcome.response is not None
    assert outcome.response.success is True

    # The tool result was fed back and the provider produced a second,
    # terminal turn from it -- i.e. a full round trip, not just a dispatch.
    # The mock upstream echoes the LAST conversation entry, so the terminal
    # text is the serialized ``list_sources`` result: asserting the catalog's
    # own source name survives that trip is what proves the tool RESULT (not
    # merely the tool CALL) crossed the boundary in both directions.
    assert result.assistant_message is not None
    assert result.assistant_message.startswith("MOCK:")
    assert '"success": true' in result.assistant_message
    assert "csv" in result.assistant_message

    # The admitted call is recorded in the audit trail, under the provider's
    # own tool-call id.
    assert len(result.tool_invocations) == 1
    assert result.tool_invocations[0].tool_call_id == "mock-call-ref-1"
    assert result.tool_invocations[0].tool_name == "list_sources"


# ---------------------------------------------------------------------------
# The boot probe against the live gateway. ``probe_composer_config``
# unconditionally sends ``max_tokens=16``, which LiteLLM's ``openai`` path
# translates to the wire field ``max_completion_tokens``; the gateway's
# ``extra="forbid"`` ``ChatRequest`` rejected that as an unknown field, so
# the probe raised ``ComposerBootConfigError`` and ``app.py`` re-raised it --
# the web app could not boot at all against this gateway.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_probe_succeeds_against_gateway(gateway_base_url: str) -> None:
    """Default settings, real probe, live gateway -- boot is not fatal.

    ``ComposerBootConfigError`` is asserted by absence deliberately rather
    than caught: a raise here IS the failure this test exists to catch, and
    letting it propagate names the real error rather than an assertion
    message.
    """
    from elspeth.web.composer.boot_probe import probe_composer_config

    probed = await probe_composer_config(
        model=_MODEL_ALIAS,
        temperature=None,
        seed=None,
        api_base=f"{gateway_base_url}/v1",
        api_key=_INBOUND_BEARER,
    )

    # True (not the transient-failure False) -- the request was accepted and
    # answered, so this proves acceptance rather than a swallowed transport
    # error.
    assert probed is True


@pytest.mark.asyncio
async def test_boot_probe_with_operator_sampling_succeeds_against_gateway(gateway_base_url: str) -> None:
    """The probe's other real payload shape: temperature + seed alongside the
    translated token cap. ``seed`` is a gated capability the reference
    adapter declares, so this also proves the alias did not disturb the
    capability path."""
    from elspeth.web.composer.boot_probe import probe_composer_config

    probed = await probe_composer_config(
        model=_MODEL_ALIAS,
        temperature=0.2,
        seed=7,
        api_base=f"{gateway_base_url}/v1",
        api_key=_INBOUND_BEARER,
    )

    assert probed is True


# ---------------------------------------------------------------------------
# Leak assertions: no gateway/agency internals (the inbound bearer, the
# OAuth client secret, the internal upstream/OAuth hostnames) ever appear in
# persisted chat history, log output, or a raised exception's message.
# ---------------------------------------------------------------------------

_LEAK_MARKERS: tuple[str, ...] = (
    _INBOUND_BEARER,
    _OAUTH_CLIENT_SECRET,
    _OAUTH_HOST,
    _UPSTREAM_HOST,
)


@pytest.mark.asyncio
async def test_gateway_internals_never_leak_on_a_real_failure(
    tmp_path: Path,
    gateway_base_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Force a real gateway-side failure (a genuine HTTP 503 from the mock
    upstream, mapped by the gateway's own error envelope, surfaced to
    LiteLLM as ``ServiceUnavailableError``) and assert none of the
    gateway/agency internals leak into the persisted turn, the exception
    message, or anything logged during the attempt.
    """
    from litellm.exceptions import ServiceUnavailableError

    caplog.set_level(logging.DEBUG)
    sessions_service = _build_sessions_service(tmp_path)
    session_id = str(uuid4())
    _insert_session_row(sessions_service, session_id)
    service = _build_service(tmp_path, gateway_base_url, sessions_service)

    # The exact exception class the mock upstream's 503 ("overloaded")
    # deterministically maps to, confirmed empirically. Narrowed
    # deliberately (not `Exception`): an unrelated failure elsewhere in the
    # turn must not satisfy this `raises` and cause the leak assertions
    # below to scan the wrong exception's message and pass vacuously.
    with pytest.raises(ServiceUnavailableError) as excinfo:
        await service._run_one_turn_for_test(session_id=session_id, message="TRIGGER_FAULT overloaded")

    exception_text = str(excinfo.value)
    for marker in _LEAK_MARKERS:
        assert marker not in exception_text, f"leak marker found in exception message: {marker!r}"
        assert marker not in caplog.text, f"leak marker found in captured logs: {marker!r}"

    with sessions_service._engine.begin() as conn:
        rows = conn.execute(select(chat_messages_table.c.content).where(chat_messages_table.c.session_id == session_id)).fetchall()
    persisted_content = "\n".join(row.content or "" for row in rows)
    for marker in _LEAK_MARKERS:
        assert marker not in persisted_content, f"leak marker found in persisted chat_messages content: {marker!r}"


# ---------------------------------------------------------------------------
# Advisor-role independence: pointing the PRIMARY role at a real, live
# gateway must not leak into an unconfigured advisor call's kwargs. Driving
# a live advisor call is disproportionate to what this asserts (the kwargs
# construction is exactly what Task 2 already unit-tests exhaustively); the
# brief explicitly permits asserting this at the kwargs level, so this test
# monkeypatches only the wire call while still constructing the service
# against the real, running gateway for the primary role.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisor_role_does_not_use_primary_gateway_endpoint(
    tmp_path: Path,
    gateway_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_service = _build_sessions_service(tmp_path)
    service = _build_service(tmp_path, gateway_base_url, sessions_service)

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        message = type("Message", (), {"tool_calls": None, "content": "advice"})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()

    monkeypatch.setattr(composer_service_module, "_litellm_acompletion", fake_acompletion)

    await service._call_advisor_with_audit(
        {
            "trigger": "reactive",
            "problem_summary": "stuck",
            "recent_errors": [],
            "attempted_actions": [],
        },
        recorder=None,
    )

    assert "api_base" not in captured
    assert "api_key" not in captured
