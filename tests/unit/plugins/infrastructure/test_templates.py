"""Tests for shared template infrastructure."""

import pickle
import threading
import time
from collections import namedtuple
from types import MappingProxyType

import pytest
from jinja2 import TemplateSyntaxError, nodes

from elspeth.contracts.freeze import FrozenJsonArray
from elspeth.plugins.infrastructure import templates as template_infrastructure
from elspeth.plugins.infrastructure.templates import (
    TemplateError,
    create_sandboxed_environment,
)
from elspeth.testing import make_pipeline_row


def test_create_sandboxed_environment_returns_immutable_sandbox():
    env = create_sandboxed_environment()
    template = env.from_string("Hello {{ name }}")
    result = template.render(name="world")
    assert result == "Hello world"


def test_worker_preserves_nested_pipeline_rows_and_frozen_lookups():
    env = create_sandboxed_environment()
    template = env.from_string("{{ row.source_row.text }} {{ lookup.labels.primary }}")
    row = {"source_row": make_pipeline_row({"text": "hello"})}
    lookup = MappingProxyType({"labels": MappingProxyType({"primary": "world"})})

    assert template.render(row=row, lookup=lookup) == "hello world"


def test_worker_preserves_shared_mapping_identity():
    env = create_sandboxed_environment()
    shared = {"value": "same"}
    template = env.from_string("{{ row.a is sameas row.b }}")

    assert template.render(row={"a": shared, "b": shared}) == "True"


def test_worker_carries_frozen_json_array_with_nested_mapping():
    env = create_sandboxed_environment()
    values = FrozenJsonArray((MappingProxyType({"label": "kept"}),))
    template = env.from_string("{{ row['values'][0].label }}")

    assert template.render(row={"values": values}) == "kept"


def test_context_transport_shares_dag_and_rejects_cycles():
    shared: object = ("leaf",)
    for _ in range(18):
        shared = (shared, shared)

    packed = template_infrastructure._pack_context_value(shared)
    assert packed[0] is packed[1]
    assert len(pickle.dumps(packed, protocol=5)) < 2048

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(TemplateError, match="cyclic"):
        template_infrastructure._pack_context_value(cyclic)


