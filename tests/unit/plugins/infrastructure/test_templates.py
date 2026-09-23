"""Tests for shared template infrastructure."""

import pickle
import threading
from collections import namedtuple
from types import MappingProxyType

import pytest
from jinja2 import TemplateSyntaxError

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


def test_saturated_template_workers_refuse_after_bounded_queue_wait(monkeypatch: pytest.MonkeyPatch):
    slot = threading.BoundedSemaphore(1)
    monkeypatch.setattr(template_infrastructure, "_WORKER_SLOTS", slot)
    monkeypatch.setattr(template_infrastructure, "_WORKER_QUEUE_TIMEOUT_SECONDS", 0.01)
    template = create_sandboxed_environment().from_string("{{ row.text }}")

    def packing_must_not_run(_context: object) -> object:
        raise AssertionError("saturated render packed its context")

    monkeypatch.setattr(template_infrastructure, "_pack_context_value", packing_must_not_run)
    with slot, pytest.raises(TemplateError, match="Too many concurrent template workers"):
        template.render(row={"text": "hello"})


def test_context_transport_rejects_oversized_mapping_key_before_serialization():
    with pytest.raises(TemplateError, match="parent packing limit"):
        template_infrastructure._pack_context_value({"x" * (9 * 1024 * 1024): "small"})


def test_context_transport_rejects_oversized_tuple_subclass_before_serialization():
    named_values = namedtuple("NamedValues", "text")
    with pytest.raises(TemplateError, match="parent packing limit"):
        template_infrastructure._pack_context_value({"value": named_values("x" * (9 * 1024 * 1024))})


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

    def record_worker(source: str, payload: bytes) -> str:
        calls.append(source)
        return original(source, payload)

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


def test_render_output_is_bounded():
    env = create_sandboxed_environment()
    template = env.from_string("{{ row.text * 5000000 }}")
    with pytest.raises(TemplateError, match="Rendered template exceeds"):
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
