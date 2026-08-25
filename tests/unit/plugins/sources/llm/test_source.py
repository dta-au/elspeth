"""One-call, one-row lifecycle contract for the source-native LLM plugin."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator
from io import StringIO
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from structlog.testing import capture_logs

from elspeth.contracts import Determinism, SourceRow
from elspeth.contracts.chat_parts import ChatMessage
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.events import ResourceCleanupFailed
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability, WebConfigAuthority
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.plugin_semantics import ContentKind, TextFraming
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm import populate_llm_operational_fields
from elspeth.plugins.transforms.llm.langfuse import NoOpLangfuseTracer
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMAuditParent, LLMQueryResult
from elspeth.plugins.transforms.llm.providers.azure import AzureLLMProvider
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockLLMProvider
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterLLMProvider
from elspeth.telemetry.exporters.console import ConsoleExporter
from tests.fixtures.factories import make_operation_context
from tests.unit.plugins.sources.llm.conftest import FakeProvider, RecordingTracer


def _install_provider(source: LLMSource, provider: FakeProvider) -> None:
    original = source._provider
    if original is not None:
        original.close()
    source._provider = provider


def _install_runtime_rejecting_schema(source: LLMSource) -> None:
    """Simulate a runtime schema mismatch without authoring an impossible source contract."""
    source._schema_class = create_schema_from_config(
        SchemaConfig(mode="fixed", fields=(FieldDefinition(name="request_id", field_type="str"),)),
        "RejectLLMSourceRuntimeRow",
        allow_coercion=False,
    )


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
    assert provider.messages == [[ChatMessage(role="user", content="Summarise the audit topic.")]]
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


@pytest.mark.parametrize(
    ("provider_close_fails", "tracer_flush_fails"),
    [(True, False), (False, True), (True, True)],
    ids=["provider", "tracer", "provider-and-tracer"],
)
def test_pre_set_shutdown_remains_primary_when_cleanup_fails(
    source: LLMSource,
    source_context: PluginContext,
    provider_close_fails: bool,
    tracer_flush_fails: bool,
) -> None:
    events: list[object] = []
    provider = FakeProvider(
        close_error=RuntimeError("provider cleanup failed") if provider_close_fails else None,
    )
    tracer = RecordingTracer()
    if tracer_flush_fails:

        def fail_tracer_flush() -> None:
            tracer.flush_calls += 1
            raise RuntimeError("tracer cleanup failed")

        tracer.flush = fail_tracer_flush  # type: ignore[method-assign]
    _install_provider(source, provider)
    source._tracer = tracer
    source._telemetry_emit = events.append
    shutdown_event = threading.Event()
    shutdown_event.set()
    source_context.shutdown_event = shutdown_event

    rows = list(source.load(source_context))

    assert rows == []
    assert provider.calls == 0
    assert provider.close_calls == 1
    assert tracer.successes == []
    assert tracer.errors == []
    assert tracer.flush_calls == 1
    assert source._provider is None
    assert source._tracer is None
    operation_id = source_context.operation_id
    assert operation_id is not None
    expected_resources = set()
    if provider_close_fails:
        expected_resources.add("provider")
    if tracer_flush_fails:
        expected_resources.add("tracer")
    assert {event.resource for event in events} == expected_resources  # type: ignore[attr-defined]
    assert all(event.run_id == source_context.run_id for event in events)  # type: ignore[attr-defined]
    assert all(event.operation_id == operation_id for event in events)  # type: ignore[attr-defined]
    assert all(event.state_id is None for event in events)  # type: ignore[attr-defined]
    assert all(event.token_id is None for event in events)  # type: ignore[attr-defined]
    assert all(event.suppressed is True for event in events)  # type: ignore[attr-defined]


@pytest.mark.parametrize("failing_resource", ["provider", "tracer"])
def test_shutdown_does_not_suppress_tier_one_resource_cleanup_failure(
    source: LLMSource,
    source_context: PluginContext,
    failing_resource: str,
) -> None:
    provider = FakeProvider(
        close_error=FrameworkBugError("provider cleanup invariant failed") if failing_resource == "provider" else None,
    )
    tracer = RecordingTracer()
    if failing_resource == "tracer":

        def fail_tracer_flush() -> None:
            tracer.flush_calls += 1
            raise FrameworkBugError("tracer cleanup invariant failed")

        tracer.flush = fail_tracer_flush  # type: ignore[method-assign]
    _install_provider(source, provider)
    source._tracer = tracer

    def probe_telemetry(event: object) -> None:
        del event

    source._telemetry_emit = probe_telemetry
    shutdown_event = threading.Event()
    shutdown_event.set()
    source_context.shutdown_event = shutdown_event

    with pytest.raises(FrameworkBugError, match="cleanup invariant failed"):
        list(source.load(source_context))

    assert provider.calls == 0
    assert provider.close_calls == 1
    assert tracer.successes == []
    assert tracer.errors == []
    assert tracer.flush_calls == 1
    assert source._provider is None
    assert source._tracer is None
    assert source._telemetry_emit is not probe_telemetry


def test_first_tier_one_cleanup_failure_wins_and_masked_failures_are_logged(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    """Cleanup primacy: when every detached resource fails Tier-1, the first
    (provider) invariant failure propagates, the tracer flush is still
    attempted, the telemetry callback is still reset, and the masked tracer
    failure is logged rather than silently discarded."""
    provider = FakeProvider(close_error=FrameworkBugError("provider cleanup invariant failed"))
    tracer = RecordingTracer()

    def fail_tracer_flush() -> None:
        tracer.flush_calls += 1
        raise FrameworkBugError("tracer cleanup invariant failed")

    tracer.flush = fail_tracer_flush  # type: ignore[method-assign]
    _install_provider(source, provider)
    source._tracer = tracer

    def probe_telemetry(event: object) -> None:
        del event

    source._telemetry_emit = probe_telemetry

    with capture_logs() as logs, pytest.raises(FrameworkBugError, match="provider cleanup invariant failed"):
        list(source.load(source_context))

    assert provider.calls == 1
    assert provider.close_calls == 1
    assert tracer.flush_calls == 1
    assert source._provider is None
    assert source._tracer is None
    assert source._telemetry_emit is not probe_telemetry
    masked = [entry for entry in logs if entry["event"] == "resource_cleanup_tier_one_failure_masked"]
    assert masked == [
        {
            "event": "resource_cleanup_tier_one_failure_masked",
            "log_level": "error",
            "component": "llm_source",
            "resource": "tracer",
            "error_type": "FrameworkBugError",
            "primary_resource": "provider",
            "primary_error_type": "FrameworkBugError",
        }
    ]


def test_tier_one_cleanup_reporting_failure_defers_until_tracer_flush(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    """elspeth-f5a9515d58: a Tier-1 raised while reporting a cleanup failure must not skip the tracer flush."""
    provider = FakeProvider(close_error=RuntimeError("provider cleanup failed"))
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    def tier_one_telemetry(_event: object) -> None:
        raise FrameworkBugError("cleanup telemetry invariant failed")

    source._telemetry_emit = tier_one_telemetry

    with pytest.raises(FrameworkBugError, match="cleanup telemetry invariant failed"):
        list(source.load(source_context))

    assert provider.calls == 1
    assert provider.close_calls == 1
    assert tracer.flush_calls == 1
    assert source._provider is None
    assert source._tracer is None
    assert source._telemetry_emit is not tier_one_telemetry


def test_shutdown_tier_one_cleanup_reporting_failure_defers_until_tracer_flush(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    """elspeth-f5a9515d58: the deferral holds on the suppress-errors shutdown path too."""
    provider = FakeProvider(close_error=RuntimeError("provider cleanup failed"))
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    def tier_one_telemetry(_event: object) -> None:
        raise FrameworkBugError("cleanup telemetry invariant failed")

    source._telemetry_emit = tier_one_telemetry
    shutdown_event = threading.Event()
    shutdown_event.set()
    source_context.shutdown_event = shutdown_event

    with pytest.raises(FrameworkBugError, match="cleanup telemetry invariant failed"):
        list(source.load(source_context))

    assert provider.calls == 0
    assert provider.close_calls == 1
    assert tracer.flush_calls == 1
    assert source._provider is None
    assert source._tracer is None
    assert source._telemetry_emit is not tier_one_telemetry


def test_shutdown_does_not_suppress_tier_one_generator_close_failure(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    rows = MagicMock(spec_set=Generator)
    rows.close.side_effect = FrameworkBugError("generator close invariant failed")
    shutdown_event = threading.Event()
    shutdown_event.set()
    source_context.shutdown_event = shutdown_event

    with (
        patch.object(source, "_load_once", return_value=cast("Generator[SourceRow, None, None]", rows)),
        pytest.raises(FrameworkBugError, match="generator close invariant failed"),
    ):
        list(source.load(source_context))

    rows.close.assert_called_once_with()


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


def test_real_lifecycle_order_correlates_post_load_cleanup_telemetry(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
) -> None:
    operation_id = source_context.operation_id
    assert operation_id is not None
    source_context.operation_id = None
    source = LLMSource(openrouter_config(response_field="answer"))
    events: list[object] = []
    source_context.telemetry_emit = events.append
    source.on_start(source_context)
    assert source._load_operation_id is None  # type: ignore[attr-defined]

    provider = FakeProvider(close_error=RuntimeError("cleanup failed with secret material"))
    _install_provider(source, provider)
    source_context.operation_id = operation_id

    rows = source.load(source_context)
    assert source._load_operation_id == operation_id  # type: ignore[attr-defined]
    cast(Generator[SourceRow, None, None], rows).close()

    assert len(events) == 1
    event = events[0]
    assert type(event).__name__ == "ResourceCleanupFailed"
    assert event.run_id == "test-run"  # type: ignore[attr-defined]
    assert event.operation_id == operation_id  # type: ignore[attr-defined]
    assert event.state_id is None  # type: ignore[attr-defined]
    assert event.token_id is None  # type: ignore[attr-defined]
    assert event.component == "llm_source"  # type: ignore[attr-defined]
    assert event.resource == "provider"  # type: ignore[attr-defined]
    assert event.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert event.suppressed is True  # type: ignore[attr-defined]
    assert "secret" not in repr(event)


def test_cleanup_telemetry_and_fallback_logs_exclude_all_llm_source_payload_sentinels(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
) -> None:
    prompt_sentinel = "TASK12_PROMPT_LITERAL_7f48d44a"
    secret_reference_sentinel = "${TASK12_LOOKUP_SECRET_REF_91b4c672}"
    credential_sentinel = "TASK12_RUNTIME_CREDENTIAL_2dd630ef"
    response_sentinel = "TASK12_PROVIDER_RESPONSE_884cb157"
    provider_body_sentinel = "TASK12_PROVIDER_ERROR_BODY_35a8e99c"
    sentinels = (
        prompt_sentinel,
        secret_reference_sentinel,
        credential_sentinel,
        response_sentinel,
        provider_body_sentinel,
    )
    assert len(set(sentinels)) == 5

    events: list[ResourceCleanupFailed] = []

    def capture_then_fail(event: object) -> None:
        assert isinstance(event, ResourceCleanupFailed)
        events.append(event)
        raise RuntimeError("telemetry callback unavailable")

    source_context.telemetry_emit = capture_then_fail
    source = LLMSource(
        openrouter_config(
            api_key=credential_sentinel,
            prompt_template=f"{prompt_sentinel}: {{{{ lookup.secret_reference }}}}",
            lookup={"secret_reference": secret_reference_sentinel},
            response_field="answer",
        )
    )
    source.on_start(source_context)
    runtime_provider = source._provider
    assert isinstance(runtime_provider, OpenRouterLLMProvider)
    assert runtime_provider._request_headers["Authorization"] == f"Bearer {credential_sentinel}"

    provider = FakeProvider(
        LLMQueryResult(
            content=response_sentinel,
            usage=TokenUsage.known(prompt_tokens=5, completion_tokens=2),
            model="served-model",
            finish_reason=FinishReason.STOP,
        ),
        close_error=RuntimeError("provider cleanup failed"),
    )
    _install_provider(source, provider)

    rows = source.load(source_context)
    row = next(rows)
    assert row.row["answer"] == response_sentinel
    assert provider.messages == [
        [
            ChatMessage(
                role="user",
                content=f"{prompt_sentinel}: {secret_reference_sentinel}",
            )
        ]
    ]

    provider_error_context = make_operation_context(
        run_id="test-run-provider-error",
        plugin_name="llm",
    )
    provider_error_context.telemetry_emit = capture_then_fail
    provider_error_source = LLMSource(
        openrouter_config(
            api_key=credential_sentinel,
            prompt_template=f"{prompt_sentinel}: {{{{ lookup.secret_reference }}}}",
            lookup={"secret_reference": secret_reference_sentinel},
            response_field="answer",
        )
    )
    provider_error_source.on_start(provider_error_context)
    provider_error = FakeProvider(
        LLMClientError(f"provider error body: {provider_body_sentinel}", retryable=False),
        close_error=RuntimeError("provider cleanup failed"),
    )
    _install_provider(provider_error_source, provider_error)

    with capture_logs() as logs, pytest.raises(LLMClientError) as exc_info:
        cast(Generator[SourceRow, None, None], rows).close()
        list(provider_error_source.load(provider_error_context))

    assert provider.close_calls == 1
    assert provider_error.calls == 1
    assert provider_error.close_calls == 1
    assert provider_body_sentinel in str(exc_info.value)
    assert len(events) == 2
    assert logs == [
        {
            "event": "resource_cleanup_telemetry_failed",
            "log_level": "warning",
            "component": "llm_source",
            "resource": "provider",
            "error_type": "RuntimeError",
        },
        {
            "event": "resource_cleanup_telemetry_failed",
            "log_level": "warning",
            "component": "llm_source",
            "resource": "provider",
            "error_type": "RuntimeError",
        },
    ]

    exporter = ConsoleExporter()
    exporter.configure({"format": "json", "output": "stdout"})
    console = StringIO()
    exporter._stream = console
    contexts = (source_context, provider_error_context)
    for event, context in zip(events, contexts, strict=True):
        operation_id = context.operation_id
        assert operation_id is not None
        recorder = context.landscape
        assert recorder is not None
        operation = cast(Any, recorder)._execution.get_operation(operation_id)
        assert operation is not None
        assert operation.operation_type == "source_load"
        assert operation.run_id == context.run_id
        assert operation.node_id == context.node_id
        assert event.to_dict() == {
            "timestamp": event.timestamp,
            "run_id": context.run_id,
            "component": "llm_source",
            "resource": "provider",
            "error_type": "RuntimeError",
            "suppressed": True,
            "state_id": None,
            "operation_id": operation_id,
            "token_id": None,
        }
        exporter.export(event)

    serialized_console = console.getvalue()
    console_events = [json.loads(line) for line in serialized_console.splitlines()]
    assert console_events == [
        {
            "timestamp": event.timestamp.isoformat(),
            "run_id": context.run_id,
            "component": "llm_source",
            "resource": "provider",
            "error_type": "RuntimeError",
            "suppressed": True,
            "state_id": None,
            "operation_id": context.operation_id,
            "token_id": None,
            "event_type": "ResourceCleanupFailed",
        }
        for event, context in zip(events, contexts, strict=True)
    ]

    captured_logs = json.dumps(logs, sort_keys=True)
    for sentinel in sentinels:
        assert all(sentinel not in repr(event) for event in events)
        assert sentinel not in serialized_console
        assert sentinel not in captured_logs


def test_initialization_cleanup_without_load_operation_emits_no_correlated_event(
    openrouter_config: Callable[..., dict[str, Any]],
    source_context: PluginContext,
) -> None:
    source_context.operation_id = None
    events: list[object] = []
    source_context.telemetry_emit = events.append
    provider = FakeProvider(close_error=RuntimeError("provider cleanup failed"))
    source = LLMSource(openrouter_config(response_field="answer"))

    with (
        patch.object(source, "_create_provider", return_value=provider),
        patch(
            "elspeth.plugins.sources.llm.source.create_langfuse_tracer",
            side_effect=RuntimeError("tracer initialization failed"),
        ),
        patch("elspeth.plugins.sources.llm.source.logger") as mock_logger,
        pytest.raises(RuntimeError, match="tracer initialization failed"),
    ):
        source.on_start(source_context)

    assert provider.close_calls == 1
    assert events == []
    mock_logger.warning.assert_called_once_with(
        "resource_cleanup_telemetry_failed",
        component="llm_source",
        resource="provider",
        cleanup_error_type="RuntimeError",
        failure_reason="missing_operation_parent",
        run_id="test-run",
    )


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


def test_error_trace_failure_does_not_replace_provider_error(
    source: LLMSource,
    source_context: PluginContext,
) -> None:
    provider = FakeProvider(LLMClientError("provider failed", retryable=False))
    tracer = RecordingTracer()
    _install_provider(source, provider)
    source._tracer = tracer

    with (
        patch.object(tracer, "record_error", side_effect=RuntimeError("tracing unavailable")),
        patch("elspeth.plugins.sources.llm.source.logger") as mock_logger,
        pytest.raises(LLMClientError, match="provider failed"),
    ):
        list(source.load(source_context))

    mock_logger.warning.assert_called_once_with(
        "llm_trace_emission_failed",
        plugin="llm",
        error_type="RuntimeError",
    )


@pytest.mark.parametrize("failure", [FrameworkBugError("trace invariant failed"), KeyboardInterrupt(), SystemExit(17)])
def test_success_trace_does_not_suppress_unsuppressible_failures(
    source: LLMSource,
    failure: BaseException,
) -> None:
    tracer = RecordingTracer()
    source._tracer = tracer

    with (
        patch.object(tracer, "record_success", side_effect=failure),
        pytest.raises(type(failure)),
    ):
        source._trace_success(
            parent=LLMAuditParent.for_operation(operation_id="operation-1"),
            prompt="prompt",
            response_content="response",
            model="served-model",
            usage=TokenUsage.unknown(),
            latency_ms=1.0,
        )


@pytest.mark.parametrize("failure", [FrameworkBugError("trace invariant failed"), KeyboardInterrupt(), SystemExit(17)])
def test_error_trace_does_not_suppress_unsuppressible_failures(
    source: LLMSource,
    failure: BaseException,
) -> None:
    tracer = RecordingTracer()
    source._tracer = tracer

    with (
        patch.object(tracer, "record_error", side_effect=failure),
        pytest.raises(type(failure)),
    ):
        source._trace_error(
            parent=LLMAuditParent.for_operation(operation_id="operation-1"),
            prompt="prompt",
            error_message="provider failed",
            model="configured-model",
            latency_ms=1.0,
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
            schema={"mode": "observed"},
            on_validation_failure=destination,
        )
    )
    _install_runtime_rejecting_schema(source)
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
            schema={"mode": "observed"},
            on_validation_failure=destination,
        )
    )
    _install_runtime_rejecting_schema(source)
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
    assert "runtime_preflight" not in dir(source)
    assert source.get_agent_assistance(issue_code=None) is not None
    assert source.output_semantics().fields[0].field_name == "answer"
    # ADR-039: a generative producer makes a POSITIVE claim about framing.
    # UNKNOWN would be an abstention, and compare_semantic can never CONFLICT
    # against an abstention. NOTE this declaration is not yet reachable from
    # the composer's semantic validator (BaseSource has no output_semantics()
    # hook), so it is pinned here at the plugin rather than through a pipeline.
    assert source.output_semantics().fields[0].text_framing is TextFraming.UNCONSTRAINED
    assert source.output_semantics().fields[0].content_kind is ContentKind.UNKNOWN
    discriminator, variants = source.discriminated_variants()
    assert discriminator == "provider"
    assert set(variants) == {"azure", "openrouter", "bedrock", "gateway"}
    assert source.get_config_schema()["discriminator"]["propertyName"] == "provider"
    assert source.probe_config()["provider"] == "openrouter"
    assert source.declared_guaranteed_fields == frozenset({"answer", "answer_usage", "answer_model"})


def test_source_does_not_inherit_or_delegate_to_transform(source: LLMSource) -> None:
    from elspeth.plugins.transforms.llm.transform import LLMTransform

    assert LLMTransform not in type(source).__mro__
    assert "_strategy" not in dir(source)
    assert "_query_executor" not in dir(source)
