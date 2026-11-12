"""Regression tests for the 20261111-plugins-review P3 LLM-core sweep findings."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.results import TransformResult
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.templates import ALL_FIELDS
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig, resolve_image_parts
from elspeth.plugins.transforms.llm.langfuse import ActiveLangfuseTracer, LangfuseTracer, create_langfuse_tracer
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, QuerySpec
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.templates import PromptTemplate
from elspeth.plugins.transforms.llm.tracing import LangfuseTracingConfig, TracingConfig, parse_tracing_config
from elspeth.plugins.transforms.llm.transform import MultiQueryStrategy, SingleQueryStrategy
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.unit.plugins.llm.test_image_inputs import FakePayloadStore


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    try:
        call()
    except Exception as exc:
        return exc
    return None


def _llm_config_dict(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "prompt_template": "hi",
    }
    config.update(overrides)
    return config


class TestFinding32ContradictoryPoolDelayKnobs:
    """Sweep finding #32: min_dispatch_delay_ms > max_dispatch_delay_ms must fail at config time."""

    @pytest.mark.parametrize("pool_size", [1, 4])
    def test_from_dict_refuses_min_delay_above_max_delay(self, pool_size: int) -> None:
        raised = _raised_by(
            lambda: LLMConfig.from_dict(
                _llm_config_dict(pool_size=pool_size, min_dispatch_delay_ms=9000, max_dispatch_delay_ms=10),
            )
        )
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError at from_dict, got {raised!r}"
        assert "min_dispatch_delay_ms" in str(raised)

    def test_pooled_config_refuses_the_other_pool_invariants_at_from_dict(self) -> None:
        raised = _raised_by(
            lambda: LLMConfig.from_dict(
                _llm_config_dict(pool_size=4, min_dispatch_delay_ms=0, recovery_step_ms=0),
            )
        )
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError at from_dict, got {raised!r}"
        assert "recovery_step_ms" in str(raised)

    def test_consistent_pool_knobs_still_build_a_pool_config(self) -> None:
        config = LLMConfig.from_dict(_llm_config_dict(pool_size=4, min_dispatch_delay_ms=10, max_dispatch_delay_ms=9000))
        pool = config.pool_config
        assert pool is not None
        assert pool.min_dispatch_delay_ms == 10
        assert pool.max_dispatch_delay_ms == 9000


class _SilentTracer:
    def record_success(self, **kwargs: Any) -> None:
        return None

    def record_error(self, **kwargs: Any) -> None:
        return None


def _identity_contract(contract: SchemaContract) -> SchemaContract:
    return contract


def _identity_row(row: PipelineRow) -> PipelineRow:
    return row


def _served_model_provider(content: str) -> Mock:
    provider = Mock(spec=LLMProvider)
    provider.execute_query.return_value = LLMQueryResult(
        content=content,
        usage=TokenUsage.known(prompt_tokens=10, completion_tokens=5),
        model="served-model",
        finish_reason=FinishReason.STOP,
    )
    return provider


def _strategy_context() -> PluginContext:
    return make_context(
        state_id="state-45",
        run_id="run-45",
        token=TokenInfo(row_id="row-1", token_id="token-1", row_data=make_pipeline_row({})),
    )


def _fields_written(result: TransformResult, input_row: dict[str, Any]) -> set[str]:
    assert result.row is not None
    return set(result.row.to_dict()) - set(input_row)


