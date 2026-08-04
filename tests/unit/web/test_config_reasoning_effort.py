"""Reasoning-effort settings knobs (elspeth-dc459d438e)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.web.config import WebSettings


def _settings(**overrides: object) -> WebSettings:
    kwargs: dict[str, object] = {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    kwargs.update(overrides)
    return WebSettings(**kwargs)


def test_reasoning_effort_defaults_are_low_high_medium() -> None:
    settings = _settings()
    assert settings.composer_discovery_reasoning_effort == "low"
    assert settings.composer_candidate_reasoning_effort == "high"
    assert settings.composer_advisor_reasoning_effort == "medium"


def test_reasoning_effort_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        _settings(composer_discovery_reasoning_effort="extreme")


def test_none_is_a_valid_opt_out() -> None:
    settings = _settings(composer_candidate_reasoning_effort="none")
    assert settings.composer_candidate_reasoning_effort == "none"


def test_advisor_completion_budget_default_fits_thinking() -> None:
    """Anthropic thinking budgets have a 1024-token floor that must fit
    INSIDE max_tokens; the old 1500 default left medium-effort advisor
    calls with an illegal or starved thinking budget."""
    assert _settings().composer_advisor_max_completion_tokens == 8192
