"""LLM transform configuration: an integer literal too long to read is a validation error naming its position.

Expected texts are spelled here so the file collects on the tree before the change.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig

_OBSERVED = SchemaConfig(mode="observed", fields=None)
LONG = "Rate {{ row.review }}. {{ " + "9" * 5000 + " }}"


def _refusal(position: str) -> str:
    return f"{position} contains a number literal too long to read. Shorten the number or quote it as text."


def _config(**options: object) -> LLMConfig:
    return LLMConfig(provider="azure", schema_config=_OBSERVED, required_input_fields=["review"], **options)


def test_a_literal_at_the_digit_limit_is_admitted() -> None:
    _config(prompt_template="Rate {{ row.review }}. {{ " + "9" * 4300 + " }}")


def test_node_prompt_names_its_position() -> None:
    with pytest.raises(ValidationError) as raised:
        _config(prompt_template=LONG)

    assert [str(error["msg"]) for error in raised.value.errors()] == [f"Value error, {_refusal('prompt_template')}"]


@pytest.mark.parametrize("form", ["mapping", "list"])
def test_query_template_names_the_query(form: str) -> None:
    query = {"input_fields": {"review": "review"}, "template": LONG}
    queries: object = {"tone": query} if form == "mapping" else [{"name": "tone", **query}]

    with pytest.raises(ValidationError) as raised:
        _config(prompt_template="Rate {{ row.review }}.", queries=queries)

    assert _refusal("queries.tone.template") in str(raised.value)
