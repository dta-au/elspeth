"""Fixtures for the gateway conformance kit.

This file carries its own ``sys.path`` shim (first lines below) so the kit
can run as a bare ``pytest conformance`` invocation, standalone, against a
derived image that has never been ``pip install``-ed -- it must never rely
on an outer conftest for imports. Every other file in this package talks to
the gateway over HTTP only; this is the one file that imports gateway
internals, and only to build fixtures.

Two modes, selected by the ``GATEWAY_CONFORMANCE_URL`` environment variable:

- **Image-qualification mode** (``GATEWAY_CONFORMANCE_URL`` set): the
  ``gateway_client`` fixture is a plain ``httpx.AsyncClient`` pointed at that
  base URL. The deployed gateway is assumed to already be wired to a
  reachable instance of this repo's mock OAuth + mock upstream stack (see
  ``mock/stack.py``) with the ``reference_v1_invoke`` adapter -- the
  conformance kit exercises that fixed, deterministic pair, not a live
  agency. Override ``GATEWAY_CONFORMANCE_BEARER`` /
  ``GATEWAY_CONFORMANCE_MODEL_ALIAS`` if that deployment's inbound bearer or
  model alias differ from this module's in-process defaults.
- **In-process mode** (default, no env var set): this fixture builds the
  mock OAuth app and the mock upstream app in-process, and dispatches to
  them with one custom ``httpx.AsyncBaseTransport`` that routes by request
  URL host to two ``httpx.ASGITransport`` instances. ``create_app``'s single
  ``upstream_client`` parameter serves *both* the OAuth ``TokenManager`` and
  the ``UpstreamClient`` -- one client wired to the router transport is what
  makes this mode work with a single call to ``create_app``. The gateway
  app itself is then wrapped in its own ``httpx.ASGITransport`` for the test
  client that every test actually talks to.
"""

import json
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

try:
    import elspeth_llm_gateway  # noqa: F401
except ImportError:
    _GATEWAY_SRC = Path(__file__).resolve().parents[1] / "src"
    if str(_GATEWAY_SRC) not in sys.path:
        sys.path.insert(0, str(_GATEWAY_SRC))

from elspeth_llm_gateway.core.app import create_app
from elspeth_llm_gateway.core.config import load_config
from elspeth_llm_gateway.reference.adapter import ReferenceV1InvokeAdapter
from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor
from elspeth_llm_gateway.sdk.types import Capability

GATEWAY_CONFORMANCE_URL_ENV = "GATEWAY_CONFORMANCE_URL"
GATEWAY_CONFORMANCE_BEARER_ENV = "GATEWAY_CONFORMANCE_BEARER"
GATEWAY_CONFORMANCE_MODEL_ALIAS_ENV = "GATEWAY_CONFORMANCE_MODEL_ALIAS"

# Development-only fixed values for the in-process mock stack this conftest
# builds itself -- never used against a real credential store or a real
# agency. Fixed (not random) so a conformance run is reproducible.
_INBOUND_BEARER = "conformance-inbound-bearer-0123456789ab"  # secret-scan: allow-this-line
_OAUTH_CLIENT_ID = "conformance-client"
_OAUTH_CLIENT_SECRET = "conformance-oauth-client-secret-0123456789ab"  # secret-scan: allow-this-line
_MODEL_ALIAS = "conformance-model"
_MODEL_TARGET = "conformance-target"
_OAUTH_HOST = "oauth.mock"
_UPSTREAM_HOST = "upstream.mock"


def _build_in_process_env() -> dict[str, str]:
    return {
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
        "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": json.dumps({_MODEL_ALIAS: {"target": _MODEL_TARGET}}),
    }


class _TextOnlyAdapter:
    """``ReferenceV1InvokeAdapter``, but declaring only ``Capability.TEXT``.

    Conftest-local wiring, not a "gateway internals" import from a test file
    -- this module already imports ``create_app``/``load_config`` to build
    fixtures. This adapter exists purely so the conformance kit can assert
    the ``capability_unsupported`` envelope shape in-process: the real
    reference adapter declares every gated capability, which makes that
    error otherwise unreachable via HTTP (see
    ``test_errors.py::test_capability_unsupported_envelope_shape``).
    Everything except ``descriptor()`` delegates straight through.
    """

    def __init__(self) -> None:
        self._delegate = ReferenceV1InvokeAdapter()

    def descriptor(self) -> AdapterDescriptor:
        real = self._delegate.descriptor()
        return AdapterDescriptor(
            name=real.name,
            version=real.version,
            adapter_api_major=real.adapter_api_major,
            capabilities=frozenset({Capability.TEXT}),
        )

    def validate_configuration(self, options: dict) -> None:
        return self._delegate.validate_configuration(options)

    def build_invoke(self, request):
        return self._delegate.build_invoke(request)

    def parse_success(self, body):
        return self._delegate.parse_success(body)

    def classify_error(self, failure):
        return self._delegate.classify_error(failure)


