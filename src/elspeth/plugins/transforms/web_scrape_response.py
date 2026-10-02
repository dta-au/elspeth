"""Admission of untrusted HTTP response bytes before page extraction."""

import codecs
import json
from dataclasses import dataclass
from typing import Literal
from xml.parsers import expat

import httpx

from elspeth.contracts.errors import TransformErrorCategory

ResponseMode = Literal["page", "json", "xml"]
CharsetPolicy = Literal["declared_or_utf8", "utf8_only"]

_CHARSETS = frozenset({"utf-8", "ascii", "iso8859-1", "cp1252", "utf-16", "utf-16-le", "utf-16-be"})


class ResponseContentError(ValueError):
    """A bounded response does not satisfy its declared content contract."""

    def __init__(self, reason: TransformErrorCategory, code: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class AdmittedResponseContent:
    text: str
    content_type: str
    charset: str


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> None:
    seen: set[str] = set()
    for name, _value in pairs:
        if name in seen:
            raise ValueError("duplicate JSON object key")
        seen.add(name)


def _reject_xml_doctype(*_args: object) -> int:
    raise ValueError("XML document type declarations are not admitted")


def _validate_xml(text: str) -> None:
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _reject_xml_doctype
    parser.EntityDeclHandler = _reject_xml_doctype
    parser.ExternalEntityRefHandler = _reject_xml_doctype
    parser.Parse(text, True)


def admit_response_content(
    response: httpx.Response,
    *,
    mode: ResponseMode,
    page_format: Literal["markdown", "text", "raw"],
    accepted_mime_types: tuple[str, ...],
    charset_policy: CharsetPolicy,
    max_decoded_bytes: int,
) -> AdmittedResponseContent:
    """Check status, MIME, charset and syntax without replacement decoding."""
    if response.status_code not in range(200, 300) or response.status_code in {204, 205, 206}:
        raise ResponseContentError(
            "api_error", "unaccepted_status", f"HTTP {response.status_code} cannot be used as a complete content response"
        )

    body = response.content
    if len(body) > max_decoded_bytes:
        raise ResponseContentError(
            "body_too_large", "body_too_large", f"decoded response exceeds max_decoded_body_bytes {max_decoded_bytes}"
        )
    if not body:
        raise ResponseContentError("content_extraction_failed", "empty_response", "the HTTP response has no content")

    raw_type: str | None = response.headers["content-type"] if "content-type" in response.headers else None
    media_type = None if raw_type is None else raw_type.split(";", 1)[0].strip().lower()
    if mode == "json":
        admitted_type = media_type == "application/json" or (media_type is not None and media_type.endswith("+json"))
    elif mode == "xml":
        admitted_type = media_type in {"text/xml", "application/xml"} or (media_type is not None and media_type.endswith("+xml"))
    else:
        admitted_type = media_type is not None and (
            media_type.startswith("text/")
            or media_type == "application/xhtml+xml"
            or (page_format == "raw" and media_type == "application/json")
        )
    if media_type is None or not admitted_type or (accepted_mime_types and media_type not in accepted_mime_types):
        raise ResponseContentError(
            "non_text_content_type", "non_text_content_type", "response Content-Type is not admitted for the configured response mode"
        )

    declared = response.charset_encoding
    if declared is None:
        charset = "utf-8"
    else:
        try:
            charset = codecs.lookup(declared).name
        except LookupError as exc:
            raise ResponseContentError("content_extraction_failed", "invalid_charset", "response declares an unknown charset") from exc
        if charset not in _CHARSETS:
            raise ResponseContentError(
                "content_extraction_failed", "invalid_charset", "response declares a charset outside the supported set"
            )
    if charset_policy == "utf8_only" and charset != "utf-8":
        raise ResponseContentError("content_extraction_failed", "invalid_charset", "response must use UTF-8")
    if mode == "json" and charset != "utf-8":
        raise ResponseContentError("content_extraction_failed", "invalid_charset", "JSON response must use UTF-8")
    try:
        text = body.decode(charset, errors="strict")
    except UnicodeError as exc:
        raise ResponseContentError(
            "content_extraction_failed", "invalid_charset", "response bytes are invalid for the selected charset"
        ) from exc

    if mode == "json":
        try:
            json.loads(text, parse_constant=_reject_json_constant, object_pairs_hook=_reject_duplicate_json_keys)
        except (ValueError, RecursionError) as exc:
            raise ResponseContentError("invalid_json", "invalid_json", "response is not strict JSON") from exc
    elif mode == "xml":
        try:
            _validate_xml(text)
        except (ValueError, expat.ExpatError) as exc:
            raise ResponseContentError("content_extraction_failed", "invalid_xml", "response is not safe well-formed XML") from exc

    return AdmittedResponseContent(text=text, content_type=media_type, charset=charset)
