"""Multi-query ``required_input_fields`` contract at the plugin layer.

In multi-query mode each query renders with ``row`` bound to a synthetic
context (``QuerySpec.build_template_context``): its ``input_fields`` variables
plus ``source_row``. The ROW COLUMNS a query reads are therefore its
``input_fields`` VALUES and any second-level ``row.source_row.<column>``
reference — never the ``row.<variable>`` names its template interpolates.

* Finding #9 (elspeth-a10d15055b): with ``required_input_fields`` declared, a
  query column outside that declaration validated green and then failed every
  row at render with ``template_context_failed``.
* Finding #10: with ``required_input_fields`` undeclared, the suggested repair
  listed template variable names (and ``source_row``) as row columns, so
  applying it produced a blocking ``schema_contract_violation``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig, multi_query_source_row_columns

_OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})


def _multi(
    queries: dict[str, Any],
    *,
    required: list[str] | None,
    template: str = "Assess",
    image_inputs: list[dict[str, str]] | None = None,
) -> LLMConfig:
    kwargs: dict[str, Any] = {
        "provider": "azure",
        "prompt_template": template,
        "schema_config": _OBSERVED_SCHEMA,
        "queries": queries,
    }
    if required is not None:
        kwargs["required_input_fields"] = required
    if image_inputs is not None:
        kwargs["image_inputs"] = image_inputs
    return LLMConfig(**kwargs)


def _suggested(exc: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    match = re.search(r"options\.required_input_fields: (\[[^\]]*\])  # Require these fields", str(exc.value))
    assert match is not None, str(exc.value)
    return json.loads(match.group(1))


class TestDeclaredMultiQueryColumnsMustBeCovered:
    """Finding #9 — the plugin layer."""

    def test_input_fields_value_outside_declaration_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi({"classify": {"input_fields": {"text": "missing_col"}, "template": "Classify {{ row.text }}"}}, required=["title"])
        message = str(exc_info.value)
        assert "Query 'classify' reads row column 'missing_col'" in message
        assert "it declares 'title'" in message

    def test_source_row_column_outside_declaration_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi(
                {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }} {{ row.source_row.title }}"}},
                required=["body"],
            )
        assert "reads row column 'title'" in str(exc_info.value)

    def test_node_level_template_source_row_column_is_checked_for_queries_that_fall_back_to_it(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi(
                {"classify": {"input_fields": {"text": "body"}}}, required=["body"], template="{{ row.text }} {{ row.source_row['title'] }}"
            )
        assert "reads row column 'title'" in str(exc_info.value)

    def test_source_row_get_call_names_its_literal_column(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi(
                {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row.source_row.get('title') }}"}},
                required=["body"],
            )
        assert "reads row column 'title'" in str(exc_info.value)

    def test_covered_columns_are_accepted(self) -> None:
        config = _multi(
            {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }} {{ row.source_row.title }}"}},
            required=["body", "title"],
        )
        assert config.declared_input_fields == frozenset({"body", "title"})

    def test_template_variable_names_need_no_declaration(self) -> None:
        """``row.text`` is a query variable, not a column: declaring only the column suffices."""
        config = _multi({"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }}"}}, required=["body"])
        assert config.required_input_fields == ["body"]

    def test_explicit_opt_out_is_still_accepted(self) -> None:
        config = _multi({"classify": {"input_fields": {"text": "missing_col"}, "template": "Classify {{ row.text }}"}}, required=[])
        assert config.required_input_fields == []

    def test_bracket_header_column_is_covered_by_its_canonical_declaration(self) -> None:
        config = _multi(
            {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row.source_row['Original Header'] }}"}},
            required=["body", "original_header"],
        )
        assert config.required_input_fields == ["body", "original_header"]

    def test_unused_node_level_template_is_not_read(self) -> None:
        """A node-level prompt_template no query falls back to never renders."""
        config = _multi(
            {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }}"}},
            required=["body"],
            template="Dead slot {{ row.source_row.never_rendered }}",
        )
        assert config.required_input_fields == ["body"]

    def test_subscripted_source_row_column_outside_declaration_is_rejected(self) -> None:
        """``row['source_row']['<column>']`` reads the row as surely as ``row.source_row.<column>``."""
        with pytest.raises(ValidationError) as exc_info:
            _multi(
                {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row['source_row']['title'] }}"}},
                required=["body"],
            )
        assert "reads row column 'title'" in str(exc_info.value)

    def test_image_input_columns_cover_query_reads(self) -> None:
        """An image query reads its image and format columns; ``image_inputs`` already declares both."""
        config = _multi(
            {
                "describe": {
                    "input_fields": {"text": "body", "picture": "photo"},
                    "template": "{{ row.text }} {{ row.picture }} {{ row.source_row.photo_mime }}",
                }
            },
            required=["body"],
            image_inputs=[{"field": "photo", "format_field": "photo_mime"}],
        )
        assert config.declared_input_fields == frozenset({"body", "photo", "photo_mime"})