def test_context_transport_limits_unique_parent_work(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(template_infrastructure, "_MAX_CONTEXT_NODES", 8)
    with pytest.raises(TemplateError, match="parent packing limit"):
        template_infrastructure._pack_context_value({"items": [{"value": i} for i in range(9)]})


def test_saturated_template_workers_wait_without_classifying_a_good_row_as_bad(monkeypatch: pytest.MonkeyPatch):
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(template_infrastructure, "_WORKER_SLOTS", slot)
    template = create_sandboxed_environment().from_string("{{ row.text }}")
    slot.acquire()

    def release_slot() -> None:
        time.sleep(0.05)
        slot.release()

    releaser = threading.Thread(target=release_slot)
    releaser.start()
    try:
        assert template.render(row={"text": "hello"}) == "hello"
    finally:
        releaser.join()


def test_render_workers_are_reused_between_rows() -> None:
    template = create_sandboxed_environment().from_string("{{ row.text }}")
    for _ in range(2):
        assert template.render(row={"text": "hello"}) == "hello"
    initial_pids = {entry[0].pid for entry in template_infrastructure._WORKERS if entry is not None}
    for _ in range(6):
        assert template.render(row={"text": "hello"}) == "hello"
    assert {entry[0].pid for entry in template_infrastructure._WORKERS if entry is not None} == initial_pids


def test_context_transport_rejects_oversized_mapping_key_before_serialization():
    with pytest.raises(TemplateError, match="parent packing limit"):
        template_infrastructure._pack_context_value({"x" * (9 * 1024 * 1024): "small"})


def test_context_transport_rejects_oversized_frozen_carriers_before_serialization():
    named_values = namedtuple("NamedValues", "text")
    large_text = "x" * (9 * 1024 * 1024)
    for value in (named_values(large_text), frozenset((large_text,))):
        with pytest.raises(TemplateError, match="parent packing limit"):
            template_infrastructure._pack_context_value({"value": value})


def test_context_transport_rejects_large_row_before_deep_export(monkeypatch: pytest.MonkeyPatch):
    from elspeth.contracts.schema_contract import PipelineRow

    large_text = "x" * (33 * 1024 * 1024)
    named_values = namedtuple("NamedValues", "text")
    rows = [
        make_pipeline_row({"text": FrozenJsonArray((large_text,))}),
        make_pipeline_row({"text": named_values(large_text)}),
    ]

    def export_must_not_run() -> dict[str, object]:
        raise AssertionError("oversized row was deep-copied")

    monkeypatch.setattr(PipelineRow, "to_dict", export_must_not_run)
    for row in rows:
        with pytest.raises(TemplateError, match="parent packing limit"):
            template_infrastructure._pack_context_value(row)


def test_literal_template_skips_worker_but_expression_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = template_infrastructure._run_template_worker

    def record_worker(source: str, payload: bytes, *, value_free: bool = False) -> str:
        calls.append(source)
        return original(source, payload, value_free=value_free)

    monkeypatch.setattr(template_infrastructure, "_run_template_worker", record_worker)
    env = create_sandboxed_environment()
    assert env.from_string("output.jsonl").render(run_id="example") == "output.jsonl"
    assert calls == []
    assert env.from_string("output/{{ run_id }}.jsonl").render(run_id="example") == "output/example.jsonl"
    assert calls == ["output/{{ run_id }}.jsonl"]


def test_sandboxed_environment_strict_undefined():
    env = create_sandboxed_environment()
    template = env.from_string("{{ missing }}")
    with pytest.raises(Exception, match="missing"):
        template.render()


def test_sandboxed_environment_rejects_invalid_syntax():
    env = create_sandboxed_environment()
    with pytest.raises(TemplateSyntaxError):
        env.from_string("{% if unclosed")


def test_template_source_limits_apply_before_parse_and_compile():
    env = create_sandboxed_environment()
    oversized = "a" * 16385
    with pytest.raises(TemplateError, match="exceeds"):
        env.parse(oversized)
    with pytest.raises(TemplateError, match="exceeds"):
        env.from_string(oversized)


def test_name_discovery_and_compilation_never_fold_authored_expressions(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.infrastructure.templates import find_runtime_unbound_variables

    calls = 0
    original = nodes.BinExpr.as_const

    def count_fold(node: nodes.BinExpr, eval_ctx: object = None) -> object:
        nonlocal calls
        calls += 1
        return original(node, eval_ctx)

    monkeypatch.setattr(nodes.BinExpr, "as_const", count_fold)
    env = create_sandboxed_environment()
    source = "{{ ('x' * 1000000000)|length }}{{ row.name }}"
    ast = env.parse(source)
    assert find_runtime_unbound_variables(ast) == frozenset({"row"})
    env.from_string(source)
    assert calls == 0


def test_parse_rejects_power_and_dynamic_autoescape_before_name_discovery() -> None:
    env = create_sandboxed_environment()
    with pytest.raises(TemplateError, match="Power expressions"):
        env.parse("{{ (3**(3**15)) % 7 }}")
    with pytest.raises(TemplateError, match="autoescape requires a literal boolean"):
        env.parse("{% autoescape ('x' * 1000000000)|length > 0 %}{{ row.name }}{% endautoescape %}")
    template = env.from_string("{% autoescape true %}{{ row.name }}{% endautoescape %}")
    assert template.render(row={"name": "<unsafe>"}) == "&lt;unsafe&gt;"


def test_render_output_is_bounded():
    env = create_sandboxed_environment()
    template = env.from_string("{{ row.text * 5000000 }}")
    with pytest.raises(TemplateError, match="Rendered template exceeds"):
        template.render(row={"text": "x"})


def test_worker_memory_limit_catches_allocation_without_large_output() -> None:
    template = create_sandboxed_environment().from_string("{{ (row.text * 320000000)|length }}")
    with pytest.raises(TemplateError, match="memory limit"):
        template.render(row={"text": "x"})


def test_render_context_is_bounded_before_worker_start():
    env = create_sandboxed_environment()
    template = env.from_string("{{ row.text }}")
    with pytest.raises(TemplateError, match="Template context exceeds"):
        template.render(row={"text": "x" * (8 * 1024 * 1024)})


def test_template_error_is_exception():
    err = TemplateError("bad template")
    assert isinstance(err, Exception)
    assert str(err) == "bad template"


def _unbound(template: str) -> frozenset[str]:
    from elspeth.plugins.infrastructure.templates import find_runtime_unbound_variables

    return find_runtime_unbound_variables(create_sandboxed_environment().parse(template))


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        pytest.param("{{ x }}", {"x"}, id="plain-load"),
        pytest.param("{% set x = 1 %}{{ x }}", set(), id="set-binds"),
        pytest.param("{% set x = y %}{{ x }}", {"y"}, id="set-rhs-scanned"),
        pytest.param("{% if c %}{% set x = 1 %}{% endif %}{{ x }}", {"c", "x"}, id="if-one-branch-unbound"),
        pytest.param("{% if c %}{% set x = 1 %}{% else %}{% set x = 2 %}{% endif %}{{ x }}", {"c"}, id="if-else-both-bind"),
        pytest.param(
            "{% if c %}{% set x = 1 %}{% elif d %}{% set x = 2 %}{% else %}{% set x = 3 %}{% endif %}{{ x }}",
            {"c", "d"},
            id="elif-all-bind",
        ),
        pytest.param("{% for i in items %}{{ i }}{{ loop.index }}{% endfor %}{{ i }}", {"items", "i"}, id="for-scopes-target"),
        pytest.param("{% for i in items if i %}{{ i }}{% else %}{{ i }}{% endfor %}", {"items", "i"}, id="for-else-outside-loop-scope"),
        pytest.param("{% with a = b %}{{ a }}{% endwith %}{{ a }}", {"a", "b"}, id="with-scopes-target"),
        pytest.param("{% macro m(p, q=r) %}{{ p }}{{ q }}{{ caller() }}{% endmacro %}{{ m(1) }}", {"r"}, id="macro-binds-name-args"),
        pytest.param("{% macro m() %}{{ caller(1) }}{% endmacro %}{% call(u) m() %}{{ u }}{% endcall %}", set(), id="call-block-args"),
        pytest.param("{% call(u) m(v) %}{{ u }}{% endcall %}", {"m", "v"}, id="call-block-call-scanned"),
        pytest.param('{% import "x" as lib %}{{ lib.f() }}', set(), id="import-binds-target"),
        pytest.param('{% from "x" import f as g, h %}{{ g }}{{ h }}', set(), id="from-import-binds-names"),
        pytest.param("{% filter upper %}{{ y }}{% endfilter %}", {"y"}, id="filter-block"),
        pytest.param("{% set x %}{{ q }}{% endset %}{{ x }}", {"q"}, id="set-block"),
        pytest.param("{% set x | upper %}{{ q }}{% endset %}{{ x }}", {"q"}, id="set-block-filter"),
        pytest.param("{% set ns = namespace() %}{% set ns.a = 1 %}{{ ns.a }}", set(), id="namespace-bound"),
        pytest.param("{% set ns.a = 1 %}{{ ns.a }}", {"ns"}, id="namespace-unbound-nsref"),
        pytest.param("{% block b %}{{ z }}{% endblock %}", {"z"}, id="block"),
        pytest.param("{% autoescape true %}{{ w }}{% endautoescape %}", {"w"}, id="scoped-eval-context"),
        pytest.param("{% for a, b in pairs %}{{ a }}{{ b }}{% endfor %}", {"pairs"}, id="tuple-target"),
        pytest.param("{{ x }}{% set x = 1 %}", {"x"}, id="order-sensitive"),
    ],
)
def test_find_runtime_unbound_variables(template: str, expected: set[str]) -> None:
    assert _unbound(template) == frozenset(expected)


