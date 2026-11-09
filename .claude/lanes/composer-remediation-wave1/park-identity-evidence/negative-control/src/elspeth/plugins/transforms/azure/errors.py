"""Shared error types for Azure transform plugins."""


class MalformedResponseError(Exception):
    """Raised when Azure API returns a response with invalid structure or types.

    This is distinct from network errors (httpx.RequestError) - malformed responses
    indicate the API returned something we can't safely interpret, which won't
    improve on retry. Used to fail CLOSED on security-critical transforms.
    """
