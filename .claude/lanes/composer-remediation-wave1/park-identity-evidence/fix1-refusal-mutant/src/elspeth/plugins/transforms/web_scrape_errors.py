"""Error hierarchy for web scraping transform.

Follows LLM plugin pattern: retryable errors are re-raised for engine
RetryManager, non-retryable errors return TransformResult.error().
"""

from collections.abc import Mapping
from types import MappingProxyType

from elspeth.contracts.errors import PluginRetryableError, TransformErrorReason
from elspeth.core.security.web import DNSFailureKind, SSRFRefusalKind
from elspeth.core.security.web import NetworkError as SSRFNetworkError
from elspeth.core.security.web import SSRFBlockedError as PolicySSRFBlockedError


class WebScrapeError(PluginRetryableError):
    """Base error for web scrape transform."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message, retryable=retryable)


# Retryable errors (re-raise for engine retry)


class RateLimitError(WebScrapeError):
    """HTTP 429 or rate limit exceeded."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class NetworkError(WebScrapeError):
    """Network/connection errors (DNS, timeout, connection refused)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class ServerError(WebScrapeError):
    """HTTP 5xx server errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


# Non-retryable errors (return TransformResult.error())


class NotFoundError(WebScrapeError):
    """HTTP 404 Not Found."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ForbiddenError(WebScrapeError):
    """HTTP 403 Forbidden."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class UnauthorizedError(WebScrapeError):
    """HTTP 401 Unauthorized."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class InvalidURLError(WebScrapeError):
    """Malformed or invalid URL."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class SSRFBlockedError(WebScrapeError):
    """URL resolves to blocked IP range (private, loopback, cloud metadata)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class ClientError(WebScrapeError):
    """Generic HTTP 4xx client error not covered by a specific arm.

    retryable=True for 408 Request Timeout (server may be temporarily
    overloaded); retryable=False for all other unenumerated 4xx codes.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable)


class BodyTooLargeError(WebScrapeError):
    """Response body exceeded web_scrape's configured max_body_bytes."""

    def __init__(self, message: str, *, body_size: int, max_body_bytes: int) -> None:
        super().__init__(message, retryable=False)
        self.body_size = body_size
        self.max_body_bytes = max_body_bytes


# Value-free audit text for a row URL refused before any fetch. The refusing
# exceptions' own text names the URL, its scheme, host, hash or the address it
# resolved to — row data, which stays in the row carrier and never enters the
# reason. The policy's structured ``kind`` travels as ``cause`` (which check
# refused: always-blocked vs blocked range, malformed, forbidden scheme, ...)
# with one fixed sentence per kind.
URL_POLICY_REFUSAL_TEXT: Mapping[SSRFRefusalKind | DNSFailureKind, str] = MappingProxyType(
    {
        "origin_not_allowed": "the row's URL origin is not allowed",
        "malformed_url": "the row's URL is malformed",
        "invalid_port": "the row's URL has an invalid port",
        "missing_scheme": "the row's URL has no scheme (expected http:// or https://)",
        "credentials_in_url": "the row's URL carries credentials, which are not allowed",
        "forbidden_scheme": "the row's URL uses a forbidden scheme (only http and https are allowed)",
        "missing_hostname": "the row's URL has no hostname",
        "port_zero": "the row's URL uses port 0, which is not allowed",
        "unparseable_ip": "the row's URL host resolved to an unparseable address",
        "always_blocked_range": "the row's URL host resolves to an always-blocked address range",
        "blocked_range": "the row's URL host resolves to a blocked address range",
        "archived_request_mismatch": "the row's URL does not match the archived request",
        "dns_failed": "the row's URL host could not be resolved",
        "dns_capacity_exhausted": "DNS resolution capacity was exhausted",
        "dns_timeout": "DNS resolution of the row's URL host timed out",
        "dns_no_addresses": "the row's URL host resolved to no addresses",
    }
)
URL_NOT_A_STRING = "the row's URL value is not a string"
URL_FIELD_MISSING = "the row has no URL field"


def row_url_policy_refusal(exc: PolicySSRFBlockedError | SSRFNetworkError) -> TransformErrorReason:
    """The validation_failed reason for a row URL the SSRF policy or DNS refused."""
    return {
        "reason": "validation_failed",
        "error": URL_POLICY_REFUSAL_TEXT[exc.kind],
        "error_type": type(exc).__name__,
        "cause": exc.kind,
    }


def row_url_value_refusal(message: str, exc: KeyError | TypeError) -> TransformErrorReason:
    """The validation_failed reason for a row whose URL field is absent or not a string."""
    return {"reason": "validation_failed", "error": message, "error_type": type(exc).__name__}
