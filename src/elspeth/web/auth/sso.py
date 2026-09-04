"""The SSO login walk: start, callback, complete.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §2, §Transaction cookie,
§Handoff, §Userinfo, §Failure categories.

THE SHAPE, AND WHY IT IS THREE STEPS RATHER THAN TWO
----------------------------------------------------
A browser leaves for the IdP and comes back to a redirect. That redirect is a
GET the user's browser makes, so anything in its URL reaches the load
balancer's logs, uvicorn's logs, and the browser's history. A session token
must never be in it.

So ``callback`` does the verification and hands back a short-lived HANDOFF
code in the URL FRAGMENT — which browsers do not send to servers — and
``complete`` is a POST that trades that code for the session token. The token
is minted inside ``complete``, after the code is consumed, so it never exists
before someone has proven they hold the code.

WHAT IS TRUSTED, AND WHAT IS NOT
--------------------------------
Nothing from the IdP is trusted before verification: not the ID token's
header, not the token endpoint's JSON shape, not userinfo's body. Each
crosses a Tier-3 boundary and is parsed into an owned type.

The transaction cookie is likewise not trusted: it is sealed with AEAD, so a
tampered cookie fails to open rather than opening into attacker-chosen state.
It is deliberately STATELESS — no server-side transaction table — because the
security it would buy is already bought elsewhere: the IdP enforces
single-use on the authorization code, and ELSPETH enforces single-use on the
handoff. A transaction table would add a write to every login start,
including the ones users abandon, and bound nothing that is not already
bounded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import ClassVar, Final, TypedDict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from elspeth.web.auth.models import AuthenticationError

# ── failure taxonomy ─────────────────────────────────────────────────────
#
# The spec requires a CLOSED set, "each an explicit exception class, never a
# ``detail`` prefix". The reason is one this codebase has already paid for: a
# prefix puts the same literal in the raiser and the classifier with nothing
# binding them, so rewording a user-facing message silently reclassifies the
# audit event while every test stays green (fixed in 08e51563b). The type is
# the contract; the message is free to change.
#
# Every category below is also a REDIRECT parameter — the browser is sent back
# to the SPA with `?error=<category>` — so the set doubles as the public
# vocabulary. Nothing carries IdP-supplied text: `error_description` is
# attacker-influenced and is never stored or reflected.


class SsoLoginError(AuthenticationError):
    """Base for every SSO login refusal. Carries its own audit category."""

    category: ClassVar[str] = "sso_error"

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.category)


class SsoCookieMissing(SsoLoginError):
    category: ClassVar[str] = "sso_cookie_missing"

    def __init__(self, detail: str = "Login transaction cookie is missing — start the login again") -> None:
        super().__init__(detail)


class SsoCookieInvalid(SsoLoginError):
    """The cookie did not open: tampered, truncated, wrong key, or expired."""

    category: ClassVar[str] = "sso_cookie_invalid"

    def __init__(self, detail: str = "Login transaction could not be verified — start the login again") -> None:
        super().__init__(detail)


class SsoStateMismatch(SsoLoginError):
    category: ClassVar[str] = "sso_state_mismatch"

    def __init__(self, detail: str = "Login transaction did not match — start the login again") -> None:
        super().__init__(detail)


class SsoIdpError(SsoLoginError):
    """The IdP itself refused. Mapped onto a two-value set, never echoed."""

    category: ClassVar[str] = "sso_idp_error"

    def __init__(self, detail: str = "The identity provider refused the sign-in") -> None:
        super().__init__(detail)


class SsoTokenExchangeFailed(SsoLoginError):
    category: ClassVar[str] = "sso_token_exchange_failed"

    def __init__(self, detail: str = "Could not complete the sign-in with the identity provider") -> None:
        super().__init__(detail)


class SsoIdTokenInvalid(SsoLoginError):
    category: ClassVar[str] = "sso_id_token_invalid"

    def __init__(self, detail: str = "The identity provider's response failed verification") -> None:
        super().__init__(detail)


class SsoClaimCheckFailed(SsoLoginError):
    """The token verified but the profile's own rule refused it."""

    category: ClassVar[str] = "sso_claim_check_failed"

    def __init__(self, detail: str = "This account is not permitted to sign in here") -> None:
        super().__init__(detail)


