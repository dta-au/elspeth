"""Fix round 2 (Task 2 IMPORTANT-2): availability must not require an
ambient provider env var for a role whose endpoint affordance is configured.

Before this fix, ``compute_availability`` (availability.py) gated readiness
solely on ``PROVIDER_REQUIRED_ENV_KEYS[provider]`` inferred from the model
prefix, with no awareness of ``composer_endpoint_api_key`` /
``composer_advisor_endpoint_api_key``. An operator who configured the
endpoint affordance CORRECTLY (paired base URL + key — the very thing the
credential-pairing validator now requires) but did not also keep an ambient
``OPENAI_API_KEY``/``ANTHROPIC_API_KEY`` around got ``composer_available:
false`` — the composer would actually have worked. This file proves the
fix: a role with a configured endpoint is exempt from the ambient-env-key
requirement, while an unconfigured role (today's default, and the other
role when only one is configured) is unaffected.

Default composer_model is ``gpt-5.5`` (provider ``openai``, requires
``OPENAI_API_KEY``); default composer_advisor_model is
``anthropic/claude-sonnet-4-6`` (provider ``anthropic``, requires
``ANTHROPIC_API_KEY``).

This directory's ``conftest.py`` has an autouse fixture
(``_composer_available_for_phase3``) that monkeypatches
``ComposerServiceImpl._compute_availability`` to always report available —
deliberately, so the compose-loop harness tests stay independent of local
API keys. That means ``service._availability`` is not useful here; every
test below calls the module-level ``compute_availability`` function
directly against the constructed service, which is unaffected by that
monkeypatch (it patches the bound *method*, not the free function the
method delegates to).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.availability import compute_availability
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.config import WebSettings

_SENTINEL_CREDENTIAL = "sk-availability-endpoint-credential-sentinel"  # secret-scan: allow-this-line


def _settings(data_dir: Path, **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "data_dir": data_dir,
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"\x00" * 32,
    }
    values.update(overrides)
    return WebSettings(**values)


def _service(tmp_path: Path, **settings_overrides: Any) -> ComposerServiceImpl:
    return ComposerServiceImpl.for_trained_operator(
        catalog=MagicMock(spec=CatalogService),
        settings=_settings(tmp_path, **settings_overrides),
    )


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_no_endpoint_no_ambient_key_is_unavailable_unchanged_behaviour(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No-regression guarantee: with no endpoint affordance configured,
    missing ambient provider env keys still make the composer unavailable —
    identical to pre-affordance behaviour."""
    _clear_provider_env(monkeypatch)

    service = _service(tmp_path)
    availability = compute_availability(service)

    assert availability.available is False
    assert "OPENAI_API_KEY" in availability.missing_keys


def test_primary_endpoint_configured_without_ambient_key_is_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The critical-path fix: a correctly configured primary endpoint (paired
    base URL + key) must not require OPENAI_API_KEY to be ambient. The
    advisor role is satisfied via its own ambient key so this isolates the
    primary role's exemption."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ambient-advisor-key")  # secret-scan: allow-this-line

    service = _service(
        tmp_path,
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )
    availability = compute_availability(service)

    assert availability.available is True
    assert availability.missing_keys == ()


def test_advisor_endpoint_configured_without_ambient_key_is_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Symmetric case for the advisor role, with the primary satisfied via
    its own ambient key."""
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-primary-key")  # secret-scan: allow-this-line
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    service = _service(
        tmp_path,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )
    availability = compute_availability(service)

    assert availability.available is True
    assert availability.missing_keys == ()


def test_only_primary_endpoint_configured_advisor_still_requires_ambient_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Role independence: exempting the primary role must not accidentally
    exempt the advisor role too."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    service = _service(
        tmp_path,
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key=_SENTINEL_CREDENTIAL,
    )
    availability = compute_availability(service)

    assert availability.available is False
    assert "ANTHROPIC_API_KEY" in availability.missing_keys


def test_both_endpoints_configured_without_any_ambient_key_is_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both roles routed through their own gateway credential, zero ambient
    provider keys anywhere — the fully-decoupled deployment this affordance
    exists to support."""
    _clear_provider_env(monkeypatch)

    service = _service(
        tmp_path,
        composer_endpoint_base_url="https://primary-gateway.example.test/v1",
        composer_endpoint_api_key=_SENTINEL_CREDENTIAL,
        composer_advisor_endpoint_base_url="https://advisor-gateway.example.test/v1",
        composer_advisor_endpoint_api_key="advisor-gateway-secret",
    )
    availability = compute_availability(service)

    assert availability.available is True
    assert availability.missing_keys == ()
