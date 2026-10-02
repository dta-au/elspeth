"""Stage-1 twin of the multi-query ``required_input_fields`` column contract (finding #9).

The composer's plugin probes construct ``LLMConfig`` and swallow its rejection
(``_is_config_probe_exception``) so a draft never crashes validation, so the
rule has to be restated in ``CompositionState.validate`` to reach Stage 1. The
message is the plugin layer's, verbatim, so the tool-call surface and a YAML
author see one wording.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    _validate_multi_query_generated_input_requirements,
)
from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES, explain_validation_code

_CODE = "query_input_columns_undeclared"


@pytest.mark.parametrize("list_form", [False, True])
@pytest.mark.parametrize("consumer", ["required", "alias", "source_row", "schema", "image", "image_format"])
def test_generated_input_conflict_has_same_stage_one_and_plugin_message(list_form: bool, consumer: str) -> None:
    query = {"input_fields": {"text": "body"}, "template": "{{ row.text }}", "output_fields": [{"suffix": "answer", "type": "string"}]}
    options = _options([], {"classify": query})
    if consumer == "required":
        options["required_input_fields"] = ["body", "classify_answer"]
    elif consumer == "alias":
        query["input_fields"] = {"text": "classify_answer"}
    elif consumer == "source_row":
        query["template"] = "{{ row.source_row.classify_answer }}"
    elif consumer == "schema":
        options["schema"]["required_fields"] = ["classify_answer"]
    elif consumer == "image":
        options["image_inputs"] = [{"field": "classify_answer", "format": "png"}]
    else:
        options["image_inputs"] = [{"field": "photo", "format_field": "classify_answer"}]
    if list_form:
        options["queries"] = [{"name": "classify", **query}]
    result = _state(options).validate()
    assert not result.is_valid
    (entry,) = [entry for entry in result.errors if entry.error_code == "query_generated_fields_required"]
    assert entry.component == "node:classify"
    assert entry.severity == "high"
    with pytest.raises(ValidationError) as exc:
        LLMConfig.model_validate(options)
    assert entry.message in str(exc.value)


def test_generated_input_code_has_direct_guidance() -> None:
    assert "query_generated_fields_required" in _CLOSED_VALIDATION_ERROR_CODES
    guidance = explain_validation_code("query_generated_fields_required")
    assert guidance is not None
    explanation, fix = guidance
    assert "generated" in explanation
    assert "downstream" in fix
    assert "distinct output names" in fix


@pytest.mark.parametrize("queries", ["invalid", {"classify": {"input_fields": ["body"]}}, [{"input_fields": {"text": "body"}}]])
def test_generated_guard_malformed_queries_are_plugin_option_rejections(queries: Any) -> None:
    result = _state(_options([], queries)).validate()
    assert not result.is_valid
    assert any(entry.error_code == "plugin_options_invalid" for entry in result.errors)
    assert not any(entry.error_code == "query_generated_fields_required" for entry in result.errors)


def test_generated_guard_does_not_parse_other_plugins_queries_options() -> None:
    node = replace(_state(_options([], "plugin-specific-query-shape")).nodes[0], plugin="passthrough")
    assert _validate_multi_query_generated_input_requirements(node) == ()


def test_generated_guard_mapping_key_overrides_redundant_name_like_plugin() -> None:
    options = _options(["body"], {"classify": {"name": 123, "input_fields": {"text": "body"}, "template": "{{ row.text }}"}})
    assert LLMConfig.model_validate(options).queries is not None
    result = _state(options).validate()
    assert result.is_valid, result.errors


@pytest.mark.parametrize("field", ["classify_reply", "classify_reply_usage", "classify_reply_model"])
def test_generated_guard_covers_custom_response_operational_fields(field: str) -> None:
    options = _options(["body", field], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }}"}})
    options["response_field"] = "reply"
    result = _state(options).validate()
    (entry,) = [entry for entry in result.errors if entry.error_code == "query_generated_fields_required"]
    assert field in entry.message
    assert entry.message in _plugin_message(options)


@pytest.mark.parametrize("required", [["body", "title"], []])
def test_generated_guard_accepts_two_upstream_only_queries_and_extra_presence(required: list[str]) -> None:
    options = _options(
        required,
        {
            name: {
                "input_fields": {"answer": "body"},
                "template": "{{ row.answer }}",
                "output_fields": [{"suffix": "answer", "type": "string"}],
            }
            for name in ("good_colour_pair", "approximate_hex")
        },
    )
    options.pop("prompt_template")
    result = _state(options).validate()
    assert result.is_valid, result.errors
    assert LLMConfig.model_validate(options).required_input_fields == required


def test_generated_input_conflict_reaches_stage_one_with_plugin_message() -> None:
    options = _options(
        ["body", "classify_answer"],
        {
            "classify": {
                "input_fields": {"text": "body"},
                "template": "{{ row.text }}",
                "output_fields": [{"suffix": "answer", "type": "string"}],
            }
        },
    )
    result = _state(options).validate()
    (entry,) = [error for error in result.errors if error.error_code == "query_generated_fields_required"]
    assert entry.component == "node:classify"
    assert entry.message in _plugin_message(options)
    assert "query_generated_fields_required" in _CLOSED_VALIDATION_ERROR_CODES
    guidance = explain_validation_code("query_generated_fields_required")
    assert guidance is not None
    assert "downstream" in guidance[1]


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


@pytest.mark.parametrize(
    "form",
    [
        pytest.param("{{ row.source_row | attr('title') }}", id="attr"),
        pytest.param("{{ [row.source_row] | map(attribute='title') | list }}", id="map-attribute"),
        pytest.param("{{ [row.source_row] | selectattr('title') | list }}", id="selectattr"),
        pytest.param("{{ [row.source_row] | groupby('title') | list }}", id="groupby"),
    ],
)
def test_a_source_row_column_read_through_a_filter_is_reported(form: str) -> None:
    """The column analysis is the shared ``multi_query_source_row_columns`` (review-G3-template-api-r2 F2)."""
    options = _options(["body"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} " + form}})
    (entry,) = _coded(_state(options))
    assert "reads row column 'title'" in entry.message
    assert entry.message in _plugin_message(options)


@pytest.mark.parametrize(
    "form",
    [
        pytest.param("{{ row.source_row.get('body', '').upper() }}", id="get-with-default"),
        pytest.param("{{ row.source_row.get('body').upper() }}", id="get"),
    ],
)
def test_a_method_on_a_source_row_value_is_not_an_unbound_query_variable(form: str) -> None:
    """The binding twin reads the shared ``multi_query_context_names`` (review-G3-template-api-r3 F4)."""
    options = _options(["body"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} " + form}})
    result = _state(options).validate()
    assert [entry for entry in result.errors if entry.error_code == "query_template_unbound_row_fields"] == []
    kwargs = {key: value for key, value in options.items() if key != "schema"}
    LLMConfig(schema_config=SchemaConfig.from_dict({"mode": "observed"}), **kwargs)


def test_subscripted_source_row_column_outside_declaration_is_reported() -> None:
    options = _options(
        ["body"], {"classify": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row['source_row']['title'] }}"}}
    )
    (entry,) = _coded(_state(options))
    assert "reads row column 'title'" in entry.message
    assert entry.message in _plugin_message(options)


def test_a_source_row_read_of_an_image_column_is_refused_on_both_surfaces() -> None:
    """``row.source_row`` holds only ``required_input_fields`` (ADR-051): an ``image_inputs`` column is not in it."""
    options = _options(
        ["body"], {"describe": {"input_fields": {"text": "body"}, "template": "{{ row.text }} {{ row.source_row.photo_mime }}"}}
    )
    options["image_inputs"] = [{"field": "photo", "format_field": "photo_mime"}]
    (entry,) = _coded(_state(options))
    assert "Query 'describe' reads row column 'photo_mime'" in entry.message
    assert entry.message in _plugin_message(options)


def test_image_input_columns_cover_query_input_fields() -> None:
    """``image_inputs`` field and format_field cover ``input_fields`` values, as ``LLMConfig.declared_input_fields`` counts them."""
    options = _options(
        ["body"],
        {
            "describe": {
                "input_fields": {"text": "body", "picture": "photo", "mime": "photo_mime"},
                "template": "{{ row.text }} {{ row.picture }} {{ row.mime }}",
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
