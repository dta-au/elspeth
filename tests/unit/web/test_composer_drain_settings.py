"""The declared infrastructure grace bounds both prepared drain budgets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.web import config as web_config
from tests.unit.web.test_config import _settings, required_web_env

__all__ = ["required_web_env"]


def test_drain_defaults_fit_the_unchanged_thirty_second_grace() -> None:
    settings = _settings()
    assert settings.web_http_drain_seconds == 5.0
    assert settings.composer_async_drain_seconds == 10.0
    assert settings.composer_shutdown_grace_seconds == 30.0
    assert settings.composer_continuation_enabled is False


@pytest.mark.parametrize("drain", ("http", "worker"))
def test_shutdown_grace_validator_refuses_a_drain_exceeding_the_declared_grace(drain: str) -> None:
    field = "web_http_drain_seconds" if drain == "http" else "composer_async_drain_seconds"
    with pytest.raises(ValidationError, match="composer_shutdown_grace_seconds"):
        _settings(**{field: 30.0}, composer_shutdown_grace_seconds=30.0, composer_continuation_enabled=True)
    settings = _settings(**{field: 30.0}, composer_shutdown_grace_seconds=60.0, composer_continuation_enabled=True)
    assert settings.composer_shutdown_grace_seconds == 60.0


def test_shutdown_grace_validator_accepts_the_exact_margin_boundary() -> None:
    settings = _settings(composer_async_drain_seconds=24.0, composer_shutdown_grace_seconds=30.0, composer_continuation_enabled=True)
    assert settings.composer_async_drain_seconds == 24.0
    with pytest.raises(ValidationError, match="composer_shutdown_grace_seconds"):
        _settings(composer_async_drain_seconds=24.001, composer_shutdown_grace_seconds=30.0, composer_continuation_enabled=True)


@pytest.mark.parametrize("duration", (25.0, 120.0))
def test_v1_retains_its_existing_drain_range_when_continuation_is_disabled(duration: float) -> None:
    settings = _settings(composer_async_drain_seconds=duration)
    assert settings.composer_continuation_enabled is False
    assert settings.composer_async_drain_seconds == duration
    assert settings.composer_shutdown_grace_seconds == 30.0


@pytest.mark.parametrize("field", ("web_http_drain_seconds", "composer_async_drain_seconds", "composer_shutdown_grace_seconds"))
@pytest.mark.parametrize("value", (float("inf"), float("-inf"), float("nan"), 0.0, -1.0))
def test_drain_settings_refuse_nonfinite_or_nonpositive_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_drain_settings_read_the_source_owned_environment_fields(required_web_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_WEB__WEB_HTTP_DRAIN_SECONDS", "7")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_ASYNC_DRAIN_SECONDS", "30")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_SHUTDOWN_GRACE_SECONDS", "60")
    monkeypatch.setenv("ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED", "true")
    settings = web_config.settings_from_env()
    assert (settings.web_http_drain_seconds, settings.composer_async_drain_seconds, settings.composer_shutdown_grace_seconds) == (
        7.0,
        30.0,
        60.0,
    )
