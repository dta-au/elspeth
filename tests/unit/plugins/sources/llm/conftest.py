"""Configuration fixtures for LLM source tests."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def openrouter_config() -> Any:
    """Build a valid OpenRouter LLM source config with optional overrides."""

    def build(**overrides: Any) -> dict[str, Any]:
        config: dict[str, Any] = {
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
        config.update(overrides)
        return config

    return build


@pytest.fixture
def provider_configs(openrouter_config: Any) -> dict[str, dict[str, Any]]:
    """Return one valid source config for each supported provider."""
    return {
        "azure": {
            "provider": "azure",
            "deployment_name": "gpt-4o-mini",
            "endpoint": "https://example.openai.azure.com",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
        "openrouter": openrouter_config(),
        "bedrock": {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
        "gateway": {
            "provider": "gateway",
            "model": "summariser",
            "endpoint": "https://gateway.example/v1",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    }
