"""Jinja block preflight must agree with the context used during rendering."""

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.templates import (
    SandboxedTemplate,
    TemplateError,
    create_sandboxed_environment,
    find_runtime_unbound_variables,
)
from elspeth.plugins.sources.llm.config import LLMSourceConfig


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("{% for row in [1] %}{% block b %}{{ row }}{% endblock %}{% endfor %}", id="loop-target"),
        pytest.param(
            "{% for item in [1] %}{% set row = item %}{% block b %}{{ row }}{% endblock %}{% endfor %}",
            id="loop-assignment",
        ),
        pytest.param("{% with row = 1 %}{% block b %}{{ row }}{% endblock %}{% endwith %}", id="with"),
        pytest.param(
            "{% macro m(row) %}{% block b %}{{ row }}{% endblock %}{% endmacro %}{{ m(1) }}",
            id="macro-argument",
        ),
        pytest.param(
            "{% block outer %}{% set row = 1 %}{% block b %}{{ row }}{% endblock %}{% endblock %}",
            id="outer-block-assignment",
        ),
        pytest.param(
            "{% filter upper %}{% set row = 1 %}{% block b %}{{ row }}{% endblock %}{% endfilter %}",
            id="filter-assignment",
        ),
        pytest.param(
            "{% autoescape true %}{% set row = 1 %}{% block b %}{{ row }}{% endblock %}{% endautoescape %}",
            id="autoescape-assignment",
        ),
        pytest.param(
            "{% set output %}{% set row = 1 %}{% block b %}{{ row }}{% endblock %}{% endset %}{{ output }}",
            id="assignment-block",
        ),
        pytest.param(
            "{% macro m() %}{{ caller(1) }}{% endmacro %}{% call(row) m() %}{% block b %}{{ row }}{% endblock %}{% endcall %}",
            id="call-block-argument",
        ),
    ],
)
def test_unscoped_block_cannot_read_enclosing_locals(source: str) -> None:
    with pytest.raises(TemplateError, match="Undefined variable: 'row' is undefined"):
        SandboxedTemplate(source).render()

    assert find_runtime_unbound_variables(create_sandboxed_environment().parse(source)) == frozenset({"row"})


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("{% set row = 1 %}{% block b %}{{ row }}{% endblock %}", "1", id="root-assignment"),
        pytest.param(
            "{% if true %}{% set row = 1 %}{% else %}{% set row = 2 %}{% endif %}{% block b %}{{ row }}{% endblock %}",
            "1",
            id="root-conditional-assignment",
        ),
        pytest.param("{% for row in [1] %}{% block b scoped %}{{ row }}{% endblock %}{% endfor %}", "1", id="scoped-loop"),
        pytest.param(
            "{% for row in [1] %}{% block outer scoped %}{% block b %}{{ row }}{% endblock %}{% endblock %}{% endfor %}",
            "1",
            id="scoped-outer-context",
        ),
        pytest.param(
            "{% set row = 2 %}{% with row = 1 %}{% block b %}{{ row }}{% endblock %}{% endwith %}",
            "2",
            id="root-binding-survives-local-shadow",
        ),
        pytest.param(
            "{% block outer %}{% set row = 1 %}{% block b scoped %}{{ row }}{% endblock %}{% endblock %}",
            "1",
            id="scoped-block-local",
        ),
    ],
)
def test_block_accepts_bindings_present_in_its_render_context(source: str, expected: str) -> None:
    assert find_runtime_unbound_variables(create_sandboxed_environment().parse(source)) == frozenset()
    assert SandboxedTemplate(source).render() == expected


def test_llm_source_rejects_loop_local_in_unscoped_block() -> None:
    with pytest.raises(PluginConfigError, match="unavailable name\\(s\\): row"):
        LLMSourceConfig.from_dict(
            {
                "provider": "openrouter",
                "prompt_template": "{% for row in [1] %}{% block b %}{{ row }}{% endblock %}{% endfor %}",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            },
            plugin_name="llm",
        )
