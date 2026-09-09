"""Provider-aware reasoning-effort kwargs (elspeth-dc459d438e)."""

from __future__ import annotations

import pytest

from elspeth.web.composer.reasoning import apply_reasoning_kwargs


@pytest.mark.parametrize("effort", [None, "none"])
def test_none_effort_is_a_no_op(effort: str | None) -> None:
    kwargs: dict[str, object] = {"model": "openrouter/anthropic/claude-sonnet-5"}
    apply_reasoning_kwargs(kwargs, model="openrouter/anthropic/claude-sonnet-5", effort=effort)
    assert kwargs == {"model": "openrouter/anthropic/claude-sonnet-5"}


def test_openrouter_models_use_the_native_reasoning_object() -> None:
    """openrouter/ models must NOT use litellm's ``reasoning_effort``: the
    1.85.0 ``supports_reasoning`` registry is stale for
    ``openrouter/anthropic/claude-sonnet-5`` and the param is silently
    dropped. OpenRouter's native top-level ``reasoning`` object passes
    litellm's shaping verbatim (pinned below)."""
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model="openrouter/anthropic/claude-sonnet-5", effort="low")
    assert kwargs == {"reasoning": {"effort": "low"}}
    assert "reasoning_effort" not in kwargs


@pytest.mark.parametrize(
    "model",
    [
        "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0",
        "azure/gpt-5-mini",
        "anthropic/claude-sonnet-5",
    ],
    ids=["bedrock", "azure", "anthropic-native"],
)
def test_non_openrouter_models_use_standard_reasoning_effort(model: str) -> None:
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model=model, effort="high")
    assert kwargs == {"reasoning_effort": "high"}


def test_invalid_effort_raises() -> None:
    with pytest.raises(ValueError, match="reasoning effort"):
        apply_reasoning_kwargs({}, model="azure/gpt-5-mini", effort="extreme")


def test_litellm_preserves_the_native_reasoning_object_for_openrouter() -> None:
    """Pin the carve-out's survival through litellm's param shaping.

    Probed on litellm 1.85.0 (2026-08-05): caller ``extra_body`` is CLOBBERED
    by ``OpenrouterConfig.map_openai_params`` (rebuilt from OpenRouter-only
    params), while a top-level ``reasoning`` object is carried through
    ``get_optional_params`` verbatim. This pin fails loudly if a litellm
    upgrade stops forwarding the native object — the exact silent-drop this
    module exists to avoid.
    """
    import litellm

    params = litellm.utils.get_optional_params(
        model="anthropic/claude-sonnet-5",
        custom_llm_provider="openrouter",
        reasoning={"effort": "low"},
    )
    assert params.get("reasoning") == {"effort": "low"}, (
        f"litellm no longer forwards a top-level reasoning object for openrouter models; got {params!r}"
    )


@pytest.mark.parametrize("model", ["gpt-5.5", "openai/gpt-5.5"], ids=["bare-alias", "openai-prefixed"])
def test_openai_surface_models_stay_unhinted(model: str) -> None:
    """litellm's responses_api_bridge_check routes GPT-5.4+ chat calls with
    tools + reasoning_effort to /v1/responses; chat-completions-only
    gateways (ELSPETH's own is extra="forbid") do not serve it. Unhinted
    until elspeth-9a46553771 lands the gateway contract field."""
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model=model, effort="high")
    assert kwargs == {}
