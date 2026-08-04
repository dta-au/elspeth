"""Reasoning-effort hints for composer-plane LLM calls (elspeth-dc459d438e).

One provider-agnostic mechanism with one carve-out: litellm's standard
``reasoning_effort`` param is translated per provider (Bedrock -> Anthropic
thinking budgets, Azure -> native effort), but litellm gates it on its
``supports_reasoning`` model registry, which is stale for
``openrouter/anthropic/claude-sonnet-5`` (verified False on litellm 1.85.0
while ``anthropic/claude-sonnet-5`` is True), and
``OpenrouterConfig.map_openai_params`` clobbers caller ``extra_body``.
``openrouter/`` models therefore get OpenRouter's native top-level
``reasoning`` object, which ``get_optional_params`` forwards verbatim —
pinned by ``test_litellm_preserves_the_native_reasoning_object_for_openrouter``.

Neutral module: ``pipeline_planner`` consumes this and must not import
``service`` (the import runs the other way).
"""

from __future__ import annotations

import logging
from typing import Literal

ReasoningEffort = Literal["none", "low", "medium", "high"]

_EFFORT_VALUES = ("none", "low", "medium", "high")

_logger = logging.getLogger(__name__)


def apply_reasoning_kwargs(kwargs: dict[str, object], *, model: str, effort: str | None) -> None:
    """Apply a reasoning-effort hint to LiteLLM call kwargs, in place.

    ``None`` and ``"none"`` add nothing — the unhinted status quo and the
    per-deployment opt-out. Unknown effort strings are a programmer error at
    the settings boundary and raise rather than silently under- or
    over-thinking.
    """
    if effort is None or effort == "none":
        return
    if effort not in _EFFORT_VALUES:
        raise ValueError(f"unknown reasoning effort {effort!r}; expected one of {_EFFORT_VALUES}")
    if model.startswith("openrouter/"):
        kwargs["reasoning"] = {"effort": effort}
        return
    if "/" not in model or model.startswith("openai/"):
        # OpenAI-surface models (bare aliases like "gpt-5.5" and openai/
        # prefixes) are the OpenAI-compatible-endpoint lever: they ride
        # either direct OpenAI or a chat-completions gateway. litellm
        # bridges GPT-5.4+ chat calls with tools + reasoning_effort to
        # /v1/responses (main.py responses_api_bridge_check), which
        # chat-completions-only gateways — including ELSPETH's own, whose
        # contract is deliberately extra="forbid" — do not serve. Until the
        # gateway's compatibility contract carries reasoning_effort
        # (elspeth-9a46553771), these models go unhinted: fail-safe, not
        # fail-broken.
        return
    kwargs["reasoning_effort"] = effort


def warn_if_not_reasoning_capable(*, model: str, role: str, effort: str) -> None:
    """Boot-time advisory when a hinted model fails litellm's registry check.

    Never raises: the registry has known gaps (the openrouter/ carve-out
    exists because of one), so this is a log line for operators, not a gate.
    """
    if effort == "none":
        return
    try:
        import litellm

        capable = bool(litellm.supports_reasoning(model=model))
    except Exception:  # registry lookup is advisory; never block boot
        return
    if not capable:
        _logger.warning(
            "composer %s model %r is not reasoning-capable per litellm's registry; "
            "reasoning effort %r may be ignored by the provider (registry gaps "
            "exist for openrouter/ model strings — those are hinted via "
            "OpenRouter's native reasoning object regardless)",
            role,
            model,
            effort,
        )
