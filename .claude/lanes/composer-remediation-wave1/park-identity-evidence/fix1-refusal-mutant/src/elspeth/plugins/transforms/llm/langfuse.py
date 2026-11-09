"""Langfuse tracing utilities for LLM transforms.

Extracts the Langfuse v3 span/generation recording pattern that was duplicated
across all 6 LLM transform files. Uses the OpenTelemetry-based context manager
API (start_as_current_observation).

Uses factory pattern to avoid mutable two-phase initialization. The factory
returns either an ActiveLangfuseTracer or NoOpLangfuseTracer — both frozen,
both satisfying the LangfuseTracer protocol.

Follows No Silent Failures: tracing failures are logged at warning level via
structlog. Langfuse is itself an optional telemetry path, so its failures use
the process logger as the last-resort observability channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts.chat_parts import ChatMessage, audit_messages
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.tracing import LangfuseTracingConfig, TracingConfig

logger = structlog.get_logger(__name__)


class LangfuseTracer(Protocol):
    """What the transform needs from tracing. Narrow interface."""

    def record_success(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        response_content: str,
        model: str | None,
        usage: TokenUsage | None = None,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None: ...

    def record_error(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        error_message: str,
        model: str,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None: ...

    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True)
class NoOpLangfuseTracer:
    """No-op tracer for when Langfuse is not configured.

    Matches LangfuseTracer Protocol signatures exactly — enables mypy to
    catch signature drift between Protocol and implementations.
    """

    def record_success(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        response_content: str,
        model: str | None,
        usage: TokenUsage | None = None,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        pass

    def record_error(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        error_message: str,
        model: str,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        pass

    def flush(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ActiveLangfuseTracer:
    """Fully-initialized Langfuse tracer. Immutable after construction."""

    transform_name: str
    client: Any  # Langfuse instance — typed as Any since it's an optional import

    def record_success(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        response_content: str,
        model: str | None,
        usage: TokenUsage | None = None,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Record successful LLM call as Langfuse span + generation."""
        # Build metadata and kwargs (OUR CODE — let bugs crash immediately)
        metadata = {"plugin": self.transform_name, "query": query_name}
        if extra_metadata:
            metadata.update(extra_metadata)
        metadata.update(parent.tracing_metadata())

        update_kwargs: dict[str, Any] = {"output": response_content}
        if usage is not None and usage.has_data:
            usage_details: dict[str, int] = {}
            if usage.prompt_tokens is not None:
                usage_details["input"] = usage.prompt_tokens
            if usage.completion_tokens is not None:
                usage_details["output"] = usage.completion_tokens
            if usage_details:
                update_kwargs["usage_details"] = usage_details
        if latency_ms is not None:
            update_kwargs["metadata"] = {"latency_ms": latency_ms}

        # Build the full message list — include system prompt if present
        messages: list[ChatMessage] = []
        if system_prompt is not None:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=prompt))
        # First-party projection (OUR CODE) runs BEFORE the try: a failure in
        # audit_messages is a bug and crashes instead of being contained as a
        # provider trace failure. Bytes-free — tracing is an audit-adjacent
        # boundary.
        traced_input = audit_messages(messages)

        # Langfuse SDK calls (EXTERNAL boundary — catch SDK/transport errors)
        try:
            with (
                self.client.start_as_current_observation(
                    as_type="span",
                    name=f"elspeth.{self.transform_name}",
                    metadata=metadata,
                ),
                self.client.start_as_current_observation(
                    as_type="generation",
                    name="llm_call",
                    model=model,
                    input=traced_input,
                ) as generation,
            ):
                generation.update(**update_kwargs)
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as e:
            _handle_trace_failure("langfuse_trace_failed", self.transform_name, e)

    def record_error(
        self,
        *,
        parent: LLMAuditParent,
        query_name: str,
        prompt: str,
        error_message: str,
        model: str,
        latency_ms: float | None = None,
        extra_metadata: dict[str, Any] | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Record failed LLM call as Langfuse span + generation with ERROR level."""
        # Build metadata and kwargs (OUR CODE — let bugs crash immediately)
        metadata = {"plugin": self.transform_name, "query": query_name}
        if extra_metadata:
            metadata.update(extra_metadata)
        metadata.update(parent.tracing_metadata())

        update_kwargs: dict[str, Any] = {
            "level": "ERROR",
            "status_message": error_message,
        }
        if latency_ms is not None:
            update_kwargs["metadata"] = {"latency_ms": latency_ms}

        # Build the full message list — include system prompt if present
        messages: list[ChatMessage] = []
        if system_prompt is not None:
            messages.append(ChatMessage(role="system", content=system_prompt))
        messages.append(ChatMessage(role="user", content=prompt))
        # First-party projection (OUR CODE) runs BEFORE the try: a failure in
        # audit_messages is a bug and crashes instead of being contained as a
        # provider trace failure. Bytes-free — tracing is an audit-adjacent
        # boundary.
        traced_input = audit_messages(messages)

        # Langfuse SDK calls (EXTERNAL boundary — catch SDK/transport errors)
        try:
            with (
                self.client.start_as_current_observation(
                    as_type="span",
                    name=f"elspeth.{self.transform_name}",
                    metadata=metadata,
                ),
                self.client.start_as_current_observation(
                    as_type="generation",
                    name="llm_call",
                    model=model,
                    input=traced_input,
                ) as generation,
            ):
                generation.update(**update_kwargs)
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as e:
            _handle_trace_failure("langfuse_error_trace_failed", self.transform_name, e)

    def flush(self) -> None:
        """Flush pending tracing data."""
        try:
            self.client.flush()
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as e:
            _handle_trace_failure("langfuse_flush_failed", self.transform_name, e)


def _handle_trace_failure(
    event_name: str,
    transform_name: str,
    error: Exception,
) -> None:
    """Handle trace recording failure — No Silent Failures via structlog.

    Langfuse is itself an optional telemetry path. If it fails, structlog is
    the independent last-resort channel and the pipeline result remains
    primary. Only ``TIER_1_ERRORS`` — ELSPETH's own invariant classes — are
    re-raised ahead of the broad clause. Everything else raised inside the
    SDK call is a Tier-3 provider failure and is contained here: a
    ``TypeError`` from a langfuse signature drift cannot be told apart from a
    programming error by its class, and a row whose provider call is already
    audited must not fail on its optional trace (elspeth-a1ab69607a).
    """
    logger.warning(
        event_name,
        plugin=transform_name,
        error_type=type(error).__name__,
        exc_info=True,
    )


def create_langfuse_tracer(
    transform_name: str,
    tracing_config: TracingConfig | None,
) -> LangfuseTracer:
    """Factory: returns ActiveLangfuseTracer or NoOpLangfuseTracer.

    Fully constructs the tracer — no deferred setup() needed. The transform
    holds the returned object from __init__ through the entire lifecycle.
    """
    if tracing_config is None:
        return NoOpLangfuseTracer()
    if not isinstance(tracing_config, LangfuseTracingConfig):
        # Non-Langfuse configs are valid no-ops for the Langfuse factory:
        # - AzureAITracingConfig: handled separately in LLMTransform.on_start()
        # - TracingConfig(provider="none"): documented no-tracing setting
        # Unknown providers are rejected by parse_tracing_config() at parse time.
        return NoOpLangfuseTracer()

    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=tracing_config.public_key,
            secret_key=tracing_config.secret_key,
            host=tracing_config.host,
            tracing_enabled=tracing_config.tracing_enabled,
        )
        return ActiveLangfuseTracer(transform_name=transform_name, client=client)
    except ImportError as exc:
        # User explicitly configured Langfuse tracing but the package is missing.
        # This is a startup error, not a silent degradation — the user has a
        # reasonable expectation that configured tracing is active.
        raise RuntimeError(
            "Langfuse tracing is configured but the 'langfuse' package is not installed. "
            "Install with: uv pip install 'elspeth[tracing-langfuse]'"
        ) from exc
