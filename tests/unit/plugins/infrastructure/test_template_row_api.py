"""The one template row API (ADR-051 (c), elspeth-5887fb7928 G3).

A template's ``row`` holds fields and one method, ``get``:

- attribute and item syntax always read a field (``row.items`` is a column
  named ``items``); ``get`` is the only method, called where it is named;
- a whole-row operation is a filter or builtin over the projected view
  (``row | list``, ``row | items``, ``row | dictsort``, ``dict(row)``,
  ``row | tojson``), and ``{{ row }}`` renders the mapping the row holds;
- the retired PipelineRow names (``row.contract``, ``row.to_dict``,
  ``row.to_checkpoint_format``) are reserved in attribute form.

Configuration refuses the row used as an object — a retired name, a call on a
row field, ``row.get`` without a call — under EVERY declaration, ``[]``
included, on all three surfaces (single-query ``row``, multi-query
``row.source_row``, RAG ``row``), with a message that never suggests ``[]``.
The runtime reads the same constants for the forms configuration cannot
follow.
"""

from __future__ import annotations

import pprint
import re
from typing import Any

import pytest
from jinja2.filters import FILTERS
from pydantic import ValidationError

from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.core.templates import RETIRED_ROW_API_NAMES, TEMPLATE_ROW_METHODS
from elspeth.plugins.infrastructure.templates import (
    ALL_FIELDS,
    DeclaredFields,
    RowProjection,
    SandboxedTemplate,
    TemplateError,
    TemplateRow,
)
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.rag.config import RAGRetrievalConfig

_OMITTED = object()
_ROW_AS_OBJECT = "uses its row as an object"
_RESERVED = (
    "Reserved row name: the template reads a reserved row name (contract, to_checkpoint_format, to_dict) as an "
    "attribute; a template row holds fields and one method, get; read a column of that name as row['<name>']"
)
_ADDRESS = re.compile(r" at 0x[0-9a-f]+")


def test_a_row_api_misuse_kind_is_never_described_as_a_computed_key() -> None:
    """The two refusals have separate texts: the computed-key text has no entry for a misuse kind."""
    from elspeth.core.templates import ROW_API_MISUSE_KINDS, describe_dynamic_row_access

    for kind in ROW_API_MISUSE_KINDS:
        with pytest.raises(KeyError):
            describe_dynamic_row_access([kind])


def test_the_row_api_constants_are_the_ones_the_runtime_implements() -> None:
    """The sandbox returns ``row.get`` for the one method; a second method needs a runtime change too."""
    assert frozenset({"get"}) == TEMPLATE_ROW_METHODS
    assert frozenset({"contract", "to_dict", "to_checkpoint_format"}) == RETIRED_ROW_API_NAMES


# ---------------------------------------------------------------------------
# Configuration: the row used as an object is refused under every declaration
# ---------------------------------------------------------------------------


def _single(template: str, required_input_fields: Any) -> None:
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4.1-nano",
        "prompt_template": template,
        "schema_config": {"mode": "observed"},
    }
    if required_input_fields is not _OMITTED:
        options["required_input_fields"] = required_input_fields
    LLMConfig(**options)


def _multi(template: str, required_input_fields: Any) -> None:
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4.1-nano",
        "prompt_template": "Assess {{ row.text }}",
        "queries": {"q1": {"input_fields": {"text": "note"}, "template": "{{ row.text }} " + template}},
        "schema_config": {"mode": "observed"},
    }
    if required_input_fields is not _OMITTED:
        options["required_input_fields"] = required_input_fields
    LLMConfig(**options)


def _rag(template: str, required_input_fields: Any) -> None:
    options: dict[str, Any] = {
        "output_prefix": "kb",
        "query_field": "question",
        "query_template": "{{ query }} " + template,
        "provider": "chroma",
        "provider_config": {"collection": "g3-row-api", "mode": "ephemeral"},
        "schema_config": {"mode": "observed"},
    }
    if required_input_fields is not _OMITTED:
        options["required_input_fields"] = required_input_fields
    RAGRetrievalConfig(**options)


# (surface id, builder, receiver, a declared field, the list declaration)
_SURFACES = (
    pytest.param(_single, "row", "meta", ["note", "meta"], id="single-query-row"),
    pytest.param(_multi, "row.source_row", "meta", ["note", "meta"], id="multi-query-source-row"),
    pytest.param(_rag, "row", "topic", ["question", "topic"], id="rag-row"),
)