class SsoUserinfoInvalid(SsoLoginError):
    category: ClassVar[str] = "sso_userinfo_invalid"

    def __init__(self, detail: str = "The identity provider's profile response failed verification") -> None:
        super().__init__(detail)


class SsoHandoffInvalid(SsoLoginError):
    """Unknown, already used, or expired handoff code."""

    category: ClassVar[str] = "sso_handoff_invalid"

    def __init__(self, detail: str = "This sign-in link has already been used or has expired") -> None:
        super().__init__(detail)


# The closed set, derived from the classes rather than restated. A runtime
# guard that repeats its own contract drifts from it silently.
SSO_FAILURE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        SsoCookieMissing.category,
        SsoCookieInvalid.category,
        SsoStateMismatch.category,
        SsoIdpError.category,
        SsoTokenExchangeFailed.category,
        SsoIdTokenInvalid.category,
        SsoClaimCheckFailed.category,
        SsoUserinfoInvalid.category,
        SsoHandoffInvalid.category,
    }
)


# ── the sealed transaction cookie ────────────────────────────────────────

COOKIE_NAME: Final = "__Host-elspeth_sso_txn"
"""``__Host-`` is a browser-enforced prefix, not a naming convention.

It requires Secure, Path=/ and NO Domain attribute, and in exchange the
browser refuses to let a sibling subdomain set or overwrite the cookie. That
matters here: the whole transaction is stateless, so an attacker who could
plant a cookie could choose the state and nonce the callback compares against.
"""

COOKIE_MAX_AGE_SECONDS: Final = 300
_FUTURE_SKEW_TOLERANCE_SECONDS: Final = 30
_TRANSACTION_VERSION: Final = 1
_NONCE_BYTES: Final = 12  # 96 bits, the size AES-GCM is specified for


@dataclass(frozen=True, slots=True)
class SsoTransaction:
    """The three secrets a login walk must remember across the redirect.

    ``state`` proves the callback belongs to a login this browser started.
    ``nonce`` binds the ID token to that same walk.
    ``verifier`` is the PKCE secret whose challenge went to the IdP.

    All three are compared, never merely present: a value that is only
    checked for existence is not a check.
    """

    state: str
    nonce: str
    verifier: str
    issued_at: int


def _transaction_key(secret: str) -> bytes:
    """Derive the sealing key from ``sso_transaction_secret``.

    HKDF with its own info string, for the same reason web/key_derivation.py
    separates the other three: this key seals browser-held state, and it must
    not be recoverable from — or grant anything to — any other purpose.
    """
    from elspeth.web.key_derivation import derive_sso_transaction_key

    return derive_sso_transaction_key(secret)


def _aad(*, provider: str, redirect_uri: str) -> bytes:
    """Additional authenticated data: what this cookie is FOR.

    AAD is authenticated but not encrypted, so binding the provider and the
    redirect URI here means a cookie sealed for one deployment or one IdP
    cannot be opened by another — the tag check fails. Without it, a cookie
    captured from a staging deployment would open against production if they
    shared a secret.
    """
    return f"{provider}|{redirect_uri}|{_TRANSACTION_VERSION}".encode()


