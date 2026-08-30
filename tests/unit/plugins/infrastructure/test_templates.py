"""Tests for shared template infrastructure."""

import pytest
from jinja2 import TemplateSyntaxError

from elspeth.plugins.infrastructure.templates import (
    TemplateError,
    create_sandboxed_environment,
)


def test_create_sandboxed_environment_returns_immutable_sandbox():
    env = create_sandboxed_environment()
    template = env.from_string("Hello {{ name }}")
    result = template.render(name="world")
    assert result == "Hello world"


def test_sandboxed_environment_strict_undefined():
    env = create_sandboxed_environment()
    template = env.from_string("{{ missing }}")
    with pytest.raises(Exception, match="missing"):
        template.render()


def test_sandboxed_environment_rejects_invalid_syntax():
    env = create_sandboxed_environment()
    with pytest.raises(TemplateSyntaxError):
        env.from_string("{% if unclosed")


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
