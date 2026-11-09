"""Single-prompt LLM source configuration."""

from elspeth.plugins.sources.llm.config import (
    SOURCE_PROVIDER_CONFIGS,
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    LLMSourceConfig,
    OpenRouterLLMSourceConfig,
)
from elspeth.plugins.sources.llm.source import LLMSource

__all__ = [
    "SOURCE_PROVIDER_CONFIGS",
    "AzureOpenAILLMSourceConfig",
    "BedrockLLMSourceConfig",
    "GatewayLLMSourceConfig",
    "LLMSource",
    "LLMSourceConfig",
    "OpenRouterLLMSourceConfig",
]
