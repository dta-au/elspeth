"""Advisor request shaping and installed LiteLLM parameter preservation."""

from __future__ import annotations

import pytest

from elspeth.web.composer.advisor_request import build_advisor_request_options


@pytest.mark.parametrize(
    ("model", "api_base", "requires_parameters"),
    [
        ("openrouter/anthropic/claude-sonnet-5", None, True),
        ("openrouter/anthropic/claude-sonnet-5", "https://gateway.example.test/v1", True),
        ("openai/gpt-4o", "https://openrouter.ai/api/v1", True),
        ("openai/gpt-4o", "https://openrouter.ai.evil.test/api/v1", False),
        ("azure/gpt-4o", None, False),
        ("bedrock/global.anthropic.claude-sonnet-4-6", None, False),
        ("openai/gpt-4o", "https://gateway.example.test/v1", False),
    ],
)
def test_checkpoint_options_require_schema_and_route_support(model: str, api_base: str | None, requires_parameters: bool) -> None:
    options = build_advisor_request_options(
        model=model,
        temperature=0.25,
        seed=73,
        max_tokens=8192,
        reasoning_effort="low",
        api_base=api_base,
        api_key="test-token",
        structured_output=True,
    )
    assert options["model"] == model
    assert options["temperature"] == 0.25
    assert options["seed"] == 73
    assert options["max_tokens"] == 8192
    assert options["api_key"] == "test-token"
    assert "messages" not in options
    response_format = options["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    if requires_parameters:
        assert options["provider"] == {"require_parameters": True}
    else:
        assert "provider" not in options
    if api_base is None:
        assert "api_base" not in options
    else:
        assert options["api_base"] == api_base


def test_hint_options_preserve_prose_request() -> None:
    options = build_advisor_request_options(
        model="openrouter/anthropic/claude-sonnet-5",
        temperature=None,
        seed=None,
        max_tokens=4096,
        reasoning_effort="low",
        api_base=None,
        api_key=None,
        structured_output=False,
    )
    assert options == {
        "model": "openrouter/anthropic/claude-sonnet-5",
        "max_tokens": 4096,
        "reasoning": {"effort": "low"},
    }


@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_installed_litellm_preserves_schema_routing_and_reasoning(provider: str) -> None:
    import litellm

    options = build_advisor_request_options(
        model="openrouter/anthropic/claude-sonnet-5",
        temperature=0.25,
        seed=73,
        max_tokens=8192,
        reasoning_effort="low",
        api_base=None,
        api_key=None,
        structured_output=True,
    )
    parameters = litellm.utils.get_optional_params(
        model="anthropic/claude-sonnet-5" if provider == "openrouter" else "gpt-4o",
        custom_llm_provider=provider,
        response_format=options["response_format"],
        provider=options["provider"],
        reasoning=options["reasoning"],
    )
    assert parameters["response_format"] == options["response_format"]
    native = parameters if provider == "openrouter" else parameters["extra_body"]
    assert native["provider"] == {"require_parameters": True}
    assert native["reasoning"] == {"effort": "low"}