def seal_transaction(
    transaction: SsoTransaction,
    *,
    secret: str,
    provider: str,
    redirect_uri: str,
) -> str:
    """Seal a transaction into the cookie value."""
    plaintext = json.dumps(
        {
            "v": _TRANSACTION_VERSION,
            "state": transaction.state,
            "nonce": transaction.nonce,
            "verifier": transaction.verifier,
            "iat": transaction.issued_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_transaction_key(secret)).encrypt(nonce, plaintext, _aad(provider=provider, redirect_uri=redirect_uri))
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def open_transaction(
    value: str,
    *,
    secret: str,
    provider: str,
    redirect_uri: str,
    now: int | None = None,
) -> SsoTransaction:
    """Open and validate a cookie value, or refuse.

    EVERY failure here is ``SsoCookieInvalid``, deliberately. Distinguishing
    "bad base64" from "bad tag" from "expired" would tell someone probing the
    endpoint which of their guesses got closer, and none of those distinctions
    helps a real user — the remedy is the same in every case: start again.
    """
    now = int(time.time()) if now is None else now
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:
        raise SsoCookieInvalid from exc
    if len(raw) <= _NONCE_BYTES:
        raise SsoCookieInvalid

    try:
        plaintext = AESGCM(_transaction_key(secret)).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(provider=provider, redirect_uri=redirect_uri)
        )
    except InvalidTag as exc:
        # Tampered, truncated, wrong key, or sealed for a different provider
        # or redirect URI. The tag cannot tell us which, and that is correct.
        raise SsoCookieInvalid from exc

    try:
        payload = json.loads(plaintext)
    except json.JSONDecodeError as exc:
        raise SsoCookieInvalid from exc
    if type(payload) is not dict or payload.get("v") != _TRANSACTION_VERSION:
        raise SsoCookieInvalid

    issued_at = payload.get("iat")
    if type(issued_at) is not int:
        raise SsoCookieInvalid
    # Two bounds, not one. The age bound is the real lifetime; the future
    # bound catches a clock that has moved backwards or a replayed cookie
    # sealed by a host running ahead, either of which would otherwise extend
    # the window indefinitely.
    if now - issued_at > COOKIE_MAX_AGE_SECONDS:
        raise SsoCookieInvalid
    if issued_at - now > _FUTURE_SKEW_TOLERANCE_SECONDS:
        raise SsoCookieInvalid

    values: dict[str, str] = {}
    for field_name in ("state", "nonce", "verifier"):
        field_value = payload.get(field_name)
        if type(field_value) is not str or not field_value:
            raise SsoCookieInvalid
        values[field_name] = field_value

    return SsoTransaction(
        state=values["state"],
        nonce=values["nonce"],
        verifier=values["verifier"],
        issued_at=issued_at,
    )


def new_transaction(*, now: int | None = None) -> SsoTransaction:
    """Mint the three secrets for one login walk.

    32 URL-safe bytes each: enough that guessing is not a strategy, and short
    enough to sit in a cookie alongside its siblings under the 4 KiB limit
    browsers enforce.
    """
    return SsoTransaction(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(32),
        issued_at=int(time.time()) if now is None else now,
    )


def pkce_challenge(verifier: str) -> str:
    """The S256 challenge for a PKCE verifier.

    S256 rather than ``plain``: the challenge travels in a URL the browser
    follows, so with ``plain`` the verifier would be in that URL too, and
    anything that reads it could complete the exchange.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class CookieAttributes(TypedDict):
    """Exactly the Set-Cookie attributes this cookie uses.

    A closed shape rather than ``dict[str, Any]``: these are keyword
    arguments to Starlette's ``set_cookie``, and a typo in one of them
    silently drops a security attribute instead of failing. Note what is
    ABSENT and must stay absent — ``domain``: the ``__Host-`` prefix is void
    if the cookie carries one, so the browser would stop enforcing the
    sibling-subdomain protection this design leans on.
    """

    key: str
    value: str
    path: str
    secure: bool
    httponly: bool
    samesite: str
    max_age: int


def cookie_attributes(value: str | None) -> CookieAttributes:
    """The Set-Cookie attributes, in one place so clearing cannot drift.

    ``value=None`` produces the clearing form. Clearing MUST repeat every
    attribute the cookie was set with: a browser matches on name, path and
    domain, so a clear that omits ``path`` silently leaves the cookie in
    place — and the next login would then compare against a stale state.
    """
    attributes: CookieAttributes = {
        "key": COOKIE_NAME,
        "value": "" if value is None else value,
        "path": "/",
        # Unconditional, not conditioned on the request scheme: uvicorn runs
        # without proxy headers here, so request.url.scheme reports the
        # INTERNAL hop and would read "http" behind a TLS-terminating load
        # balancer. A cookie that drops Secure there is a cookie sent in
        # clear on the next plain-HTTP request.
        "secure": True,
        "httponly": True,
        # Lax, not Strict: the IdP redirects back with a top-level GET, and
        # Strict would withhold the cookie on exactly that navigation,
        # breaking every login. Lax sends it on top-level GETs and withholds
        # it from cross-site POSTs, which is the threat that matters.
        "samesite": "lax",
        "max_age": 0 if value is None else COOKIE_MAX_AGE_SECONDS,
    }
    return attributes
