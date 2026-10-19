"""Forwarding trust is explicit and preserves real quota client addresses."""

from __future__ import annotations

import os

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from elspeth.web.config import WebSettings, settings_from_env, trusted_proxy_hosts_from_env


def _settings(hosts: tuple[str, ...]) -> WebSettings:
    return WebSettings(
        trusted_proxy_hosts=hosts,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"x" * 32,
    )


@pytest.mark.parametrize("peer", ["172.30.85.1", "127.0.0.1", "::1", "10.20.0.0/24", "2001:db8::/64"])
def test_numeric_peers_and_explicit_networks_are_accepted(peer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__TRUSTED_PROXY_HOSTS", f'["{peer}"]')
    assert _settings((peer,)).trusted_proxy_hosts == (peer,)
    assert trusted_proxy_hosts_from_env() == (peer,)


@pytest.mark.parametrize("peer", ["*", "0.0.0.0/0", "::/0", "0.0.0.0", "::", "localhost", "10.20.0.1/24", "127.0.0.1 ", "fe80::1%eth0"])
def test_ambiguous_or_arbitrary_forwarding_trust_is_rejected(peer: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__TRUSTED_PROXY_HOSTS", f'["{peer}"]')
    with pytest.raises(ValidationError):
        _settings((peer,))
    with pytest.raises(ValueError):
        trusted_proxy_hosts_from_env()


@pytest.mark.parametrize("raw", ["127.0.0.1", '"127.0.0.1"', "null", "{}", "[1]", "[true]"])
def test_transport_parser_requires_json_string_array(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__TRUSTED_PROXY_HOSTS", raw)
    with pytest.raises(ValueError):
        trusted_proxy_hosts_from_env()


def test_forwarding_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_WEB__TRUSTED_PROXY_HOSTS", raising=False)
    assert trusted_proxy_hosts_from_env() == ()
    assert _settings(()).trusted_proxy_hosts == ()


def test_application_and_transport_parse_the_same_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("ELSPETH_WEB__"):
            monkeypatch.delenv(name)
    required = {
        "COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "COMPOSER_TIMEOUT_SECONDS": "85.0",
        "COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
        "TRUSTED_PROXY_HOSTS": '["172.30.85.1", "0:0:0:0:0:0:0:1"]',
    }
    for name, value in required.items():
        monkeypatch.setenv(f"ELSPETH_WEB__{name}", value)
    assert settings_from_env().trusted_proxy_hosts == trusted_proxy_hosts_from_env() == ("172.30.85.1", "::1")


async def _client_address(request: Request) -> JSONResponse:
    assert request.client is not None
    return JSONResponse({"client": request.client.host, "scheme": request.url.scheme})


@pytest.mark.parametrize("peer", ["172.30.85.1", "172.30.85.20", "198.51.100.40"])
@pytest.mark.asyncio
async def test_only_configured_gateway_can_supply_distinct_client_addresses(peer: str) -> None:
    application = Starlette(routes=[Route("/", _client_address)])
    proxy = ProxyHeadersMiddleware(application, trusted_hosts=list(_settings(("172.30.85.1",)).trusted_proxy_hosts))
    transport = httpx.ASGITransport(app=proxy, client=(peer, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for address in ("198.51.100.10", "198.51.100.11"):
            response = await client.get("/", headers={"X-Forwarded-For": address, "X-Forwarded-Proto": "https"})
            assert response.json() == {
                "client": address if peer == "172.30.85.1" else peer,
                "scheme": "https" if peer == "172.30.85.1" else "http",
            }