_MISUSES = (
    pytest.param("{{ R.contract }}", "row.contract, row.to_dict", id="retired-contract"),
    pytest.param("{{ R.to_dict() }}", "row.contract, row.to_dict", id="retired-to-dict"),
    pytest.param("{{ R.to_checkpoint_format() }}", "row.contract, row.to_dict", id="retired-to-checkpoint-format"),
    pytest.param("{{ R | attr('contract') }}", "row.contract, row.to_dict", id="retired-through-attr"),
    pytest.param("{{ R.keys() | list }}", "a call on a row field", id="keys-call"),
    pytest.param("{% for k, v in R.items() %}{{ k }}{% endfor %}", "a call on a row field", id="items-call"),
    pytest.param("{{ R.values() | list }}", "a call on a row field", id="values-call"),
    pytest.param("{{ R['keys']() }}", "a call on a row field", id="item-call"),
    pytest.param("{{ R.F() }}", "a call on a row field", id="declared-field-called"),
    pytest.param("{% set r = R %}{{ r.keys() | list }}", "a call on a row field", id="aliased-call"),
    pytest.param("{% for r in [R] %}{{ r.keys() | list }}{% endfor %}", "a call on a row field", id="loop-variable-call"),
    pytest.param("{% macro m(r) %}{{ r.keys() | list }}{% endmacro %}{{ m(R) }}", "a call on a row field", id="macro-argument-call"),
    pytest.param("{{ (R if R else {}).keys() }}", "a call on a row field", id="call-through-a-conditional"),
    pytest.param("{{ (R | attr('keys'))() }}", "a call on a row field", id="attr-filter-call"),
    pytest.param("{{ (R | default({})).keys() }}", "a call on a row field", id="call-through-default"),
    pytest.param("{{ ([R] | max).keys() }}", "a call on a row field", id="call-on-a-collection-element"),
    pytest.param("{{ R.get('F')() }}", "a call on a row field", id="get-result-called"),
    pytest.param("{{ R.get('F', none)() }}", "a call on a row field", id="get-result-with-a-literal-default-called"),
    pytest.param("{{ dict(R).F() }}", "a call on a row field", id="field-of-a-dict-built-from-the-row-called"),
    pytest.param("{% set d = dict(R) %}{{ d.F() }}", "a call on a row field", id="field-of-an-aliased-dict-called"),
    pytest.param("{{ dict(R)['F']() }}", "a call on a row field", id="item-of-a-whole-row-value-called"),
    pytest.param("{{ (R | first)() }}", "a call on a row field", id="element-of-the-row-called"),
    pytest.param("{{ (R | list | last)() }}", "a call on a row field", id="element-of-the-row-names-called"),
    pytest.param("{{ (dict(R) | max)() }}", "a call on a row field", id="element-of-a-whole-row-value-called"),
    pytest.param("{{ R.get('F', R.F)() }}", "a call on a row field", id="get-result-with-a-row-field-default-called"),
    pytest.param("{{ R.get('F', R.get('F'))() }}", "a call on a row field", id="get-result-with-a-get-result-default-called"),
    pytest.param("{{ (R.F | default('x'))() }}", "a call on a row field", id="defaulted-field-called"),
    pytest.param("{{ R.get('F', R.F | default('x'))() }}", "a call on a row field", id="get-result-with-a-defaulted-field-default-called"),
    pytest.param("{{ R.get }}", "row.get without a call", id="uncalled-get"),
    pytest.param("{{ R | attr('get') }}", "row.get without a call", id="uncalled-get-through-attr"),
)

_DECLARATIONS = (
    pytest.param("list", id="list"),
    pytest.param([], id="opt-out"),
    pytest.param(_OMITTED, id="omitted"),
)


@pytest.mark.parametrize(("build", "receiver", "field", "declared"), _SURFACES)
@pytest.mark.parametrize(("form", "kind_text"), _MISUSES)
@pytest.mark.parametrize("declaration", _DECLARATIONS)
def test_the_row_used_as_an_object_is_refused_under_every_declaration(
    build: Any, receiver: str, field: str, declared: list[str], form: str, kind_text: str, declaration: Any
) -> None:
    template = form.replace("R", receiver).replace("F", field)
    required = declared if declaration == "list" else declaration
    with pytest.raises(ValidationError) as caught:
        build(template, required)
    message = str(caught.value)
    assert _ROW_AS_OBJECT in message
    assert kind_text in message
    # No declaration makes these forms work, so the refusal never suggests one.
    assert "required_input_fields: []" not in message
    assert "'row | list'" in message
    assert "dict(row)" in message


