"""Content extraction utilities for web scraping.

Converts HTML to markdown, text, or raw format with configurable
element stripping.
"""

import json
from dataclasses import asdict, dataclass
from typing import Literal

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
    multiple: Literal["first", "one", "all"] = "first"
    max_values: int = Field(default=16, ge=1, le=256)

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
    provenance_field: str | None = Field(default=None, min_length=1, max_length=128)
    selector: str = Field(min_length=1, max_length=512)
    columns: list[CSSRecordColumn] = Field(min_length=1, max_length=32)
    max_records: int = Field(default=200, ge=1, le=1000)
    max_value_chars: int = Field(default=4096, ge=1, le=65536)
    max_total_values: int = Field(default=10000, ge=1, le=100000)
    max_output_chars: int = Field(default=100000, ge=1, le=1000000)

    @field_validator("field")
    @classmethod
    def _validate_field_name(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or "\r" in value or "\n" in value:
            raise ValueError("records field must be nonempty and contain no surrounding whitespace or line breaks")
        return value

    @field_validator("provenance_field")
    @classmethod
    def _validate_provenance_field_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip() or "\r" in value or "\n" in value):
            raise ValueError("provenance_field must be nonempty and contain no surrounding whitespace or line breaks")
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
        if self.provenance_field == self.field:
            raise ValueError("provenance_field must differ from records field")
        return self


@dataclass(frozen=True)
class CSSFieldProvenance:
    """Selector evidence for one extracted field; source_url must be persistence-safe."""

    source_url: str
    record_selector: str
    selector: str | None
    attribute: str | None
    match_policy: Literal["first", "one", "all"]
    selected_count: int


@dataclass(frozen=True)
class CSSRecordExtraction:
    """Structured values and aligned per-field provenance for untrusted HTML."""

    records: list[dict[str, str | list[str] | None]]
    provenance: list[dict[str, CSSFieldProvenance]]


def _extract_css_records(
    html: str,
    config: CSSRecordsConfig,
    strip_elements: list[str],
    *,
    source_url: str | None,
) -> CSSRecordExtraction:
    if source_url is not None and (not source_url or len(source_url) > 2048):
        raise ValueError("source_url must be nonempty and at most 2048 characters")
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag_name in strip_elements:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        selected = soup.select(config.selector, limit=config.max_records + 1)
        if len(selected) > config.max_records:
            raise ValueError(f"record count exceeds max_records {config.max_records}")

        output: list[dict[str, str | list[str] | None]] = []
        provenance: list[dict[str, CSSFieldProvenance]] = []
        output_chars = 0
        serialized_chars = 2 + (32 if source_url is not None else 0)  # Lists and optional wrapper field names.
        total_values = 0
        for element in selected:
            record: dict[str, str | list[str] | None] = {}
            record_provenance: dict[str, CSSFieldProvenance] = {}
            for column in config.columns:
                if column.selector is None:
                    targets = [element]
                else:
                    limit = column.max_values + 1 if column.multiple == "all" else 2 if column.multiple == "one" else 1
                    targets = element.select(column.selector, limit=limit)
                if column.multiple == "one" and len(targets) > 1:
                    raise ValueError(f"record column {column.field!r} has multiple matches")
                if column.multiple == "all" and len(targets) > column.max_values:
                    raise ValueError(f"record column {column.field!r} exceeds max_values")
                total_values += len(targets)
                if total_values > config.max_total_values:
                    raise ValueError("record extraction exceeds max_total_values")

                values: list[str] = []
                for target in targets:
                    if column.attribute is None:
                        values.append(" ".join(target.stripped_strings))
                    elif column.attribute in target.attrs:
                        attribute_value = target.attrs[column.attribute]
                        if type(attribute_value) is str:
                            values.append(attribute_value)
                        elif type(attribute_value) is AttributeValueList and all(type(part) is str for part in attribute_value):
                            values.append(" ".join(attribute_value))
                        else:
                            raise ValueError(f"record attribute {column.attribute!r} has an unsupported value")
                if column.required and not any(values):
                    raise ValueError(f"required record column {column.field!r} is missing")
                for value in values:
                    if len(value) > config.max_value_chars:
                        raise ValueError(f"record column {column.field!r} exceeds max_value_chars")
                    output_chars += len(value)
                output_chars += len(column.field)
                if source_url is not None:
                    output_chars += len(column.field) + len(source_url) + len(config.selector) + len(column.selector or "")
                    output_chars += len(column.attribute or "") + len(column.multiple)
                    record_provenance[column.field] = CSSFieldProvenance(
                        source_url=source_url,
                        record_selector=config.selector,
                        selector=column.selector,
                        attribute=column.attribute,
                        match_policy=column.multiple,
                        selected_count=len(targets),
                    )
                if output_chars > config.max_output_chars:
                    raise ValueError("record output exceeds max_output_chars")
                field_value: str | list[str] | None = values if column.multiple == "all" else values[0] if values else None
                record[column.field] = field_value
            serialized_chars += len(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            if output:
                serialized_chars += 1  # Record separator.
            if source_url is not None:
                serialized_chars += len(
                    json.dumps(
                        {field: asdict(evidence) for field, evidence in record_provenance.items()},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                if provenance:
                    serialized_chars += 1
            if serialized_chars > config.max_output_chars:
                raise ValueError("record output exceeds max_output_chars")
            output.append(record)
            if source_url is not None:
                provenance.append(record_provenance)
        if serialized_chars > config.max_output_chars:
            raise ValueError("record output exceeds max_output_chars")
        return CSSRecordExtraction(records=output, provenance=provenance)
    except ValueError:
        raise
    except (AttributeError, TypeError) as exc:
        raise ValueError("HTML record extraction failed on malformed content") from exc


def extract_css_records(html: str, config: CSSRecordsConfig, strip_elements: list[str]) -> list[dict[str, str | list[str] | None]]:
    """Extract bounded candidate records from untrusted HTML without truncation."""
    return _extract_css_records(html, config, strip_elements, source_url=None).records


def extract_css_records_with_provenance(
    html: str,
    config: CSSRecordsConfig,
    strip_elements: list[str],
    *,
    source_url: str,
) -> CSSRecordExtraction:
    """Extract records with selector evidence; caller must supply a persistence-safe URL.

    A fetched URL can contain credentials or sensitive query values. Pass the
    same sanitized/fingerprinted URL used for persisted fetch provenance.
    Values remain untrusted row data; this function does not bless HTML text.
    """
    return _extract_css_records(html, config, strip_elements, source_url=source_url)


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
