"""LLM source: a prompt whose loops nest too deeply is a configuration error naming its position, not a bare SyntaxError."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.llm.config import OpenRouterLLMSourceConfig


def _nested_loops(levels: int) -> str:
    return "List audit topics. " + "{% for item in [1] %}" * levels + "x" + "{% endfor %}" * levels


def test_twenty_nested_loops_are_admitted(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template=_nested_loops(20)), plugin_name="llm")


def test_source_prompt_nested_too_deeply_names_its_position(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(PluginConfigError) as raised:
        OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template=_nested_loops(21)), plugin_name="llm")

    assert raised.value.cause is not None
    assert (
        "prompt_template is too deeply nested to compile: its loops or blocks nest deeper than Python allows. Flatten the nesting."
    ) in raised.value.cause