@pytest.mark.parametrize(("build", "receiver", "field", "declared"), _SURFACES)
@pytest.mark.parametrize(
    "form",
    [
        pytest.param("{{ R.get('F', 'none') }}", id="get-with-default"),
        pytest.param("{{ R.F.upper() }}", id="a-method-on-a-field-value"),
        pytest.param("{{ R['F'].split() | list }}", id="a-method-on-an-item-value"),
        pytest.param("{% for k, v in R | items %}{{ k }}={{ v }};{% endfor %}", id="items-filter"),
        pytest.param("{{ R | dictsort }}", id="dictsort-filter"),
        pytest.param("{{ R | list }}", id="list-filter"),
        pytest.param("{{ dict(R) }}", id="dict-builtin"),
        pytest.param("{{ R | tojson }}", id="tojson-filter"),
        pytest.param("{{ R }}", id="the-row-as-text"),
    ],
)
@pytest.mark.parametrize("declaration", [pytest.param("list", id="list"), pytest.param([], id="opt-out")])
def test_a_field_read_and_every_whole_row_filter_are_admitted(
    build: Any, receiver: str, field: str, declared: list[str], form: str, declaration: Any
) -> None:
    build(form.replace("R", receiver).replace("F", field), declared if declaration == "list" else declaration)


@pytest.mark.parametrize(
    ("template", "row_attribute"),
    [
        pytest.param("{{ row.a.upper() }}", None, id="field-value"),
        pytest.param("{{ row.get('a').upper() }}", None, id="get-value"),
        pytest.param("{{ (row.a ~ row.b).upper() }}", None, id="computed-value"),
        pytest.param("{{ (row.a | default('')).strip() }}", None, id="filtered-value"),
        pytest.param("{{ row.a.split(',')[0].strip() }}", None, id="indexed-value"),
        pytest.param("{{ row.var.items() | list }}", None, id="a-dict-valued-query-variable"),
        pytest.param("{{ row.source_row.get('a').upper() }}", "source_row", id="source-row-get-value"),
        pytest.param("{{ row.source_row.a.upper() }}", "source_row", id="source-row-field-value"),
    ],
)
def test_a_method_on_a_value_is_not_a_row_call(template: str, row_attribute: str | None) -> None:
    """The receiver must be able to BE the row: a value that merely mentions it is plain data with its own methods."""
    from elspeth.core.templates import extract_jinja2_field_usage

    assert extract_jinja2_field_usage(template, row_attribute=row_attribute).row_api_misuses == ()


# A method on a whole-row value: the value a builtin or a filter builds from the
# row has its own methods (G3 fix round 1, review F1).
_WHOLE_ROW_VALUE_METHODS = (
    pytest.param("{% for k, v in dict(R).items() %}{{ k }}={{ v }};{% endfor %}", id="dict-items"),
    pytest.param("{{ dict(R).keys() | list }}", id="dict-keys"),
    pytest.param("{{ dict(R).get('note') }}", id="dict-get"),
    pytest.param("{{ (R | list).count('note') }}", id="list-count"),
    pytest.param("{{ (R | list | join(',')).upper() }}", id="joined-names-upper"),
    pytest.param("{{ (R | tojson).upper() }}", id="json-text-upper"),
    pytest.param("{% set d = dict(R) %}{{ d.items() | list }}", id="aliased-dict-items"),
    pytest.param("{% with d = dict(R) %}{{ d.values() | list }}{% endwith %}", id="with-dict-values"),
    pytest.param("{% set names = R | list %}{{ names.count('note') }}", id="aliased-list-count"),
    pytest.param("{% for d in [dict(R)] %}{{ d.items() | list }}{% endfor %}", id="looped-dict-items"),
    pytest.param("{{ [dict(R)][0].items() | list }}", id="indexed-dict-items"),
    pytest.param("{% macro m(d) %}{{ d.items() | list }}{% endmacro %}{{ m(dict(R)) }}", id="macro-dict-items"),
)