class TestFinding45SuccessReasonFieldInventoryAndModel:
    """Sweep finding #45: fields_added must list every written field; model keys must mean the same in both modes."""

    def _single_result(self) -> TransformResult:
        strategy = SingleQueryStrategy(
            template=PromptTemplate("Classify: {{ row.text }}"),
            row_projection=ALL_FIELDS,
            system_prompt=None,
            system_prompt_source=None,
            model="configured-model",
            temperature=0.0,
            max_tokens=None,
            response_field="llm_response",
            align_output_contract=_identity_contract,
            apply_declared_output_field_contracts=_identity_contract,
            output_fields=(OutputFieldConfig(suffix="score", type="integer"),),
        )
        return strategy.execute(
            make_pipeline_row({"text": "hello"}),
            _strategy_context(),
            provider=_served_model_provider('{"score": 7}'),
            tracer=_SilentTracer(),
        )

    def _multi_result(self) -> TransformResult:
        strategy = MultiQueryStrategy(
            query_specs=[QuerySpec(name="sentiment", input_fields=MappingProxyType({"text": "text"}))],
            template=PromptTemplate("Analyze: {{ row.text }}"),
            row_projection=ALL_FIELDS,
            system_prompt=None,
            system_prompt_source=None,
            model="configured-model",
            temperature=0.0,
            max_tokens=None,
            response_field="llm_response",
            align_output_contract=_identity_contract,
            align_output_row_contract=_identity_row,
            apply_declared_output_field_contracts=_identity_contract,
            executor=None,
        )
        return strategy.execute(
            make_pipeline_row({"text": "hello"}),
            _strategy_context(),
            provider=_served_model_provider("positive"),
            tracer=_SilentTracer(),
        )

    def test_single_mode_fields_added_lists_every_written_field(self) -> None:
        result = self._single_result()
        assert result.success_reason is not None
        written = _fields_written(result, {"text": "hello"})
        assert written == {"llm_response", "llm_response_usage", "llm_response_model", "score"}
        assert set(result.success_reason["fields_added"]) == written

    def test_single_mode_metadata_model_is_the_configured_model_and_served_model_is_separate(self) -> None:
        result = self._single_result()
        assert result.success_reason is not None
        metadata = result.success_reason["metadata"]
        assert metadata["model"] == "configured-model"
        assert "served_model" in metadata, sorted(metadata)
        assert metadata["served_model"] == "served-model"

    def test_multi_mode_metadata_records_the_served_model_per_query(self) -> None:
        result = self._multi_result()
        assert result.success_reason is not None
        metadata = result.success_reason["metadata"]
        assert metadata["model"] == "configured-model"
        assert "sentiment_served_model" in metadata, sorted(metadata)
        assert metadata["sentiment_served_model"] == "served-model"
        assert set(result.success_reason["fields_added"]) == _fields_written(result, {"text": "hello"})