@pytest.mark.parametrize(
    "malformed_entry",
    [
        pytest.param(3, id="non-str-non-tuple"),
        pytest.param(("f",), id="short-tuple"),
        pytest.param(("f", 7), id="non-str-alias"),
        pytest.param(("f", "g", "h"), id="long-tuple"),
    ],
)
def test_from_import_binding_rejects_malformed_names(malformed_entry: object) -> None:
    """A FromImport names entry that is neither str nor (name, alias) of str raises."""
    from jinja2 import nodes

    from elspeth.plugins.infrastructure.templates import _DefiniteBindingAnalyzer

    analyzer = _DefiniteBindingAnalyzer(frozenset())
    node = nodes.FromImport(nodes.Const("x"), [malformed_entry], False)
    with pytest.raises(TemplateError):
        analyzer.visit_FromImport(node, frozenset())


# ---------------------------------------------------------------------------
# SandboxedTemplate: the ONE value-free render-error renderer (elspeth-5887fb7928
# RAG-F1). Jinja's messages quote a lookup key the template may compute from the
# row, and Python errors inside a template quote operands. Every assertion is on
# the WHOLE message: a ``sentinel not in`` check passes on a codec error that
# leaks one character of the row and its offset.
# ---------------------------------------------------------------------------

_SENTINEL = "SENTINEL-R5-7c1e"
_UNSPELLED = "<a key the template does not spell out>"
_WITHHELD = "(message withheld: it can quote row data)"