class TestSourceRowColumnExtraction:
    """``multi_query_source_row_columns`` reads every literal ``source_row`` spelling and nothing else."""

    def test_every_literal_spelling_is_read(self) -> None:
        template = (
            "{{ row.source_row.a }} {{ row.source_row['b'] }} {{ row['source_row']['c'] }} "
            "{{ row['source_row'].d }} {{ row.source_row.get('e') }} {{ row['source_row'].get('f') }}"
        )
        assert multi_query_source_row_columns(template) == frozenset({"a", "b", "c", "d", "e", "f"})

    def test_api_names_query_variables_and_computed_keys_contribute_nothing(self) -> None:
        template = (
            "{{ row.text }} {{ row.source_row.to_dict() }} {{ row['source_row'].contract }} "
            "{{ row.source_row[key] }} {{ row['other']['x'] }} {{ row.source_row }}"
        )
        assert multi_query_source_row_columns(template) == frozenset()


class TestUndeclaredMultiQuerySuggestionNamesColumnsOnly:
    """Finding #10 — the repair suggestion must list row columns, never query variables."""

    def test_suggestion_is_the_input_fields_values(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi({"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }}"}}, required=None)
        assert _suggested(exc_info) == ["body"]

    def test_suggestion_adds_source_row_columns_not_source_row(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi(
                {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }} {{ row.source_row.title }}"}},
                required=None,
            )
        assert _suggested(exc_info) == ["body", "title"]

    def test_suggestion_ignores_variables_in_the_node_level_template(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi({"classify": {"input_fields": {"text": "body"}}}, required=None, template="Classify {{ row.text }}")
        assert _suggested(exc_info) == ["body"]

    def test_suggestion_reads_source_row_columns_of_the_node_level_template_a_query_falls_back_to(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            _multi({"classify": {"input_fields": {"text": "body"}}}, required=None, template="{{ row.text }} {{ row.source_row.title }}")
        assert _suggested(exc_info) == ["body", "title"]

    def test_applying_the_suggestion_validates(self) -> None:
        queries = {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }} {{ row.source_row.title }}"}}
        with pytest.raises(ValidationError) as exc_info:
            _multi(queries, required=None)
        config = _multi(queries, required=_suggested(exc_info))
        assert config.declared_input_fields == frozenset({"body", "title"})

    def test_single_prompt_suggestion_is_unchanged(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            LLMConfig(provider="azure", prompt_template="Classify {{ row.body }}", schema_config=_OBSERVED_SCHEMA)
        assert "LLM prompt_template references row fields ['body'] but options.required_input_fields is not declared." in str(
            exc_info.value
        )
        assert _suggested(exc_info) == ["body"]


def test_all_overrides_need_no_dead_node_template() -> None:
    config = LLMConfig(
        provider="azure",
        schema_config=_OBSERVED_SCHEMA,
        required_input_fields=["colour"],
        queries={"decorate": {"input_fields": {"colour": "colour"}, "template": "Decorate {{ row.colour }}"}},
    )
    assert config.prompt_template is None


def test_missing_effective_query_template_rejected() -> None:
    with pytest.raises(ValidationError, match="prompt_template") as exc_info:
        LLMConfig(
            provider="azure",
            schema_config=_OBSERVED_SCHEMA,
            required_input_fields=[],
            queries={"decorate": {"input_fields": {"colour": "colour"}}},
        )
    assert exc_info.value.errors()[0]["loc"] == ("prompt_template",)
