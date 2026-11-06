"""Offline capacity and clarification contracts; no historical attachment replay."""

from decimal import Decimal

import pytest

from elspeth.web import config as web_config
from elspeth.web.config import WebSettings


def test_deployed_capacity_environment_override_retains_transport_cost_and_repair_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "COMPOSER_MAX_COMPOSITION_TURNS": "15",
        "COMPOSER_MAX_DISCOVERY_TURNS": "10",
        "COMPOSER_RATE_LIMIT_PER_MINUTE": "10",
        "SHAREABLE_LINK_SIGNING_KEY": "0" * 64,
        "COMPOSER_TIMEOUT_SECONDS": "900",
        "COMPOSER_PLANNER_MAX_COMPLETION_TOKENS": "64000",
        "COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS": "120",
        "COMPOSER_TRANSPORT_HEADROOM_SECONDS": "30",
        "COMPOSER_PLANNER_MAX_CUMULATIVE_PROVIDER_COST": "5.00",
        "COMPOSER_PLANNER_REPAIR_BUDGET": "2",
    }
    for suffix, value in values.items():
        monkeypatch.setenv(f"ELSPETH_WEB__{suffix}", value)

    settings = web_config.settings_from_env()

    assert settings.composer_timeout_seconds == 900
    assert settings.composer_planner_max_completion_tokens == 64000
    assert settings.composer_sync_timeout_seconds == 90
    assert settings.composer_transport_idle_ceiling_seconds == 120
    assert settings.composer_planner_max_cumulative_provider_cost == Decimal("5.00")
    assert settings.composer_planner_repair_budget == 2

    defaults = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=300,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )
    assert defaults.composer_planner_max_completion_tokens == 16384
    assert defaults.composer_planner_repair_budget == 2
    assert defaults.composer_planner_max_cumulative_provider_cost == Decimal("5.00")