def _render_error(source: str, **context: object) -> str:
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate

    with pytest.raises(TemplateError) as caught:
        SandboxedTemplate(source).render(**context)
    return str(caught.value)


@pytest.mark.parametrize(
    ("source", "row", "expected"),
    [
        pytest.param(
            "{{ row[row.k] }}",
            {"k": _SENTINEL},
            f"Undefined variable: 'dict object' has no attribute {_UNSPELLED}",
            id="item-key-computed-from-the-row",
        ),
        pytest.param(
            "{{ row | attr(row.k) }}",
            {"k": _SENTINEL},
            f"Undefined variable: 'dict object' has no attribute {_UNSPELLED}",
            id="attr-filter-key-from-the-row",
        ),
        pytest.param(
            "{{ row.lst[row.i] }}",
            {"lst": [1], "i": 739184265},
            f"Undefined variable: list object has no element {_UNSPELLED}",
            id="element-index-from-the-row",
        ),
        pytest.param(
            "{{ row.q[row.k] }}",
            {"q": "x", "k": "__class__"},
            f"Sandbox violation: access to attribute {_UNSPELLED} of str object is unsafe",
            id="unsafe-attribute-named-by-the-row",
        ),
        pytest.param(
            "{{ row.q | wordwrap(row.w) }}",
            {"q": "a b", "w": -739184265},
            f"Template rendering failed: ValueError {_WITHHELD}",
            id="python-error-quoting-an-operand",
        ),
        pytest.param(
            "{{ row.q.encode('ascii') }}",
            {"q": "abé" + _SENTINEL},
            f"Template rendering failed: UnicodeEncodeError {_WITHHELD}",
            id="codec-error-quoting-a-character-and-offset",
        ),
        pytest.param(
            "{{ (row.lst | first).x }}",
            {"lst": []},
            "Undefined variable: a value is undefined",
            id="jinja-hint-withheld",
        ),
    ],
)
def test_a_render_failure_names_no_row_value(source: str, row: dict[str, object], expected: str) -> None:
    assert _render_error(source, row=row) == expected


