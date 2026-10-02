"""Composer configuration warnings must follow the runtime plugin contract."""

from typing import Any

import pytest

from elspeth.plugins.transforms.json_explode import JSONExplodeConfig
from elspeth.plugins.transforms.keyword_filter import KeywordFilterConfig
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec


@pytest.mark.parametrize(
    ("plugin", "config_model", "options", "required_key", "empty_value"),
    [
        (
            "keyword_filter",
            KeywordFilterConfig,
            {"schema": {"mode": "observed"}, "fields": ["note"], "blocked_patterns": [r"(?i)\bsecret\b"]},
            "blocked_patterns",
            [],
        ),
        (
            "json_explode",
            JSONExplodeConfig,
            {"schema": {"mode": "observed"}, "array_field": "items"},
            "array_field",
            "",
        ),
        (
            "llm",
            LLMConfig,
            {
                "schema": {"mode": "observed"},
                "required_input_fields": ["note"],
                "provider": "openrouter",
                "model": "test-model",
                "queries": {"classify": {"input_fields": {"text": "note"}, "template": "Classify {{ row.text }}"}},
            },
            "queries",
            {},
        ),
        (
            "llm",
            LLMConfig,
            {
                "schema": {"mode": "observed"},
                "required_input_fields": ["note"],
                "provider": "openrouter",
                "model": "test-model",
                "queries": [{"name": "classify", "input_fields": {"text": "note"}, "template": "Classify {{ row.text }}"}],
            },
            "queries",
            [],
        ),
        (
            "llm",
            LLMConfig,
            {
                "schema": {"mode": "observed"},
                "required_input_fields": ["note"],
                "provider": "openrouter",
                "model": "test-model",
                "prompt_template": "Classify {{ row.note }}",
            },
            "prompt_template",
            "",
        ),
    ],
)
@pytest.mark.parametrize("configuration", ["valid", "missing", "empty"])
def test_plugin_configuration_warning_uses_runtime_option(
    plugin: str,
    config_model: type[KeywordFilterConfig] | type[JSONExplodeConfig] | type[LLMConfig],
    options: dict[str, Any],
    required_key: str,
    empty_value: object,
    configuration: str,
) -> None:
    # The plugin's own parser is the positive authority, not a test-only key list.
    config_model.model_validate(options)
    configured_options = dict(options)
    if configuration == "missing":
        del configured_options[required_key]
    elif configuration == "empty":
        configured_options[required_key] = empty_value
    state = CompositionState(
        source=SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard"),
        nodes=(
            NodeSpec(
                id="transform_under_test",
                node_type="transform",
                plugin=plugin,
                input="rows",
                on_success="result",
                on_error="discard",
                options=configured_options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(OutputSpec(name="result", plugin="csv", options={"path": "result.csv"}, on_write_failure="discard"),),
        metadata=PipelineMetadata(),
        version=1,
    )
    warnings = state.validate().warnings
    config_warnings = [warning for warning in warnings if "appears incomplete" in warning.message or "has empty" in warning.message]
    if configuration == "valid":
        assert config_warnings == []
    else:
        assert len(config_warnings) == 1
        assert required_key in config_warnings[0].message
