"""Stage-1 twin of the multi-query ``required_input_fields`` column contract (finding #9).

The composer's plugin probes construct ``LLMConfig`` and swallow its rejection
(``_is_config_probe_exception``) so a draft never crashes validation, so the
rule has to be restated in ``CompositionState.validate`` to reach Stage 1. The
message is the plugin layer's, verbatim, so the tool-call surface and a YAML
author see one wording.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES, explain_validation_code

_CODE = "query_input_columns_undeclared"


def _options(required: list[str] | None, queries: Any, template: str = "Classify") -> dict[str, Any]:
    options: dict[str, Any] = {
        "provider": "azure",
        "system_prompt": "You classify documents. Reply with one category label.",
        "prompt_template": template,
        "schema": {"mode": "observed"},
        "queries": queries,
    }
    if required is not None:
        options["required_input_fields"] = required
    return options


def _state(options: dict[str, Any]) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/tmp/x.csv", "schema": {"mode": "fixed", "fields": ["body: str", "title: str"]}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="classify",
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="out", plugin="json", options={"path": "/tmp/o.json", "schema": {"mode": "observed"}}, on_write_failure="discard"
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _plugin_message(options: dict[str, Any]) -> str:
    kwargs = {key: value for key, value in options.items() if key != "schema"}
    with pytest.raises(ValidationError) as exc_info:
        LLMConfig(schema_config=SchemaConfig.from_dict({"mode": "observed"}), **kwargs)
    return str(exc_info.value)


def _coded(state: CompositionState) -> list[Any]:
    return [entry for entry in state.validate().errors if entry.error_code == _CODE]


def test_query_column_outside_declaration_is_a_stage_one_error_with_the_plugin_message() -> None:
    options = _options(["title"], {"classify": {"input_fields": {"text": "missing_col"}, "template": "Classify {{ row.text }}"}})
    result = _state(options).validate()
    assert not result.is_valid
    (entry,) = [entry for entry in result.errors if entry.error_code == _CODE]
    assert entry.component == "node:classify"
    assert entry.severity == "high"
    assert "Query 'classify' reads row column 'missing_col'" in entry.message
    assert entry.message in _plugin_message(options)


def test_source_row_column_outside_declaration_is_reported() -> None:
    options = _options(["body"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row.source_row.title }}"}})
    (entry,) = _coded(_state(options))
    assert "reads row column 'title'" in entry.message
    assert entry.message in _plugin_message(options)


def test_list_form_queries_and_node_level_fallback_template_are_read() -> None:
    options = _options(
        ["body"],
        [{"name": "classify", "input_fields": {"text": "body"}}],
        template="{{ row.text }} {{ row.source_row['title'] }}",
    )
    (entry,) = _coded(_state(options))
    assert "reads row column 'title'" in entry.message


def test_subscripted_source_row_column_outside_declaration_is_reported() -> None:
    options = _options(
        ["body"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row['source_row']['title'] }}"}}
    )
    (entry,) = _coded(_state(options))
    assert "reads row column 'title'" in entry.message
    assert entry.message in _plugin_message(options)


def test_image_input_columns_cover_query_reads() -> None:
    """``image_inputs`` field and format_field are declared input columns, as ``LLMConfig.declared_input_fields`` counts them."""
    options = _options(
        ["body"],
        {
            "describe": {
                "input_fields": {"text": "body", "picture": "photo"},
                "template": "{{ row.text }} {{ row.picture }} {{ row.source_row.photo_mime }}",
            }
        },
    )
    options["image_inputs"] = [{"field": "photo", "format_field": "photo_mime"}]
    assert _coded(_state(options)) == []
    plugin_kwargs = {key: value for key, value in options.items() if key != "schema"}
    assert LLMConfig(schema_config=SchemaConfig.from_dict({"mode": "observed"}), **plugin_kwargs).declared_input_fields == frozenset(
        {"body", "photo", "photo_mime"}
    )


def test_covered_columns_and_opt_out_raise_nothing() -> None:
    covered = _options(
        ["body", "title"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row.source_row.title }}"}}
    )
    assert not _coded(_state(covered))
    assert _state(covered).validate().is_valid
    opt_out = _options([], {"classify": {"input_fields": {"text": "missing_col"}, "template": "{{ row.text }}"}})
    assert not _coded(_state(opt_out))


@pytest.mark.parametrize(
    ("queries", "expected_column"),
    [
        ("not-a-mapping", None),
        ({"classify": "not-an-entry"}, None),
        ({"classify": {"input_fields": ["body"]}}, None),
        ({"classify": {"input_fields": {"text": 7}, "template": "{{ row.text }}"}}, None),
        # An unparseable template contributes no source_row columns, but the
        # input_fields values need no template and are still checked.
        ({"classify": {"input_fields": {"text": "body"}, "template": "{% if %}"}}, "body"),
    ],
)
def test_malformed_shapes_never_raise_and_skip_only_the_malformed_piece(queries: Any, expected_column: str | None) -> None:
    coded = _coded(_state(_options(["title"], queries)))
    if expected_column is None:
        assert coded == []
    else:
        (entry,) = coded
        assert f"reads row column '{expected_column}'" in entry.message


def test_applying_the_plugin_suggestion_clears_the_contract_errors() -> None:
    """Finding #10 end to end: the repair's list validates at Stage 1."""
    queries = {"classify": {"input_fields": {"text": "body"}, "template": "Classify {{ row.text }} {{ row.source_row.title }}"}}
    message = _plugin_message(_options(None, queries))
    assert 'options.required_input_fields: ["body", "title"]  # Require these fields' in message
    result = _state(_options(["body", "title"], queries)).validate()
    assert result.is_valid, result.errors


def test_code_is_closed_and_resolves_to_its_own_guidance() -> None:
    assert _CODE in _CLOSED_VALIDATION_ERROR_CODES
    guidance = explain_validation_code(_CODE)
    assert guidance is not None
    explanation, fix = guidance
    assert "input_fields" in explanation and "required_input_fields" in explanation
    assert "input_fields" in fix and "source_row" in fix
    for sibling in ("query_template_unbound_row_fields", "prompt_template_undeclared_row_fields", "schema_contract_violation"):
        assert guidance != explain_validation_code(sibling)
