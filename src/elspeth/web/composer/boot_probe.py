"""Boot-time validation of configured Composer provider capabilities."""

from __future__ import annotations

from typing import Literal

import httpx

from elspeth.web.composer.advisor_output import parse_advisor_checkpoint_response
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.service import (
    _apply_endpoint_kwargs,
    _capture_composer_llm_completion_fields,
    _litellm_acompletion,
    _MalformedLLMResponseError,
)


class ComposerBootConfigError(RuntimeError):
    """The configured Composer request or advisor capability was rejected."""


async def probe_composer_config(
    *,
    role: Literal["planner", "advisor"],
    model: str,
    temperature: float | None,
    seed: int | None,
    api_base: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> bool:
    """Observe request acceptance and advisor conformance on its real endpoint.

    The advisor uses checkpoint request options and strict local admission.
    A passing probe observes conformance once; it cannot prove that a gateway
    always enforces the schema. Transient transport failures remain nonfatal.
    The application bounds the entire request with its boot-probe timeout.
    """
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from openai import OpenAIError as OpenAIProviderError

    if role == "advisor":
        if max_tokens is None:
            raise ValueError("advisor boot probe requires the configured completion budget")
        kwargs = build_advisor_request_options(
            model=model,
            temperature=temperature,
            seed=seed,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            api_base=api_base,
            api_key=api_key,
            structured_output=True,
        )
        kwargs["messages"] = [
            {
                "role": "user",
                "content": (
                    "This is an advisor structured-output configuration probe. Return exactly this JSON object: "
                    '{"verdict":"CLEAN","category":"other","steps":[],"findings":"","note":null}'
                ),
            }
        ]
    elif role == "planner":
        # Preserve the planner's existing prompt, budget and request options.
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": "This is a composer boot-time configuration smoke test. Please reply with ok."}],
            "max_tokens": 16,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if seed is not None:
            kwargs["seed"] = seed
        _apply_endpoint_kwargs(kwargs, base_url=api_base, api_key=api_key)
    else:
        raise ValueError(f"unknown composer probe role: {role!r}")

    try:
        response = await _litellm_acompletion(on_provider_dispatch=None, **kwargs)
    except LiteLLMBadRequestError as exc:
        if role == "advisor":
            raise ComposerBootConfigError(f"advisor {model} structured-output capability request rejected") from exc
        raise ComposerBootConfigError(f"composer sampling rejected by {model}: temperature={temperature}, seed={seed} - {exc}") from exc
    except (LiteLLMAPIError, OpenAIProviderError, TimeoutError, httpx.HTTPError):
        return False

    if role == "advisor":
        try:
            message, tool_calls, _metadata = _capture_composer_llm_completion_fields(response, pricing_model=model)
        except _MalformedLLMResponseError as exc:
            raise ComposerBootConfigError(f"advisor {model} structured-output probe returned a malformed response") from exc
        if message.content is None or tool_calls or parse_advisor_checkpoint_response(message.content).response is None:
            raise ComposerBootConfigError(f"advisor {model} structured-output probe failed JSON/schema/semantic admission")
    return True
