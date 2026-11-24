"""Provider failures in the shape the Composer receives them.

Every ``litellm.acompletion`` failure passes through LiteLLM's exception
mapper before Composer code sees it. A test that builds a LiteLLM exception by
hand can carry fields the mapper never delivers, so tests about retry policy
build their failures here, through the same mapper.
"""

from __future__ import annotations

import httpx
import openai
from litellm.litellm_core_utils.exception_mapping_utils import exception_type


def through_litellm_mapper(original: Exception, *, provider: str, model: str = "gpt-4o") -> Exception:
    """Return the exception ``litellm.acompletion`` raises for ``original``."""
    try:
        exception_type(model=model, original_exception=original, custom_llm_provider=provider)
    except Exception as mapped:  # the mapper always raises; its result is the value under test
        return mapped
    raise AssertionError("LiteLLM's exception mapper returned instead of raising")


def insufficient_quota_429() -> openai.RateLimitError:
    """A provider 429 that carries the structured quota code in its body."""
    request = httpx.Request("POST", "https://provider.example.invalid/v1/chat/completions")
    body = {"code": "insufficient_quota", "type": "insufficient_quota", "message": "quota"}
    response = httpx.Response(429, request=request, json={"error": body})
    return openai.RateLimitError("quota", response=response, body=body)
