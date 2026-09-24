"""Bounds on template-name discovery before Composer admits authored text."""

from elspeth.web.composer.state import _parse_template_names


def test_template_name_advisory_rejects_unbounded_compiler_expressions() -> None:
    for source in (
        "{{ (3**(3**16)) % 7 }}{{ row.name }}",
        "{% autoescape ('x' * 1000000000)|length > 0 %}{{ row.name }}{% endautoescape %}",
    ):
        parsed, error = _parse_template_names(source)
        assert parsed is None
        assert error is not None
