"""Advisor provider options and model-facing feedback projection."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from elspeth.web.composer.advisor_checkpoint_telemetry import AdvisorCheckpointPhase
from elspeth.web.composer.advisor_output import advisor_abandonment_response_format, advisor_response_format
from elspeth.web.composer.reasoning import apply_reasoning_kwargs


def advisor_feedback_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Keep accounting in the audit/API result without coaching model scarcity."""
    return {key: value for key, value in payload.items() if key not in {"budget_used", "budget_remaining"}}


def build_advisor_request_options(
    *,
    model: str,
    temperature: float | None,
    seed: int | None,
    max_tokens: int,
    reasoning_effort: str | None,
    api_base: str | None,
    api_key: str | None,
    structured_output: bool,
    checkpoint_phase: AdvisorCheckpointPhase | None,
    abandonment_review: bool = False,
) -> dict[str, object]:
    """Keep checkpoint capabilities identical at boot and during a turn.

    Hint calls remain prose. OpenRouter's default routing can ignore unsupported
    parameters, so checkpoint requests require support for every requested
    parameter. Recognize both its LiteLLM prefix and its public custom endpoint;
    arbitrary gateway capabilities remain observable only by probing.
    """
    # The checkpoint loop owns its two audited attempts. Keep LiteLLM and the
    # underlying SDK from retrying inside one admitted provider call.
    kwargs: dict[str, object] = {"model": model, "max_tokens": max_tokens, "num_retries": 0, "max_retries": 0}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    apply_reasoning_kwargs(kwargs, model=model, effort=reasoning_effort)
    if api_base is not None:
        kwargs["api_base"] = api_base
    if api_key is not None:
        kwargs["api_key"] = api_key
    if structured_output:
        if abandonment_review:
            if checkpoint_phase is not None:
                raise ValueError("Abandonment review is independent of graph checkpoint phases")
            kwargs["response_format"] = advisor_abandonment_response_format()
        elif checkpoint_phase is None:
            raise ValueError("Structured advisor requests require their owned checkpoint phase")
        else:
            kwargs["response_format"] = advisor_response_format(phase=checkpoint_phase)
        if model.startswith("openrouter/") or (api_base is not None and urlsplit(api_base).hostname == "openrouter.ai"):
            # Top-level provider survives LiteLLM's OpenRouter transformation;
            # caller-supplied extra_body is rebuilt by that adapter.
            kwargs["provider"] = {"require_parameters": True}
    elif checkpoint_phase is not None or abandonment_review:
        raise ValueError("Manual prose hints have no checkpoint phase")
    return kwargs
