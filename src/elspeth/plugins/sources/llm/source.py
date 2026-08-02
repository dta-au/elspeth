"""Source-native single-prompt LLM plugin."""

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Iterator, Mapping
from functools import reduce
from operator import or_
from typing import Annotated, Any, cast

import structlog
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic import Field as PydanticField

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import Determinism, PluginSchema, SourceRow
from elspeth.contracts.contexts import LifecycleContext, SourceContext
from elspeth.contracts.contract_builder import ContractBuilder
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability, WebConfigAuthority
from elspeth.contracts.schema_contract_factory import create_contract_from_config
from elspeth.contracts.token_usage import TokenUsage
from elspeth.contracts.value_source import register_value_source_plugin
from elspeth.core.landscape.plugin_audit_writer import PluginAuditWriterAdapter
from elspeth.core.llm_profiles import require_lowered_llm_profile_alias
from elspeth.plugins.infrastructure.base import BaseSource
from elspeth.plugins.infrastructure.clients.base import TelemetryEmitCallback
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.infrastructure.telemetry import emit_resource_cleanup_failed, make_warn_telemetry_before_start
from elspeth.plugins.sources._safe_validation_errors import safe_validation_error_text
from elspeth.plugins.sources.llm.config import (
    SOURCE_PROVIDER_CONFIGS,
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    LLMSourceConfig,
    OpenRouterLLMSourceConfig,
)
from elspeth.plugins.transforms.llm import populate_llm_operational_fields
from elspeth.plugins.transforms.llm.langfuse import LangfuseTracer, create_langfuse_tracer
from elspeth.plugins.transforms.llm.provider import LLMAuditParent, LLMProvider, LLMQueryResult, classify_finish_reason_failure
from elspeth.plugins.transforms.llm.providers.azure import AzureLLMProvider, _configure_azure_monitor
from elspeth.plugins.transforms.llm.providers.bedrock import BedrockLLMProvider
from elspeth.plugins.transforms.llm.providers.gateway import GatewayLLMProvider
from elspeth.plugins.transforms.llm.providers.openrouter import OpenRouterLLMProvider
from elspeth.plugins.transforms.llm.templates import PromptTemplate
from elspeth.plugins.transforms.llm.tracing import AzureAITracingConfig, TracingConfig, parse_tracing_config
from elspeth.plugins.transforms.llm.validation import strip_markdown_fences

logger = structlog.get_logger(__name__)


class _LLMSourceLoadSession(Iterator[SourceRow]):
    """Own the load iterator and its exactly-once resource teardown."""

    def __init__(
        self,
        source: LLMSource,
        rows: Generator[SourceRow, None, None],
        shutdown_event: threading.Event | None,
    ) -> None:
        self._source = source
        self._rows = rows
        self._shutdown_event = shutdown_event
        self._closed = False

    def __next__(self) -> SourceRow:
        if self._closed:
            raise StopIteration
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            self._closed = True
            try:
                self._rows.close()
            except contract_errors.TIER_1_ERRORS:
                raise
            except Exception as exc:
                self._source._report_cleanup_failure(resource="load_iterator", error=exc, suppressed=True)
            finally:
                self._source._release_load_resources(suppress_errors=True)
            raise StopIteration
        try:
            return next(self._rows)
        except StopIteration:
            self._closed = True
            self._source._release_load_resources(
                suppress_errors=(
                    self._source._validation_failure_handled or (self._shutdown_event is not None and self._shutdown_event.is_set())
                ),
            )
            raise
        except BaseException:
            self._closed = True
            self._source._release_load_resources(suppress_errors=True)
            raise

    def close(self) -> None:
        """Cancel iteration without allowing cleanup to replace cancellation."""
        if self._closed:
            return
        self._closed = True
        try:
            self._rows.close()
        except BaseException:
            self._source._release_load_resources(suppress_errors=True)
            raise
        self._source._release_load_resources(suppress_errors=True)


