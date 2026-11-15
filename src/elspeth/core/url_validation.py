"""Shared URL validators for credential-bearing configuration.

Stdlib-only (``re`` + ``urllib.parse``), so it sits at L1/core per ADR-006's
"move the needed code down" protocol: plugins, web, and
:mod:`elspeth.core.llm_profiles` all bind credential-bearing URLs and must
share one validator rather than one per layer.
"""

from __future__ import annotations

import re
from urllib.parse import ParseResult, urlparse

_ASCII_IPV4_LABEL_RE = re.compile(r"^[0-9]+$")


def parse_credential_url_components(value: str) -> tuple[ParseResult | None, str | None, str | None, str | None]:
    """Parse an authored URL without letting parser diagnostics escape the boundary."""
    try:
        parsed = urlparse(value)
        return parsed, parsed.hostname, parsed.username, parsed.password
    except ValueError:
        return None, None, None, None


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.casefold()
    if normalized == "localhost":
        return True
    if normalized in {"::1", "0:0:0:0:0:0:0:1"}:
        return True
    parts = normalized.split(".")
    if len(parts) != 4 or any(_ASCII_IPV4_LABEL_RE.fullmatch(part) is None for part in parts):
        return False
    octets = tuple(int(part) for part in parts)
    return octets[0] == 127 and all(0 <= octet <= 255 for octet in octets)


def validate_credential_safe_https_url(value: str, *, field_name: str, allow_http_loopback: bool = False) -> str:
    """Return a stripped credential-safe URL.

    HTTPS is required for credential-bearing URLs, except when a caller
    explicitly permits HTTP loopback endpoints for local compatible test
    servers or local sidecar gateways.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be empty")

    parsed, hostname, username, password = parse_credential_url_components(stripped)
    if parsed is None:
        raise ValueError(f"{field_name} must be a valid URL")
    if not hostname:
        raise ValueError(f"{field_name} must include a hostname")
    if username is not None or password is not None:
        raise ValueError(f"{field_name} must not contain embedded credentials")
    if parsed.scheme == "https":
        return stripped
    if allow_http_loopback and parsed.scheme == "http" and _is_loopback_host(hostname):
        return stripped
    if allow_http_loopback:
        raise ValueError(f"{field_name} must use HTTPS unless targeting an HTTP loopback host")
    raise ValueError(f"{field_name} must use HTTPS scheme")
