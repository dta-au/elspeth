"""Plugin configuration: a prompt template over the 64 KiB bound is refused by position, size and limit.

The refusal is not a syntax fault, so it does not read "Invalid Jinja2 template".
A template up to the bound (20 KB, over the old 16 KiB) is admitted. Expected
texts are spelled here so the file collects on the tree before the change.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.multi_query import QueryDefinition

LIMIT = 64 * 1024
_OBSERVED = SchemaConfig(mode="observed", fields=None)


def _template(size: int, field: str = "text") -> str:
    head = f"Classify this text: {{{{ row.{field} }}}}. "
    return head + "x" * (size - len(head))


def _refusal(position: str, size: int) -> str:
    return (
        f"{position} is {size} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    )


def _messages(raised: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    return [str(error["msg"]) for error in raised.value.errors()]


@pytest.mark.parametrize("size", [pytest.param(20_000, id="twenty-kb"), pytest.param(LIMIT, id="exactly-the-bound")])
def test_node_prompt_template_up_to_the_bound_is_admitted(size: int) -> None:
    config = LLMConfig(provider="azure", prompt_template=_template(size), schema_config=_OBSERVED, required_input_fields=["text"])

    assert len(config.prompt_template.encode("utf-8")) == size


def test_node_prompt_template_over_the_bound_names_position_size_and_limit() -> None:
    with pytest.raises(ValidationError) as raised:
        LLMConfig(provider="azure", prompt_template=_template(LIMIT + 1), schema_config=_OBSERVED, required_input_fields=["text"])

    assert _messages(raised) == [f"Value error, {_refusal('prompt_template', LIMIT + 1)}"]
    assert "Invalid Jinja2 template" not in str(raised.value)


def test_a_syntax_fault_still_reads_as_one() -> None:
    with pytest.raises(ValidationError, match="Invalid Jinja2 template"):
        LLMConfig(provider="azure", prompt_template="{% if unclosed", schema_config=_OBSERVED, required_input_fields=[])


@pytest.mark.parametrize("size", [pytest.param(20_000, id="twenty-kb"), pytest.param(LIMIT, id="exactly-the-bound")])
def test_query_template_up_to_the_bound_is_admitted(size: int) -> None:
    query = QueryDefinition(name="tone", input_fields={"text": "body"}, template=_template(size))

    assert query.template is not None and len(query.template.encode("utf-8")) == size


def test_query_template_over_the_bound_names_the_query() -> None:
    with pytest.raises(ValidationError) as raised:
        QueryDefinition(name="tone", input_fields={"text": "body"}, template=_template(LIMIT + 1))

    assert _messages(raised) == [f"Value error, {_refusal('queries.tone.template', LIMIT + 1)}"]
    assert "Invalid Jinja2 template" not in str(raised.value)


@pytest.mark.parametrize("form", ["mapping", "list"])
def test_both_queries_forms_name_the_query_through_the_node_config(form: str) -> None:
    definition: dict[str, Any] = {"input_fields": {"text": "body"}, "template": _template(LIMIT + 1)}
    queries: Any = {"tone": definition} if form == "mapping" else [{"name": "tone", **definition}]

    with pytest.raises(ValidationError) as raised:
        LLMConfig(
            provider="azure",
            prompt_template="Classify: {{ row.body }}",
            queries=queries,
            schema_config=_OBSERVED,
            required_input_fields=["body"],
        )

    assert _refusal("queries.tone.template", LIMIT + 1) in str(raised.value)
    assert "Invalid Jinja2 template" not in str(raised.value)
