"""Provider inference and environment contracts for composer LLM availability."""

from __future__ import annotations

PROVIDER_REQUIRED_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "azure": ("AZURE_API_KEY",),
    "azure_ai": ("AZURE_AI_API_KEY",),
    # LiteLLM's Bedrock provider uses boto3's default AWS credential chain
    # (task role, environment, profile, etc.), so Composer REQUIRES no key:
    # a non-empty tuple here would mark Bedrock unavailable on every
    # task-role deployment. A Bedrock API key (AWS_BEARER_TOKEN_BEDROCK) or
    # static IAM pair is an optional alternative that LiteLLM reads from the
    # same environment; it is never a precondition for availability.
    "bedrock": (),
    "openai": ("OPENAI_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}


def infer_provider_from_model_name(model: str) -> str | None:
    """Infer provider from a provider-prefixed model string."""
    if "/" not in model:
        return None
    return model.split("/", 1)[0]


def infer_provider_from_unprefixed_model_name(model: str) -> str | None:
    """Infer provider for common unprefixed model families."""
    normalized = model.lower()
    if normalized.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if normalized.startswith("claude"):
        return "anthropic"
    return None
