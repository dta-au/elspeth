"""An integer literal longer than Python reads from text is refused in a typed way, never with a bare ValueError.

Jinja's lexer reads an integer literal with ``int()``, and CPython refuses more
than 4300 digits from text with a builtin ``ValueError``. A 5,000-digit literal
is 5 KB, inside every source bound, so before the change it escaped the field
analysis (authoring validation, the advisor's summary) and the sandbox's
compile bare. The catches are exact: a ValueError SUBCLASS raised during the
parse (an owned refusal, a Unicode error) is another cause and passes unchanged.

A hex, octal or binary literal is read whatever its length (Python does not
limit power-of-two bases), but Jinja's code generator writes every constant
with ``repr()``, which refuses a value of more than 4300 decimal digits. Both
AST walks (the sandbox's and the analysis's) refuse that value at its cause,
decided from the int without writing it as text. The cases sit exactly on the
bound: ``10**4300`` (4301 digits) and ``10**4300 - 1`` (4300 digits) have the
same bit length, so a bit-length test alone could not tell them apart.

Inputs are generated; expected texts are spelled here and new types are
imported inside the tests, after the call, so the file collects on the tree
before the change.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest

from elspeth.core import templates
from elspeth.plugins.infrastructure.templates import TemplateError, create_sandboxed_environment

LIMIT_DIGITS = sys.get_int_max_str_digits()
LONG = "Rate {{ row.review }}. {{ " + "9" * (LIMIT_DIGITS + 700) + " }}"
AT_LIMIT = "Rate {{ row.review }}. {{ " + "9" * LIMIT_DIGITS + " }}"

SMALLEST_TOO_LONG = 10**LIMIT_DIGITS
BASES = {"hex": hex, "octal": oct, "binary": bin}


def _literal(base: str, value: int) -> str:
    return "Rate {{ row.review }}. {{ " + BASES[base](value) + " }}"


ANALYSES: dict[str, Callable[[str], object]] = {
    "field-usage": templates.extract_jinja2_field_usage,
    "fields": templates.extract_jinja2_fields,
    "context-fields": lambda text: templates.extract_jinja2_context_fields(text, row_attribute="source_row"),
    "loads-row": lambda text: templates.template_loads_name(text, "row"),
}


@pytest.fixture(autouse=True)
def _clear_analysis_caches() -> None:
    templates._cached_jinja2_field_usage.cache_clear()
    templates._cached_jinja2_context_fields.cache_clear()


def test_a_literal_at_pythons_digit_limit_compiles_and_is_analysed() -> None:
    assert LIMIT_DIGITS == 4300, "CPython's default; the inputs below are sized from it"
    create_sandboxed_environment().from_string(AT_LIMIT)
    for analysis in ANALYSES.values():
        analysis(AT_LIMIT)


def test_the_sandbox_refuses_a_longer_literal_as_a_template_error() -> None:
    with pytest.raises(TemplateError) as raised:
        create_sandboxed_environment().from_string(LONG)

    from elspeth.plugins.infrastructure.templates import TemplateNumberLiteralTooLong

    assert type(raised.value) is TemplateNumberLiteralTooLong
    assert str(raised.value) == "Template contains a number literal too long to read"
    assert type(raised.value.__cause__) is ValueError


@pytest.mark.parametrize("analysis", ANALYSES.values(), ids=ANALYSES.keys())
def test_the_analysis_refuses_a_longer_literal_as_an_owned_source_refusal(analysis: Callable[[str], object]) -> None:
    with pytest.raises(ValueError) as raised:
        analysis(LONG)

    from elspeth.core.templates import TemplateNumberLiteralTooLongError, TemplateSourceRefusedError

    assert type(raised.value) is TemplateNumberLiteralTooLongError
    assert isinstance(raised.value, TemplateSourceRefusedError)
    assert str(raised.value) == "Template contains a number literal too long to read"


@pytest.mark.parametrize(
    "raised_inside_the_parse",
    [
        pytest.param(lambda: templates.TemplateSourceRefusedError("an owned refusal"), id="owned-refusal"),
        pytest.param(lambda: UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), id="unicode-error"),
    ],
)
def test_a_value_error_subclass_raised_during_the_parse_passes_unchanged(
    monkeypatch: pytest.MonkeyPatch, raised_inside_the_parse: Callable[[], ValueError]
) -> None:
    error = raised_inside_the_parse()

    def refuse(*_args: object) -> object:
        raise error

    for environment in (templates._create_field_extraction_environment(), create_sandboxed_environment()):
        # Jinja's parse() calls the instance's _parse; only that call is wrapped by the catch.
        monkeypatch.setattr(environment, "_parse", refuse)
        with pytest.raises(ValueError) as raised:
            environment.parse("Rate {{ row.review }}.")
        assert raised.value is error


@pytest.mark.parametrize("base", BASES)
def test_a_power_of_two_base_literal_just_under_the_bound_compiles_and_is_analysed(base: str) -> None:
    text = _literal(base, SMALLEST_TOO_LONG - 1)

    create_sandboxed_environment().from_string(text)
    for analysis in ANALYSES.values():
        analysis(text)


@pytest.mark.parametrize("base", BASES)
def test_a_power_of_two_base_literal_just_over_the_bound_is_refused_by_the_sandbox(base: str) -> None:
    with pytest.raises(TemplateError) as raised:
        create_sandboxed_environment().from_string(_literal(base, SMALLEST_TOO_LONG))

    assert str(raised.value) == "Template contains a number literal too long to read"
    from elspeth.plugins.infrastructure.templates import TemplateNumberLiteralTooLong

    assert type(raised.value) is TemplateNumberLiteralTooLong


@pytest.mark.parametrize("analysis", ANALYSES.values(), ids=ANALYSES.keys())
@pytest.mark.parametrize("base", BASES)
def test_a_power_of_two_base_literal_just_over_the_bound_is_refused_by_the_analysis(base: str, analysis: Callable[[str], object]) -> None:
    with pytest.raises(ValueError) as raised:
        analysis(_literal(base, SMALLEST_TOO_LONG))

    from elspeth.core.templates import TemplateNumberLiteralTooLongError

    assert type(raised.value) is TemplateNumberLiteralTooLongError


def test_with_pythons_limit_disabled_nothing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = sys.get_int_max_str_digits()
    monkeypatch.setattr(sys, "get_int_max_str_digits", lambda: 0)
    from elspeth.core.templates import int_literal_too_long_to_render

    assert int_literal_too_long_to_render(10 ** (previous + 1)) is False
