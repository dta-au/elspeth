"""Content extraction utilities for web scraping.

Converts HTML to markdown, text, or raw format with configurable
element stripping.
"""

import html2text
import soupsieve
from bs4 import BeautifulSoup
from bs4.element import AttributeValueList
from pydantic import BaseModel, Field, field_validator, model_validator


def _validate_css_selector(value: str) -> str:
    if not value.strip():
        raise ValueError("CSS selector must not be empty")
    try:
        soupsieve.compile(value)
    except soupsieve.SelectorSyntaxError as exc:
        raise ValueError("invalid CSS selector") from exc
    return value


class CSSRecordColumn(BaseModel):
    """One field extracted from each selected HTML record."""

    model_config = {"extra": "forbid"}

    field: str = Field(min_length=1, max_length=128)
    selector: str | None = Field(default=None, max_length=512)
    attribute: str | None = Field(default=None, max_length=128)
    required: bool = False

    @field_validator("field", "attribute")
    @classmethod
    def _validate_names(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip() or "\r" in value or "\n" in value):
            raise ValueError("field and attribute names must be nonempty and contain no surrounding whitespace or line breaks")
        return value

    @field_validator("selector")
    @classmethod
    def _validate_selector(cls, value: str | None) -> str | None:
        return _validate_css_selector(value) if value is not None else None


class CSSRecordsConfig(BaseModel):
    """Bounded list of CSS-selected records emitted as one row field."""

    model_config = {"extra": "forbid"}

    field: str = Field(min_length=1, max_length=128)
    selector: str = Field(min_length=1, max_length=512)
    columns: list[CSSRecordColumn] = Field(min_length=1, max_length=32)
    max_records: int = Field(default=200, ge=1, le=1000)
    max_value_chars: int = Field(default=4096, ge=1, le=65536)
    max_output_chars: int = Field(default=100000, ge=1, le=1000000)

    @field_validator("field")
    @classmethod
    def _validate_field_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("records field must be nonempty and contain no surrounding whitespace or line breaks")
        return value

    @field_validator("selector")
    @classmethod
    def _validate_selector(cls, value: str) -> str:
        return _validate_css_selector(value)

    @model_validator(mode="after")
    def _unique_columns(self) -> "CSSRecordsConfig":
        names = [column.field for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("record column field names must be unique")
        return self


def extract_css_records(html: str, config: CSSRecordsConfig, strip_elements: list[str]) -> list[dict[str, str | None]]:
    """Extract a bounded candidate set from untrusted HTML without truncation."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag_name in strip_elements:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        selected = soup.select(config.selector, limit=config.max_records + 1)
        if len(selected) > config.max_records:
            raise ValueError(f"record count exceeds max_records {config.max_records}")

        output: list[dict[str, str | None]] = []
        output_chars = 0
        for element in selected:
            record: dict[str, str | None] = {}
            for column in config.columns:
                target = element.select_one(column.selector) if column.selector is not None else element
                value: str | None = None
                if target is not None:
                    if column.attribute is None:
                        value = " ".join(target.stripped_strings)
                    elif column.attribute in target.attrs:
                        attribute_value = target.attrs[column.attribute]
                        if type(attribute_value) is str:
                            value = attribute_value
                        elif type(attribute_value) is AttributeValueList and all(type(part) is str for part in attribute_value):
                            value = " ".join(attribute_value)
                        else:
                            raise ValueError(f"record attribute {column.attribute!r} has an unsupported value")
                if column.required and not value:
                    raise ValueError(f"required record column {column.field!r} is missing")
                if value is not None:
                    if len(value) > config.max_value_chars:
                        raise ValueError(f"record column {column.field!r} exceeds max_value_chars")
                    output_chars += len(value)
                    if output_chars > config.max_output_chars:
                        raise ValueError("record output exceeds max_output_chars")
                record[column.field] = value
            output.append(record)
        return output
    except ValueError:
        raise
    except (AttributeError, TypeError) as exc:
        raise ValueError("HTML record extraction failed on malformed content") from exc


def extract_content(
    html: str,
    format: str,
    strip_elements: list[str] | None = None,
    text_separator: str = " ",
) -> str:
    """Extract content from HTML in specified format.

    This is a Tier 3 trust boundary: ``html`` is external data and
    third-party libraries (BeautifulSoup, html2text) may raise
    ``AttributeError`` or ``TypeError`` on pathological input.  These
    are caught here and re-raised as ``ValueError`` so callers only
    need to handle the documented exception contract.

    Args:
        html: Raw HTML content (Tier 3 — untrusted)
        format: Output format ("markdown", "text", "raw")
        strip_elements: HTML tags to remove before extraction
        text_separator: Separator used between DOM text nodes for text output

    Returns:
        Extracted content as string

    Raises:
        ValueError: If format is invalid, or if HTML parsing/extraction
            fails due to malformed external content.
    """
    if format == "raw":
        return html

    try:
        # Parse HTML and strip unwanted elements
        soup = BeautifulSoup(html, "html.parser")

        if strip_elements:
            for tag_name in strip_elements:
                for tag in soup.find_all(tag_name):
                    tag.decompose()

        # Extract based on format
        if format == "markdown":
            # Get cleaned HTML back from soup
            cleaned_html = str(soup)

            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = False
            h.body_width = 0  # Don't wrap lines
            h.ignore_tables = False
            h.ignore_emphasis = False

            return h.handle(cleaned_html)

        elif format == "text":
            text = soup.get_text(separator=text_separator, strip=True)
            if "\n" not in text_separator and "\r" not in text_separator:
                # ``strip=True`` trims only the EDGES of each DOM text node, so a
                # record separator INSIDE one node survives — and pretty-printed
                # HTML puts them there routinely. A caller who chose a separator
                # with no CR/LF asked for a single-line join, and web_scrape
                # DECLARES ``TextFraming.COMPACT`` for exactly this case. Leaving
                # the newline in makes that declaration false at its source, which
                # a downstream ``sink:text`` then grades SATISFIED before diverting
                # the row at runtime (elspeth-afdf55a17c's own failure mode, with a
                # green certification on top). Normalise on the sink's exact rule —
                # CR or LF, not every Unicode line boundary — so the claim is true.
                segments = (segment.strip() for segment in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
                text = text_separator.join(segment for segment in segments if segment)
            return text

        else:
            raise ValueError(f"Unknown format: {format}")
    except ValueError:
        raise
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"HTML extraction failed on malformed content: {exc}") from exc