@pytest.mark.parametrize("receiver", [pytest.param("row", id="row"), pytest.param("row.source_row", id="source-row")])
@pytest.mark.parametrize("form", _WHOLE_ROW_VALUE_METHODS)
def test_a_method_on_a_whole_row_value_is_not_a_row_call(receiver: str, form: str) -> None:
    from elspeth.core.templates import extract_jinja2_field_usage

    row_attribute = "source_row" if receiver == "row.source_row" else None
    assert extract_jinja2_field_usage(form.replace("R", receiver), row_attribute=row_attribute).row_api_misuses == ()


@pytest.mark.parametrize("build", [pytest.param(_single, id="single-query-row"), pytest.param(_rag, id="rag-row")])
@pytest.mark.parametrize("form", _WHOLE_ROW_VALUE_METHODS)
def test_a_method_on_a_whole_row_value_is_admitted_by_the_opt_out_and_renders(build: Any, form: str) -> None:
    """Configuration admits what the runtime delivers: the dict's, list's or string's own method on the projected view."""
    template = form.replace("R", "row")
    build(template, [])
    assert _ADDRESS.search(_render(template, ALL_FIELDS)) is None


# A callable the template itself supplies, through a default the check cannot
# see or a keyword of dict(), is not row data: calling it is not a row call
# (G3 fix round 2, review F4). Each renders a non-empty list, so an empty
# render cannot pass.
_SUPPLIED_CALLABLES = (
    pytest.param("{{ R.get('absent', range)(2) | list }}", "[0, 1]", id="get-with-a-callable-default"),
    pytest.param("{% set a = [range] %}{{ R.get('absent', *a)(2) | list }}", "[0, 1]", id="get-with-a-splatted-default"),
    pytest.param(
        "{% set k = {'default': range} %}{{ R.get('absent', **k)(2) | list }}", "[0, 1]", id="get-with-a-keyword-splatted-default"
    ),
    pytest.param("{{ dict(R, f=range).f(2) | list }}", "[0, 1]", id="a-keyword-of-dict-called"),
    pytest.param("{% set d = dict(R, f=range) %}{{ d.f(2) | list }}", "[0, 1]", id="a-keyword-of-an-aliased-dict-called"),
    pytest.param("{{ (R.absent | default(range))(2) | list }}", "[0, 1]", id="a-field-defaulted-to-a-callable"),
    pytest.param("{% set a = [range] %}{{ (R.absent | default(*a))(2) | list }}", "[0, 1]", id="a-field-with-a-splatted-default"),
)


@pytest.mark.parametrize("receiver", [pytest.param("row", id="row"), pytest.param("row.source_row", id="source-row")])
@pytest.mark.parametrize(("form", "rendered"), _SUPPLIED_CALLABLES)
def test_a_callable_the_template_supplies_is_not_a_row_call(receiver: str, form: str, rendered: str) -> None:
    from elspeth.core.templates import extract_jinja2_field_usage

    row_attribute = "source_row" if receiver == "row.source_row" else None
    assert extract_jinja2_field_usage(form.replace("R", receiver), row_attribute=row_attribute).row_api_misuses == ()


@pytest.mark.parametrize("build", [pytest.param(_single, id="single-query-row"), pytest.param(_rag, id="rag-row")])
@pytest.mark.parametrize(("form", "rendered"), _SUPPLIED_CALLABLES)
def test_a_callable_the_template_supplies_is_admitted_by_the_opt_out_and_renders(build: Any, form: str, rendered: str) -> None:
    template = form.replace("R", "row")
    build(template, [])
    assert _render(template, ALL_FIELDS) == rendered


def test_a_column_named_like_a_method_or_a_reserved_name_is_read_by_item_or_attribute() -> None:
    """``row.items`` / ``row['contract']`` read columns; only the attribute form of a retired name is refused."""
    _single("{{ row.items }} {{ row.keys }} {{ row['contract'] }} {{ row['to_dict'] }}", ["items", "keys", "contract", "to_dict"])
    _single("{{ row.items }} {{ row['contract'] }}", [])


def test_a_computed_key_is_still_admitted_by_the_opt_out() -> None:
    """Only the row-as-object forms lost the ``[]`` exemption: a computed key can resolve against the whole row."""
    _single("{{ row[row.selector] }}", [])
    with pytest.raises(ValidationError, match="dynamic row field access"):
        _single("{{ row[row.selector] }}", ["selector"])