@pytest.mark.parametrize(
    ("source", "row", "expected"),
    [
        pytest.param("{{ nosuchvar }}", {}, "Undefined variable: 'nosuchvar' is undefined", id="top-level-name"),
        pytest.param(
            "{{ row.missing_field }}",
            {"q": "x"},
            "Undefined variable: 'dict object' has no attribute 'missing_field'",
            id="attribute-written-in-the-template",
        ),
        pytest.param(
            "{{ row['Amount USD'] }}",
            {"q": "x"},
            "Undefined variable: 'dict object' has no attribute 'Amount USD'",
            id="subscript-written-in-the-template",
        ),
        pytest.param("{{ row.lst[5] }}", {"lst": [1]}, "Undefined variable: list object has no element 5", id="index-literal"),
        pytest.param("{{ row.lst[-4] }}", {"lst": [1]}, "Undefined variable: list object has no element -4", id="negative-index"),
        pytest.param(
            "{{ row | map(attribute='a.b') | list }}",
            {"k": {"a": {}}},
            "Undefined variable: 'str object' has no attribute 'a'",
            id="dotted-attribute-literal",
        ),
        pytest.param(
            "{{ row.missing + 1 }}",
            {"q": "x"},
            "Undefined variable: 'dict object' has no attribute 'missing'",
            id="operator-dunder-path",
        ),
        pytest.param(
            "{{ row.q.__class__ }}",
            {"q": "x"},
            "Sandbox violation: access to attribute '__class__' of str object is unsafe",
            id="unsafe-attribute-written-in-the-template",
        ),
    ],
)
def test_a_render_failure_keeps_the_names_the_template_spells_out(source: str, row: dict[str, object], expected: str) -> None:
    """The operator's own names are config text and stay in the diagnostic."""
    assert _render_error(source, row=row) == expected


def test_a_row_value_equal_to_a_template_literal_prints_as_that_literal() -> None:
    """The printable set is the template's text, so a coinciding row value prints as config.

    The same rule as ``safe_validation_error_text`` printing a key its schema
    declares; recorded here so a change to it is deliberate.
    """
    assert _render_error("{{ row.spelled }}{{ row[row.k] }}", row={"spelled": "", "k": "spelled_too"}) == (
        f"Undefined variable: 'dict object' has no attribute {_UNSPELLED}"
    )
    assert _render_error("{{ row.q }}{{ row[row.k] }}{{ 'present' }}", row={"q": "", "k": "present"}) == (
        "Undefined variable: 'dict object' has no attribute 'present'"
    )


def test_a_pipeline_row_lookup_is_rendered_value_free() -> None:
    """Production renders a PipelineRow, whose type repr is its qualified class name."""
    from elspeth.testing import make_pipeline_row

    row = make_pipeline_row({"q": "x", "k": _SENTINEL})
    assert _render_error("{{ row[row.k] }}", row=row) == (
        f"Undefined variable: 'elspeth.contracts.schema_contract.PipelineRow object' has no attribute {_UNSPELLED}"
    )


def test_withheld_error_detail_keeps_only_the_class() -> None:
    from jinja2 import UndefinedError

    from elspeth.plugins.infrastructure.templates import withheld_error_detail

    # An UndefinedError that SandboxedTemplate's undefined type did not raise is
    # not trusted to be value-free, so render() gives it this treatment too.
    assert withheld_error_detail(UndefinedError(f"'dict object' has no attribute '{_SENTINEL}'")) == f"UndefinedError {_WITHHELD}"
    assert withheld_error_detail(ValueError(f"Got: {_SENTINEL!r}")) == f"ValueError {_WITHHELD}"


def test_the_value_free_undefined_refuses_an_exception_type_jinja_never_passes() -> None:
    from jinja2 import TemplateRuntimeError

    from elspeth.plugins.infrastructure.templates import _value_free_undefined

    undefined_type = _value_free_undefined(frozenset())
    with pytest.raises(RuntimeError, match="unexpected exception type TemplateRuntimeError"):
        undefined_type(name="x", exc=TemplateRuntimeError)


def test_a_broken_jinja_undefined_contract_escapes_render_unrouted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker reporting a broken Jinja contract cannot become a row error."""
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate, _UndefinedContractError

    def broken_worker(source: str, payload: bytes, *, value_free: bool = False) -> str:
        raise _UndefinedContractError("jinja2 built an Undefined with an unexpected exception type TemplateRuntimeError")

    monkeypatch.setattr(template_infrastructure, "_run_template_worker", broken_worker)
    template = SandboxedTemplate("{{ row.missing }}")
    with pytest.raises(RuntimeError, match="unexpected exception type TemplateRuntimeError"):
        template.render(row={})


def test_sandboxed_template_reports_malformed_source_as_a_syntax_error() -> None:
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate

    with pytest.raises(TemplateSyntaxError):
        SandboxedTemplate("{% if unclosed")
    with pytest.raises(TemplateSyntaxError):
        SandboxedTemplate("{{ x | no_such_filter }}")
