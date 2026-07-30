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

Central criterion 8 (tool-call / tool-result round trip) is NOT included as
a passing test here. Driving it exposed a real, pre-existing defect in
``elspeth.web.composer.tool_batch._admit_tool_batch`` -- see
``.superpowers/sdd/2026-07-31-llm-gateway-phase3-endpoint-affordance/task-3-report.md``
for the full reproduction and evidence. Per this phase's own constraint
("If a genuine defect in Phase 1/2/3 surfaces, STOP and report BLOCKED...
Do NOT weaken an assertion to make it pass"), that test is not landed here;
a landed red/xfail test would also muddy the release-branch full-suite gate
this phase does not own.

A second, independent genuine defect surfaced while probing the real boot
probe against the live gateway (``elspeth.web.composer.boot_probe``, which
unconditionally sends ``max_tokens=16``): LiteLLM's ``openai`` provider path
translates any caller-supplied ``max_tokens`` into the wire field
``max_completion_tokens`` (confirmed empirically -- see the task report),
which the gateway's ``ChatRequest`` (``extra="forbid"``, no
``max_completion_tokens`` field) rejects as an unrecognised field. This
affects the boot probe (always) and the advisor role's real calls (which
also set ``max_tokens`` from ``composer_advisor_max_completion_tokens``)
whenever either is pointed at the gateway -- it does NOT affect the
primary role's ordinary compose-loop turns, confirmed empirically:
``_call_llm``/``_call_text_llm`` never set ``max_tokens``, which is why the
text round trip below passes. A boot-probe-against-the-gateway test is
therefore also not landed here (same red-test discipline); see the task
report for the full reproduction. What IS proven here, durably:

- The gateway's tool-call response shape satisfies the freeform loop's HARD
  attribute access (``response.choices[0].message.tool_calls[i].id`` /
  ``.function.name`` / ``.function.arguments``, ``finish_reason``) when
  consumed via a real ``litellm.acompletion`` call -- i.e. the gateway side
  of criterion 8 is NOT the blocker.
- A real text (non-tool) round trip through Composer's actual compose loop
  against the live gateway.
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
# Step 2 -- the durable response-shape proof (design acceptance criterion 8,
# gateway side). Drives a real ``litellm.acompletion`` call -- not the
# low-level GatewayLLMProvider, not a hand-rolled HTTP client -- against the
# live gateway, and inspects the exact HARD attribute-access chain the
# freeform loop uses (service.py:3112-3117, tool_batch.py's
# ``_ProviderToolFunctionMetadata``/consumption of ``tc.id`` /
# ``tc.function.name`` / ``tc.function.arguments``).
# ---------------------------------------------------------------------------


class TestGatewayResponseShapeSatisfiesFreeformAttributeAccess:
    @pytest.mark.asyncio
    async def test_tool_call_response_shape(self, gateway_base_url: str) -> None:
        """USE_TOOL trigger -> real litellm.acompletion -> dotted attribute
        access on tool_calls, exactly as the freeform loop performs it.

        This is the definitive answer to the brief's Step 2 question: the
        GATEWAY's response shape is fully consumable via
        ``response.choices[0].message.tool_calls[i].id`` /
        ``.function.name`` / ``.function.arguments`` -- no dict-key access,
        no gateway-side fix needed. (The separate, blocking defect this
        task surfaced lives in Composer's OWN tool-batch admission code,
        not here -- see the module docstring and the task report.)
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
    caplog.set_level(logging.DEBUG)
    sessions_service = _build_sessions_service(tmp_path)
    session_id = str(uuid4())
    _insert_session_row(sessions_service, session_id)
    service = _build_service(tmp_path, gateway_base_url, sessions_service)

    with pytest.raises(Exception) as excinfo:
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
