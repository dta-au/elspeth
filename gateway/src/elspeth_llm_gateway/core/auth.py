"""Static bearer authentication for inbound requests.

``check_bearer`` is the gateway's only inbound credential channel: a single,
strict parse of the ``Authorization`` header against exactly the
``Bearer <token>`` shape (case-sensitive scheme, exactly one separating
space), followed by a constant-time comparison of the token against the
expected value. There is no query-parameter or cookie credential path
anywhere in the app, and this module must never grow one.
"""

import hmac

_SCHEME_PREFIX = "Bearer "


def check_bearer(authorization_header: str | None, expected: str) -> bool:
    """Return whether ``authorization_header`` carries the expected bearer token.

    The parse gate is strict but narrow: the header must start with the
    case-sensitive scheme prefix ``"Bearer "`` (exactly one separating
    space) followed by a non-empty token; anything else (``None``, wrong
    case, wrong scheme, no token) is rejected here without ever reaching the
    comparison. A malformed *token* — extra internal or trailing whitespace,
    e.g. from a doubled space after the scheme or a trailing space on the
    credential — is not filtered by the gate; it is passed through to
    ``hmac.compare_digest``, which naturally rejects it because it differs
    byte-for-byte from ``expected``. Using ``compare_digest`` for every
    comparison that reaches it (rather than ``==``) keeps token comparison
    constant-time regardless of which path produced the token.
    """
    if authorization_header is None:
        return False
    if not authorization_header.startswith(_SCHEME_PREFIX):
        return False
    token = authorization_header[len(_SCHEME_PREFIX) :]
    if token == "":
        return False
    return hmac.compare_digest(token.encode(), expected.encode())
