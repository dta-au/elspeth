"""The composer's per-route tool-contract resolution (S1 T8, D20, D24).

``resolve_composer_tool_contract(settings, *, env)`` resolves the planner
route (``composer_model`` + ``composer_endpoint_base_url``) and the
escape-hatch route (``composer_advisor_model`` +
``composer_advisor_endpoint_base_url``) under ``composer_strict_tools`` and
returns one owned, frozen contract. ``ComposerServiceImpl.__init__`` and the
boot probe both call it, so the service's sent lists and the probe's lists
come from one resolution.

The resolver reads ``OPENAI_BASE_URL``, ``OPENAI_API_BASE``,
``OPENROUTER_API_BASE`` and ``AZURE_API_VERSION``; the unit conftest scrubs
them, and the env-dependent tests here set them with ``monkeypatch``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.boot_probe import build_composer_probe_requests
from elspeth.web.composer.service import ComposerServiceImpl, composer_loop_tool_definitions
from elspeth.web.composer.strict_transport import (
    StrictTransport,
    StrictTransportDiagnostic,
    resolve_composer_tool_contract,
)
from elspeth.web.config import WebSettings
from tests.unit.web.composer._helpers import _mock_catalog

_OPENROUTER_PLANNER = "openrouter/deepseek/deepseek-v4.1-flash"
_OPENROUTER_ADVISOR = "openrouter/z-ai/glm-5.3"
_STRICT = ToolContractDialect.OPENAI_STRICT
_NONE = ToolContractDialect.NONE


def _settings(**overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": Path("/data"),
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


def _service(settings: WebSettings) -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)


# ---------------------------------------------------------------- the two-route helper


@pytest.mark.parametrize(
    ("overrides", "planner", "hatch"),
    [
        ({}, (StrictTransport.NONE, _NONE), (StrictTransport.NONE, _NONE)),
        (
            {"composer_model": _OPENROUTER_PLANNER, "composer_advisor_model": _OPENROUTER_ADVISOR},
            (StrictTransport.FORWARDING, _STRICT),
            (StrictTransport.FORWARDING, _STRICT),
        ),
        (
            {"composer_model": _OPENROUTER_PLANNER, "composer_advisor_model": _OPENROUTER_ADVISOR, "composer_strict_tools": "off"},
            (StrictTransport.NONE, _NONE),
            (StrictTransport.NONE, _NONE),
        ),
        (
            {
                "composer_model": _OPENROUTER_PLANNER,
                "composer_advisor_model": _OPENROUTER_ADVISOR,
                "composer_advisor_endpoint_base_url": "https://proxy.example/v1",
                "composer_advisor_endpoint_api_key": "advisor-proxy-key",  # secret-scan: allow-this-line
            },
            (StrictTransport.FORWARDING, _STRICT),
            (StrictTransport.NONE, _NONE),
        ),
        (
            {
                "composer_model": _OPENROUTER_PLANNER,
                "composer_endpoint_base_url": "https://proxy.example/v1",
                "composer_endpoint_api_key": "planner-proxy-key",  # secret-scan: allow-this-line
                "composer_advisor_model": _OPENROUTER_ADVISOR,
            },
            (StrictTransport.NONE, _NONE),
            (StrictTransport.FORWARDING, _STRICT),
        ),
        ({"composer_strict_tools": "forward_to_endpoint"}, (StrictTransport.ENFORCING, _STRICT), (StrictTransport.NONE, _NONE)),
    ],
    ids=["defaults", "openrouter_both", "off", "proxy_hatch", "proxy_planner", "forward_hosted_openai"],
)
def test_each_route_resolves_from_its_own_model_and_endpoint(
    overrides: dict[str, Any],
    planner: tuple[StrictTransport, ToolContractDialect],
    hatch: tuple[StrictTransport, ToolContractDialect],
) -> None:
    settings = _settings(**overrides)
    contract = resolve_composer_tool_contract(settings, env={})

    assert contract.setting == settings.composer_strict_tools
    assert (contract.planner.resolution.transport, contract.planner.dialect) == planner
    assert (contract.hatch.resolution.transport, contract.hatch.dialect) == hatch
    assert contract.planner.resolution.diagnostic is None
    assert contract.hatch.resolution.diagnostic is None


# ---------------------------------------------------------------- the service's one resolution point


@pytest.mark.parametrize(
    ("overrides", "dialect", "strict_true"),
    [
        ({"composer_model": _OPENROUTER_PLANNER}, _STRICT, 32),
        ({"composer_model": _OPENROUTER_PLANNER, "composer_strict_tools": "off"}, _NONE, 0),
        ({}, _NONE, 0),
        ({"composer_strict_tools": "forward_to_endpoint"}, _STRICT, 32),
    ],
    ids=["openrouter", "openrouter_off", "defaults", "forward_hosted_openai"],
)
def test_service_resolves_its_dialects_and_summary_from_settings(
    overrides: dict[str, Any], dialect: ToolContractDialect, strict_true: int
) -> None:
    settings = _settings(**overrides)
    service = _service(settings)
    summary = service.tool_contract_summary

    assert summary.contract == resolve_composer_tool_contract(settings, env={})
    assert service._planner_dialect is summary.contract.planner.dialect is dialect
    assert service._hatch_dialect is summary.contract.hatch.dialect
    assert (summary.loop_strict_tool_count, summary.loop_tool_count) == (strict_true, 42)
    sent = composer_loop_tool_definitions(service._planner_dialect)
    assert sum(1 for tool in sent if "strict" in tool["function"] and tool["function"]["strict"] is True) == strict_true


def test_resolution_event_carries_closed_values_and_counts_only() -> None:
    with capture_logs() as logs:
        _service(_settings(composer_model=_OPENROUTER_PLANNER, composer_advisor_model=_OPENROUTER_ADVISOR))

    [event] = [entry for entry in logs if entry["event"] == "composer_tool_contract_resolved"]
    assert {key: event[key] for key in event if key not in {"event", "log_level"}} == {
        "setting": "preferred",
        "planner_transport": "forwarding",
        "planner_dialect": "openai_strict",
        "planner_diagnostic": None,
        "hatch_transport": "forwarding",
        "hatch_dialect": "openai_strict",
        "hatch_diagnostic": None,
        "loop_strict_tool_count": 32,
        "loop_tool_count": 42,
    }


# ---------------------------------------------------------------- env hermeticity (the scrub's control)


@pytest.mark.parametrize(
    ("proxy_env", "transport", "dialect"),
    [(True, StrictTransport.NONE, _NONE), (False, StrictTransport.FORWARDING, _STRICT)],
    ids=["openrouter_api_base_set", "unset"],
)
def test_openrouter_api_base_in_env_moves_the_service_route(
    monkeypatch: pytest.MonkeyPatch, proxy_env: bool, transport: StrictTransport, dialect: ToolContractDialect
) -> None:
    if proxy_env:
        monkeypatch.setenv("OPENROUTER_API_BASE", "https://proxy.example/v1")
    summary = _service(_settings(composer_model=_OPENROUTER_PLANNER)).tool_contract_summary

    assert summary.contract.planner.resolution.transport is transport
    assert summary.contract.planner.dialect is dialect


@pytest.mark.parametrize(
    ("gateway_env", "transport"),
    [(True, StrictTransport.FORWARDING), (False, StrictTransport.ENFORCING)],
    ids=["openai_base_url_set", "unset"],
)
def test_openai_base_url_in_env_moves_the_service_route_under_forward_to_endpoint(
    monkeypatch: pytest.MonkeyPatch, gateway_env: bool, transport: StrictTransport
) -> None:
    if gateway_env:
        monkeypatch.setenv("OPENAI_BASE_URL", "https://gw.example/v1")
    summary = _service(_settings(composer_strict_tools="forward_to_endpoint")).tool_contract_summary

    assert summary.contract.planner.resolution.transport is transport


# ---------------------------------------------------------------- malformed env (D24, Codex finding 1)


def test_a_malformed_env_base_never_stops_construction_and_is_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: remove the resolver's ``ValueError`` catch and construction raises ``ValueError``."""
    monkeypatch.setenv("OPENROUTER_API_BASE", "http://[")
    settings = _settings(composer_boot_probe_enabled=False, composer_model=_OPENROUTER_PLANNER, composer_advisor_model=_OPENROUTER_ADVISOR)

    with capture_logs() as logs:
        service = _service(settings)

    contract = service.tool_contract_summary.contract
    for route in (contract.planner, contract.hatch):
        assert route.resolution.transport is StrictTransport.NONE
        assert route.resolution.diagnostic is StrictTransportDiagnostic.UNPARSEABLE_OPENROUTER_API_BASE
        assert route.dialect is _NONE
    assert all("strict" not in tool["function"] for tool in composer_loop_tool_definitions(service._planner_dialect))
    [event] = [entry for entry in logs if entry["event"] == "composer_tool_contract_resolved"]
    assert event["planner_diagnostic"] == event["hatch_diagnostic"] == "unparseable_openrouter_api_base"
    assert "http://[" not in repr(logs)


def test_a_malformed_env_base_never_stops_the_probe_builder() -> None:
    settings = _settings(composer_model=_OPENROUTER_PLANNER, composer_advisor_model=_OPENROUTER_ADVISOR)

    requests = build_composer_probe_requests(settings, env={"OPENROUTER_API_BASE": "http://["})

    assert [request.surface for request in requests] == ["loop_tools", "planner_tools", "advisor"]
    for request in requests[:2]:
        assert request.strict_true_count == request.strict_false_count == 0
