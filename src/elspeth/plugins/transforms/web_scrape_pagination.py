"""Bounded next-page discovery for audited web scraping.

Remote links are untrusted input. Discovery never fetches: the caller must
reapply its origin and SSRF policy before dispatching every returned URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, model_validator

from elspeth.plugins.infrastructure.clients.fingerprinting import (
    MAX_AUDIT_QUERY_CHARS,
    MAX_AUDIT_QUERY_FIELDS,
    fingerprint_url,
    is_sensitive_query_param,
    safe_link_header_for_audit,
)


class PaginationConfig(BaseModel):
    """One output row contains the bounded, ordered pages for one input row."""

    model_config = {"extra": "forbid"}

    mode: Literal["next_link_css", "link_header"] = Field(description="Discover the next URL from one CSS anchor or an HTTP Link header")
    next_link_selector: str | None = Field(default=None, min_length=1, max_length=512)
    max_pages: int = Field(default=5, ge=1, le=100)
    max_total_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_total_content_chars: int = Field(default=10 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    max_total_records: int = Field(default=1000, ge=1, le=10000)
    max_elapsed_seconds: float = Field(default=120.0, gt=0, le=3600)

    @model_validator(mode="after")
    def _validate_mode(self) -> PaginationConfig:
        if self.mode not in {"next_link_css", "link_header"}:
            raise ValueError("pagination mode must be next_link_css or link_header")
        if (self.mode == "next_link_css") != (self.next_link_selector is not None):
            raise ValueError("next_link_selector is required only for next_link_css pagination")
        if self.next_link_selector is not None:
            try:
                BeautifulSoup("<html></html>", "html.parser").select(self.next_link_selector)
            except (ValueError, TypeError) as exc:
                raise ValueError("next_link_selector is not a valid CSS selector") from exc
        return self


@dataclass(frozen=True)
class PageProvenance:
    number: int
    request_url: str
    final_url: str
    request_ref: str
    response_ref: str
    body_bytes: int
    next_source: str | None
    selected_next_url: str | None = None

    def to_audit(self) -> dict[str, str | int | None]:
        return {
            "number": self.number,
            "request_url": self.request_url,
            "final_url": self.final_url,
            "request_ref": self.request_ref,
            "response_ref": self.response_ref,
            "body_bytes": self.body_bytes,
            "next_source": self.next_source,
            "selected_next_url": self.selected_next_url,
        }


_NEXT_REL = re.compile(r"(?:^|\s)next(?:\s|$)", re.IGNORECASE)
_LINK_ITEM = re.compile(r'<([^<>]+)>\s*;\s*rel\s*=\s*(?:"([a-z0-9_ -]+)"|([a-z0-9_-]+))\s*\Z', re.IGNORECASE)
_OPAQUE_QUERY_NAMES = frozenset({"cursor", "continuation", "next_token", "page_token", "session", "state"})


def is_disallowed_pagination_query_param(name: str) -> bool:
    """Reject values whose audit-safe replay cannot be established by name."""
    return is_sensitive_query_param(name) or name.casefold() in _OPAQUE_QUERY_NAMES


def _link_header_next(values: list[str]) -> str | None:
    """Accept a narrow, unambiguous RFC 8288 next relation."""
    candidates: list[str] = []
    combined = ", ".join(values)
    if combined and safe_link_header_for_audit(combined) != combined:
        raise ValueError("pagination Link header cannot be retained as exact replay evidence")
    for value in values:
        if any(ord(char) < 32 for char in value):
            raise ValueError("pagination Link header contains control characters")
        # A comma inside an angle-bracket URL or quoted parameter is legal in
        # the grammar, but unsupported here: reject ambiguity instead of
        # choosing a different URL during replay.
        for part in value.split(","):
            part = part.strip()
            match = _LINK_ITEM.fullmatch(part)
            if match is None:
                raise ValueError("pagination Link header is malformed")
            href = match[1]
            try:
                query = urlsplit(href).query
            except ValueError as exc:
                raise ValueError("pagination Link target is malformed") from exc
            if len(query) > MAX_AUDIT_QUERY_CHARS:
                raise ValueError("pagination Link target exceeds audit bounds")
            try:
                pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=MAX_AUDIT_QUERY_FIELDS)
            except ValueError as exc:
                raise ValueError("pagination Link target has too many query fields") from exc
            if any(is_disallowed_pagination_query_param(name) for name, _value in pairs):
                raise ValueError("pagination Link target has a credential or opaque cursor field")
            if fingerprint_url(href) != href:
                raise ValueError("pagination Link target is not replay-safe")
            relation = match[2] or match[3] or ""
            if _NEXT_REL.search(relation):
                candidates.append(href)
    if len(candidates) > 1:
        raise ValueError("pagination Link header has multiple next targets")
    return candidates[0] if candidates else None


def discover_next_url(response: httpx.Response, final_url: str, config: PaginationConfig) -> str | None:
    """Return an absolute fragment-free candidate; network policy is caller-owned."""
    if config.mode == "next_link_css":
        content_type_header = response.headers["content-type"] if "content-type" in response.headers else ""
        content_type = content_type_header.split(";", 1)[0].strip().lower()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("CSS pagination requires an HTML response")
        soup = BeautifulSoup(response.text, "html.parser")
        matches = soup.select(config.next_link_selector or "")
        if len(matches) > 1:
            raise ValueError("next_link_selector matched multiple elements")
        if not matches:
            return None
        attrs = matches[0].attrs
        href = attrs["href"] if "href" in attrs else None
        if type(href) is not str or not href:
            raise ValueError("next_link_selector target lacks one href")
    else:
        href = _link_header_next(response.headers.get_list("link"))
        if href is None:
            return None

    # URL joining may produce a non-HTTP scheme or another origin; the caller
    # rejects those before DNS. Strip fragment because it is not sent on wire.
    resolved = urljoin(final_url, href)
    parsed = urlsplit(resolved)
    resolved = urlunsplit(parsed._replace(fragment=""))
    if not resolved or len(resolved.encode("utf-8")) > 8192:
        raise ValueError("pagination next URL exceeds 8192 bytes")
    return resolved
