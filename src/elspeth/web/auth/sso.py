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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Final, Protocol, TypedDict, cast
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.urls import DiscoveredEndpoints, validate_discovered_endpoints

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


# ── the handoff ──────────────────────────────────────────────────────────
#
# The callback cannot hand the browser a session token: it answers a
# top-level GET, so anything in that URL reaches the load balancer's logs,
# uvicorn's logs and the browser's history. It hands back a HANDOFF code in
# the URL fragment instead — browsers never send fragments to servers — and
# ``complete`` trades that code for the token over POST.
#
# The code is therefore a bearer credential for exactly one exchange, and is
# treated like one: 32 random bytes, only its SHA-256 stored, single use
# enforced by the database rather than by a read-then-write in Python.

HANDOFF_TTL_SECONDS: Final = 900


def new_handoff_code() -> str:
    """Mint a handoff code.

    ``token_urlsafe(32)`` is 256 bits of entropy. It travels in a URL
    fragment and is exchanged within 15 minutes, but it authorises minting a
    session token, so it is sized as a credential rather than as a nonce.
    """
    return secrets.token_urlsafe(32)


def handoff_code_hash(code: str) -> str:
    """Hash a handoff code for storage.

    Only the hash is ever stored. A database read — a backup, a replica, an
    accidental log of a row — must not yield anything that can be redeemed,
    and the code's own entropy means a hash is enough: there is no dictionary
    to attack, so no salt or stretching is required.
    """
    return hashlib.sha256(code.encode("ascii")).hexdigest()


class HandoffStore(Protocol):
    """Where handoffs live. Injected, not imported.

    ``sso_handoffs`` is a SESSIONS-store table, and ``web.auth`` does not
    depend on ``web.sessions`` — the dependency runs the other way in
    ``sessions/ownership.py``, and the local provider reaches the identity
    substrate through injected callables for the same reason. A Protocol here
    keeps that direction intact and lets the login service be tested without
    a database.
    """

    def issue(self, *, code_hash: str, identity_id: str, request_id: str) -> None:
        """Record a handoff. Called after the identity is verified."""
        ...

    def consume(self, *, code_hash: str) -> str | None:
        """Atomically claim a handoff, returning its identity_id or None.

        The CLAIM must be one conditional UPDATE: the row is claimed by the
        same statement that decides it may be claimed. Two ``complete`` calls
        racing on one code is the expected case (a double-submitted form, a
        retried request), and reading the row first and updating it second
        would let both win and mint two sessions from one login.

        Its expiry bound must come from the DATABASE's clock, not the
        replica's — otherwise a single-use code is single-use only as far as
        clock drift between replicas allows. Reading that clock is a separate
        statement and must be, since it reads the clock rather than the row;
        the ban is on reading the ROW before claiming it.

        ``None`` covers unknown, already-consumed and expired alike. The
        caller cannot distinguish them and must not: telling a caller which
        of those it hit is telling an attacker whether a guessed code ever
        existed.
        """
        ...


# ── endpoint resolution ──────────────────────────────────────────────────
#
# Where the login walk's four URLs come from, and why each is checked before
# anything is fetched from it. Every URL here is remote-supplied: an IdP that
# is impersonated, compromised, or merely proxied by something misbehaving can
# put any string in a discovery document.

_DISCOVERY_PATH: Final = "/.well-known/openid-configuration"
_DISCOVERY_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)

# A discovery document is a handful of URLs. A body far past that is either a
# different kind of resource or an attempt to make the JSON parser the
# expensive part of a login attempt.
_MAX_DISCOVERY_BYTES: Final = 256 * 1024


class SsoDiscoveryFailed(SsoLoginError):
    """Discovery could not be completed, or returned something unusable."""

    category: ClassVar[str] = "sso_discovery_failed"


