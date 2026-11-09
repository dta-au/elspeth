"""Reasoning-effort hints for composer-plane LLM calls (elspeth-dc459d438e).

Composer and advisor must be reasoning models. Every composer-plane call
carries an effort hint; there is no unhinted opt-out. A provider, gateway or
model that cannot honour the hint must refuse the call loudly — the composer
never drops the hint on its behalf.

One provider-agnostic mechanism with one carve-out: litellm's standard
``reasoning_effort`` param is translated per provider (Bedrock -> Anthropic
thinking budgets, Azure -> native effort, OpenAI-compatible endpoints ->
the chat-completions ``reasoning_effort`` field), but litellm gates it on its
``supports_reasoning`` model registry, which is stale for
``openrouter/anthropic/claude-sonnet-5`` (verified False on litellm 1.85.0
while ``anthropic/claude-sonnet-5`` is True), and
``OpenrouterConfig.map_openai_params`` clobbers caller ``extra_body``.
``openrouter/`` models therefore get OpenRouter's native top-level
``reasoning`` object, which ``get_optional_params`` forwards verbatim —
pinned by ``test_litellm_preserves_the_native_reasoning_object_for_openrouter``.

Bare and ``openai/`` models (direct OpenAI and OpenAI-compatible gateways,
ELSPETH's own included) get ``reasoning_effort``. litellm bridges GPT-5.4+
tool calls that carry it to ``/v1/responses`` (``responses_api_bridge_check``);
direct OpenAI and Azure already take that route with or without the hint. A
chat-completions-only gateway whose model alias is GPT-5.4+-named has no such
route and fails the call — the intended loud failure: name the alias
something litellm does not classify as GPT-5.4+, or serve ``/v1/responses``.

Neutral module: ``pipeline_planner`` consumes this and must not import
``service`` (the import runs the other way).
"""

from __future__ import annotations

import logging
from typing import Literal

ReasoningEffort = Literal["low", "medium", "high"]

_EFFORT_VALUES = ("low", "medium", "high")

_logger = logging.getLogger(__name__)


def apply_reasoning_kwargs(kwargs: dict[str, object], *, model: str, effort: str | None) -> None:
    """Apply a reasoning-effort hint to LiteLLM call kwargs, in place.

    Composer and advisor must be reasoning models, so every call is hinted.
    ``None``, ``"none"`` and unknown effort strings are a programmer error
    (settings refuse them at load) and raise rather than send an unhinted or
    mis-hinted call.
    """
    if effort not in _EFFORT_VALUES:
        raise ValueError(
            f"unknown reasoning effort {effort!r}; expected one of {_EFFORT_VALUES} (composer and advisor must be reasoning models)"
        )
    if model.startswith("openrouter/"):
        kwargs["reasoning"] = {"effort": effort}
        return
    kwargs["reasoning_effort"] = effort


def warn_if_not_reasoning_capable(*, model: str, role: str, effort: str) -> None:
    """Boot-time advisory when a hinted model fails litellm's registry check.

    Never raises: the registry has known gaps (the openrouter/ carve-out
    exists because of one, and gateway aliases are never registered), so
    this is a log line for operators, not a gate. The hint is sent
    regardless; a model that cannot honour it is refused at call time.
    """
    try:
        import litellm

        capable = bool(litellm.supports_reasoning(model=model))
    except Exception as registry_failure:  # registry lookup is advisory; never block boot
        # This function's only product is an operator advisory, so a failure
        # here has no committed outcome to fall back on. Record the failure
        # class (never the exception text, which can carry provider or path
        # detail) so an unavailable registry is visible rather than silent.
        _logger.warning(
            "composer %s reasoning-capability check for model %r could not run (%s); the reasoning effort hint is applied regardless",
            role,
            model,
            type(registry_failure).__name__,
        )
        return
    if not capable:
        _logger.warning(
            "composer %s model %r is not reasoning-capable per litellm's registry; "
            "reasoning effort %r is sent on every call and a provider or gateway "
            "that cannot honour it will refuse the call — composer and advisor "
            "must be reasoning models (the registry has gaps: openrouter/ model "
            "strings and gateway aliases may be reasoning-capable regardless)",
            role,
            model,
            effort,
        )
