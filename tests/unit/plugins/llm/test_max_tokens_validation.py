"""Token budgets must reject booleans before integer coercion."""

from typing import Any, Literal

import pytest
from pydantic import ValidationError

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.multi_query import QueryDefinition, resolve_queries


def _config_options(value: object, location: Literal["node", "mapping", "list"]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "provider": "azure",
        "prompt_template": "Classify {{ row.text }}",
        "schema": {"mode": "observed"},
        "required_input_fields": ["text"],
    }
    if location == "node":
        options["max_tokens"] = value
    else:
        query = {"input_fields": {"text": "text"}, "max_tokens": value}
        options["queries"] = {"classify": query} if location == "mapping" else [{"name": "classify", **query}]
    return options


@pytest.mark.parametrize("value", [True, False])
def test_query_definition_rejects_boolean_budget(value: bool) -> None:
    with pytest.raises(ValidationError, match="max_tokens must be an integer, not a boolean") as exc_info:
        QueryDefinition(input_fields={"text": "text"}, max_tokens=value)
    assert exc_info.value.errors()[0]["loc"] == ("max_tokens",)


@pytest.mark.parametrize("location", ["node", "mapping", "list"])
@pytest.mark.parametrize("value", [True, False])
def test_config_rejects_boolean_budget(value: bool, location: Literal["node", "mapping", "list"]) -> None:
    with pytest.raises(ValidationError, match="max_tokens must be an integer, not a boolean") as exc_info:
        LLMConfig.model_validate(_config_options(value, location))
    assert any(error["loc"][-1] == "max_tokens" and error["input"] is value for error in exc_info.value.errors())


@pytest.mark.parametrize("location", ["node", "mapping", "list"])
@pytest.mark.parametrize("value", [True, False])
def test_from_dict_reports_boolean_budget_as_config_error(value: bool, location: Literal["node", "mapping", "list"]) -> None:
    with pytest.raises(PluginConfigError, match="max_tokens must be an integer, not a boolean"):
        LLMConfig.from_dict(_config_options(value, location), plugin_name="llm")


@pytest.mark.parametrize("location", ["node", "mapping", "list"])
@pytest.mark.parametrize("value", [None, 1, 256, "256"])
def test_config_preserves_supported_budgets(value: object, location: Literal["node", "mapping", "list"]) -> None:
    config = LLMConfig.from_dict(_config_options(value, location), plugin_name="llm")
    if location == "node":
        actual = config.max_tokens
    else:
        assert config.queries is not None
        actual = resolve_queries(config.queries)[0].max_tokens
    expected = 256 if value == "256" else value
    assert actual == expected
    assert actual is None or type(actual) is int


@pytest.mark.parametrize("location", ["node", "mapping", "list"])
@pytest.mark.parametrize("value", [0, -1])
def test_config_still_rejects_nonpositive_budgets(value: int, location: Literal["node", "mapping", "list"]) -> None:
    with pytest.raises(PluginConfigError, match="max_tokens"):
        LLMConfig.from_dict(_config_options(value, location), plugin_name="llm")
