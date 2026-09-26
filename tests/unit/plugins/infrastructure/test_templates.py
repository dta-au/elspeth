"""Tests for shared template infrastructure."""

import multiprocessing
import operator
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
    ALL_FIELDS,
    TemplateError,
    TemplateRow,
    create_sandboxed_environment,
)
from elspeth.testing import make_pipeline_row


def _whole(row: object) -> TemplateRow:
    """The row as a node that opted out with ``required_input_fields: []`` sees it: every field (ADR-051)."""
    from elspeth.contracts.schema_contract import PipelineRow

    assert type(row) is PipelineRow
    return TemplateRow.project(row, ALL_FIELDS)


def test_create_sandboxed_environment_returns_immutable_sandbox():
    env = create_sandboxed_environment()
    template = env.from_string("Hello {{ name }}")
    result = template.render(name="world")
    assert result == "Hello world"


def test_worker_preserves_nested_pipeline_rows_and_frozen_lookups():
    env = create_sandboxed_environment()
    template = env.from_string("{{ row.source_row.text }} {{ lookup.labels.primary }}")
    row = {"source_row": _whole(make_pipeline_row({"text": "hello"}))}
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

    def export_must_not_run(*_args: object) -> dict[str, object]:
        raise AssertionError("oversized row was deep-copied")

    monkeypatch.setattr(PipelineRow, "to_dict", export_must_not_run)
    monkeypatch.setattr("elspeth.contracts.freeze.deep_thaw", export_must_not_run)
    for row in rows:
        with pytest.raises(TemplateError, match="parent packing limit"):
            template_infrastructure._pack_context_value(_whole(row))


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


@pytest.mark.parametrize("expression", ["*".join(["7"] * 300), "not " * 1500 + "true"], ids=["binary-chain", "parser-overflow"])
def test_deep_template_is_a_typed_error_before_name_discovery(expression: str) -> None:
    source = "{{ " + expression + " }}{{ row.a }}"
    with pytest.raises(TemplateError, match="nesting"):
        create_sandboxed_environment().parse(source)


