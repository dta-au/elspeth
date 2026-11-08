"""Pure ADI endpoint defaults: explicit configuration always takes precedence."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

_AZURE_DOCUMENT_INTELLIGENCE_HOST_SUFFIXES: tuple[str, ...] = (
    ".cognitiveservices.azure.com",
    ".api.cognitive.microsoft.com",
    ".cognitiveservices.azure.us",
    ".api.cognitive.microsoft.us",
    ".cognitiveservices.azure.cn",
    ".api.cognitive.azure.cn",
)


def validate_document_intelligence_endpoint(value: str) -> str:
    """Validate that a credential-bearing endpoint targets Azure Document Intelligence."""
    from elspeth.core.url_validation import validate_credential_safe_https_url

    endpoint = validate_credential_safe_https_url(value, field_name="endpoint")
    parsed = urlparse(endpoint)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not any(hostname.endswith(suffix) and bool(hostname.removesuffix(suffix)) for suffix in _AZURE_DOCUMENT_INTELLIGENCE_HOST_SUFFIXES):
        allowed = ", ".join(f"*{suffix}" for suffix in _AZURE_DOCUMENT_INTELLIGENCE_HOST_SUFFIXES)
        raise ValueError(f"endpoint must be an Azure Document Intelligence resource endpoint ({allowed})")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("endpoint must use a valid HTTPS port") from exc
    if port not in (None, 443):
        raise ValueError("endpoint must use the standard HTTPS port 443")
    if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be an origin URL without a path, parameters, query, or fragment")
    return endpoint


def materialize_document_intelligence_endpoint[T](options: Mapping[str, T], endpoint: str | None) -> dict[str, T | str]:
    """Fill only an absent key; never conceal explicit malformed author input."""
    result: dict[str, T | str] = dict(options)
    if endpoint is not None and "endpoint" not in result:
        result["endpoint"] = validate_document_intelligence_endpoint(endpoint)
    return result
