"""Request-side pin for OpenRouter usage accounting (elspeth-190a5c4668).

The call audit's provider cost (``response_usage.cost``) and in-band cache
detail (``prompt_tokens_details.cached_tokens``) ride OpenRouter's usage
block, which the API returns under the ``usage: {"include": true}`` request
opt-in. litellm 1.85.0 injects that opt-in unconditionally in
``OpenrouterConfig.transform_request``; the dependency range admits versions
that may not. These tests pin ELSPETH's explicit opt-in at the acompletion
choke point and its survival through litellm's shaping, failing loudly on
upgrade drift (the ``test_litellm_preserves_the_native_reasoning_object_for_openrouter``
pattern).
"""

from __future__ import annotations

import asyncio

import pytest

from elspeth.web.composer.service import (
    _apply_openrouter_usage_accounting,
    _litellm_acompletion,
)


def test_openrouter_models_get_the_usage_accounting_opt_in() -> None:
    kwargs: dict[str, object] = {"model": "openrouter/anthropic/claude-sonnet-5"}
    _apply_openrouter_usage_accounting(kwargs)
    assert kwargs["usage"] == {"include": True}


@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-sonnet-5", "azure/gpt-5-mini", "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0"],
    ids=["anthropic-native", "azure", "bedrock"],
)
def test_non_openrouter_models_are_never_touched(model: str) -> None:
    kwargs: dict[str, object] = {"model": model}
    _apply_openrouter_usage_accounting(kwargs)
    assert "usage" not in kwargs


def test_a_caller_supplied_usage_value_wins() -> None:
    kwargs: dict[str, object] = {
        "model": "openrouter/anthropic/claude-sonnet-5",
        "usage": {"include": False},
    }
    _apply_openrouter_usage_accounting(kwargs)
    assert kwargs["usage"] == {"include": False}


def test_litellm_forwards_the_usage_object_for_openrouter() -> None:
    """Pin the opt-in's survival through litellm's param shaping.

    Probed on litellm 1.85.0 (2026-08-19): ``usage`` is not an OpenAI
    default param, so it bypasses ``map_openai_params``'s ``extra_body``
    rebuild entirely and ``get_optional_params`` carries it verbatim. This
    pin fails loudly if a litellm upgrade starts dropping it.
    """
    import litellm

    params = litellm.utils.get_optional_params(
        model="anthropic/claude-sonnet-5",
        custom_llm_provider="openrouter",
        usage={"include": True},
    )
    assert params.get("usage") == {"include": True}, (
        f"litellm no longer forwards a top-level usage object for openrouter models; got {params!r}"
    )


def test_litellm_puts_the_opt_in_on_the_openrouter_wire_body() -> None:
    """Pin the opt-in landing in the actual HTTP request body."""
    from litellm.llms.openrouter.chat.transformation import OpenrouterConfig

    body = OpenrouterConfig().transform_request(
        model="anthropic/claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        optional_params={"usage": {"include": True}},
        litellm_params={},
        headers={},
    )
    assert body["usage"] == {"include": True}


def test_the_acompletion_choke_point_applies_the_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    captured: dict[str, object] = {}

    async def fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    asyncio.run(_litellm_acompletion(model="openrouter/anthropic/claude-sonnet-5", messages=[]))
    assert captured["usage"] == {"include": True}

    captured.clear()
    asyncio.run(_litellm_acompletion(model="anthropic/claude-sonnet-5", messages=[]))
    assert "usage" not in captured
