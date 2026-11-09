"""Bounded, non-credential request headers for public page searches."""

from collections.abc import Mapping

from elspeth.contracts.schema_contract import PipelineRow

# This is deliberately a small allowlist. Authentication, cookies, body framing,
# cache validators and origin/host identity have separate protocol ownership.
_CANONICAL_NAMES = {
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "user-agent": "User-Agent",
    "x-requested-with": "X-Requested-With",
}
_FORBIDDEN_NAMES = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-abuse-contact",
        "x-scraping-reason",
    }
)
MAX_HEADER_VALUE_BYTES = 1024
MAX_HEADER_BLOCK_BYTES = 4096


def canonical_request_header_name(name: object) -> str:
    """Admit only public page-search headers; never echo untrusted names."""
    if type(name) is not str or not name.isascii() or not name:
        raise ValueError("request header name is invalid")
    lowered = name.lower()
    if lowered in _FORBIDDEN_NAMES or lowered not in _CANONICAL_NAMES:
        raise ValueError("request header name is not allowed")
    return _CANONICAL_NAMES[lowered]


def validate_request_header_value(value: object) -> str:
    """Reject HTTP control bytes and large values without reflecting row data."""
    if type(value) is not str or not value or not value.isascii():
        raise ValueError("request header value must be a nonempty ASCII string")
    if len(value) > MAX_HEADER_VALUE_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("request header value contains control bytes or exceeds the size limit")
    return value


def build_request_headers(static: Mapping[str, str], row_fields: Mapping[str, str], row: PipelineRow | Mapping[str, str]) -> dict[str, str]:
    """Validate static and row-bound headers before the caller resolves DNS."""
    result: dict[str, str] = {}
    for names, is_row in ((static, False), (row_fields, True)):
        for name, candidate in names.items():
            canonical = canonical_request_header_name(name)
            if canonical in result:
                raise ValueError("request header name is configured more than once")
            if is_row:
                if candidate not in row:
                    raise ValueError("request header row field is missing")
                value = row[candidate]
            else:
                value = candidate
            result[canonical] = validate_request_header_value(value)
    if sum(len(name) + len(value) + 4 for name, value in result.items()) > MAX_HEADER_BLOCK_BYTES:
        raise ValueError("request headers exceed the size limit")
    return result
