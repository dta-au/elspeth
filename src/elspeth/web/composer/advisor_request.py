"""Shared runtime and boot-probe options for the advisor provider request."""

from __future__ import annotations

from urllib.parse import urlsplit

from elspeth.web.composer.advisor_output import advisor_response_format
from elspeth.web.composer.reasoning import apply_reasoning_kwargs


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
) -> dict[str, object]:
    """Keep checkpoint capabilities identical at boot and during a turn.

    Hint calls remain prose. OpenRouter's default routing can ignore unsupported
    parameters, so checkpoint requests require support for every requested
    parameter. Recognize both its LiteLLM prefix and its public custom endpoint;
    arbitrary gateway capabilities remain observable only by probing.
    """
    kwargs: dict[str, object] = {"model": model, "max_tokens": max_tokens}
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
        kwargs["response_format"] = advisor_response_format()
        if model.startswith("openrouter/") or (api_base is not None and urlsplit(api_base).hostname == "openrouter.ai"):
            # Top-level provider survives LiteLLM's OpenRouter transformation;
            # caller-supplied extra_body is rebuilt by that adapter.
            kwargs["provider"] = {"require_parameters": True}
    return kwargs