class _HostRoutedTransport(httpx.AsyncBaseTransport):
    """Dispatches an outbound request to one of several transports by URL host.

    This is the seam that makes in-process mode work with a single
    ``upstream_client``: the gateway's ``TokenManager`` posts to
    ``oauth_token_url`` and its ``UpstreamClient`` posts to
    ``upstream_origin`` -- two different hosts, both reachable through this
    one router transport, each proxied to its own ``httpx.ASGITransport``.
    """

    def __init__(self, routes: dict[str, httpx.AsyncBaseTransport]) -> None:
        self._routes = routes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        transport = self._routes.get(host)
        if transport is None:
            raise RuntimeError(f"conformance router transport: no route registered for host {host!r}")
        return await transport.handle_async_request(request)


@asynccontextmanager
async def _in_process_gateway_client(*, base_url: str, adapter=None) -> AsyncIterator[httpx.AsyncClient]:
    """Build one full in-process mock stack + gateway app, wrapped in a client.

    Shared by both ``gateway_client`` (the real reference adapter) and
    ``limited_gateway_client`` (the capability-limited one below) -- each
    call builds its own independent mock OAuth/upstream pair and gateway
    app, so the two never share state.
    """
    from mock.oauth import create_mock_oauth_app
    from mock.upstream import create_mock_upstream_app

    oauth_app = create_mock_oauth_app(client_id=_OAUTH_CLIENT_ID, client_secret=_OAUTH_CLIENT_SECRET)
    upstream_app = create_mock_upstream_app()
    router_transport = _HostRoutedTransport(
        {
            _OAUTH_HOST: httpx.ASGITransport(app=oauth_app),
            _UPSTREAM_HOST: httpx.ASGITransport(app=upstream_app),
        }
    )

    async with httpx.AsyncClient(transport=router_transport) as upstream_client:
        config = load_config(_build_in_process_env())
        gateway_app = create_app(config, adapter=adapter, upstream_client=upstream_client)
        gateway_transport = httpx.ASGITransport(app=gateway_app)
        async with httpx.AsyncClient(transport=gateway_transport, base_url=base_url) as client:
            yield client


@pytest.fixture
async def gateway_client() -> AsyncIterator[httpx.AsyncClient]:
    """An ``httpx.AsyncClient`` talking to the gateway under test.

    Image-qualification mode when ``GATEWAY_CONFORMANCE_URL`` is set;
    otherwise builds the whole mock stack in-process (see module docstring).
    """
    external_url = os.environ.get(GATEWAY_CONFORMANCE_URL_ENV)
    if external_url:
        async with httpx.AsyncClient(base_url=external_url) as client:
            yield client
        return

    async with _in_process_gateway_client(base_url="http://gateway.conformance") as client:
        yield client


@pytest.fixture
async def limited_gateway_client() -> AsyncIterator[httpx.AsyncClient]:
    """A second, independent gateway app built from ``_TextOnlyAdapter``.

    In-process only: a derived image in image-qualification mode ships
    whatever adapter it ships, and this fixture cannot spin up a second
    variant of a real deployment -- it skips there rather than pointing at
    the same (possibly fully-capable) deployment under a different name.
    """
    if os.environ.get(GATEWAY_CONFORMANCE_URL_ENV):
        pytest.skip("limited_gateway_client is in-process only; GATEWAY_CONFORMANCE_URL points at a real deployment")

    async with _in_process_gateway_client(base_url="http://gateway.conformance.limited", adapter=_TextOnlyAdapter()) as client:
        yield client


@pytest.fixture
def bearer() -> str:
    """The inbound bearer token the gateway under test expects."""
    return os.environ.get(GATEWAY_CONFORMANCE_BEARER_ENV) or _INBOUND_BEARER


@pytest.fixture
def model_alias() -> str:
    """A model alias the gateway under test has mapped to an upstream target."""
    return os.environ.get(GATEWAY_CONFORMANCE_MODEL_ALIAS_ENV) or _MODEL_ALIAS


@pytest.fixture
async def declared_capabilities(gateway_client: httpx.AsyncClient) -> frozenset[str]:
    """The capability names the target adapter declares, parsed from ``/readyz``.

    ``/readyz`` requires no auth, so this never depends on ``bearer``.
    """
    response = await gateway_client.get("/readyz")
    assert response.status_code in (200, 503), f"unexpected /readyz status: {response.status_code}"
    return frozenset(response.json()["capabilities"])


@pytest.fixture
def chat_headers(bearer: str) -> dict[str, str]:
    """The minimal valid header set for ``POST /v1/chat/completions``."""
    return {
        "Authorization": f"Bearer {bearer}",
        "X-ELSPETH-LLM-Gateway-Contract": "1",
        "Content-Type": "application/json",
    }


@pytest.fixture
def chat_body_factory(model_alias: str) -> Callable[..., dict]:
    """A factory for a minimal valid chat-completion request body."""

    def _factory(content: str, **extra) -> dict:
        return {"model": model_alias, "messages": [{"role": "user", "content": content}], **extra}

    return _factory