@pytest.mark.parametrize("template", ["{{ row | dictsort }}", "{{ dict(row) }}", "{{ row }}", "{% for k in row %}{{ k }}{% endfor %}"])
def test_a_single_query_whole_row_read_with_no_declaration_is_refused(template: str) -> None:
    """Omitted declares no field, so the row a whole-row form shows is empty on every row (ADR-051)."""
    with pytest.raises(ValidationError, match=re.escape("uses 'row' as a whole, but options.required_input_fields is not declared")):
        _single(template, _OMITTED)
    _single(template, [])
    _single(template, ["note"])


def test_a_rag_whole_row_read_with_no_declaration_holds_the_query_field() -> None:
    """A retrieval row always holds ``query_field``, so the omitted declaration is not an empty row there."""
    _rag("{{ row | dictsort }}", _OMITTED)


@pytest.mark.parametrize("variable", ["items", "keys", "values", "get", "copy", "source_row"])
def test_a_query_variable_the_query_row_cannot_read_by_attribute_is_refused(variable: str) -> None:
    """A query's ``row`` is a mapping: ``row.items`` finds the method, and ``source_row`` is its own entry."""
    options: dict[str, Any] = {
        "provider": "openrouter",
        "model": "openai/gpt-4.1-nano",
        "prompt_template": "x",
        "queries": {"q1": {"input_fields": {variable: "note"}, "template": "{{ row['" + variable + "'] }}"}},
        "required_input_fields": ["note"],
        "schema_config": {"mode": "observed"},
    }
    with pytest.raises(ValidationError, match=f"input_fields variable '{variable}' cannot be read as that variable"):
        LLMConfig(**options)


# ---------------------------------------------------------------------------
# Runtime: the residual configuration cannot follow reads the same constants
# ---------------------------------------------------------------------------

_ROW = {"id": 1, "note": "first", "items": "COL-ITEMS", "keys": "COL-KEYS", "contract": "COL-CONTRACT", "k": "contract"}


def _row(values: dict[str, Any] = _ROW) -> PipelineRow:
    fields = tuple(
        FieldContract(normalized_name=name, original_name=name, python_type=type(value), required=True, source="declared")
        for name, value in values.items()
    )
    return PipelineRow(values, SchemaContract(mode="FLEXIBLE", fields=fields, locked=True))


_PROJECTIONS = (
    pytest.param(ALL_FIELDS, id="opt-out"),
    pytest.param(DeclaredFields(frozenset({"note", "contract", "k"})), id="declared-with-the-column"),
    pytest.param(DeclaredFields(frozenset({"note", "k"})), id="declared-without-the-column"),
)


def _render(source: str, projection: RowProjection, values: dict[str, Any] = _ROW) -> str:
    return SandboxedTemplate(source).render(row=TemplateRow.project(_row(values), projection))


def _render_error(source: str, projection: RowProjection) -> str:
    with pytest.raises(TemplateError) as caught:
        _render(source, projection)
    return str(caught.value)


@pytest.mark.parametrize("projection", _PROJECTIONS)
@pytest.mark.parametrize(
    "source",
    [
        "{{ row.contract }}",
        "{{ row.to_dict() }}",
        "{{ row | attr('contract') }}",
        # A name computed from the row: configuration cannot follow it; the reason names the set, not the value.
        "{{ row | attr(row.k) }}",
        "{% set a = {'k': row} %}{% set a = {'k': a} %}{{ a.k.k.to_checkpoint_format }}",
    ],
)
def test_a_retired_name_in_attribute_form_is_refused_on_every_projection(source: str, projection: RowProjection) -> None:
    assert _render_error(source, projection) == _RESERVED


def test_the_column_of_a_reserved_name_is_read_by_item() -> None:
    assert _render("{{ row['contract'] }}", ALL_FIELDS) == "COL-CONTRACT"


def test_a_missing_field_is_named_without_the_internal_class() -> None:
    message = _render_error("{{ row.absent }}", ALL_FIELDS)
    assert message == "Undefined variable: the row has no field 'absent'"


@pytest.mark.parametrize(
    ("projection", "expected"),
    [
        pytest.param(ALL_FIELDS, _ROW, id="opt-out"),
        pytest.param(DeclaredFields(frozenset({"note", "k"})), {"note": "first", "k": "contract"}, id="declared"),
    ],
)
def test_the_row_as_text_is_the_mapping_it_holds(projection: RowProjection, expected: dict[str, Any]) -> None:
    """``{{ row }}`` and every string conversion render the declared fields, deterministically (no address)."""
    for source in ("{{ row }}", "{{ row | string }}", "{{ row ~ '' }}", "{{ '%s' % (row,) }}"):
        assert _render(source, projection) == str(expected)
    assert _render("{{ row | pprint }}", projection) == pprint.pformat(expected)


