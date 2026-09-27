"""A RAG query template sees only the fields its node declares (ADR-051 (f), elspeth-5887fb7928 P3).

The retrieval query goes to an external search provider, so a
``query_template`` is held to the rules of an LLM prompt template:

- configuration refuses the reads it can prove fail or go unused
  (``RetrievalOutputConfig._validate_query_template_row_reads``): an undefined
  top-level name, a computed row key under a declaration, a literal read
  outside ``required_input_fields`` plus ``query_field`` (omitted declares
  ``query_field`` alone), and fields declared for a template that never reads
  ``row``;
- the render sees the row projected to that same declaration
  (``query_template_row_projection``), in the parent process, before the
  context crosses to the render worker: a list holds those fields plus
  ``query_field``, omitted holds ``query_field``, ``[]`` holds the whole row.

Every configuration check reads the projection the render uses, so the two
cannot disagree about what the template may see.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure import templates as template_infrastructure
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.templates import (
    ALL_FIELDS,
    DeclaredFields,
    RowProjection,
    TemplateError,
    declared_row_projection,
)
from elspeth.plugins.transforms.azure.ai_search import AzureAISearchConfig
from elspeth.plugins.transforms.rag.config import RAGRetrievalConfig
from elspeth.plugins.transforms.rag.query import QueryBuilder
from tests.unit.plugins.infrastructure.test_template_projection import _LEAK_FORMS

_VALUE_SENTINEL = "SENTINEL-P3-RAG-VALUE-7d1"
_KEY_SENTINEL = "SENTINEL_P3_RAG_KEY_7d1"
_UNDECLARED_SECRET = "Undeclared field: the template reads 'secret', a field this node does not declare in required_input_fields"
_OMITTED = object()


def _config(template: str, required_input_fields: Any = _OMITTED) -> RAGRetrievalConfig:
    options: dict[str, Any] = {
        "output_prefix": "kb",
        "query_field": "question",
        "query_template": template,
        "provider": "chroma",
        "provider_config": {"collection": "p3-facts", "mode": "ephemeral"},
        "schema_config": {"mode": "observed"},
    }
    if required_input_fields is not _OMITTED:
        options["required_input_fields"] = required_input_fields
    return RAGRetrievalConfig(**options)


def _refusal(template: str, required_input_fields: Any = _OMITTED) -> str:
    with pytest.raises(ValidationError) as caught:
        _config(template, required_input_fields)
    [error] = caught.value.errors()
    return str(error["msg"])


def _row(**overrides: object) -> PipelineRow:
    """The query field, a declared-shaped topic, an undeclared column and an undeclared extra key, both carrying sentinels."""
    fields = (
        FieldContract("question", "Question", str, True, "declared"),
        FieldContract("topic", "Topic", str, True, "declared"),
        FieldContract("secret", "secret", str, True, "declared"),
    )
    data: dict[str, object] = {
        "question": "how do plants eat",
        "topic": "biology",
        "secret": _VALUE_SENTINEL,
        _KEY_SENTINEL: _VALUE_SENTINEL,
    }
    data.update(overrides)
    return PipelineRow(data, SchemaContract(mode="FLEXIBLE", fields=fields, locked=True))


def _outcome(template: str, projection: RowProjection, row: PipelineRow | None = None) -> str:
    """The query a template builds, or its routed reason, or its construction refusal, as text."""
    try:
        builder = QueryBuilder("question", row_projection=projection, query_template=template)
    except TemplateError as exc:
        return f"refused at construction: {exc}"
    try:
        result = builder.build(_row() if row is None else row)
    finally:
        builder.close()
    return result.query if result.query is not None else repr(result.error)


def _shows_undeclared(text: str) -> bool:
    return _VALUE_SENTINEL in text or _KEY_SENTINEL in text


_QUESTION_ONLY = DeclaredFields(frozenset({"question"}))
_QUESTION_AND_TOPIC = DeclaredFields(frozenset({"question", "topic"}))


# ---------------------------------------------------------------------------
# The declaration is read one way, with query_field always declared
# ---------------------------------------------------------------------------


def test_always_declared_fields_join_a_list_and_the_omitted_case_but_never_the_opt_out() -> None:
    always = frozenset({"question"})
    assert declared_row_projection(["topic"], always_declared=always) == _QUESTION_AND_TOPIC
    assert declared_row_projection(None, always_declared=always) == _QUESTION_ONLY
    # ``[]`` stays the whole row: pre-joining ``[] + [query_field]`` would silently withdraw the opt-out.
    assert declared_row_projection([], always_declared=always) is ALL_FIELDS


@pytest.mark.parametrize(
    ("required_input_fields", "expected"),
    [
        pytest.param(_OMITTED, _QUESTION_ONLY, id="omitted-holds-the-query-field"),
        pytest.param(["topic"], _QUESTION_AND_TOPIC, id="list-plus-the-query-field"),
        pytest.param(["question", "topic"], _QUESTION_AND_TOPIC, id="list-naming-the-query-field"),
        pytest.param([], ALL_FIELDS, id="opt-out-is-the-whole-row"),
    ],
)
def test_the_query_template_projection_is_the_declaration(required_input_fields: Any, expected: RowProjection) -> None:
    assert _config("{{ query }} {{ row | dictsort }}", required_input_fields).query_template_row_projection() == expected


# ---------------------------------------------------------------------------
# Configuration refuses what it can prove fails or goes unused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "required_input_fields", "expected"),
    [
        pytest.param(
            "{{ query }} {{ row.secret }}",
            ["question"],
            "query_template reads 'secret' under 'row', which this node does not declare: its template sees "
            "'question' (options.required_input_fields and query_field), so every row fails at render with "
            "'Undeclared field'.",
            id="probe-rag-dotted-declared",
        ),
        pytest.param(
            "{{ query }} {{ row.secret }}",
            _OMITTED,
            "query_template reads 'secret' under 'row', but options.required_input_fields is not declared, so the "
            "template's row holds only the query field 'question' and every row fails at render with 'Undeclared field'.",
            id="probe-rag-dotted-omitted",
        ),
        pytest.param(
            "{{ query }} {{ row.items() | list }}",
            ["question"],
            "query_template uses its row as an object (a call on a row field, such as row.keys(), row.items(), "
            "row['keys'](), row.name() or row.get('name')()). A template's row holds fields and one method, get",
            id="probe-rag-items-is-a-field-call",
        ),
        pytest.param(
            "{{ query }} {{ row['Secret Header'] }}",
            ["question", "topic"],
            "query_template reads 'Secret Header' (declare as 'secret_header') under 'row'",
            id="bracket-literal-names-its-declarable-form",
        ),
        pytest.param(
            "{{ query }} {{ row[query] }}",
            ["question"],
            "query_template uses dynamic row field access (item via row[expr]).",
            id="computed-key-declared",
        ),
        pytest.param(
            "{{ query }} {{ row.get(query) }}",
            _OMITTED,
            "query_template uses dynamic row field access (get via row.get(expr)).",
            id="computed-get-omitted",
        ),
        pytest.param(
            "{{ query }} {{ row.to_dict() }}",
            ["question"],
            "query_template uses its row as an object (row.contract, row.to_dict, row.to_checkpoint_format or a name starting with '_').",
            id="reserved-row-api",
        ),
        pytest.param(
            "{{ qurey }}",
            [],
            "query_template references 'qurey', which the query render context does not define",
            id="unbound-name-even-under-the-opt-out",
        ),
        pytest.param(
            "{{ query }} in the natural sciences",
            ["question", "topic"],
            "options.required_input_fields declares 'topic', but query_template never reads 'row', so no declared "
            "field can reach the query.",
            id="dual-declared-but-row-never-read",
        ),
        # A ``row`` the template binds itself is a local variable, never the
        # context row: none of these can let a declared field reach the query
        # (P3 review, finding 1).
        pytest.param(
            "{% for row in [1] %}{% endfor %}{{ query }}",
            ["question", "topic"],
            "options.required_input_fields declares 'topic', but query_template never reads 'row'",
            id="dual-a-loop-variable-named-row",
        ),
        pytest.param(
            "{% set row = query %}{{ row }}",
            ["question", "topic"],
            "options.required_input_fields declares 'topic', but query_template never reads 'row'",
            id="dual-a-set-variable-named-row",
        ),
        pytest.param(
            "{% macro m(row) %}{{ row }}{% endmacro %}{{ m(query) }}",
            ["question", "topic"],
            "options.required_input_fields declares 'topic', but query_template never reads 'row'",
            id="dual-a-macro-parameter-named-row",
        ),
    ],
)
def test_configuration_refuses_a_read_it_can_prove_fails_or_goes_unused(template: str, required_input_fields: Any, expected: str) -> None:
    assert expected in _refusal(template, required_input_fields)


def test_the_dual_remedy_never_points_at_the_whole_row_opt_out() -> None:
    """Dropping the unused declaration is the repair; ``[]`` would send every column to the provider."""
    message = _refusal("{{ query }} in the natural sciences", ["question", "topic"])
    assert "drop them from options.required_input_fields" in message
    assert "[]" not in message


@pytest.mark.parametrize(
    ("template", "required_input_fields"),
    [
        pytest.param("{{ query }}", _OMITTED, id="query-only-omitted"),
        pytest.param("{{ query }}", ["question"], id="query-only-declaring-the-query-field"),
        pytest.param("{{ row.question }}", _OMITTED, id="the-query-field-needs-no-declaration"),
        pytest.param("{{ query }} ({{ row.topic }})", ["question", "topic"], id="declared-read"),
        pytest.param("{{ query }} ({{ row.topic }})", ["topic"], id="declared-read-without-naming-the-query-field"),
        # A whole row used as a value is not refused: it holds only the declaration (ADR-051 (c)).
        pytest.param("{{ query }} {{ row | dictsort }}", ["question"], id="probe-rag-dictsort-declared"),
        pytest.param("{{ query }} {{ row | dictsort }}", _OMITTED, id="probe-rag-dictsort-omitted"),
        pytest.param("{{ query }} {{ row | dictsort }}", ["question", "topic"], id="dual-counts-a-whole-row-use"),
        pytest.param("{% for a, b in [(1, [row])] %}{{ b[0] | dictsort }}{% endfor %}", ["topic"], id="r3-escape-form"),
        pytest.param("{{ query }} {{ row.secret }} {{ row[query] }}", [], id="the-opt-out-skips-the-row-checks"),
        # A macro body's read of ``row`` is a read of the context row.
        pytest.param(
            "{% macro m() %}{{ row.topic }}{% endmacro %}{{ query }} {{ m() }}", ["topic"], id="a-macro-body-reads-the-context-row"
        ),
    ],
)
def test_configuration_admits_what_the_declaration_covers(template: str, required_input_fields: Any) -> None:
    _config(template, required_input_fields)


def test_azure_ai_search_carries_the_same_checks() -> None:
    """``AzureAISearchConfig`` inherits ``RetrievalOutputConfig``: one rule for both retrieval plugins."""
    options = {
        "output_prefix": "policy",
        "query_field": "question",
        "endpoint": "https://svc-a.search.windows.net",
        "index": "approved-documents",
        "api_key": "test-key",
        "schema": {"mode": "observed"},
        "query_template": "{{ query }} {{ row.secret }}",
        "required_input_fields": ["question"],
    }
    with pytest.raises(PluginConfigError, match="query_template reads 'secret' under 'row', which this node does not declare"):
        AzureAISearchConfig.from_dict(options, plugin_name="azure_ai_search")
    assert (
        AzureAISearchConfig.from_dict(
            {**options, "query_template": "{{ query }} {{ row.topic }}", "required_input_fields": ["topic"]},
            plugin_name="azure_ai_search",
        ).query_template_row_projection()
        == _QUESTION_AND_TOPIC
    )


# ---------------------------------------------------------------------------
# The render sees the projection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("projection", "expected"),
    [
        pytest.param(_QUESTION_ONLY, "how do plants eat [('question', 'how do plants eat')]", id="omitted"),
        pytest.param(
            _QUESTION_AND_TOPIC, "how do plants eat [('question', 'how do plants eat'), ('topic', 'biology')]", id="declared-list"
        ),
    ],
)
def test_the_query_template_row_holds_exactly_the_declaration(projection: DeclaredFields, expected: str) -> None:
    assert _outcome("{{ query }} {{ row | dictsort }}", projection) == expected


def test_the_opt_out_holds_the_whole_row() -> None:
    """Positive control for every negative below: under ``[]`` the undeclared column and key are rendered."""
    rendered = _outcome("{{ query }} {{ row | dictsort }}", ALL_FIELDS)
    assert _VALUE_SENTINEL in rendered
    assert _KEY_SENTINEL in rendered


def test_a_declared_field_reads_by_either_spelling() -> None:
    assert _outcome("{{ row.topic }}|{{ row['Topic'] }}|{{ query }}", _QUESTION_AND_TOPIC) == "biology|biology|how do plants eat"


@pytest.mark.parametrize(
    "template",
    [
        "{{ query }} {% for v in [[row]] %}{{ v[0].secret }}{% endfor %}",
        "{{ query }}{% if 'secret' in row %} HAS{% endif %}",
        "{{ query }} {{ row.get('secret', 'D') }}",
    ],
)
def test_an_undeclared_read_configuration_cannot_see_routes_with_its_own_reason(template: str) -> None:
    """The reason is value-free and distinct from an absent declared field; ``in``/``get`` learn nothing either."""
    assert _outcome(template, _QUESTION_ONLY) == repr(
        {"reason": "template_rendering_failed", "error": _UNDECLARED_SECRET, "field": "question"}
    )


@pytest.mark.parametrize("projection", [_QUESTION_ONLY, _QUESTION_AND_TOPIC], ids=["omitted", "declared-topic"])
@pytest.mark.parametrize("template", _LEAK_FORMS)
def test_no_s0_leak_form_shows_an_undeclared_field_in_a_query(template: str, projection: DeclaredFields) -> None:
    """The S0 leak corpus (268 forms), rendered as a query template: no query and no reason shows a sentinel."""
    assert not _shows_undeclared(_outcome(template, projection))


@pytest.mark.parametrize(
    "template",
    [
        "{{ row.note }} {{ row | dictsort }}",
        "{% for a, b in [(1, [row])] %}{{ row.note }} {{ b[0] | dictsort }}{% endfor %}",
        "{% set d = {'r': row} %}{{ row.note }} {{ d.values() | first | dictsort }}",
    ],
)
def test_the_corpus_instrument_detects_a_leak_in_a_query_on_the_whole_row(template: str) -> None:
    """Positive control for the corpus scan: the same forms show the sentinel under ``[]`` (``note`` is simply absent here)."""
    assert template in _LEAK_FORMS
    assert _shows_undeclared(_outcome(template.replace("row.note", "query"), ALL_FIELDS))


# ---------------------------------------------------------------------------
# What crosses to the render worker
# ---------------------------------------------------------------------------


def _bytes_sent(monkeypatch: pytest.MonkeyPatch, template: str, projection: RowProjection) -> tuple[str, list[bytes]]:
    sent: list[bytes] = []
    original = template_infrastructure._run_template_worker

    def record(source: str, payload: bytes, *, value_free: bool = False) -> str:
        sent.append(payload)
        return original(source, payload, value_free=value_free)

    monkeypatch.setattr(template_infrastructure, "_run_template_worker", record)
    return _outcome(template, projection), sent


@pytest.mark.parametrize("projection", [_QUESTION_ONLY, _QUESTION_AND_TOPIC], ids=["omitted", "declared-topic"])
def test_the_bytes_a_query_render_sends_to_the_worker_carry_no_undeclared_field(
    monkeypatch: pytest.MonkeyPatch, projection: DeclaredFields
) -> None:
    rendered, sent = _bytes_sent(monkeypatch, "{{ query }} {{ row | dictsort }}", projection)
    assert rendered.startswith("how do plants eat [('question'")
    [payload] = sent
    assert not _shows_undeclared(payload.decode("latin-1"))
    assert b"how do plants eat" in payload


def test_the_transport_instrument_sees_an_opted_out_query_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control for the byte scan: under ``[]`` the whole row crosses to the worker."""
    _rendered, [payload] = _bytes_sent(monkeypatch, "{{ query }} {{ row | dictsort }}", ALL_FIELDS)
    assert _VALUE_SENTINEL.encode() in payload
    assert _KEY_SENTINEL.encode() in payload


def test_field_and_regex_modes_render_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only template mode renders: the other modes read the query field alone and never reach the worker."""
    sent: list[bytes] = []
    monkeypatch.setattr(template_infrastructure, "_run_template_worker", lambda *args, **kwargs: sent.append(args[1]))
    builder = QueryBuilder("question", row_projection=_QUESTION_ONLY)
    assert builder.build(_row()).query == "how do plants eat"
    assert sent == []
