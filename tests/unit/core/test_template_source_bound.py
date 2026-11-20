"""The template source bound: 64 KiB, counted in UTF-8 bytes, with the other bounds still standing.

The expected figures and texts are spelled here, not imported, so the file
collects on the tree before the change (where the bound was 16 KiB).
"""

from __future__ import annotations

import pytest

from elspeth.core import templates as core_templates
from elspeth.plugins.infrastructure.templates import TemplateError, create_sandboxed_environment

LIMIT = 64 * 1024
OLD_LIMIT = 16 * 1024


def _prompt(size: int) -> str:
    """Honest instruction text reading one row field, exactly ``size`` UTF-8 bytes."""
    head = "Summarise this customer review: {{ row.review }}. "
    return head + "x" * (size - len(head))


def test_the_bound_is_64_kib() -> None:
    assert core_templates.MAX_JINJA_TEMPLATE_BYTES == LIMIT


@pytest.mark.parametrize(
    "size",
    [
        pytest.param(20_000, id="twenty-kb"),
        pytest.param(OLD_LIMIT + 1, id="old-bound-plus-one"),
        pytest.param(LIMIT, id="exactly-the-bound"),
    ],
)
def test_templates_up_to_the_bound_parse_compile_and_are_analysed(size: int) -> None:
    text = _prompt(size)
    assert len(text.encode("utf-8")) == size

    core_templates.validate_jinja_source(text)
    create_sandboxed_environment().from_string(text)
    assert core_templates.extract_jinja2_field_usage(text).fields == frozenset({"review"})


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("a" * (LIMIT + 1), id="ascii"),
        # Far under the bound in characters, one byte over it in UTF-8.
        pytest.param("é" * (LIMIT // 2) + "a", id="multibyte-under-in-characters"),
    ],
)
def test_one_byte_over_the_bound_is_refused_with_its_figures(text: str) -> None:
    assert len(text.encode("utf-8")) == LIMIT + 1

    with pytest.raises(ValueError, match=f"^Template exceeds {LIMIT} UTF-8 bytes$") as core_refusal:
        core_templates.validate_jinja_source(text)
    with pytest.raises(TemplateError, match=f"^Template exceeds {LIMIT} UTF-8 bytes$") as sandbox_refusal:
        create_sandboxed_environment().parse(text)

    from elspeth.core.templates import TemplateSourceTooLargeError
    from elspeth.plugins.infrastructure.templates import TemplateSourceTooLarge

    core_typed, sandbox_typed = core_refusal.value, sandbox_refusal.value
    assert isinstance(core_typed, TemplateSourceTooLargeError)
    assert isinstance(sandbox_typed, TemplateSourceTooLarge)
    assert (core_typed.size_bytes, core_typed.limit_bytes) == (len(text.encode("utf-8")), LIMIT)
    assert (sandbox_typed.size_bytes, sandbox_typed.limit_bytes) == (len(text.encode("utf-8")), LIMIT)


def test_multibyte_text_exactly_at_the_bound_in_bytes_is_admitted() -> None:
    text = "é" * (LIMIT // 2)

    core_templates.validate_jinja_source(text)
    create_sandboxed_environment().from_string(text)


def test_the_refusal_sentence_names_position_size_and_limit() -> None:
    assert core_templates.template_size_refusal("queries.tone.template", LIMIT + 9, LIMIT) == (
        f"queries.tone.template is {LIMIT + 9} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    )


def _padded(shape: str) -> str:
    """The shape followed by plain text, exactly at the bound."""
    padding = LIMIT - len(shape.encode("utf-8"))
    assert padding >= 0
    return shape + "p" * padding


@pytest.mark.parametrize(
    ("shape", "refusal"),
    [
        pytest.param("{{ " + "1 + " * 200 + "1 }}", "Template AST nesting exceeds 64 levels", id="arithmetic-chain-depth"),
        pytest.param("{{ [" + "1, " * 3000 + "1] }}", "Template AST exceeds 2048 nodes", id="wide-list-node-count"),
        pytest.param("{{ " + "not " * 1500 + "true }}", "Template expression nesting exceeds the parser limit", id="unary-chain-parser"),
        pytest.param("{{ row.review }}" * 257, "Template has too many Jinja expressions or blocks", id="expression-count"),
        pytest.param("{{ " + "(" * 65 + "1" + ")" * 65 + " }}", "Template expression nesting exceeds 64", id="bracket-nesting"),
    ],
)
def test_hostile_shapes_padded_to_the_bound_are_still_refused(shape: str, refusal: str) -> None:
    text = _padded(shape)
    assert len(text.encode("utf-8")) == LIMIT

    with pytest.raises(TemplateError, match=refusal):
        create_sandboxed_environment().from_string(text)
