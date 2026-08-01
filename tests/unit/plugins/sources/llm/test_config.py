"""Configuration contract for the single-prompt LLM source."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.plugins.infrastructure.config_base import DataPluginConfig, PluginConfigError
from elspeth.plugins.sources.llm.config import (
    SOURCE_PROVIDER_CONFIGS,
    AzureOpenAILLMSourceConfig,
    BedrockLLMSourceConfig,
    GatewayLLMSourceConfig,
    LLMSourceConfig,
    OpenRouterLLMSourceConfig,
)
from elspeth.plugins.transforms.llm import build_llm_source_output_schema_config

TRANSFORM_ONLY_FIELDS = {
    "required_input_fields",
    "queries",
    "pool_size",
    "min_dispatch_delay_ms",
    "max_dispatch_delay_ms",
    "backoff_multiplier",
    "recovery_step_ms",
    "max_capacity_retry_seconds",
    "resolved_prompt_template_hash",
}


@pytest.mark.parametrize("provider", ["azure", "openrouter", "bedrock", "gateway"])
def test_source_variants_publish_only_single_request_fields(provider: str) -> None:
    model = SOURCE_PROVIDER_CONFIGS[provider]
    assert {
        "prompt_template",
        "system_prompt",
        "temperature",
        "max_tokens",
        "response_field",
        "schema_config",
        "on_validation_failure",
    } <= set(model.model_fields)
    assert TRANSFORM_ONLY_FIELDS.isdisjoint(model.model_fields)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("azure", AzureOpenAILLMSourceConfig),
        ("openrouter", OpenRouterLLMSourceConfig),
        ("bedrock", BedrockLLMSourceConfig),
        ("gateway", GatewayLLMSourceConfig),
    ],
)
def test_source_provider_models_are_rooted_in_data_plugin_config(
    provider: str,
    model: type[LLMSourceConfig],
) -> None:
    assert SOURCE_PROVIDER_CONFIGS[provider] is model
    assert issubclass(model, DataPluginConfig)
    assert model._plugin_component_type == "source"


def test_source_prompt_rejects_row_access_but_accepts_lookup(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(PluginConfigError, match="row"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(prompt_template="{{ row.text }}"),
            plugin_name="llm",
        )

    cfg = OpenRouterLLMSourceConfig.from_dict(
        openrouter_config(
            prompt_template="Summarise {{ lookup.topic }}",
            lookup={"topic": "audit"},
        ),
        plugin_name="llm",
    )
    assert cfg.prompt_template == "Summarise {{ lookup.topic }}"


def test_source_prompt_accepts_jinja_globals(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    cfg = OpenRouterLLMSourceConfig.from_dict(
        openrouter_config(prompt_template="{{ range(3) | join(',') }}"),
        plugin_name="llm",
    )
    assert cfg.prompt_template == "{{ range(3) | join(',') }}"


def test_source_prompt_rejects_undeclared_name(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(PluginConfigError, match="customer"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(prompt_template="Hello {{ customer }}"),
            plugin_name="llm",
        )


def test_source_prompt_rejects_invalid_syntax(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(PluginConfigError, match="Invalid Jinja2 template"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(prompt_template="{% if lookup.topic %}"),
            plugin_name="llm",
        )


def test_invalid_provider_is_rejected() -> None:
    with pytest.raises(PluginConfigError, match="provider"):
        LLMSourceConfig.from_dict(
            {
                "provider": "invalid",
                "prompt_template": "Hello",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
            plugin_name="llm",
        )


@pytest.mark.parametrize("response_field", ["", "   ", "response-field", "2response", "class", "for"])
def test_invalid_response_field_is_rejected(
    response_field: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError, match="response_field"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(response_field=response_field),
            plugin_name="llm",
        )


@pytest.mark.parametrize("response_field", ["class", "for"])
def test_keyword_response_field_reports_owned_source_config_error(
    response_field: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError) as exc_info:
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(response_field=response_field),
            plugin_name="llm",
        )

    error = exc_info.value
    assert error.component_type == "source"
    assert error.plugin_name == "llm"
    assert error.plugin_class == "OpenRouterLLMSourceConfig"
    assert error.cause is not None
    assert "Python keyword" in error.cause


@pytest.mark.parametrize("field", sorted(TRANSFORM_ONLY_FIELDS))
def test_transform_only_options_are_rejected(
    field: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError, match=field):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(**{field: [] if field in {"required_input_fields", "queries"} else 1}),
            plugin_name="llm",
        )


@pytest.mark.parametrize(
    ("field_name", "field_type", "expected_type"),
    [
        ("answer", "int", "str"),
        ("answer_usage", "str", "any"),
        ("answer_model", "int", "str"),
    ],
)
def test_contradictory_authored_output_types_are_rejected(
    field_name: str,
    field_type: str,
    expected_type: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError, match=rf"{field_name}.*{expected_type}"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(
                response_field="answer",
                schema={"mode": "fixed", "fields": [f"{field_name}: {field_type}"]},
            ),
            plugin_name="llm",
        )


@pytest.mark.parametrize(
    ("field_name", "field_type"),
    [
        ("answer", "str"),
        ("answer_usage", "any"),
        ("answer_model", "str"),
    ],
)
def test_optional_authored_output_fields_are_rejected(
    field_name: str,
    field_type: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError, match=rf"{field_name}.*required"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(
                response_field="answer",
                schema={
                    "mode": "fixed",
                    "fields": [
                        {
                            "name": field_name,
                            "field_type": field_type,
                            "required": False,
                            "nullable": False,
                        }
                    ],
                },
            ),
            plugin_name="llm",
        )


@pytest.mark.parametrize(
    ("field_name", "field_type"),
    [
        ("answer", "str"),
        ("answer_usage", "any"),
        ("answer_model", "str"),
    ],
)
def test_nullable_authored_output_fields_are_rejected(
    field_name: str,
    field_type: str,
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    with pytest.raises(PluginConfigError, match=rf"{field_name}.*non-nullable"):
        OpenRouterLLMSourceConfig.from_dict(
            openrouter_config(
                response_field="answer",
                schema={
                    "mode": "fixed",
                    "fields": [
                        {
                            "name": field_name,
                            "field_type": field_type,
                            "required": True,
                            "nullable": True,
                        }
                    ],
                },
            ),
            plugin_name="llm",
        )


def test_source_config_augments_fixed_schema_and_preserves_mode(
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    cfg = OpenRouterLLMSourceConfig.from_dict(
        openrouter_config(
            response_field="answer",
            schema={
                "mode": "fixed",
                "fields": ["request_id: str"],
                "guaranteed_fields": ["request_id"],
            },
        ),
        plugin_name="llm",
    )

    assert cfg.schema_config.mode == "fixed"
    assert cfg.schema_config.fields is not None
    fields = {field.name: field for field in cfg.schema_config.fields}
    assert fields == {
        "request_id": FieldDefinition(name="request_id", field_type="str"),
        "answer": FieldDefinition(name="answer", field_type="str"),
        "answer_usage": FieldDefinition(name="answer_usage", field_type="any"),
        "answer_model": FieldDefinition(name="answer_model", field_type="str"),
    }
    assert set(cfg.schema_config.guaranteed_fields or ()) == {
        "request_id",
        "answer",
        "answer_usage",
        "answer_model",
    }


def test_source_config_augments_observed_schema_guarantees(
    openrouter_config: Callable[..., dict[str, Any]],
) -> None:
    cfg = OpenRouterLLMSourceConfig.from_dict(
        openrouter_config(
            response_field="answer",
            schema={"mode": "observed", "guaranteed_fields": ["request_id"]},
        ),
        plugin_name="llm",
    )

    assert cfg.schema_config.mode == "observed"
    assert cfg.schema_config.fields is None
    assert set(cfg.schema_config.guaranteed_fields or ()) == {
        "request_id",
        "answer",
        "answer_usage",
        "answer_model",
    }


def test_public_source_schema_helper_uses_configurable_output_base() -> None:
    result = build_llm_source_output_schema_config(
        SchemaConfig(mode="flexible", fields=(FieldDefinition(name="request_id", field_type="str"),)),
        "summary",
    )

    assert result.mode == "flexible"
    assert result.fields is not None
    assert {field.name: field.field_type for field in result.fields} == {
        "request_id": "str",
        "summary": "str",
        "summary_usage": "any",
        "summary_model": "str",
    }


def test_public_source_schema_helper_rejects_keyword_output_base() -> None:
    with pytest.raises(ValueError, match="Python keyword"):
        build_llm_source_output_schema_config(SchemaConfig(mode="observed"), "class")


def test_valid_authored_output_definitions_are_preserved() -> None:
    authored_fields = (
        FieldDefinition(name="answer", field_type="str"),
        FieldDefinition(name="answer_usage", field_type="any"),
        FieldDefinition(name="answer_model", field_type="str"),
    )

    result = build_llm_source_output_schema_config(
        SchemaConfig(mode="fixed", fields=authored_fields),
        "answer",
    )

    assert result.mode == "fixed"
    assert result.fields == authored_fields


@pytest.mark.parametrize("provider", ["azure", "openrouter", "bedrock", "gateway"])
def test_each_provider_accepts_its_single_request_config(
    provider: str,
    provider_configs: dict[str, dict[str, Any]],
) -> None:
    cfg = SOURCE_PROVIDER_CONFIGS[provider].from_dict(provider_configs[provider], plugin_name="llm")
    assert cfg.provider == provider
