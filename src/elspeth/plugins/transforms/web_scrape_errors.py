"""Error hierarchy for web scraping transform.

Follows LLM plugin pattern: retryable errors are re-raised for engine
RetryManager, non-retryable errors return TransformResult.error().
"""

from elspeth.contracts.errors import PluginRetryableError, TransformErrorReason


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
# exceptions' own text names the URL, its host or the address it resolved to —
# row data, which stays in the row carrier and never enters the reason. Each
# except clause picks its sentence; error_type beside it keeps the class.
URL_REFUSED_BY_SSRF_POLICY = (
    "the row's URL is refused by the SSRF policy (malformed, forbidden scheme or credentials, or a blocked address)"
)
URL_HOST_UNRESOLVED = "the row's URL host could not be resolved"
URL_NOT_A_STRING = "the row's URL value is not a string"
URL_FIELD_MISSING = "the row has no URL field"


def row_url_refusal(message: str, exc: Exception) -> TransformErrorReason:
    """The validation_failed reason for a refused row URL: fixed message, exception class."""
    return {"reason": "validation_failed", "error": message, "error_type": type(exc).__name__}