def test_template_ast_budget_preserves_shallow_names_and_bounds_wide_trees() -> None:
    from elspeth.plugins.infrastructure.templates import find_runtime_unbound_variables

    env = create_sandboxed_environment()
    source = "{{ " + "*".join(["7"] * 30) + " }}{{ row.a }}"
    assert find_runtime_unbound_variables(env.parse(source)) == frozenset({"row"})
    env.from_string(source)
    with pytest.raises(TemplateError, match="2048 nodes"):
        env.parse("{{ [" + ",".join(["1"] * 2050) + "] }}")


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
    source = "{{ ('x' * 100)|length }}{{ row.name }}"
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
    """Production renders a PipelineRow as its TemplateRow projection, whose type repr is its qualified class name."""
    from elspeth.testing import make_pipeline_row

    row = _whole(make_pipeline_row({"q": "x", "k": _SENTINEL}))
    assert _render_error("{{ row[row.k] }}", row=row) == (
        f"Undefined variable: 'elspeth.plugins.infrastructure.templates.TemplateRow object' has no attribute {_UNSPELLED}"
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

    from elspeth.contracts.errors import TIER_1_ERRORS
    from elspeth.plugins.infrastructure.templates import _UndefinedContractError, _value_free_undefined

    undefined_type = _value_free_undefined(frozenset())
    with pytest.raises(_UndefinedContractError, match="unexpected exception type TemplateRuntimeError") as caught:
        undefined_type(name="x", exc=TemplateRuntimeError)
    # A framework fault, so Tier-1: no ``on_error`` can route it.
    assert isinstance(caught.value, TIER_1_ERRORS)


def test_a_broken_jinja_undefined_contract_escapes_render_unrouted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker reporting a broken Jinja contract cannot become a row error."""
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate, _UndefinedContractError

    def broken_worker(source: str, payload: bytes, *, value_free: bool = False) -> str:
        raise _UndefinedContractError("jinja2 built an Undefined with an unexpected exception type TemplateRuntimeError")

    monkeypatch.setattr(template_infrastructure, "_run_template_worker", broken_worker)
    template = SandboxedTemplate("{{ row.missing }}")
    with pytest.raises(_UndefinedContractError, match="unexpected exception type TemplateRuntimeError"):
        template.render(row={})


def _render_with_a_foreign_undefined_exception(connection: object) -> None:
    """Spawn target: the real serving worker, with jinja2's sandbox building an Undefined around a foreign exception type.

    Module-level so the spawned child can import it; the patch lives only in the child.
    The worker serves ``(source, payload, value_free)`` requests over the pipe
    until the parent closes it (release/0.8.1 313a85bb1 reuses render workers).
    """
    from jinja2 import TemplateRuntimeError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    def undefined_with_foreign_exc(self: ImmutableSandboxedEnvironment, obj: object, attribute: str) -> object:
        return self.undefined(obj=obj, name=attribute, exc=TemplateRuntimeError)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ImmutableSandboxedEnvironment, "getattr", undefined_with_foreign_exc)
        template_infrastructure._template_worker(connection)


def test_the_worker_reports_a_broken_undefined_contract_under_its_own_status() -> None:
    """The worker half of the broken-contract guard: it is not a routable render failure.

    The render runs in a spawned worker, so the parent-side test above cannot
    reach the worker's own catch arm. Without that arm the RuntimeError would
    escape the worker, and the parent would see a stopped worker, which it
    reports as an ordinary (routable) TemplateError.
    """
    process_context = multiprocessing.get_context("spawn")
    parent, child = process_context.Pipe()
    payload = pickle.dumps(template_infrastructure._pack_context_value({"row": {}}), protocol=5)
    process = process_context.Process(target=_render_with_a_foreign_undefined_exception, args=(child,))
    process.start()
    child.close()
    try:
        # A worker's first message reports it ready (S3b); the request's reply follows.
        assert parent.poll(60), "the worker never reported ready"
        assert parent.recv() == ("ready", "")
        parent.send(("{{ row.missing }}", payload, True))
        assert parent.poll(60), "the worker sent nothing"
        status, message = parent.recv()
    finally:
        parent.close()
        process.join(60)
        if process.is_alive():
            process.kill()
            process.join()

    assert status == "undefined_contract"
    assert "unexpected exception type TemplateRuntimeError" in message


def test_sandboxed_template_reports_malformed_source_as_a_syntax_error() -> None:
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate

    with pytest.raises(TemplateSyntaxError):
        SandboxedTemplate("{% if unclosed")
    with pytest.raises(TemplateSyntaxError):
        SandboxedTemplate("{{ x | no_such_filter }}")


# ---------------------------------------------------------------------------
# A template whose own literals make it fail on every row is a configuration
# error (elspeth-5887fb7928 S3): it is refused when built, never routed once per
# row. Jinja defers an unknown filter or test inside ``{% if %}`` or an inline
# ``if`` to render time, and ``truncate`` asserts its arguments at render.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param("{% if row.q %}{{ row.q | no_such_filter }}{% endif %}", "No filter named 'no_such_filter'.", id="filter-in-if"),
        pytest.param("{% if row.q is no_such_test %}x{% endif %}", "No test named 'no_such_test'.", id="test-in-if"),
        pytest.param("{% if row.q %}{{ row.q is no_such_test }}{% endif %}", "No test named 'no_such_test'.", id="test-in-if-body"),
        pytest.param("{{ (row.q | no_such_filter) if row.q else '' }}", "No filter named 'no_such_filter'.", id="filter-in-condexpr"),
        pytest.param(
            "{% if row.q %}{% filter no_such_filter %}x{% endfilter %}{% endif %}", "No filter named 'no_such_filter'.", id="filter-block"
        ),
        pytest.param("{{ row.l | map('no_such_filter') | list }}", "No filter named 'no_such_filter'.", id="map-filter-name"),
        pytest.param("{{ row.l | select('no_such_test') | list }}", "No test named 'no_such_test'.", id="select-test-name"),
        pytest.param("{{ row.l | reject('no_such_test') | list }}", "No test named 'no_such_test'.", id="reject-test-name"),
        pytest.param("{{ row.l | selectattr('a', 'no_such_test') | list }}", "No test named 'no_such_test'.", id="selectattr-test-name"),
        pytest.param("{{ row.l | rejectattr('a', 'no_such_test') | list }}", "No test named 'no_such_test'.", id="rejectattr-test-name"),
        pytest.param(
            "{{ row.q | truncate(2) }}", "truncate() arguments can never be satisfied: expected length >= 3, got 2", id="truncate-length"
        ),
        pytest.param(
            "{{ row.q | truncate(length=2) }}",
            "truncate() arguments can never be satisfied: expected length >= 3, got 2",
            id="truncate-length-keyword",
        ),
        pytest.param(
            "{{ row.q | truncate(4, row.k, '.....') }}",
            "truncate() arguments can never be satisfied: expected length >= 5, got 4",
            id="truncate-end-positional",
        ),
        pytest.param(
            "{{ row.q | truncate(end='abcd', length=3) }}",
            "truncate() arguments can never be satisfied: expected length >= 4, got 3",
            id="truncate-end-keyword",
        ),
        pytest.param(
            "{{ row.q | truncate(10, leeway=-1) }}",
            "truncate() arguments can never be satisfied: expected leeway >= 0, got -1",
            id="truncate-negative-leeway",
        ),
        pytest.param(
            "{% if row.q %}{{ row.q | truncate(-3) }}{% endif %}",
            "truncate() arguments can never be satisfied: expected length >= 3, got -3",
            id="truncate-inside-if",
        ),
        # Literals of the wrong type fail Jinja's truncate with a TypeError, not
        # an assertion; they are refused the same way (S3 fix round 1, F2).
        pytest.param(
            "{{ row.q | truncate(5, end=None) }}",
            "truncate() arguments can never be satisfied: object of type 'NoneType' has no len()",
            id="truncate-end-none",
        ),
        pytest.param("{{ row.q | truncate('abc') }}", "truncate() length must be an integer literal, got str.", id="truncate-length-str"),
        # A float length passes truncate's assertions and the empty-string
        # probe, then fails every row long enough to be sliced ("slice indices
        # must be integers") — P4 review r1 F2.
        pytest.param(
            "{{ row.q | truncate(10.5) }}", "truncate() length must be an integer literal, got float.", id="truncate-length-float"
        ),
        pytest.param(
            "{{ row.q | truncate(length=10.0) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-keyword",
        ),
        # The integer precondition reads length alone, so a row-derived sibling
        # argument does not excuse it; unary plus and bool literals are literals
        # too (P5 review r1 F1).
        pytest.param(
            "{{ row.q | truncate(10.5, end=row.e) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-beside-row-end",
        ),
        pytest.param(
            "{{ row.q | truncate(length=10.5, end=row.e) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-keyword-beside-row-end",
        ),
        pytest.param(
            "{{ row.q | truncate(10.5, leeway=row.n) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-beside-row-leeway",
        ),
        pytest.param(
            "{{ row.q | truncate(+10.5) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-unary-plus-float",
        ),
        pytest.param(
            "{{ row.q | truncate(+2) }}",
            "truncate() arguments can never be satisfied: expected length >= 3, got 2",
            id="truncate-length-unary-plus-short",
        ),
        pytest.param("{{ row.q | truncate(True) }}", "truncate() length must be an integer literal, got bool.", id="truncate-length-true"),
        pytest.param(
            "{{ row.q | truncate(False) }}", "truncate() length must be an integer literal, got bool.", id="truncate-length-false"
        ),
        pytest.param(
            "{{ row.q | truncate(10, end=True) }}",
            "truncate() arguments can never be satisfied: object of type 'bool' has no len()",
            id="truncate-end-bool",
        ),
        # Each precondition is decided over only the arguments it reads, so a
        # row-derived argument it does not read does not defer it
        # (review-F1-final-minors-r1 F1): ``length >= len(end)`` reads length
        # and end, ``leeway >= 0`` reads leeway.
        pytest.param(
            "{{ row.q | truncate(2, leeway=row.n) }}",
            "truncate() arguments can never be satisfied: expected length >= 3, got 2",
            id="truncate-short-length-beside-row-leeway",
        ),
        pytest.param(
            "{{ row.q | truncate(+2, leeway=row.n) }}",
            "truncate() arguments can never be satisfied: expected length >= 3, got 2",
            id="truncate-unary-plus-short-beside-row-leeway",
        ),
        pytest.param(
            "{{ row.q | truncate(-5, end=row.e) }}",
            "truncate() arguments can never be satisfied: expected length >= 0, got -5",
            id="truncate-negative-length-beside-row-end",
        ),
        pytest.param(
            "{{ row.q | truncate(20, end=True, leeway=row.n) }}",
            "truncate() arguments can never be satisfied: object of type 'bool' has no len()",
            id="truncate-end-bool-beside-row-leeway",
        ),
        pytest.param(
            "{{ row.q | truncate(row.n, end=5) }}",
            "truncate() arguments can never be satisfied: object of type 'int' has no len()",
            id="truncate-end-int-beside-row-length",
        ),
        pytest.param(
            "{{ row.q | truncate(row.n, leeway=-1) }}",
            "truncate() arguments can never be satisfied: expected leeway >= 0, got -1",
            id="truncate-negative-leeway-beside-row-length",
        ),
        pytest.param(
            "{{ row.q | truncate(10.5, *row.args) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-beside-a-spread",
        ),
        pytest.param(
            "{{ row.q | truncate(10.5, **row.kwargs) }}",
            "truncate() length must be an integer literal, got float.",
            id="truncate-length-float-beside-a-keyword-spread",
        ),
        # A call that cannot bind is a TypeError on every render.
        pytest.param(
            "{{ row.q | truncate(1, False, '', 0, 9) }}",
            "truncate() takes at most 4 arguments after the filtered value, got 5.",
            id="truncate-too-many-arguments",
        ),
        pytest.param(
            "{{ row.q | truncate(10.5, foo=1) }}", "truncate() got an unexpected keyword argument 'foo'.", id="truncate-unknown-keyword"
        ),
        pytest.param(
            "{{ row.q | truncate(10, length=5) }}",
            "truncate() got multiple values for argument 'length'.",
            id="truncate-argument-given-twice",
        ),
        # ``length + leeway`` overflows when an int literal is too large for a
        # float: an OverflowError on every render, not a construction crash.
        pytest.param(
            "{{ row.q | truncate(1" + "0" * 400 + ", leeway=0.5) }}",
            "truncate() arguments can never be satisfied: int too large to convert to float",
            id="truncate-overflowing-length",
        ),
        # A float literal too large for a float is infinity, which Jinja's code
        # generator writes as the bare name ``inf``: every render in an
        # expression raised NameError (P4 review r1 F2).
        pytest.param(
            "{{ row.q | truncate(1e400) }}",
            "A number literal in this template is too large for a float (it overflows to infinity).",
            id="truncate-infinite-length",
        ),
        pytest.param(
            "{{ row.q | truncate(10, leeway=1e400) }}",
            "A number literal in this template is too large for a float (it overflows to infinity).",
            id="truncate-infinite-leeway",
        ),
        pytest.param(
            "{% if row.n == -1e400 %}x{% endif %}",
            "A number literal in this template is too large for a float (it overflows to infinity).",
            id="infinite-literal-in-a-comparison",
        ),
        pytest.param(
            "{{ 1e400 }}",
            "A number literal in this template is too large for a float (it overflows to infinity).",
            id="infinite-literal-printed",
        ),
    ],
)
def test_a_template_its_own_literals_fail_is_refused_when_built(source: str, message: str) -> None:
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate

    with pytest.raises(TemplateSyntaxError) as caught:
        SandboxedTemplate(source)
    assert str(caught.value) == message


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("{{ row.q | truncate(row.n) }}", id="truncate-length-from-the-row"),
        pytest.param("{{ row.q | truncate(5, end=row.e) }}", id="truncate-end-from-the-row"),
        pytest.param("{{ row.q | truncate(3) }}", id="truncate-length-equal-to-end"),
        pytest.param("{{ row.q | truncate(10, leeway=0) }}", id="truncate-zero-leeway"),
        pytest.param("{{ row.q | truncate(10, leeway=0.5) }}", id="truncate-float-leeway"),
        pytest.param("{{ row.q | truncate(+10, end=row.e) }}", id="truncate-unary-plus-int-beside-row-end"),
        pytest.param("{{ row.n * 1e300 }}", id="finite-float-literal"),
        pytest.param("{{ row.q | truncate(*row.args) }}", id="truncate-splat"),
        # A row-derived argument a precondition reads stands in as the value
        # that satisfies it, so each of these can render (an ``end`` of ''
        # admits any length >= 0; a spread may supply ``end``).
        pytest.param("{{ row.q | truncate(0, end=row.e) }}", id="truncate-zero-length-beside-row-end"),
        pytest.param("{{ row.q | truncate(3, leeway=row.n) }}", id="truncate-length-equal-to-end-beside-row-leeway"),
        pytest.param("{{ row.q | truncate(2, **row.kwargs) }}", id="truncate-short-length-beside-a-keyword-spread"),
        pytest.param("{{ row.q | truncate(1, *row.args) }}", id="truncate-short-length-beside-a-spread"),
        pytest.param("{{ row.q | truncate(row.n, end='abc') }}", id="truncate-row-length-beside-literal-end"),
        pytest.param("{{ row.l | map(attribute='a') | list }}", id="map-attribute"),
        pytest.param("{{ row.l | map('upper') | list }}", id="map-known-filter"),
        pytest.param("{{ row.l | map(row.f) | list }}", id="map-filter-from-the-row"),
        pytest.param("{{ row.l | select | list }}", id="select-truthiness"),
        pytest.param("{{ row.l | select('odd') | list }}", id="select-known-test"),
        pytest.param("{{ row.l | selectattr('a') | list }}", id="selectattr-truthiness"),
        pytest.param("{% if row.q is defined %}{{ row.q | upper }}{% endif %}", id="known-names-in-if"),
    ],
)
def test_a_template_whose_failure_depends_on_the_row_is_built(source: str) -> None:
    from elspeth.plugins.infrastructure.templates import SandboxedTemplate

    SandboxedTemplate(source)


# ---------------------------------------------------------------------------
# The template row is a plain projection (elspeth-5887fb7928 S0). A template is
# operator-authored code, so it sees the row's field values and nothing else:
# never the PipelineRow, its SchemaContract or a FieldContract. Before this,
# ``row.contract.from_checkpoint(row.blob)`` raised AuditIntegrityError (a
# Tier-1 class) quoting row keys from inside a template, and ``row.to_dict()``
# / ``row.contract`` rendered framework objects into the prompt.
# ---------------------------------------------------------------------------

_ROW_TYPE = "elspeth.plugins.infrastructure.templates.TemplateRow object"


def _owned_api_row() -> object:
    from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

    fields = (
        FieldContract("amount_usd", "Amount USD", int, True, "declared"),
        FieldContract("q", "q", str, True, "declared"),
        FieldContract("blob", "blob", object, False, "declared"),
    )
    contract = SchemaContract(mode="FIXED", fields=fields, locked=True)
    return PipelineRow({"amount_usd": 5, "q": "hello", "blob": {_SENTINEL: 1, "data": _SENTINEL}}, contract)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("{{ row.contract }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'contract'", id="contract"),
        pytest.param(
            "{{ row.contract.from_checkpoint(row.blob) }}",
            f"Undefined variable: '{_ROW_TYPE}' has no attribute 'contract'",
            id="contract-from-checkpoint-tier1",
        ),
        pytest.param(
            "{{ row.from_checkpoint(row.blob, row.contract) }}",
            f"Undefined variable: '{_ROW_TYPE}' has no attribute 'from_checkpoint'",
            id="row-from-checkpoint",
        ),
        pytest.param("{{ row.to_dict() }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'to_dict'", id="to-dict"),
        pytest.param(
            "{{ row.to_checkpoint_format() }}",
            f"Undefined variable: '{_ROW_TYPE}' has no attribute 'to_checkpoint_format'",
            id="to-checkpoint-format",
        ),
        pytest.param("{{ row.name_index() }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'name_index'", id="name-index"),
        pytest.param("{{ row.keys() | list }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'keys'", id="keys-method"),
        pytest.param("{{ row.items() | list }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'items'", id="items-method"),
        pytest.param("{{ row['values'] }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'values'", id="item-no-method-fallback"),
        pytest.param("{{ row | attr('contract') }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'contract'", id="attr-filter"),
        pytest.param("{{ row | attr('keys') }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute 'keys'", id="attr-filter-method"),
        pytest.param("{{ row['__class__'] }}", f"Undefined variable: '{_ROW_TYPE}' has no attribute '__class__'", id="item-dunder"),
        pytest.param(
            "{{ row.__class__ }}", f"Sandbox violation: access to attribute '__class__' of {_ROW_TYPE} is unsafe", id="dunder-class"
        ),
        pytest.param(
            "{{ row.__class__.__mro__ }}",
            f"Sandbox violation: access to attribute '__class__' of {_ROW_TYPE} is unsafe",
            id="dunder-mro",
        ),
        pytest.param("{{ row._values }}", f"Sandbox violation: access to attribute '_values' of {_ROW_TYPE} is unsafe", id="private-slot"),
        pytest.param("{{ row._data }}", f"Sandbox violation: access to attribute '_data' of {_ROW_TYPE} is unsafe", id="private-name"),
    ],
)
def test_a_template_reaches_no_framework_api_on_the_row(source: str, expected: str) -> None:
    """Every owned surface is a routed, value-free template error — never a Tier-1 or a framework repr.

    The row is a real PipelineRow whose blob carries the sentinel as a key and a
    value; each render goes through the bounded worker.
    """
    assert _render_error(source, row=_whole(_owned_api_row())) == expected


def test_a_multi_query_source_row_reaches_no_framework_api() -> None:
    """A row nested in the context (multi-query ``row.source_row``) crosses as the same projection."""
    context_row = {"text": "hello", "source_row": _whole(_owned_api_row())}
    assert _render_error("{{ row.source_row.contract.from_checkpoint(row.source_row.blob) }}", row=context_row) == (
        f"Undefined variable: '{_ROW_TYPE}' has no attribute 'contract'"
    )
    assert create_sandboxed_environment().from_string("{{ row.source_row.q }}").render(row=context_row) == "hello"


def test_a_field_named_like_a_method_reads_the_field() -> None:
    """Dot, item and ``attr`` lookups resolve to fields: a column named ``keys`` is no longer shadowed by a method."""
    row = _whole(make_pipeline_row({"keys": "K", "contract": "C", "items": "I", "to_dict": "D", "values": "V"}))
    template = create_sandboxed_environment().from_string(
        "{{ row.keys }}|{{ row.contract }}|{{ row['items'] }}|{{ row.to_dict }}|{{ row | attr('values') }}"
    )
    assert template.render(row=row) == "K|C|I|D|V"


def test_the_template_row_keeps_every_documented_row_form() -> None:
    """What a template may do with ``row`` is unchanged: dual-name reads, ``get``, ``in``, iteration, filters."""
    template = create_sandboxed_environment().from_string(
        "{{ row.amount_usd }}|{{ row['Amount USD'] }}|{{ row.get('Amount USD') }}|{{ row.get('absent') is none }}|"
        "{{ 'Amount USD' in row }}|{{ 'absent' in row }}|{% for k in row %}{{ k }},{% endfor %}|{{ row | length }}|"
        "{{ row | list | join(',') }}|{{ row.blob.data == row['blob']['data'] }}|{{ row.q.upper() }}|"
        "{{ row | attr('q') }}"
    )
    assert template.render(row=_whole(_owned_api_row())) == (
        "5|5|5|True|True|False|amount_usd,q,blob,|3|amount_usd,q,blob|True|HELLO|hello"
    )


@pytest.mark.parametrize("source", ["{{ row | dictsort }}", "{{ row | items | list }}", "{{ dict(row) }}", "{{ '%(q)s' % row }}"])
def test_an_opted_out_whole_row_value_renders_field_values_never_framework_objects(source: str) -> None:
    """The row is a Mapping of field values: under the ``[]`` opt-out a whole-row form reads every field.

    A node that opts out gets exactly the row's values, never framework
    objects. With a declared list the same forms see only the declared fields
    (test_template_projection.py).
    """
    rendered = create_sandboxed_environment().from_string(source).render(row=_whole(_owned_api_row()))
    assert "hello" in rendered
    assert "Contract" not in rendered


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("{{ row[[1]] }}", f"Undefined variable: {_ROW_TYPE} has no element {_UNSPELLED}", id="unhashable-key"),
        pytest.param("{{ row[{}] }}", f"Undefined variable: {_ROW_TYPE} has no element {_UNSPELLED}", id="unhashable-dict-key"),
        pytest.param("{{ row[0] }}", f"Undefined variable: {_ROW_TYPE} has no element 0", id="int-key"),
    ],
)
def test_a_non_string_key_is_the_ordinary_undefined_field(source: str, expected: str) -> None:
    """Only a str names a field. Any other key is the value-free undefined error, never a TypeError from hashing it."""
    assert _render_error(source, row=_whole(_owned_api_row())) == expected


def test_row_values_render_exactly_as_the_frozen_pipeline_row_rendered_them() -> None:
    """The projection carries the PipelineRow's deep-frozen values, so nested containers print as before."""
    row = _whole(make_pipeline_row({"meta": {"a": 1}, "tags": [1, 2]}))
    template = create_sandboxed_environment().from_string("{{ row.meta }}|{{ row.tags }}|{{ row.meta.items() | list }}")
    assert template.render(row=row) == "{'a': 1}|(1, 2)|[('a', 1)]"


@pytest.mark.parametrize("owned", ["contract", "field"])
def test_a_contract_object_in_a_template_context_is_a_framework_bug(owned: str) -> None:
    """Only row values cross to the worker; a contract object in a context is the caller's bug, not a row fault."""
    from elspeth.contracts.errors import FrameworkBugError

    row = _owned_api_row()
    value = row.contract if owned == "contract" else row.contract.fields[0]
    with pytest.raises(FrameworkBugError) as caught:
        create_sandboxed_environment().from_string("{{ x }}").render(x=value)
    assert str(caught.value) == f"A template context carries a {type(value).__name__}: templates see row values only"


def _parity_rows() -> list[object]:
    from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

    fields = (
        FieldContract("amount_usd", "Amount USD", int, True, "declared"),
        FieldContract("note", "Note!", str, False, "declared", nullable=True),
    )
    rows = []
    for mode in ("FIXED", "FLEXIBLE", "OBSERVED"):
        contract = SchemaContract(mode=mode, fields=fields, locked=True)
        rows.append(PipelineRow({"amount_usd": 5}, contract))  # optional field absent from the data
        rows.append(PipelineRow({"amount_usd": 5, "note": "n"}, contract))
        # An extra key the contract does not name (FLEXIBLE/OBSERVED read it; FIXED does not),
        # and one spelled as an absent field's original name.
        rows.append(PipelineRow({"amount_usd": 5, "extra": "e", "Note!": "raw"}, contract))
    return rows


@pytest.mark.parametrize("row_index", range(9))
def test_the_projection_resolves_every_key_as_the_pipeline_row_does(row_index: int) -> None:
    """``TemplateRow`` and ``PipelineRow.__getitem__`` agree on every spelling, in every schema mode.

    The projection is built by the real transport (pack in the parent, restore in the worker).
    """
    pipeline_row = _parity_rows()[row_index]
    projected = template_infrastructure._restore_context_value(template_infrastructure._pack_context_value(_whole(pipeline_row)))
    assert type(projected) is template_infrastructure.TemplateRow

    for key in ("amount_usd", "Amount USD", "note", "Note!", "extra", "absent"):
        try:
            expected: object = pipeline_row[key]
        except KeyError:
            expected = KeyError
        try:
            actual: object = projected[key]
        except KeyError:
            actual = KeyError
        assert actual == expected, key
        assert (key in projected) is (expected is not KeyError), key
        assert projected.get(key) == pipeline_row.get(key), key
    assert list(projected) == list(pipeline_row)
    assert len(projected) == len(pipeline_row.keys())


def test_the_template_row_is_immutable() -> None:
    projected = template_infrastructure._restore_context_value(template_infrastructure._pack_context_value(_whole(_owned_api_row())))
    with pytest.raises(TypeError, match="immutable"):
        projected.q = "changed"
    with pytest.raises(TypeError, match="immutable"):
        del projected.q
    with pytest.raises(TypeError):
        operator.setitem(projected, "q", "changed")