def test_a_nested_value_renders_as_its_data_not_its_frozen_carrier() -> None:
    values = {"note": "first", "meta": {"a": [1, 2]}}
    fields = (
        FieldContract(normalized_name="note", original_name="note", python_type=str, required=True, source="declared"),
        FieldContract(normalized_name="meta", original_name="meta", python_type=object, required=True, source="declared"),
    )
    row = TemplateRow.project(PipelineRow(values, SchemaContract(mode="FLEXIBLE", fields=fields, locked=True)), ALL_FIELDS)
    assert SandboxedTemplate("{{ row }}").render(row=row) == "{'note': 'first', 'meta': {'a': [1, 2]}}"
    assert SandboxedTemplate("{{ row | tojson }}").render(row=row) == '{"meta": {"a": [1, 2]}, "note": "first"}'
    assert SandboxedTemplate("{{ row.meta | tojson }}").render(row=row) == '{"a": [1, 2]}'
    assert SandboxedTemplate("{{ {'r': row} | tojson }}").render(row=row) == '{"r": {"meta": {"a": [1, 2]}, "note": "first"}}'


def test_the_row_repr_names_fields_and_never_a_value() -> None:
    """A repr can reach exception text, so it prints names only; a row carried in a list shows those names."""
    rendered = _render("{{ [row] }}", DeclaredFields(frozenset({"note", "k"})))
    assert rendered == "[<TemplateRow: note, k>]"
    assert "first" not in rendered


def test_last_and_urlencode_see_the_mapping() -> None:
    projection = DeclaredFields(frozenset({"note", "k"}))
    assert _render("{{ row | last }}", projection) == "k"
    assert _render("{{ row | urlencode }}", projection) == "note=first&k=contract"


@pytest.mark.parametrize("projection", _PROJECTIONS)
def test_reverse_is_the_list_of_field_names_not_a_lazy_iterator(projection: RowProjection) -> None:
    """``row | reverse`` printed bare sends the names, as it did before the row had ``__reversed__``, never an address."""
    names = list(TemplateRow.project(_row(), projection))
    rendered = _render("{{ row | reverse }}", projection)
    assert rendered == str(names[::-1])
    assert not _ADDRESS.search(rendered)
    assert _render("{% for name in row | reverse %}{{ name }};{% endfor %}", projection) == "".join(f"{n};" for n in names[::-1])
    assert _render("{{ row | last }}", projection) == names[-1]
    # Any other value keeps the builtin: a list's reverse is still its lazy iterator.
    assert _ADDRESS.search(_render("{{ [1, 2] | reverse }}", projection))


_SCALAR_ROW = {"id": 1, "note": "first", "items": "COL-ITEMS", "keys": "COL-KEYS", "contract": "COL-CONTRACT"}


def _outcome(source: str, value: Any) -> tuple[str, str]:
    try:
        return "ok", _ADDRESS.sub(" at 0x", SandboxedTemplate(source).render(row=value))
    except TemplateError:
        return "error", ""


@pytest.mark.parametrize("name", sorted(FILTERS))
@pytest.mark.parametrize(
    "projection",
    [pytest.param(ALL_FIELDS, id="opt-out"), pytest.param(DeclaredFields(frozenset({"note", "items", "keys"})), id="declared")],
)
def test_every_builtin_filter_sees_the_row_as_the_mapping_it_holds(name: str, projection: RowProjection) -> None:
    """A whole row under any builtin filter renders exactly what the same filter renders for ``dict(row)``.

    Both fail, or both render the same text. The one filter that reads a row
    field instead is ``attr`` (attribute syntax), and it needs an argument, so
    bare ``attr`` fails on both. ``reverse`` of a dict is a lazy iterator
    whose printed form is its own class and address, while the row's is the
    list of its names, so the two are compared materialised.
    """
    row = TemplateRow.project(_row(_SCALAR_ROW), projection)
    plain = dict(row)
    source = "{{ row | reverse | list }}" if name == "reverse" else f"{{{{ row | {name} }}}}"
    assert _outcome(source, row) == _outcome(source, plain)