class LLMSource(BaseSource):
    """Issue one authored prompt and emit at most one validated source row."""

    name = "llm"
    determinism = Determinism.NON_DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:c63cd6b96f4c7372"
    web_config_authority = WebConfigAuthority.OPERATOR_PROFILED
    policy_capabilities = frozenset({CapabilityDeclaration(PluginCapability.LLM)})
    capability_tags: tuple[str, ...] = ("llm", "generation", "single-row")
    config_model = LLMSourceConfig

    usage_when_to_use: str | None = (
        "Use an operator-approved LLM profile when a pipeline should begin with one authored prompt and one generated row, "
        "with the served model and reported token usage available to downstream nodes."
    )
    usage_when_not_to_use: str | None = (
        "Do not use this source for row-by-row enrichment, streaming generation, or deterministic replay. Use the LLM transform "
        "when prompts depend on incoming rows."
    )
    example_use: str | None = (
        "sources:\n"
        "  generated_brief:\n"
        "    plugin: llm\n"
        "    on_success: output\n"
        "    options:\n"
        "      profile: approved-generation\n"
        "      prompt_template: 'Write a concise audit briefing.'\n"
        "      response_field: briefing\n"
        "      schema: {mode: observed}\n"
        "      on_validation_failure: discard"
    )
    _provider: LLMProvider | None

    @classmethod
    def get_config_model(cls, config: dict[str, Any] | None = None) -> type[LLMSourceConfig]:
        provider = config.get("provider") if config is not None else None
        if provider is None:
            return LLMSourceConfig
        if type(provider) is not str or provider not in SOURCE_PROVIDER_CONFIGS:
            raise ValueError(f"Unknown LLM provider {provider!r}. Valid providers: {sorted(SOURCE_PROVIDER_CONFIGS)}")
        return SOURCE_PROVIDER_CONFIGS[provider]

    @classmethod
    def get_config_schema(cls) -> dict[str, Any]:
        variants = tuple(SOURCE_PROVIDER_CONFIGS.values())
        union_type = reduce(or_, variants[1:], variants[0])
        adapter: TypeAdapter[Any] = TypeAdapter(Annotated[union_type, PydanticField(discriminator="provider")])
        schema: dict[str, Any] = adapter.json_schema(ref_template="#/$defs/{model}")
        for config_cls in variants:
            variant_schema = schema["$defs"][config_cls.__name__]
            required = variant_schema["required"]
            if "provider" not in required:
                required.append("provider")
        return schema

    @classmethod
    def discriminated_variants(cls) -> tuple[str, dict[str, type[BaseModel]]]:
        return ("provider", dict(SOURCE_PROVIDER_CONFIGS))

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {
            "provider": "openrouter",
            "model": "openai/gpt-4o",
            "api_key": "probe-key",
            "prompt_template": "Return one probe response.",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }

    def __init__(self, config: dict[str, Any]) -> None:
        profile_alias = require_lowered_llm_profile_alias(config)
        if profile_alias is None:
            base_config = config
        else:
            # Consume the nominal admission proof at the plugin boundary and
            # retain only the opaque plain-string alias in audit config.
            base_config = dict(config)
            base_config["profile_alias"] = profile_alias
        super().__init__(base_config)
        provider_config = {key: value for key, value in base_config.items() if key != "profile_alias"}
        config_cls = self.get_config_model(provider_config)
        self._config = cast(
            "AzureOpenAILLMSourceConfig | OpenRouterLLMSourceConfig | BedrockLLMSourceConfig | GatewayLLMSourceConfig",
            config_cls.from_dict(provider_config, plugin_name=self.name),
        )

        self._model = self._config.model
        self._template = PromptTemplate(
            self._config.prompt_template,
            template_source=self._config.prompt_template_source,
            lookup_data=self._config.lookup,
            lookup_source=self._config.lookup_source,
        )
        self._system_prompt = self._config.system_prompt
        self._temperature = self._config.temperature
        self._max_tokens = self._config.max_tokens
        self._response_field = self._config.response_field
        self._on_validation_failure = self._config.on_validation_failure

        self._schema_config = self._config.schema_config
        self._initialize_declared_guaranteed_fields(self._schema_config)
        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config,
            "LLMSourceRowSchema",
            allow_coercion=True,
        )
        self.output_schema = self._schema_class
        initial_contract = create_contract_from_config(self._schema_config)
        if initial_contract.locked:
            self.set_schema_contract(initial_contract)
            self._contract_builder: ContractBuilder | None = None
        else:
            self._contract_builder = ContractBuilder(initial_contract)

        tracing_config = parse_tracing_config(self._config.tracing) if self._config.tracing else None
        if isinstance(tracing_config, AzureAITracingConfig) and not isinstance(self._config, AzureOpenAILLMSourceConfig):
            raise ValueError(f"azure_ai tracing requires provider='azure', got {self._config.provider!r}")
        self._tracing_config: TracingConfig | None = tracing_config
        self._tracer: LangfuseTracer | None = None
        self._provider = None
        self._run_id: str | None = None
        self._load_operation_id: str | None = None
        self._telemetry_emit: TelemetryEmitCallback = make_warn_telemetry_before_start(logger)
        self._limiter: Any = None
        self._load_started = False
        self._validation_failure_handled = False

    @property
    def provider_config(
        self,
    ) -> AzureOpenAILLMSourceConfig | OpenRouterLLMSourceConfig | BedrockLLMSourceConfig | GatewayLLMSourceConfig:
        """Return the strict typed provider config used by cross-cutting preflight checks."""
        return self._config

    def on_start(self, ctx: LifecycleContext) -> None:
        super().on_start(ctx)
        recorder = ctx.landscape
        if not isinstance(recorder, PluginAuditWriterAdapter):
            raise FrameworkBugError("LLMSource requires a concrete Landscape PluginAuditWriterAdapter")

        # The engine calls on_start once, but keeping direct lifecycle use
        # leak-free makes the BaseSource contract's no-raise hook probe safe.
        self.close()

        self._run_id = ctx.run_id
        self._telemetry_emit = ctx.telemetry_emit
        limiter_name = (
            "azure_openai"
            if isinstance(self._config, AzureOpenAILLMSourceConfig)
            else "bedrock"
            if isinstance(self._config, BedrockLLMSourceConfig)
            else "gateway"
            if isinstance(self._config, GatewayLLMSourceConfig)
            else "openrouter"
        )
        self._limiter = ctx.rate_limit_registry.get_limiter(limiter_name) if ctx.rate_limit_registry is not None else None
        self._provider = self._create_provider(recorder)
        try:
            self._tracer = create_langfuse_tracer(transform_name=self.name, tracing_config=self._tracing_config)
            if isinstance(self._tracing_config, AzureAITracingConfig):
                _configure_azure_monitor(self._tracing_config)
        except BaseException:
            self._release_load_resources(suppress_errors=True)
            raise

    @staticmethod
    def _require_load_operation_id(operation_id: object) -> str:
        if type(operation_id) is not str or not operation_id.strip():
            raise FrameworkBugError("LLMSource.load requires a non-empty source-load operation_id")
        return operation_id

    def load(self, ctx: SourceContext) -> Iterator[SourceRow]:
        if self._load_started:
            raise FrameworkBugError("LLMSource can only be loaded once per lifecycle")
        if self._provider is None or not self._on_start_called:
            raise FrameworkBugError("LLMSource.on_start() must initialize the provider before load()")
        operation_id = self._require_load_operation_id(ctx.operation_id)
        self._load_operation_id = operation_id
        self._load_started = True
        return _LLMSourceLoadSession(self, self._load_once(operation_id, ctx), ctx.shutdown_event)

    def _load_once(self, operation_id: str, ctx: SourceContext) -> Generator[SourceRow, None, None]:
        provider = self._provider
        if provider is None:
            raise FrameworkBugError("LLMSource provider disappeared after load admission")
        if ctx.shutdown_event is not None and ctx.shutdown_event.is_set():
            return
        rendered = self._template.render_static_with_metadata()
        if not rendered.prompt.strip():
            raise LLMClientError("LLM source rendered an empty prompt", retryable=False)

        messages: list[dict[str, str]] = []
        if self._system_prompt:
            messages.append({"role": "system", "content": self._system_prompt})
        messages.append({"role": "user", "content": rendered.prompt})
        trace_parent = LLMAuditParent.for_operation(operation_id=operation_id)
        started_at = time.monotonic()
        try:
            result = provider.execute_query(
                messages,
                model=self._model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                audit_parent=trace_parent,
            )
        except LLMClientError as exc:
            latency_ms = (time.monotonic() - started_at) * 1000
            self._trace_error(
                parent=trace_parent,
                prompt=rendered.prompt,
                error_message=str(exc),
                model=self._model,
                latency_ms=latency_ms,
            )
            raise
        latency_ms = (time.monotonic() - started_at) * 1000
        yield from self._validated_result_rows(
            result,
            ctx,
            trace_parent=trace_parent,
            prompt=rendered.prompt,
            latency_ms=latency_ms,
        )

    def _validated_result_rows(
        self,
        result: LLMQueryResult,
        ctx: SourceContext,
        *,
        trace_parent: LLMAuditParent,
        prompt: str,
        latency_ms: float,
    ) -> Iterator[SourceRow]:
        failure = classify_finish_reason_failure(result.finish_reason)
        if failure is not None:
            self._trace_error(
                parent=trace_parent,
                prompt=prompt,
                error_message=failure.error_message,
                model=result.model,
                latency_ms=latency_ms,
            )
            raise LLMClientError(failure.error_message, retryable=False)

        content = strip_markdown_fences(result.content)
        if not content.strip():
            self._trace_error(
                parent=trace_parent,
                prompt=prompt,
                error_message="LLM source returned empty content after markdown fence cleanup",
                model=result.model,
                latency_ms=latency_ms,
            )
            raise LLMClientError("LLM source returned empty content after markdown fence cleanup", retryable=False)

        output: dict[str, Any] = {
            self._response_field: content,
        }
        populate_llm_operational_fields(
            output,
            self._response_field,
            usage=result.usage,
            model=result.model,
        )
        self._trace_success(
            parent=trace_parent,
            prompt=prompt,
            response_content=content,
            model=result.model,
            usage=result.usage,
            latency_ms=latency_ms,
        )

        try:
            validated = self._schema_class.model_validate(output)
            validated_row = validated.to_row()
        except ValidationError as exc:
            yield from self._validation_failure(output, safe_validation_error_text(exc), ctx)
            return

        if self._contract_builder is not None:
            contract = self._contract_builder.process_first_row(
                validated_row,
                {field_name: field_name for field_name in validated_row},
            )
            self.set_schema_contract(contract)
        contract = self.require_schema_contract()
        violations = contract.validate(validated_row) if contract.locked else ()
        if violations:
            yield from self._validation_failure(validated_row, "; ".join(str(item) for item in violations), ctx)
            return

        yield SourceRow.valid(validated_row, contract=contract, source_row_index=0)

    def _validation_failure(self, row: Mapping[str, Any], error: str, ctx: SourceContext) -> Iterator[SourceRow]:
        self._validation_failure_handled = True
        ctx.record_validation_error(
            row=row,
            error=error,
            schema_mode=self._schema_config.mode,
            destination=self._on_validation_failure,
        )
        if self._on_validation_failure != "discard":
            yield SourceRow.quarantined(
                row=row,
                error=error,
                destination=self._on_validation_failure,
                source_row_index=0,
            )

    def _trace_success(
        self,
        *,
        parent: LLMAuditParent,
        prompt: str,
        response_content: str,
        model: str,
        usage: TokenUsage,
        latency_ms: float,
    ) -> None:
        tracer = self._tracer
        if tracer is None:
            return
        try:
            tracer.record_success(
                parent=parent,
                query_name="source",
                prompt=prompt,
                response_content=response_content,
                model=model,
                usage=usage,
                latency_ms=latency_ms,
                extra_metadata=None,
                system_prompt=self._system_prompt,
            )
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as trace_error:
            logger.warning(
                "llm_trace_emission_failed",
                plugin=self.name,
                error_type=type(trace_error).__name__,
            )

    def _trace_error(
        self,
        *,
        parent: LLMAuditParent,
        prompt: str,
        error_message: str,
        model: str,
        latency_ms: float,
    ) -> None:
        tracer = self._tracer
        if tracer is None:
            return
        try:
            tracer.record_error(
                parent=parent,
                query_name="source",
                prompt=prompt,
                error_message=error_message,
                model=model,
                latency_ms=latency_ms,
                extra_metadata=None,
                system_prompt=self._system_prompt,
            )
        except contract_errors.TIER_1_ERRORS:
            raise
        except Exception as trace_error:
            logger.warning(
                "llm_trace_emission_failed",
                plugin=self.name,
                error_type=type(trace_error).__name__,
            )

    def _create_provider(self, recorder: PluginAuditWriterAdapter) -> LLMProvider:
        if self._run_id is None:
            raise FrameworkBugError("LLMSource run identity is unavailable during provider construction")
        common = {
            "recorder": recorder,
            "run_id": self._run_id,
            "telemetry_emit": self._telemetry_emit,
            "limiter": self._limiter,
            "resolved_prompt_template_hash": self._template.template_hash,
        }
        if isinstance(self._config, AzureOpenAILLMSourceConfig):
            return AzureLLMProvider(
                endpoint=self._config.endpoint,
                api_key=self._config.api_key,
                api_version=self._config.api_version,
                deployment_name=self._config.deployment_name,
                **common,
            )
        if isinstance(self._config, OpenRouterLLMSourceConfig):
            return OpenRouterLLMProvider(
                api_key=self._config.api_key,
                base_url=self._config.base_url,
                timeout_seconds=self._config.timeout_seconds,
                **common,
            )
        if isinstance(self._config, BedrockLLMSourceConfig):
            return BedrockLLMProvider(region_name=self._config.region_name, **common)
        if isinstance(self._config, GatewayLLMSourceConfig):
            return GatewayLLMProvider(
                endpoint=self._config.endpoint,
                api_key=self._config.api_key,
                contract_major=self._config.contract_major,
                required_capabilities=self._config.required_capabilities,
                timeout_seconds=self._config.timeout_seconds,
                **common,
            )
        raise FrameworkBugError(f"Unsupported LLM source config type: {type(self._config).__name__}")

    def close(self) -> None:
        self._release_load_resources(suppress_errors=False)

    def _report_cleanup_failure(self, *, resource: str, error: Exception, suppressed: bool) -> None:
        """Emit cleanup health or acknowledge why correlation is unavailable."""
        if self._run_id is not None and self._load_operation_id is not None:
            emit_resource_cleanup_failed(
                self._telemetry_emit,
                run_id=self._run_id,
                component="llm_source",
                resource=resource,
                error=error,
                suppressed=suppressed,
                state_id=None,
                operation_id=self._load_operation_id,
                token_id=None,
                logger=logger,
            )
            return
        logger.warning(
            "resource_cleanup_telemetry_failed",
            component="llm_source",
            resource=resource,
            cleanup_error_type=type(error).__name__,
            failure_reason="missing_operation_parent",
            run_id=self._run_id,
        )

    def _release_load_resources(self, *, suppress_errors: bool) -> None:
        """Detach and close every resource, preserving any primary load outcome."""
        provider, self._provider = self._provider, None
        tracer, self._tracer = self._tracer, None
        self._limiter = None
        failures: list[Exception] = []
        # Both resources are already detached. Defer Tier-1 propagation until
        # the tracer has been attempted, then raise the first invariant failure
        # regardless of the caller's ordinary-error suppression policy.
        tier_one_failures: list[Exception] = []
        for resource, close_resource in (
            ("provider", provider.close if provider is not None else None),
            ("tracer", tracer.flush if tracer else None),
        ):
            if close_resource is None:
                continue
            try:
                close_resource()
            except BaseException as exc:
                if isinstance(exc, contract_errors.TIER_1_ERRORS):
                    tier_one_failures.append(exc)
                elif isinstance(exc, Exception):
                    failures.append(exc)
                    self._report_cleanup_failure(resource=resource, error=exc, suppressed=suppress_errors)
                else:
                    raise
        self._telemetry_emit = make_warn_telemetry_before_start(logger)
        if tier_one_failures:
            raise tier_one_failures[0]
        if failures and not suppress_errors:
            raise failures[0]

    def output_semantics(self) -> Any:
        from elspeth.contracts.plugin_semantics import (
            ContentKind,
            FieldSemanticFacts,
            OutputSemanticDeclaration,
            SemanticValueType,
            TextFraming,
        )

        return OutputSemanticDeclaration(
            fields=(
                FieldSemanticFacts(
                    field_name=self._response_field,
                    content_kind=ContentKind.UNKNOWN,
                    text_framing=TextFraming.UNKNOWN,
                    value_type=SemanticValueType.STR,
                    fact_code="llm.source_response_field.string",
                    configured_by=("response_field",),
                ),
            )
        )

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code is not None:
            return None
        return PluginAssistance(
            plugin_name=cls.name,
            issue_code=None,
            summary="Call one operator-profiled LLM with a static prompt and emit at most one validated source row.",
            composer_hints=(
                "Use this source only when the prompt needs no incoming row data; use the LLM transform for row-dependent prompts.",
                "The response field and its usage/model diagnostic fields are emitted automatically.",
                "Choose quarantine when an invalid generated row must remain inspectable; choose discard only for intentional dropping.",
            ),
        )


register_value_source_plugin(LLMSource, config_attr="provider_config")


__all__ = ["LLMSource"]
