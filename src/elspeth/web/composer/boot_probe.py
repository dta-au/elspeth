"""Boot-time validation of operator-set composer sampling config.

The composer sends operator-set temperature/seed verbatim. A provider rejecting
those configured values is the operator's fixable config error, so this probe
surfaces it at boot rather than on the first user request. Transient provider,
auth, or network failures are not fatal: non-LLM web features must still boot.
"""

from __future__ import annotations

import httpx

from elspeth.web.composer.service import _litellm_acompletion


class ComposerBootConfigError(RuntimeError):
    """The configured composer sampling was rejected by the provider at boot."""


async def probe_composer_config(
    *,
    model: str,
    temperature: float | None,
    seed: int | None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> bool:
    """Return True on successful probe, False on transient failure.

    Raise :class:`ComposerBootConfigError` for LiteLLM bad-request responses.
    The discriminator is the exception class, not message prose: this probe sends
    a fixed trivial prompt, so a 400 on that payload is a config rejection for
    the requested model/temperature/seed tuple.

    ``api_base``/``api_key`` are the endpoint affordance (Phase 3 Task 2):
    when the operator has pointed this role at a custom OpenAI-compatible
    endpoint, the probe must hit that same endpoint — a probe that silently
    validated against the provider's default endpoint while the real calls
    go to a misconfigured custom one would defeat the entire point of
    probing at boot. Both default to ``None`` so unconfigured deployments
    keep sending the exact same request as before this affordance existed.
    """
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from openai import OpenAIError as OpenAIProviderError

    kwargs: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "This is a composer boot-time configuration smoke test. Please reply with ok."}],
        # The probe only needs the request ACCEPTED (to validate temperature/seed);
        # the output is discarded. Some providers (Azure-backed OpenAI via
        # OpenRouter) reject max_output_tokens below 16, so use that floor rather
        # than 1 — otherwise the probe's own payload trips a 400 and is misread as
        # an operator sampling-config rejection, killing boot.
        "max_tokens": 16,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if seed is not None:
        kwargs["seed"] = seed
    if api_base is not None:
        kwargs["api_base"] = api_base
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        await _litellm_acompletion(**kwargs)
        return True
    except LiteLLMBadRequestError as exc:
        raise ComposerBootConfigError(f"composer sampling rejected by {model}: temperature={temperature}, seed={seed} - {exc}") from exc
    except (LiteLLMAPIError, OpenAIProviderError, TimeoutError, httpx.HTTPError):
        return False
