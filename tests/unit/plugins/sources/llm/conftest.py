"""Configuration fixtures for LLM source tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMAuditParent, LLMQueryResult
from tests.fixtures.factories import make_operation_context


class FakeProvider:
    """Lifecycle-safe provider double at the source/provider boundary."""

    def __init__(
        self,
        result: LLMQueryResult | BaseException | None = None,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.result = result or LLMQueryResult(
            content="A careful answer",
            usage=TokenUsage.known(prompt_tokens=7, completion_tokens=3),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
        self.calls = 0
        self.messages: list[Sequence[ChatMessage]] = []
        self.audit_parents: list[LLMAuditParent] = []
        self.runtime_preflight_calls = 0
        self.close_calls = 0
        self.close_error = close_error

    def execute_query(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        audit_parent: LLMAuditParent,
        response_format: dict[str, Any] | None = None,
    ) -> LLMQueryResult:
        del model, temperature, max_tokens, response_format
        self.calls += 1
        self.messages.append(messages)
        self.audit_parents.append(audit_parent)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def runtime_preflight(self, *, operation_id: str, model: str) -> None:
        del operation_id, model
        self.runtime_preflight_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class RecordingTracer:
    """Operation-aware tracer double for source tracing boundaries."""

    def __init__(self) -> None:
        self.successes: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.flush_calls = 0

    def record_success(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        response_content: str,
        model: str | None,
        usage: TokenUsage | None,
        latency_ms: float | None,
        extra_metadata: dict[str, Any] | None,
        system_prompt: str | None,
    ) -> None:
        self.successes.append(
            {
                "parent": parent,
                "query_name": query_name,
                "prompt": prompt,
                "response_content": response_content,
                "model": model,
                "usage": usage,
                "latency_ms": latency_ms,
                "extra_metadata": extra_metadata,
                "system_prompt": system_prompt,
            }
        )

    def record_error(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        error_message: str,
        model: str,
        latency_ms: float | None,
        extra_metadata: dict[str, Any] | None,
        system_prompt: str | None,
    ) -> None:
        self.errors.append(
            {
                "parent": parent,
                "query_name": query_name,
                "prompt": prompt,
                "error_message": error_message,
                "model": model,
                "latency_ms": latency_ms,
                "extra_metadata": extra_metadata,
                "system_prompt": system_prompt,
            }
        )

    def flush(self) -> None:
        self.flush_calls += 1


@pytest.fixture
def openrouter_config() -> Any:
    """Build a valid OpenRouter LLM source config with optional overrides."""

    def build(**overrides: Any) -> dict[str, Any]:
        config: dict[str, Any] = {
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
        config.update(overrides)
        return config

    return build


@pytest.fixture
def provider_configs(openrouter_config: Any) -> dict[str, dict[str, Any]]:
    """Return one valid source config for each supported provider."""
    return {
        "azure": {
            "provider": "azure",
            "deployment_name": "gpt-4o-mini",
            "endpoint": "https://example.openai.azure.com",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
        "openrouter": openrouter_config(),
        "bedrock": {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
        "gateway": {
            "provider": "gateway",
            "model": "summariser",
            "endpoint": "https://gateway.example/v1",
            "api_key": "test-api-key",
            "prompt_template": "Summarise the audit topic.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        },
    }


@pytest.fixture
def source_context() -> PluginContext:
    """Build the real source-load operation identity and audit recorder."""
    return make_operation_context(plugin_name="llm")


@pytest.fixture
def source(openrouter_config: Any, source_context: PluginContext) -> LLMSource:
    """Build and start an LLM source without issuing a network request."""
    instance = LLMSource(openrouter_config(response_field="answer"))
    instance.on_start(source_context)
    return instance
