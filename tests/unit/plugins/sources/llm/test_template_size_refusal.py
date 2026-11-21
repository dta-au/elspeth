"""LLM source: its prompt is a template under the same 64 KiB bound and the same refusal."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sources.llm.config import OpenRouterLLMSourceConfig

LIMIT = 64 * 1024


def _prompt(size: int) -> str:
    head = "Summarise the audit topic. "
    return head + "x" * (size - len(head))


@pytest.mark.parametrize("size", [pytest.param(20_000, id="twenty-kb"), pytest.param(LIMIT, id="exactly-the-bound")])
def test_source_prompt_up_to_the_bound_is_admitted(openrouter_config: Callable[..., dict[str, Any]], size: int) -> None:
    config = OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template=_prompt(size)), plugin_name="llm")

    assert len(config.prompt_template.encode("utf-8")) == size


def test_source_prompt_over_the_bound_names_position_size_and_limit(openrouter_config: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(PluginConfigError) as raised:
        OpenRouterLLMSourceConfig.from_dict(openrouter_config(prompt_template=_prompt(LIMIT + 1)), plugin_name="llm")

    assert raised.value.cause is not None
    assert (
        f"prompt_template is {LIMIT + 1} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    ) in raised.value.cause
    assert "Invalid Jinja2 template" not in raised.value.cause