def _discovery_string(document: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    """Read one endpoint URL out of a Tier-3 discovery document.

    Membership-then-subscript rather than ``.get()``: absence is a decision
    this function makes explicitly, and for a required endpoint it is fatal.
    A present-but-wrong-type value is fatal in both cases — a document that
    puts an object where a URL belongs is not one to take the rest of on
    trust.
    """
    value = document[key] if key in document else None
    if value is None:
        if required:
            raise SsoDiscoveryFailed(f"discovery document is missing a usable {key!r}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise SsoDiscoveryFailed(f"discovery document {key!r} is not a non-empty string")
    return value


def discovery_endpoints(document: object, *, issuer: str, expected_origins: frozenset[str]) -> DiscoveredEndpoints:
    """Parse and validate a discovery document into the four endpoints.

    THE ISSUER CHECK IS EXACT, and it is the first thing that happens. A
    document whose ``issuer`` differs from the configured one — by a trailing
    slash, by case, by anything — is refused rather than reconciled. Without
    it, an IdP that can serve the discovery URL can hand back another
    provider's endpoints and quietly relocate the whole login walk; every
    later check would then be applied faithfully to the wrong provider.

    Splitting this from the fetch is deliberate: the parse is the part with
    the security decisions in it, and it is pure, so the adversarial cases can
    be tested without a network or a fake server standing in the way.
    """
    if not isinstance(document, dict):
        raise SsoDiscoveryFailed(f"discovery document is not a JSON object (got {type(document).__name__})")
    mapping = cast("Mapping[str, Any]", document)

    document_issuer = mapping["issuer"] if "issuer" in mapping else None
    if not isinstance(document_issuer, str) or document_issuer != issuer:
        # Neither value is echoed: one is remote-supplied and the other names
        # the deployment's IdP.
        raise SsoDiscoveryFailed("discovery document failed the exact issuer check")

    try:
        return validate_discovered_endpoints(
            DiscoveredEndpoints(
                authorization_endpoint=cast("str", _discovery_string(mapping, "authorization_endpoint", required=True)),
                token_endpoint=cast("str", _discovery_string(mapping, "token_endpoint", required=True)),
                jwks_uri=cast("str", _discovery_string(mapping, "jwks_uri", required=True)),
                userinfo_endpoint=_discovery_string(mapping, "userinfo_endpoint", required=False),
            ),
            expected_origins=expected_origins,
        )
    except ValueError as exc:
        # validate_discovered_endpoints raises ValueError with a static
        # message naming the field and the check. Re-raise as a login failure
        # so a bad document is a 401-class refusal rather than a 500.
        raise SsoDiscoveryFailed(str(exc)) from exc


async def fetch_discovery_endpoints(
    *,
    issuer: str,
    expected_origins: frozenset[str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> DiscoveredEndpoints:
    """Fetch the discovery document and return its validated endpoints.

    REDIRECTS ARE DISABLED. This is the load-bearing flag, not a default worth
    keeping: following one would let a response move the document off the
    origin the issuer names, after which every endpoint in it is attacker-
    chosen and the origin policy has been applied to a URL nobody fetched.
    ``httpx`` does not follow redirects unless asked, and it is passed
    explicitly so that removing it is a visible edit rather than a default
    quietly changing underneath.

    The body is read with a size bound before it is parsed. A discovery
    document is a handful of URLs; anything far past that is either a
    different resource or an attempt to make JSON parsing the expensive part
    of an unauthenticated request.
    """
    url = f"{issuer.rstrip('/')}{_DISCOVERY_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT, follow_redirects=False, transport=transport) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.content
    except httpx.HTTPError as exc:
        # Class name only. str(exc) on a connect error can carry the resolved
        # IP of the IdP, and on an InvalidURL the offending URL itself.
        raise SsoDiscoveryFailed(f"discovery request failed ({type(exc).__name__})") from exc

    if len(body) > _MAX_DISCOVERY_BYTES:
        raise SsoDiscoveryFailed("discovery document exceeds the maximum accepted size")
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise SsoDiscoveryFailed("discovery document is not valid JSON") from exc

    return discovery_endpoints(document, issuer=issuer, expected_origins=expected_origins)


class SsoEndpointSettings(Protocol):
    """The four break-glass settings, and nothing else.

    Narrower than ``WebSettings`` on purpose: this module has no business
    reading the rest of the configuration, and a Protocol naming exactly four
    attributes says so in the type rather than in a comment.
    """

    @property
    def sso_authorization_endpoint(self) -> str | None: ...
    @property
    def sso_token_endpoint(self) -> str | None: ...
    @property
    def sso_jwks_uri(self) -> str | None: ...
    @property
    def sso_userinfo_endpoint(self) -> str | None: ...


def configured_endpoint_override(settings: SsoEndpointSettings) -> DiscoveredEndpoints | None:
    """Return the break-glass override, or ``None`` to use discovery.

    Presence of the three required endpoints IS the override — the
    all-or-none rule and the origin policy were already enforced when the
    settings were built, so by the time this runs the values are validated or
    the process never started. Re-validating here would be a second, drifting
    copy of that decision.
    """
    if settings.sso_authorization_endpoint is None or settings.sso_token_endpoint is None or settings.sso_jwks_uri is None:
        return None
    return DiscoveredEndpoints(
        authorization_endpoint=settings.sso_authorization_endpoint,
        token_endpoint=settings.sso_token_endpoint,
        jwks_uri=settings.sso_jwks_uri,
        userinfo_endpoint=settings.sso_userinfo_endpoint,
    )


# ── start ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AuthorizationRedirect:
    """What ``start`` returns: where to send the browser, and what to seal."""

    location: str
    cookie_value: str


def authorization_redirect(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    transaction_secret: str,
    provider: str,
    now: int | None = None,
) -> AuthorizationRedirect:
    """Build the redirect that starts a login, and the cookie that remembers it.

    ``start`` accepts NO query parameters. It takes nothing from the caller
    because there is nothing a caller could usefully supply: every value below
    is generated here or comes from configuration. A ``start`` that accepted,
    say, a return path would be an open-redirect parameter on the one endpoint
    guaranteed to be reachable unauthenticated.

    PKCE is S256, never ``plain``. The verifier stays sealed in the cookie and
    only its hash travels in the URL the browser follows, so the authorization
    request — which passes through the user's browser and its history — never
    carries the secret that redeems the code.

    ``state`` and ``nonce`` are generated per walk and sealed together with the
    verifier: one cookie, opened once, or the callback refuses. Sealing them
    separately would allow a walk to proceed with a matching state and a nonce
    from some other login.
    """
    transaction = new_transaction(now=now)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": transaction.state,
            "nonce": transaction.nonce,
            "code_challenge": pkce_challenge(transaction.verifier),
            "code_challenge_method": "S256",
        }
    )
    return AuthorizationRedirect(
        location=f"{authorization_endpoint}?{query}",
        cookie_value=seal_transaction(
            transaction,
            secret=transaction_secret,
            provider=provider,
            redirect_uri=redirect_uri,
        ),
    )
