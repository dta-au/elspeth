"""LLM source: a prompt holding an integer literal too long to read is a configuration error naming its position."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.llm.config import OpenRouterLLMSourceConfig


def test_source_prompt_names_its_position(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    prompt = "List audit topics. {{ " + "9" * 5000 + " }}"

    with pytest.raises(PluginConfigError) as raised:
        OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template=prompt), plugin_name="llm")

    assert raised.value.cause is not None
    assert "prompt_template contains a number literal too long to read. Shorten the number or quote it as text." in raised.value.cause
