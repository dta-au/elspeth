"""Bounded, typed JSON response extraction for web_scrape.

JSON paths are lists of exact object keys and zero-based array indexes. They
cannot execute code, evaluate predicates, or follow response-supplied paths.
Extracted values remain untrusted even when the request origin was approved.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Annotated, Literal, NoReturn, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, Field, StrictInt, StrictStr, field_validator, model_validator

from elspeth.contracts.plugin_capabilities import ContentTrust
from elspeth.plugins.infrastructure.clients.fingerprinting import is_sensitive_query_param

JSONPathToken = Annotated[StrictStr, Field(min_length=1, max_length=128)] | Annotated[StrictInt, Field(ge=0, le=1_000_000)]
JSONPath = list[JSONPathToken]
type JSONNode = None | bool | int | Decimal | str | list[JSONNode] | dict[str, JSONNode]


def _validate_field(value: str) -> str:
    if not value.strip() or value != value.strip() or "\r" in value or "\n" in value:
        raise ValueError("JSON extraction field names must be nonempty and contain no surrounding whitespace or line breaks")
    return value


class JSONRecordColumn(BaseModel):
    """One scalar or scalar-array field selected from each JSON record."""

    model_config = {"extra": "forbid"}

    field: str = Field(min_length=1, max_length=128)
    path: JSONPath = Field(default_factory=list, max_length=32)
    required: bool = False
    multiple: Literal["first", "one", "all"] = "first"
    max_values: int = Field(default=16, ge=1, le=256)

    @field_validator("field")
    @classmethod
    def _field_name(cls, value: str) -> str:
        return _validate_field(value)


class JSONRecordsConfig(BaseModel):
    """An operator-declared bounded record set inside a JSON response."""

    model_config = {"extra": "forbid"}

    field: str = Field(min_length=1, max_length=128)
    provenance_field: str | None = Field(default=None, min_length=1, max_length=128)
    records_path: JSONPath = Field(default_factory=list, max_length=32)
    columns: list[JSONRecordColumn] = Field(min_length=1, max_length=32)
    max_records: int = Field(default=200, ge=1, le=1000)
    max_values_per_record: int = Field(default=256, ge=1, le=8192)
    max_total_values: int = Field(default=10000, ge=1, le=100000)
    max_value_chars: int = Field(default=4096, ge=1, le=65536)
    max_output_chars: int = Field(default=100000, ge=1, le=1000000)
    max_json_chars: int = Field(default=1000000, ge=1, le=10000000)

    @field_validator("field", "provenance_field")
    @classmethod
    def _field_names(cls, value: str | None) -> str | None:
        return _validate_field(value) if value is not None else None

    @model_validator(mode="after")
    def _unique_fields(self) -> JSONRecordsConfig:
        names = [column.field for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("JSON record column field names must be unique")
        if self.provenance_field == self.field:
            raise ValueError("provenance_field must differ from records field")
        return self


@dataclass(frozen=True, slots=True)
class JSONFieldProvenance:
    """Path evidence for one untrusted field, using a persistence-safe URL."""

    source_url: str
    records_path: tuple[str | int, ...]
    record_index: int
    value_path: tuple[str | int, ...]
    match_policy: Literal["first", "one", "all"]
    selected_count: int


@dataclass(slots=True)
class JSONRecordExtraction:
    """Values and aligned provenance; the caller must retain untrusted status."""

    records: list[dict[str, str | list[str] | None]]
    provenance: list[dict[str, JSONFieldProvenance]]
    content_trust: ContentTrust = ContentTrust.UNTRUSTED


_MISSING = object()


def _check_source_url(source_url: str) -> None:
    if not source_url or len(source_url) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in source_url):
        raise ValueError("source_url must be a bounded persistence-safe HTTP URL")
    try:
        parsed = urlsplit(source_url)
        hostname = parsed.hostname
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256)
    except ValueError as exc:
        raise ValueError("source_url must be a bounded persistence-safe HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname or "@" in parsed.netloc or parsed.fragment:
        raise ValueError("source_url must be a bounded persistence-safe HTTP URL")
    for name, value in query:
        if is_sensitive_query_param(name) and not (value.startswith("<fingerprint:") and value.endswith(">")):
            raise ValueError("source_url contains an unfingerprinted sensitive query parameter")


def _unique_object(pairs: list[tuple[str, JSONNode]]) -> dict[str, JSONNode]:
    result: dict[str, JSONNode] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("JSON response contains duplicate object keys")
        result[name] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("JSON response contains a non-finite number")


def _select(document: object, path: JSONPath) -> object:
    current = document
    for token in path:
        if type(token) is str and type(current) is dict:
            object_value = cast("dict[str, object]", current)
            current = object_value[token] if token in object_value else _MISSING
        elif type(token) is int and type(current) is list:
            array_value = cast("list[object]", current)
            current = array_value[token] if token < len(array_value) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _scalar_text(value: object) -> str:
    if type(value) is str:
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("JSON record column contains an invalid Unicode scalar")
        return " ".join(value.split())
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in {int, Decimal}:
        return str(value)
    raise ValueError("JSON record column contains a non-scalar value")


def _selected_values(selected: object, column: JSONRecordColumn) -> list[str]:
    if selected is _MISSING or selected is None:
        return []
    if type(selected) is list:
        elements = cast("list[object]", selected)
        count = len(elements)
        if column.multiple == "one" and count > 1:
            raise ValueError(f"JSON record column {column.field!r} has multiple values")
        if column.multiple == "all" and count > column.max_values:
            raise ValueError(f"JSON record column {column.field!r} exceeds max_values")
        if column.multiple == "first":
            return [_scalar_text(elements[0])] if elements else []
        return [_scalar_text(element) for element in elements]
    return [_scalar_text(selected)]


def extract_json_records_with_provenance(document: str, config: JSONRecordsConfig, *, source_url: str) -> JSONRecordExtraction:
    """Extract records from untrusted JSON with finite work and output budgets.

    ``source_url`` must come from ``fingerprint_url(final_hostname_url)`` at
    the fetch boundary. The function checks for unsafe URL shapes and known
    unfingerprinted sensitive parameters, then preserves safe query identity.
    Caller owns the response media-type check and row-level error conversion.
    """
    _check_source_url(source_url)
    if type(document) is not str:
        raise ValueError("JSON response must be text")
    if len(document) > config.max_json_chars:
        raise ValueError("JSON response exceeds max_json_chars")
    try:
        parsed: object = json.loads(
            document,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=Decimal,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON response is malformed or too deeply nested") from exc

    selected_records = _select(parsed, config.records_path)
    if type(selected_records) is not list:
        raise ValueError("JSON records_path must select an array")
    input_records = cast("list[object]", selected_records)
    if len(input_records) > config.max_records:
        raise ValueError(f"JSON record count exceeds max_records {config.max_records}")

    records: list[dict[str, str | list[str] | None]] = []
    provenance: list[dict[str, JSONFieldProvenance]] = []
    total_values = 0
    output_chars = len('{"records":[],"provenance":[],"content_trust":"untrusted"}')
    for record_index, item in enumerate(input_records):
        if type(item) is not dict:
            raise ValueError("JSON record must be an object")
        row: dict[str, str | list[str] | None] = {}
        row_provenance: dict[str, JSONFieldProvenance] = {}
        record_values = 0
        for column in config.columns:
            values = _selected_values(_select(item, column.path), column)
            if column.required and not any(values):
                raise ValueError(f"required JSON record column {column.field!r} is missing")
            record_values += len(values)
            total_values += len(values)
            if record_values > config.max_values_per_record:
                raise ValueError("JSON record extraction exceeds max_values_per_record")
            if total_values > config.max_total_values:
                raise ValueError("JSON record extraction exceeds max_total_values")
            for value in values:
                if len(value) > config.max_value_chars:
                    raise ValueError(f"JSON record column {column.field!r} exceeds max_value_chars")
            row[column.field] = values if column.multiple == "all" else values[0] if values else None
            row_provenance[column.field] = JSONFieldProvenance(
                source_url=source_url,
                records_path=tuple(config.records_path),
                record_index=record_index,
                value_path=tuple(column.path),
                match_policy=column.multiple,
                selected_count=len(values),
            )
        output_chars += len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        output_chars += len(
            json.dumps({field: asdict(entry) for field, entry in row_provenance.items()}, ensure_ascii=False, separators=(",", ":"))
        )
        if records:
            output_chars += 2  # Separators in aligned record and provenance lists.
        if output_chars > config.max_output_chars:
            raise ValueError("JSON record output exceeds max_output_chars")
        records.append(row)
        provenance.append(row_provenance)
    return JSONRecordExtraction(records=records, provenance=provenance)
