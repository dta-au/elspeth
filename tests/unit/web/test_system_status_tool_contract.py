"""``/api/system/status`` publishes only route-independent strict-tool facts (S1 T10).

Ruling 2 (D14, Codex finding 2): the unauthenticated status body carries the
``composer_strict_tools`` setting and two properties of the tool set (the
strict-capable count and the loop tool count), never a route's transport,
its effective strict count or a probe outcome, because those reveal whether a
custom endpoint is configured and whether a provider was reachable at boot.
Per-surface boot outcomes go to the ``composer_boot_probe_outcome`` log event.

Plan: ``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`` T10.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from structlog.testing import capture_logs

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.app import create_app, lifespan
from elspeth.web.composer.boot_probe import ComposerBootConfigError, ComposerProbeRequest
from elspeth.web.composer.service import ComposerServiceImpl
from tests.unit.web.test_app import _RecordingCounter, _RecordingHistogram, _settings, _StaticAsyncClient

_OPENROUTER_PLANNER = "openrouter/deepseek/deepseek-v4.1-flash"
_OPENROUTER_ADVISOR = "openrouter/z-ai/glm-5.3"
_PROXY = "https://proxy.example/v1"
_KEY_CANARY = "sk-or-status-canary-5Hq2"


def _tool_contract(app: FastAPI) -> dict[str, Any]:
    response = TestClient(app).get("/api/system/status")
    assert response.status_code == 200
    body = response.json()
    value = body["composer_tool_contract"]
    assert type(value) is dict
    return value


def _service(app: FastAPI) -> ComposerServiceImpl:
    service = app.state.composer_service
    assert type(service) is ComposerServiceImpl
    return service


def test_default_settings_publish_the_setting_and_the_tool_set_counts(tmp_path: Path) -> None:
    assert _tool_contract(create_app(_settings(tmp_path))) == {"setting": "preferred", "strict_capable_tool_count": 32, "tool_count": 42}


def test_off_publishes_the_same_counts(tmp_path: Path) -> None:
    assert _tool_contract(create_app(_settings(tmp_path, composer_strict_tools="off"))) == {
        "setting": "off",
        "strict_capable_tool_count": 32,
        "tool_count": 42,
    }


def test_planner_route_is_not_disclosed(tmp_path: Path) -> None:
    """Row 4 (OpenRouter host, effective 32) and row 5 (custom base, effective 0) publish identical values."""
    hosted = create_app(_settings(tmp_path / "a", composer_model=_OPENROUTER_PLANNER))
    proxied = create_app(
        _settings(
            tmp_path / "b",
            composer_model=_OPENROUTER_PLANNER,
            composer_endpoint_base_url=_PROXY,
            composer_endpoint_api_key=SecretStr(_KEY_CANARY),
        )
    )
    # Not vacuous: the two configs really do send different strict counts.
    assert _service(hosted).tool_contract_summary.loop_strict_tool_count == 32
    assert _service(proxied).tool_contract_summary.loop_strict_tool_count == 0

    assert repr(_tool_contract(hosted)) == repr(_tool_contract(proxied))


def test_hatch_route_is_not_disclosed(tmp_path: Path) -> None:
    """The same pin on the advisor (escape-hatch) route: row 4 versus row 5."""
    hosted = create_app(_settings(tmp_path / "a", composer_advisor_model=_OPENROUTER_ADVISOR))
    proxied = create_app(
        _settings(
            tmp_path / "b",
            composer_advisor_model=_OPENROUTER_ADVISOR,
            composer_advisor_endpoint_base_url=_PROXY,
            composer_advisor_endpoint_api_key=SecretStr(_KEY_CANARY),
        )
    )
    assert _service(hosted).tool_contract_summary.contract.hatch.dialect is ToolContractDialect.OPENAI_STRICT
    assert _service(proxied).tool_contract_summary.contract.hatch.dialect is ToolContractDialect.NONE

    assert repr(_tool_contract(hosted)) == repr(_tool_contract(proxied))


def _openrouter_pair(tmp_path: Path, **overrides: Any) -> FastAPI:
    return create_app(
        _settings(
            tmp_path,
            composer_model=_OPENROUTER_PLANNER,
            composer_advisor_model=_OPENROUTER_ADVISOR,
            composer_advisor_endpoint_base_url="https://openrouter.ai/api/v1",
            composer_advisor_endpoint_api_key=SecretStr(_KEY_CANARY),
            **overrides,
        )
    )


async def _boot(app: FastAPI, monkeypatch: pytest.MonkeyPatch, *, rejected_surface: str | None) -> list[dict[str, Any]]:
    sent: list[str] = []

    async def _probe(request: ComposerProbeRequest) -> bool:
        sent.append(request.surface)
        if request.surface == rejected_surface:
            raise ComposerBootConfigError(f"composer {request.role} boot request rejected by {request.model}: surface={request.surface}")
        return True

    monkeypatch.setattr("elspeth.web.composer.boot_probe.probe_composer_config", _probe)
    monkeypatch.setattr("elspeth.web.app._COMPOSER_BOOT_CONFIG_COUNTER", _RecordingCounter())
    monkeypatch.setattr("elspeth.web.app._COMPOSER_BOOT_CONFIG_PROBE_LATENCY", _RecordingHistogram())
    with capture_logs() as logs, patch("httpx.AsyncClient", return_value=_StaticAsyncClient([])):
        async with lifespan(app):
            pass
    assert sent == ["loop_tools", "planner_tools", "hatch_terminal", "advisor"]
    return logs


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected_surface", [None, "hatch_terminal"], ids=["all-accepted", "hatch-rejected"])
async def test_probe_outcomes_do_not_change_the_published_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rejected_surface: str | None
) -> None:
    unprobed = _tool_contract(_openrouter_pair(tmp_path / "off"))
    app = _openrouter_pair(tmp_path / "on", composer_boot_probe_enabled=True)

    await _boot(app, monkeypatch, rejected_surface=rejected_surface)

    assert repr(_tool_contract(app)) == repr(unprobed)


@pytest.mark.asyncio
async def test_each_sent_surface_logs_one_owned_boot_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _openrouter_pair(tmp_path, composer_boot_probe_enabled=True)

    logs = await _boot(app, monkeypatch, rejected_surface="hatch_terminal")

    outcomes = [entry for entry in logs if entry["event"] == "composer_boot_probe_outcome"]
    assert [(entry["probed_surface"], entry["probed_role"], entry["probe_status"]) for entry in outcomes] == [
        ("loop_tools", "planner", "success"),
        ("planner_tools", "planner", "success"),
        ("hatch_terminal", "planner", "rejected"),
        ("advisor", "advisor", "success"),
    ]
    counts = {
        entry["probed_surface"]: (entry["tool_count"], entry["strict_true_count"], entry["strict_false_count"], entry["strict_key_omitted"])
        for entry in outcomes
    }
    assert counts["loop_tools"] == (42, 32, 10, 0)
    assert counts["hatch_terminal"] == (1, 0, 1, 0)
    assert counts["advisor"] == (0, 0, 0, 0)
    rendered = repr(outcomes)
    assert _KEY_CANARY not in rendered
    assert "openrouter.ai" not in rendered
    assert "https://" not in rendered
