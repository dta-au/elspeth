"""Provider-aware reasoning-effort kwargs (elspeth-dc459d438e)."""

from __future__ import annotations

import pytest

from elspeth.web.composer.reasoning import apply_reasoning_kwargs


@pytest.mark.parametrize("effort", [None, "none"])
def test_missing_or_none_effort_is_refused(effort: str | None) -> None:
    """Composer and advisor must be reasoning models: every call carries a
    hint, so an absent or ``"none"`` effort is a programmer error, never a
    silent unhinted call."""
    kwargs: dict[str, object] = {"model": "openrouter/anthropic/claude-sonnet-5"}
    with pytest.raises(ValueError, match="reasoning effort"):
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


@pytest.mark.parametrize(
    "model",
    ["gpt-5.5", "openai/gpt-5.5", "gw-alias", "openai/gw-alias"],
    ids=["bare-gpt", "openai-gpt", "bare-gateway-alias", "openai-gateway-alias"],
)
def test_openai_surface_models_are_hinted(model: str) -> None:
    """Bare and ``openai/`` models — direct OpenAI and OpenAI-compatible
    gateways, ELSPETH's own included — carry ``reasoning_effort``. A
    provider or gateway that cannot honour it must reject the call loudly;
    the composer no longer drops the hint on its behalf."""
    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model=model, effort="high")
    assert kwargs == {"reasoning_effort": "high"}


@pytest.mark.parametrize("model", ["openai/gw-alias", "gw-alias"])
def test_gateway_alias_hint_reaches_the_chat_completions_wire(model: str) -> None:
    """A non-GPT gateway alias with tools stays on /chat/completions and
    carries ``reasoning_effort`` through litellm's param shaping (measured on
    the locked litellm). Fails if litellm starts dropping or rerouting it."""
    import litellm

    kwargs: dict[str, object] = {}
    apply_reasoning_kwargs(kwargs, model=model, effort="medium")
    params = litellm.utils.get_optional_params(
        model=model.removeprefix("openai/"),
        custom_llm_provider="openai",
        tools=[{"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}],
        **kwargs,
    )
    assert params.get("reasoning_effort") == "medium", f"litellm dropped reasoning_effort for {model!r}; got {params!r}"


def test_registry_advisory_states_the_hint_is_sent_and_a_refusal_will_fail_the_call(caplog: pytest.LogCaptureFixture) -> None:
    """The advisory must not imply an unhinted call is merely 'ignored':
    the hint is always sent, so a model that cannot reason is refused by its
    provider or gateway on the first call."""
    import logging

    from elspeth.web.composer.reasoning import warn_if_not_reasoning_capable

    with caplog.at_level(logging.WARNING, logger="elspeth.web.composer.reasoning"):
        warn_if_not_reasoning_capable(model="openai/gw-alias", role="candidate", effort="high")

    message = caplog.records[-1].getMessage()
    assert "may be ignored" not in message
    assert "composer and advisor must be reasoning models" in message
    assert "'high'" in message
