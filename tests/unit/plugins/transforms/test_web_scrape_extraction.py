"""Tests for web scrape content extraction utilities."""

import html2text
import pytest
from hypothesis import given
from hypothesis.strategies import characters, text

from elspeth.contracts.plugin_semantics import TextFraming
from elspeth.plugins.transforms.web_scrape import _build_web_scrape_output_semantics
from elspeth.plugins.transforms.web_scrape_extraction import extract_content


def test_extract_content_markdown():
    """HTML should convert to markdown."""
    html = "<html><body><h1>Title</h1><p>Content here</p></body></html>"

    result = extract_content(html, format="markdown")

    assert "# Title" in result
    assert "Content here" in result


def test_extract_content_text():
    """HTML should convert to plain text."""
    html = "<html><body><h1>Title</h1><p>Content</p></body></html>"

    result = extract_content(html, format="text")

    assert result == "Title Content"
    assert "Title" in result
    assert "Content" in result
    assert "<h1>" not in result  # No HTML tags


def test_extract_content_text_uses_configured_separator():
    """Text extraction can preserve DOM text boundaries for line-oriented flows."""
    html = "<html><body><h1>Title</h1><p>Content</p><ul><li>One</li><li>Two</li></ul></body></html>"

    result = extract_content(html, format="text", text_separator="\n")

    assert result == "Title\nContent\nOne\nTwo"


def test_extract_content_raw():
    """Raw format should return HTML unchanged."""
    html = "<html><body><h1>Test</h1></body></html>"

    result = extract_content(html, format="raw")

    assert result == html


def test_extract_content_strips_configured_elements():
    """Should remove configured HTML elements."""
    html = """
    <html>
        <head><script>alert('bad')</script></head>
        <body>
            <nav>Navigation</nav>
            <main><p>Content</p></main>
            <footer>Footer</footer>
        </body>
    </html>
    """

    result = extract_content(
        html,
        format="text",
        strip_elements=["script", "nav", "footer"],
    )

    assert "Content" in result
    assert "Navigation" not in result
    assert "Footer" not in result
    assert "alert" not in result


def test_extract_content_unknown_format_raises():
    """Unknown format should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown format"):
        extract_content("<html><body>test</body></html>", format="invalid")


def test_extract_content_none_input_raises_valueerror():
    """Tier 3 boundary: None input is caught inside extract_content and raised as ValueError.

    BeautifulSoup raises TypeError on None input. extract_content wraps
    this at the Tier 3 boundary, converting it to ValueError so callers
    only need to catch the documented exception contract.
    """
    import pytest

    with pytest.raises(ValueError, match="malformed content"):
        extract_content(None, format="markdown")


def test_html2text_deterministic_simple():
    """html2text must produce identical output for identical input."""
    html = "<html><body><h1>Title</h1><p>Content</p></body></html>"

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0

    result1 = h.handle(html)
    result2 = h.handle(html)

    assert result1 == result2, "html2text output is non-deterministic!"


@given(text(alphabet=characters(exclude_categories=("Cc", "Cs")), min_size=10, max_size=200))
def test_html2text_deterministic_property(content: str):
    """Property test: html2text must be deterministic for printable inputs.

    Excludes control characters (Cc) and surrogates (Cs) — html2text has
    known non-determinism with control chars like \\x1f and \\r that interact
    with its internal whitespace normalization state.  Real HTML content
    does not contain these characters.
    """
    # Wrap content in minimal HTML structure
    html = f"<html><body><p>{content}</p></body></html>"

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0

    result1 = h.handle(html)
    result2 = h.handle(html)

    assert result1 == result2, f"Non-deterministic for input: {html!r}"


def test_html2text_deterministic_across_instances():
    """Verify determinism even with separate HTML2Text instances."""
    html = "<html><body><h1>Test</h1><p>Content</p></body></html>"

    h1 = html2text.HTML2Text()
    h1.ignore_links = False
    h1.body_width = 0

    h2 = html2text.HTML2Text()
    h2.ignore_links = False
    h2.body_width = 0

    result1 = h1.handle(html)
    result2 = h2.handle(html)

    assert result1 == result2, "html2text not deterministic across instances!"


@given(text(alphabet=characters(blacklist_characters="<>&"), min_size=1, max_size=200))
def test_compact_declaration_is_true_of_what_extraction_actually_emits(content: str):
    """The COMPACT claim must be TRUE, not merely intended.

    web_scrape DECLARES ``TextFraming.COMPACT`` whenever ``text_separator``
    carries no CR/LF, and since ADR-039 ``sink:text`` READS that declaration and
    grades the edge SATISFIED at build time — then diverts any row containing CR
    or LF at runtime. So a COMPACT declaration that is not literally true
    reproduces elspeth-afdf55a17c's own zero-byte outcome with a green
    certification on top, which is strictly worse than the abstention it
    replaced.

    The gap this closes: ``strip=True`` trims only the EDGES of each DOM text
    node, so a record separator INSIDE one node survived — and pretty-printed
    HTML puts them there routinely.

    This binds the two halves together, driving the REAL declaration builder and
    the REAL extractor from one config. Pinning that the declaration merely
    exists, or that the composer edge grades SATISFIED, would not catch a lie.
    """
    html = f"<html><body><p>{content}</p><div>More content</div></body></html>"

    for separator in (" ", "\t", " | ", "\r", "\n", "\r\n"):
        declared = _build_web_scrape_output_semantics(
            content_field="content", format="text", text_separator=separator
        ).fields[0]
        emitted = extract_content(html, format="text", text_separator=separator)

        if declared.text_framing is TextFraming.COMPACT:
            assert "\r" not in emitted and "\n" not in emitted, (
                f"COMPACT declared for separator {separator!r}, but extraction emitted a "
                f"record separator sink:text would divert on: {emitted!r}"
            )