class TestFinding139LangfuseResourceManagerSharedByPublicKey:
    """Sweep finding #139: a cached Langfuse resource manager must not redirect a later config's traces."""

    @pytest.fixture
    def isolated_langfuse(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Real SDK client with an isolated tracer provider, exporter and HTTP transport (no egress)."""
        import langfuse
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        provider = TracerProvider()
        http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
        monkeypatch.setattr(
            langfuse,
            "Langfuse",
            partial(langfuse.Langfuse, tracer_provider=provider, httpx_client=http_client, span_exporter=InMemorySpanExporter()),
        )
        yield
        provider.shutdown()
        http_client.close()

    @staticmethod
    def _shutdown(tracer: LangfuseTracer) -> None:
        assert isinstance(tracer, ActiveLangfuseTracer)
        tracer.client.shutdown()
        resources = tracer.client._resources
        assert resources is not None
        resources.prompt_cache._task_manager.shutdown()

    @pytest.mark.parametrize(
        ("second_host", "second_secret"),
        [
            pytest.param("https://lf-b.example.invalid", "sk-a", id="different-host"),
            pytest.param("https://lf-a.example.invalid", "sk-rotated", id="rotated-secret"),
        ],
    )
    def test_second_config_with_same_public_key_is_refused(self, isolated_langfuse: None, second_host: str, second_secret: str) -> None:
        public_key = f"pk-p3-{uuid4().hex}"
        first = create_langfuse_tracer(
            "first_transform",
            LangfuseTracingConfig(public_key=public_key, secret_key="sk-a", host="https://lf-a.example.invalid"),
        )
        try:
            raised = _raised_by(
                lambda: create_langfuse_tracer(
                    "second_transform",
                    LangfuseTracingConfig(public_key=public_key, secret_key=second_secret, host=second_host),
                )
            )
            assert isinstance(raised, RuntimeError), f"expected RuntimeError for a divergent config, got {raised!r}"
            assert "public_key" in str(raised)
            assert second_secret not in str(raised)
        finally:
            self._shutdown(first)

    def test_identical_config_with_same_public_key_is_admitted(self, isolated_langfuse: None) -> None:
        config = LangfuseTracingConfig(public_key=f"pk-p3-{uuid4().hex}", secret_key="sk-a", host="https://lf-a.example.invalid")
        first = create_langfuse_tracer("first_transform", config)
        try:
            second = create_langfuse_tracer("second_transform", config)
            assert isinstance(second, ActiveLangfuseTracer)
        finally:
            self._shutdown(first)


class TestFinding148RequiredImageEmptyList:
    """Sweep finding #148: a required image field holding no references must not send a text-only call."""

    @pytest.mark.parametrize("empty", [[], ()], ids=["list", "tuple"])
    def test_required_image_with_empty_reference_list_is_a_row_error(self, empty: list[str] | tuple[str, ...]) -> None:
        store = FakePayloadStore(contents={})
        result = resolve_image_parts(
            make_pipeline_row({"pic": empty}),
            payload_store=store,
            specs=[ImageInputConfig(field="pic", format="png")],
            max_image_bytes=10_000_000,
            max_images_per_call=20,
        )
        assert isinstance(result, TransformResult), f"expected a row-level error, got {result!r}"
        assert result.status == "error"
        assert result.reason == {"reason": "invalid_input", "field": "pic", "error_type": "empty_image_list"}
        assert result.retryable is False
        assert store.retrieve_calls == []

    def test_optional_image_with_empty_reference_list_resolves_to_no_parts(self) -> None:
        result = resolve_image_parts(
            make_pipeline_row({"pic": []}),
            payload_store=FakePayloadStore(contents={}),
            specs=[ImageInputConfig(field="pic", format="png", required=False)],
            max_image_bytes=10_000_000,
            max_images_per_call=20,
        )
        assert result == ()


class TestFinding151TracingWithoutProviderDropsKeys:
    """Sweep finding #151: a 'none' tracing config must not silently discard other keys."""

    @pytest.mark.parametrize(
        "config",
        [
            pytest.param({"connection_string": "InstrumentationKey=canary-value"}, id="provider-omitted"),
            pytest.param({"provider": "none", "host": "https://canary-value.example.com"}, id="provider-none-with-keys"),
        ],
    )
    def test_none_provider_with_other_keys_is_refused(self, config: dict[str, Any]) -> None:
        raised = _raised_by(lambda: parse_tracing_config(config))
        assert isinstance(raised, ValueError), f"expected ValueError, got {raised!r}"
        message = str(raised)
        assert "provider" in message
        assert "canary-value" not in message

    @pytest.mark.parametrize("config", [{}, {"provider": "none"}], ids=["empty", "explicit-none"])
    def test_bare_none_config_still_means_no_tracing(self, config: dict[str, Any]) -> None:
        assert parse_tracing_config(config) == TracingConfig(provider="none")


class TestFinding116UpstreamOperationalSuffixCollision:
    """Sweep finding #116: upstream multi-query refuses suffixes the transform's own fields would overwrite.

    Single-prompt mode refuses an output suffix equal to ``response_field`` or
    its ``_usage``/``_model`` companions. Upstream multi-query mode with the
    default (query-name) stem used to accept the same collision and then let
    the raw response and operational values silently overwrite the extracted
    value at row assembly. The collision is refused at config time instead.
    """

    @staticmethod
    def _queries(suffix: str, *, explicit_none: bool) -> dict[str, Any]:
        definition: dict[str, Any] = {
            "input_fields": {"text": "text"},
            "response_format": "structured",
            "output_fields": [{"suffix": suffix, "type": "integer"}],
        }
        if explicit_none:
            definition["structured_output_prefix"] = None
        return {"quality": definition}

    @staticmethod
    def _config(queries: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        return _llm_config_dict(required_input_fields=["text"], queries=queries, **overrides)

    @pytest.mark.parametrize("explicit_none", [False, True], ids=["prefix-omitted", "prefix-explicit-none"])
    @pytest.mark.parametrize(
        ("response_field", "suffix"),
        [
            ("llm_response", "llm_response"),
            ("llm_response", "llm_response_usage"),
            ("llm_response", "llm_response_model"),
            ("answer", "answer"),
            ("answer", "answer_model"),
        ],
    )
    def test_upstream_default_stem_refuses_operational_collision(self, response_field: str, suffix: str, explicit_none: bool) -> None:
        raised = _raised_by(
            lambda: LLMConfig.from_dict(self._config(self._queries(suffix, explicit_none=explicit_none), response_field=response_field))
        )
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError, got {raised!r}"
        assert "collides with the transform's own response/operational fields" in str(raised)
        assert f"quality_{suffix}" in str(raised)

    def test_upstream_default_stem_refuses_cross_query_operational_collision(self) -> None:
        queries = {
            "a": {"input_fields": {"text": "text"}, "output_fields": [{"suffix": "b_llm_response", "type": "string"}]},
            "a_b": {"input_fields": {"text": "text"}},
        }
        raised = _raised_by(lambda: LLMConfig.from_dict(self._config(queries)))
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError, got {raised!r}"
        assert "collides with the transform's own response/operational fields" in str(raised)
        assert "a_b_llm_response" in str(raised)

    def test_upstream_default_stem_non_colliding_suffix_is_admitted(self) -> None:
        config = LLMConfig.from_dict(self._config(self._queries("score", explicit_none=False)))
        assert [spec.structured_field_name("score") for spec in config.resolved_queries()] == ["quality_score"]
