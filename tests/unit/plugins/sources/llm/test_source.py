"""One-call, one-row lifecycle contract for the source-native LLM plugin."""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from typing import Any, cast
from unittest.mock import patch

import pytest

from elspeth.contracts import Determinism, SourceRow
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability, WebConfigAuthority
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm import populate_llm_operational_fields
from elspeth.plugins.transforms.llm.langfuse import NoOpLangfuseTracer
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMAuditParent, LLMQueryResult
from elspeth.plugins.transforms.llm.providers.azure import AzureLLMProvider
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockLLMProvider
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterLLMProvider
from tests.unit.plugins.sources.llm.conftest import FakeProvider, RecordingTracer


def _install_provider(source: LLMSource, provider: FakeProvider) -> None:
    original = source._provider
    if original is not None:
        original.close()
    source._provider = provider


def test_load_calls_provider_once_and_emits_one_transform_compatible_row(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content="```text\nA careful answer\n```",
            usage=TokenUsage.known(prompt_tokens=7, completion_tokens=3),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    _install_provider(source, provider)

    rows = list(source.load(source_context))

    assert provider.calls == 1
    operation_id = source_context.operation_id
    assert operation_id is not None
    assert provider.audit_parents == [LLMAuditParent.for_operation(operation_id=operation_id)]
    assert provider.messages == [[{"role": "user", "content": "Summarise the audit topic."}]]
    assert provider.runtime_preflight_calls == 0
    assert len(rows) == 1
    assert rows[0].source_row_index == 0
    assert rows[0].row == {
        "answer": "A careful answer",
        "answer_usage": {"prompt_tokens": 7, "completion_tokens": 3},
        "answer_model": "served-model",
    }


@pytest.mark.parametrize(
    "usage",
    [
        TokenUsage.unknown(),
        TokenUsage(prompt_tokens=4),
        TokenUsage.known(prompt_tokens=4, completion_tokens=2),
        TokenUsage(prompt_tokens=4, completion_tokens=2, reported_total=99),
    ],
    ids=["unknown", "partial", "known-without-reported-total", "inconsistent-reported-total"],
)
def test_usage_output_matches_transform_operational_helper_exactly(
    source: LLMSource,
    source_context: PluginContext,
    usage: TokenUsage,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content="A careful answer",
            usage=usage,
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    _install_provider(source, provider)
    expected: dict[str, object] = {}
    populate_llm_operational_fields(expected, "answer", usage=usage, model="served-model")

    row = next(iter(source.load(source_context))).row

    assert row["answer_usage"] == expected["answer_usage"]
    assert row["answer_model"] == expected["answer_model"]


@pytest.mark.parametrize("operation_id", [None, "", "   "])
def test_load_requires_non_empty_operation_id(
    source: LLMSource,
    source_context: PluginContext,
    operation_id: str | None,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)
    source_context.operation_id = operation_id

    with pytest.raises(FrameworkBugError, match="operation_id"):
        source.load(source_context)

    assert provider.calls == 0


def test_load_rejects_provider_not_started_before_request(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
) -> None:
    source = LLMSource(openrouter_config())

    with pytest.raises(FrameworkBugError, match="on_start"):
        source.load(source_context)


def test_double_load_is_rejected_before_any_request(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)

    first = source.load(source_context)
    with pytest.raises(FrameworkBugError, match="only be loaded once"):
        source.load(source_context)

    assert provider.calls == 0
    cast(Generator[SourceRow, None, None], first).close()
    source.close()
    assert provider.close_calls == 1


def test_second_load_after_exhaustion_is_rejected_without_second_request(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)

    assert len(list(source.load(source_context))) == 1
    with pytest.raises(FrameworkBugError, match="only be loaded once"):
        source.load(source_context)

    assert provider.calls == 1


def test_provider_error_propagates_and_closes_provider(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(LLMClientError("provider failed", retryable=False))
    _install_provider(source, provider)

    with pytest.raises(LLMClientError, match="provider failed"):
        list(source.load(source_context))

    assert provider.calls == 1
    assert provider.close_calls == 1
    assert source._provider is None


def test_provider_error_remains_primary_when_close_also_fails(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(
        LLMClientError("provider failed", retryable=False),
        close_error=RuntimeError("cleanup failed"),
    )
    _install_provider(source, provider)

    with pytest.raises(LLMClientError, match="provider failed"):
        list(source.load(source_context))

    assert provider.close_calls == 1
    assert source._provider is None


def test_provider_error_records_operation_parented_trace(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(LLMClientError("provider failed", retryable=False))
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    with pytest.raises(LLMClientError, match="provider failed"):
        list(source.load(source_context))

    operation_id = source_context.operation_id
    assert operation_id is not None
    assert tracer.successes == []
    assert len(tracer.errors) == 1
    assert tracer.errors[0]["parent"] == LLMAuditParent.for_operation(operation_id=operation_id)
    assert tracer.errors[0]["prompt"] == "Summarise the audit topic."
    assert tracer.errors[0]["model"] == "openai/gpt-4o-mini"
    assert isinstance(tracer.errors[0]["latency_ms"], float)


def test_bad_provider_result_remains_primary_when_close_also_fails(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content="partial answer",
            usage=TokenUsage.unknown(),
            model="served-model",
            finish_reason=FinishReason.LENGTH,
        ),
        close_error=RuntimeError("cleanup failed"),
    )
    _install_provider(source, provider)

    with pytest.raises(LLMClientError, match="truncated"):
        list(source.load(source_context))

    assert provider.close_calls == 1
    assert source._provider is None


@pytest.mark.parametrize(
    "result",
    [
        LLMQueryResult(
            content="partial answer",
            usage=TokenUsage.unknown(),
            model="served-model",
            finish_reason=FinishReason.LENGTH,
        ),
        LLMQueryResult(
            content="```text\n   \n```",
            usage=TokenUsage.unknown(),
            model="served-model",
            finish_reason=FinishReason.STOP,
        ),
    ],
    ids=["finish-reason", "empty-after-fence-cleanup"],
)
def test_bad_provider_result_records_operation_parented_error_trace(
    source: LLMSource,
    source_context: PluginContext,
    result: LLMQueryResult,
) -> None:
    provider = FakeProvider(result)
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    with pytest.raises(LLMClientError):
        list(source.load(source_context))

    operation_id = source_context.operation_id
    assert operation_id is not None
    assert tracer.successes == []
    assert len(tracer.errors) == 1
    assert tracer.errors[0]["parent"] == LLMAuditParent.for_operation(operation_id=operation_id)
    assert tracer.errors[0]["prompt"] == "Summarise the audit topic."
    assert tracer.errors[0]["model"] == "served-model"
    assert isinstance(tracer.errors[0]["latency_ms"], float)


@pytest.mark.parametrize(
    "finish_reason",
    [FinishReason.LENGTH, FinishReason.CONTENT_FILTER, FinishReason.TOOL_CALLS],
)
def test_non_success_finish_reason_fails_closed(
    source: LLMSource,
    source_context: PluginContext,
    finish_reason: FinishReason,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content="partial answer",
            usage=TokenUsage.unknown(),
            model="served-model",
            finish_reason=finish_reason,
        )
    )
    _install_provider(source, provider)

    with pytest.raises(LLMClientError, match=r"finish reason|truncated|content filter"):
        list(source.load(source_context))

    assert provider.calls == 1


@pytest.mark.parametrize("content", ["```\n```", "```text\n   \n```"])
def test_fence_only_content_is_rejected_after_cleanup(
    source: LLMSource,
    source_context: PluginContext,
    content: str,
) -> None:
    provider = FakeProvider(
        LLMQueryResult(
            content=content,
            usage=TokenUsage.unknown(),
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    _install_provider(source, provider)

    with pytest.raises(LLMClientError, match="empty content"):
        list(source.load(source_context))


def test_generator_close_releases_provider_once(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)

    rows = source.load(source_context)
    first = next(rows)
    assert first.source_row_index == 0
    assert provider.close_calls == 0

    cast(Generator[SourceRow, None, None], rows).close()
    source.close()
    source.close()

    assert provider.close_calls == 1
    assert source._provider is None


def test_never_started_iterator_close_releases_provider_once(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)

    rows = source.load(source_context)
    cast(Generator[SourceRow, None, None], rows).close()

    assert provider.calls == 0
    assert provider.close_calls == 1
    assert source._provider is None


def test_pre_set_shutdown_before_first_next_has_no_request_row_or_trace(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer
    shutdown_event = threading.Event()
    shutdown_event.set()
    source_context.shutdown_event = shutdown_event

    rows = list(source.load(source_context))

    assert rows == []
    assert provider.calls == 0
    assert provider.close_calls == 1
    assert source._provider is None
    assert tracer.successes == []
    assert tracer.errors == []


def test_generator_close_does_not_surface_cleanup_failure(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(close_error=RuntimeError("cleanup failed"))
    _install_provider(source, provider)

    rows = source.load(source_context)
    assert next(rows).source_row_index == 0
    cast(Generator[SourceRow, None, None], rows).close()

    assert provider.close_calls == 1
    assert source._provider is None


def test_standalone_close_surfaces_cleanup_failure_once(
    source: LLMSource,
) -> None:
    provider = FakeProvider(close_error=RuntimeError("cleanup failed"))
    _install_provider(source, provider)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        source.close()
    source.close()

    assert provider.close_calls == 1
    assert source._provider is None


def test_cleanup_failure_emits_bounded_system_health_telemetry(
    source: LLMSource,
) -> None:
    events: list[object] = []
    provider = FakeProvider(close_error=RuntimeError("cleanup failed with secret material"))
    _install_provider(source, provider)
    source._telemetry_emit = events.append

    with pytest.raises(RuntimeError, match="cleanup failed"):
        source.close()

    assert len(events) == 1
    event = events[0]
    assert type(event).__name__ == "ResourceCleanupFailed"
    assert event.run_id == "test-run"  # type: ignore[attr-defined]
    assert event.operation_id == source._start_operation_id  # type: ignore[attr-defined]
    assert event.component == "llm_source"  # type: ignore[attr-defined]
    assert event.resource == "provider"  # type: ignore[attr-defined]
    assert event.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert event.suppressed is False  # type: ignore[attr-defined]
    assert "secret" not in repr(event)


def test_cleanup_telemetry_failure_logs_last_resort_without_replacing_primary(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(
        LLMClientError("provider failed", retryable=False),
        close_error=RuntimeError("cleanup failed"),
    )
    _install_provider(source, provider)

    def broken_telemetry(_event: object) -> None:
        raise RuntimeError("telemetry unavailable")

    source._telemetry_emit = broken_telemetry

    with (
        patch("elspeth.plugins.sources.llm.source.logger") as mock_logger,
        pytest.raises(LLMClientError, match="provider failed"),
    ):
        list(source.load(source_context))

    mock_logger.warning.assert_called_once_with(
        "resource_cleanup_telemetry_failed",
        component="llm_source",
        resource="provider",
        error_type="RuntimeError",
    )


def test_azure_tracing_initialization_has_no_redundant_lifecycle_log(
    provider_configs: dict[str, dict[str, Any]],
    source_context: PluginContext,
) -> None:
    config = dict(provider_configs["azure"])
    config["tracing"] = {
        "provider": "azure_ai",
        "connection_string": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    }
    source = LLMSource(config)

    with (
        patch("elspeth.plugins.sources.llm.source._configure_azure_monitor", autospec=True),
        patch("elspeth.plugins.sources.llm.source.logger") as mock_logger,
    ):
        source.on_start(source_context)

    try:
        mock_logger.info.assert_not_called()
    finally:
        source.close()


def test_successful_exhaustion_surfaces_standalone_cleanup_failure(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(close_error=RuntimeError("cleanup failed"))
    _install_provider(source, provider)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        list(source.load(source_context))

    assert provider.calls == 1
    assert provider.close_calls == 1
    assert source._provider is None


def test_exhaustion_and_repeated_close_clear_provider_exactly_once(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    _install_provider(source, provider)

    assert len(list(source.load(source_context))) == 1
    source.close()
    source.close()

    assert provider.close_calls == 1
    assert source._provider is None


def test_fixed_schema_validation_success(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
) -> None:
    source = LLMSource(
        openrouter_config(
            response_field="answer",
            schema={
                "mode": "fixed",
                "fields": ["answer: str", "answer_usage: any", "answer_model: str"],
            },
        )
    )
    source.on_start(source_context)
    provider = FakeProvider()
    _install_provider(source, provider)

    rows = list(source.load(source_context))

    assert len(rows) == 1
    assert rows[0].is_quarantined is False
    assert rows[0].contract is source.get_schema_contract()
    assert rows[0].contract is not None and rows[0].contract.locked


def test_success_records_operation_parented_trace_with_served_result_details(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    usage = TokenUsage(prompt_tokens=7, completion_tokens=3, reported_total=11)
    provider = FakeProvider(
        LLMQueryResult(
            content="```text\nA careful answer\n```",
            usage=usage,
            model="served-model",
            finish_reason=FinishReason.STOP,
        )
    )
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    rows = list(source.load(source_context))

    operation_id = source_context.operation_id
    assert operation_id is not None
    assert len(rows) == 1
    assert tracer.errors == []
    assert len(tracer.successes) == 1
    trace = tracer.successes[0]
    assert trace["parent"] == LLMAuditParent.for_operation(operation_id=operation_id)
    assert trace["prompt"] == "Summarise the audit topic."
    assert trace["response_content"] == "A careful answer"
    assert trace["model"] == "served-model"
    assert trace["usage"] is usage
    assert isinstance(trace["latency_ms"], float)


def test_success_trace_failure_does_not_replace_source_result(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider()
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    with (
        patch.object(tracer, "record_success", side_effect=RuntimeError("tracing unavailable")),
        patch("elspeth.plugins.sources.llm.source.logger") as mock_logger,
    ):
        rows = list(source.load(source_context))

    assert len(rows) == 1
    assert rows[0].row["answer"] == "A careful answer"
    mock_logger.warning.assert_called_once_with(
        "llm_trace_emission_failed",
        plugin="llm",
        error_type="RuntimeError",
    )


@pytest.mark.parametrize("destination", ["quarantine", "discard"])
def test_schema_validation_failure_applies_configured_policy(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
    destination: str,
) -> None:
    source = LLMSource(
        openrouter_config(
            response_field="answer",
            schema={"mode": "fixed", "fields": ["request_id: str"]},
            on_validation_failure=destination,
        )
    )
    source.on_start(source_context)
    provider = FakeProvider()
    _install_provider(source, provider)

    rows = list(source.load(source_context))

    assert provider.calls == 1
    if destination == "discard":
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0].is_quarantined is True
        assert rows[0].source_row_index == 0
        assert rows[0].quarantine_destination == destination
        assert source_context.pop_pending_quarantine_validation_error_id(rows[0].row) is not None


@pytest.mark.parametrize("destination", ["quarantine", "discard"])
def test_validation_failure_outcome_remains_primary_when_close_fails(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
    destination: str,
) -> None:
    source = LLMSource(
        openrouter_config(
            response_field="answer",
            schema={"mode": "fixed", "fields": ["request_id: str"]},
            on_validation_failure=destination,
        )
    )
    source.on_start(source_context)
    provider = FakeProvider(close_error=RuntimeError("cleanup failed"))
    _install_provider(source, provider)

    rows = list(source.load(source_context))

    assert provider.close_calls == 1
    assert source._provider is None
    if destination == "discard":
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0].is_quarantined is True


@pytest.mark.parametrize(
    ("provider_name", "provider_type"),
    [
        ("azure", AzureLLMProvider),
        ("openrouter", OpenRouterLLMProvider),
        ("bedrock", BedrockLLMProvider),
        ("gateway", GatewayLLMProvider),
    ],
)
def test_on_start_constructs_real_provider_variant_without_preflight(
    provider_configs: dict[str, dict[str, Any]],
    source_context: PluginContext,
    provider_name: str,
    provider_type: type[AzureLLMProvider | OpenRouterLLMProvider | BedrockLLMProvider | GatewayLLMProvider],
) -> None:
    config = dict(provider_configs[provider_name])
    if provider_name == "azure":
        config.update(api_version="2025-01-01-preview")
    elif provider_name == "openrouter":
        config.update(timeout_seconds=17.0)
    elif provider_name == "bedrock":
        config.update(region_name="ap-southeast-2")
    else:
        config.update(contract_major=1, required_capabilities=["json_schema"], timeout_seconds=19.0)
    source = LLMSource(config)

    with patch.object(provider_type, "runtime_preflight", autospec=True) as preflight:
        source.on_start(source_context)

    try:
        provider = source._provider
        assert type(provider) is provider_type
        assert isinstance(source._tracer, NoOpLangfuseTracer)
        assert source._tracing_config is None
        preflight.assert_not_called()
        assert provider._recorder is source_context.landscape
        assert provider._run_id == source_context.run_id
        assert provider._telemetry_emit is source_context.telemetry_emit
        assert provider._limiter is source._limiter
        assert provider._resolved_prompt_template_hash == source._template.template_hash
        if isinstance(provider, AzureLLMProvider):
            assert provider._endpoint == "https://example.openai.azure.com"
            assert provider._api_key == "test-api-key"
            assert provider._api_version == "2025-01-01-preview"
            assert provider._deployment_name == "gpt-4o-mini"
        elif isinstance(provider, OpenRouterLLMProvider):
            assert provider._base_url == OPENROUTER_BASE_URL
            assert provider._timeout == 17.0
            assert provider._request_headers["Authorization"] == "Bearer test-api-key"
        elif isinstance(provider, BedrockLLMProvider):
            assert provider._region_name == "ap-southeast-2"
        elif isinstance(provider, GatewayLLMProvider):
            assert provider._base_url == "https://gateway.example/v1"
            assert provider._contract_major == 1
            assert provider._required_capabilities == ("json_schema",)
            assert provider._timeout == 19.0
            assert provider._request_headers["Authorization"] == "Bearer test-api-key"
    finally:
        source.close()


def test_metadata_and_catalogue_hooks_are_source_native(
    source: LLMSource,
) -> None:
    assert source.name == "llm"
    assert source.determinism is Determinism.NON_DETERMINISTIC
    assert source.plugin_version == "1.0.0"
    assert source.web_config_authority is WebConfigAuthority.OPERATOR_PROFILED
    assert source.policy_capabilities == frozenset({CapabilityDeclaration(PluginCapability.LLM)})
    assert source.capability_tags == ("llm", "generation", "single-row")
    assert not hasattr(source, "runtime_preflight")
    assert source.get_agent_assistance(issue_code=None) is not None
    assert source.output_semantics().fields[0].field_name == "answer"
    discriminator, variants = source.discriminated_variants()
    assert discriminator == "provider"
    assert set(variants) == {"azure", "openrouter", "bedrock", "gateway"}
    assert source.get_config_schema()["discriminator"]["propertyName"] == "provider"
    assert source.probe_config()["provider"] == "openrouter"
    assert source.declared_guaranteed_fields == frozenset({"answer", "answer_usage", "answer_model"})


def test_source_does_not_inherit_or_delegate_to_transform(source: LLMSource) -> None:
    from elspeth.plugins.transforms.llm.transform import LLMTransform

    assert LLMTransform not in type(source).__mro__
    assert not hasattr(source, "_strategy")
    assert not hasattr(source, "_query_executor")
