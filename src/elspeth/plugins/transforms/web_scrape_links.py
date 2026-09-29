"""Resolve extracted HTML links before they enter persistent pipeline rows."""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import IPv4Network, IPv6Network
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

from elspeth.core.security.web import (
    HTTPOrigin,
    SSRFBlockedError,
    validate_allowed_http_origin,
    validate_configured_url_for_ssrf,
)
from elspeth.plugins.infrastructure.clients.fingerprinting import (
    MAX_AUDIT_QUERY_CHARS,
    MAX_AUDIT_QUERY_FIELDS,
    fingerprint_url,
    is_sensitive_query_param,
)

_MAX_DISCOVERED_URL_BYTES = 8192


def resolve_discovered_href(
    href: str,
    *,
    base_url: str,
    allowed_origins: Sequence[HTTPOrigin],
    allowed_ranges: Sequence[IPv4Network | IPv6Network],
) -> str:
    """Return an absolute, persistence-safe HTTP URL from an untrusted href.

    This admission is DNS-free so a replay of the search page stays offline.
    The downstream row-URL transform resolves, checks, and pins the destination
    IP again immediately before dispatch. A configured exact origin is required
    so a page cannot emit links to arbitrary hosts in the meantime.
    This only protects emitted href row values and URL metadata. The fetched
    HTML remains exact audited response evidence and may contain page tokens.
    """
    if not allowed_origins:
        raise ValueError("resolved links require http.allowed_origins")
    if not href or any(ord(char) < 33 or char == "\\" for char in href):
        raise ValueError("discovered link is not an approved HTTP target")

    try:
        if len(href.encode("utf-8")) > _MAX_DISCOVERED_URL_BYTES:
            raise ValueError("discovered link exceeds the URL size limit")
        joined = urljoin(base_url, href)
        parsed = urlsplit(joined)
        candidate = urlunsplit(parsed._replace(fragment=""))
        if len(candidate.encode("utf-8")) > _MAX_DISCOVERED_URL_BYTES:
            raise ValueError("discovered link exceeds the URL size limit")
        validate_allowed_http_origin(candidate, allowed_origins)
        validate_configured_url_for_ssrf(candidate, allowed_ranges=allowed_ranges)

        # The emitted value is itself audited row data. A query token cannot be
        # fingerprinted in-place because the next HTTP request needs the real
        # value, so fail this link instead of persisting a navigable secret.
        if len(parsed.query) > MAX_AUDIT_QUERY_CHARS:
            raise ValueError("discovered link query exceeds the audit limit")
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=MAX_AUDIT_QUERY_FIELDS)
        if any(is_sensitive_query_param(name) for name, _ in query):
            raise ValueError("discovered link contains a sensitive query parameter")

        safe_url = fingerprint_url(candidate)
        validate_allowed_http_origin(safe_url, allowed_origins)
        validate_configured_url_for_ssrf(safe_url, allowed_ranges=allowed_ranges)
        return safe_url
    except (SSRFBlockedError, TypeError, UnicodeError, ValueError):
        # Policy errors and URL parsing errors can contain the raw href. Never
        # forward their text into a TransformResult or the audit trail.
        raise ValueError("discovered link is not an approved HTTP target") from None
