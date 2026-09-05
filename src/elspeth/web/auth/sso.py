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
import hmac
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal, NamedTuple, Protocol, TypedDict, cast
from urllib.parse import quote, urlencode

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthenticationError, AuthProviderUnavailable, IdentityClaims
from elspeth.web.auth.session_token import SessionTokenIssuer
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


IdpErrorReason = Literal["access_denied", "other"]


class SsoIdpError(SsoLoginError):
    """The IdP itself refused, or answered with something that is not a code.

    ``reason`` is the IdP's ``error`` parameter mapped onto a two-value set.
    ``access_denied`` is the one value with a distinct meaning for an
    operator (the user, or the IdP's policy, said no); everything else is
    ``other``. The raw value is never stored and never in the message: the
    parameter arrives in a URL the user's browser was redirected to, so it is
    whatever the redirecting party wanted it to be.
    """

    category: ClassVar[str] = "sso_idp_error"

    def __init__(self, *, reason: IdpErrorReason, detail: str = "The identity provider refused the sign-in") -> None:
        super().__init__(detail)
        self.reason: IdpErrorReason = reason


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


class SsoIdentityDisabled(SsoLoginError):
    """The person proved who they are; an administrator has disabled them."""

    category: ClassVar[str] = "sso_identity_disabled"

    def __init__(self, detail: str = "This account has been disabled") -> None:
        super().__init__(detail)


class SsoAccessPending(SsoLoginError):
    """The person proved who they are; nobody has admitted them yet."""

    category: ClassVar[str] = "sso_access_pending"

    def __init__(self, detail: str = "This account is awaiting approval") -> None:
        super().__init__(detail)


class SsoHandoffInvalid(SsoLoginError):
    """Unknown, already used, or expired handoff code."""

    category: ClassVar[str] = "sso_handoff_invalid"

    def __init__(self, detail: str = "This sign-in link has already been used or has expired") -> None:
        super().__init__(detail)


PROVIDER_UNAVAILABLE_CATEGORY: Final = "provider_unavailable"
"""The one category without the ``sso_`` prefix, and the one that is not a refusal.

Spec §Failure categories names it as a member of the closed set. It is
carried by the pre-existing ``AuthProviderUnavailable`` (a 503, not a 401)
rather than by an ``SsoLoginError`` subclass, because the browser's remedy
is different: every ``sso_*`` category means "start again"; this one means
"wait". The prefix rule in the tests admits exactly this name.
"""


def failure_category(exc: SsoLoginError | AuthProviderUnavailable) -> str:
    """The redirect parameter for a login failure. Total over the two types the route catches."""
    if isinstance(exc, SsoLoginError):
        return exc.category
    return PROVIDER_UNAVAILABLE_CATEGORY


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
        SsoIdentityDisabled.category,
        SsoAccessPending.category,
        SsoHandoffInvalid.category,
        PROVIDER_UNAVAILABLE_CATEGORY,
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


class ConsumedHandoff(NamedTuple):
    """What a claimed handoff row says: who, and which login it came from.

    ``login_request_id`` is the request id of the CALLBACK that issued the
    handoff. The ``token_issued`` row written at ``complete`` carries it so
    the two audit rows of one login — ``login`` at callback, ``token_issued``
    at complete, on different requests and possibly different replicas — are
    joinable (spec §2 complete: "joined to the login row by request_id").
    Returning only the identity would leave the join to a timestamp guess.
    """

    identity_id: str
    login_request_id: str


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

    def consume(self, *, code_hash: str) -> ConsumedHandoff | None:
        """Atomically claim a handoff, returning what its row says, or None.

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
    """Discovery could not be completed, or returned something unusable.

    Its category is ``provider_unavailable``: spec §2 maps a discovery outage
    onto the existing 503 path, and from the browser's side an IdP whose
    discovery document is unreachable and one whose document is unusable
    are the same event — the provider cannot be used right now, and the
    remedy is to wait, not to start again. The audit row keeps the
    distinction through ``exception_class`` and the detail.
    """

    category: ClassVar[str] = PROVIDER_UNAVAILABLE_CATEGORY


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


# ── callback ─────────────────────────────────────────────────────────────
#
# The browser is back. Everything it brought — the cookie, the query string —
# and everything the IdP will now send — the token response, the ID token,
# the userinfo body — is unverified until the step that verifies it runs.
# The order below is the order the checks CAN run in: nothing is fetched from
# the IdP until the cookie has opened and the state has matched, so a
# callback that was not started by this browser costs no token-endpoint call.

_TOKEN_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
_USERINFO_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)

# A token response is three or four short strings and a JWT; a userinfo body
# is a handful of claims. Either far past this is a different resource.
_MAX_TOKEN_RESPONSE_BYTES: Final = 64 * 1024
_MAX_USERINFO_BYTES: Final = 64 * 1024

# An authorization code is an opaque string the IdP minted moments ago. It
# is forwarded verbatim to the token endpoint, so its size is bounded here
# rather than by whatever the token endpoint will accept.
_MAX_AUTHORIZATION_CODE_LENGTH: Final = 2048


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    """The three query parameters a callback may carry, as the route read them.

    ``None`` is absence. A present-but-empty parameter is a value, and it is
    refused where a value would be — an empty ``state`` does not match, an
    empty ``code`` is not a code.
    """

    code: str | None
    state: str | None
    error: str | None


def open_callback(
    query: CallbackQuery,
    cookie_value: str | None,
    *,
    transaction_secret: str,
    provider: str,
    redirect_uri: str,
    now: int | None = None,
) -> tuple[str, SsoTransaction]:
    """Verify the callback belongs to a walk this browser started; return its code.

    THE ORDER IS THE POINT. Cookie, then state, then the IdP's ``error``,
    then the code — and every step before the code is a check that costs
    nothing remote. An ``error`` parameter is only honoured once the state
    has matched, so a link someone crafted to ``?error=access_denied`` does
    not write an ``sso_idp_error`` row against a walk it was never part of.

    ``state`` is compared in constant time as bytes: ``compare_digest`` on
    ``str`` raises on non-ASCII, and the query string is attacker-supplied.
    """
    if cookie_value is None:
        raise SsoCookieMissing
    transaction = open_transaction(cookie_value, secret=transaction_secret, provider=provider, redirect_uri=redirect_uri, now=now)

    if query.state is None or not hmac.compare_digest(query.state.encode("utf-8"), transaction.state.encode("utf-8")):
        raise SsoStateMismatch

    if query.error is not None:
        raise SsoIdpError(reason="access_denied" if query.error == "access_denied" else "other")

    code = query.code
    if code is None or not code or len(code) > _MAX_AUTHORIZATION_CODE_LENGTH or not code.isprintable():
        # No error and no usable code is not a refusal the IdP expressed; it
        # is a response that is not an OAuth response. Same category: the
        # provider's answer could not be used.
        raise SsoIdpError(reason="other", detail="The identity provider returned no usable authorization code")
    return code, transaction


@dataclass(frozen=True, slots=True)
class RedeemedTokens:
    """What the token endpoint returned, for exactly as long as the callback needs it.

    ``access_token`` exists to make ONE userinfo call and is then dropped;
    ``id_token`` exists to be verified and is then dropped. Neither is ever
    stored, and neither appears in ``repr`` — a dataclass in a traceback or
    a log line must not be the place an IdP token is written down.

    A refresh token, if the IdP sent one, is never read out of the response.
    """

    id_token: str = field(repr=False)
    access_token: str = field(repr=False)


def _token_string(document: Mapping[str, Any], key: str) -> str:
    value = document[key] if key in document else None
    if type(value) is not str or not value:
        raise SsoTokenExchangeFailed(f"token response {key!r} is not a non-empty string")
    return value


@trust_boundary(
    tier=3,
    source="token endpoint response JSON from the IdP, after the authorization-code exchange",
    source_param="document",
    suppresses=("R1",),
    invariant="raises SsoTokenExchangeFailed unless document is a JSON object with a Bearer token_type and non-empty id_token and access_token strings; never coerces",
    test_ref="tests/unit/web/auth/test_sso_callback.py::TestTokenResponseBoundary::test_parse_token_response_non_dict_raises",
    test_fingerprint="8f300a081a1c9e166b725115f73e27f9edd9f139850b230509a065923df5509c",
)
def parse_token_response(document: object) -> RedeemedTokens:
    """Parse the token endpoint's JSON into the two strings the callback uses.

    ``token_type`` is compared case-insensitively: RFC 6749 §5.1 makes the
    value case-insensitive and providers differ (``Bearer``, ``bearer``).
    Anything that is not a bearer token is not a token this code knows how
    to present to userinfo, so it is refused rather than tried.
    """
    if not isinstance(document, dict):
        raise SsoTokenExchangeFailed(f"token response is not a JSON object (got {type(document).__name__})")
    mapping = cast("Mapping[str, Any]", document)
    if _token_string(mapping, "token_type").lower() != "bearer":
        raise SsoTokenExchangeFailed("token response token_type is not Bearer")
    return RedeemedTokens(id_token=_token_string(mapping, "id_token"), access_token=_token_string(mapping, "access_token"))


def _client_secret_basic(client_id: str, client_secret: str) -> str:
    """The ``Authorization`` header for ``client_secret_basic``.

    RFC 6749 §2.3.1: the id and secret are form-URL-encoded BEFORE they are
    joined and base64-encoded. ``httpx``'s Basic auth helper skips the
    encoding step, which is only harmless while neither value contains a
    character the encoding would change.
    """
    credentials = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
    return "Basic " + base64.b64encode(credentials.encode("ascii")).decode("ascii")


async def redeem_authorization_code(
    code: str,
    *,
    verifier: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RedeemedTokens:
    """Exchange the code for tokens at the token endpoint, or refuse.

    The client authenticates with ``client_secret_basic``: the secret goes in
    the ``Authorization`` header, never in the form body, so it is absent
    from any request log that records bodies. ``client_id`` is repeated in
    the body because some providers require it there even when the header
    already identifies the client, and RFC 6749 permits the repetition.

    REDIRECTS ARE DISABLED. A followed redirect would post the client secret
    and the authorization code to wherever the redirect pointed.

    Every failure is ``SsoTokenExchangeFailed``. The HTTP status is named in
    the detail because it is the one fact an operator needs and it is not
    IdP-authored text; the response body never is.
    """
    headers = {
        "Authorization": _client_secret_basic(client_id, client_secret),
        "Accept": "application/json",
    }
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT, follow_redirects=False, transport=transport) as client:
            response = await client.post(token_endpoint, data=form, headers=headers)
    except httpx.HTTPError as exc:
        # Class name only: str(exc) can carry the resolved address of the IdP.
        raise SsoTokenExchangeFailed(f"token request failed ({type(exc).__name__})") from exc

    if response.status_code != 200:
        raise SsoTokenExchangeFailed(f"token endpoint returned HTTP {response.status_code}")
    if len(response.content) > _MAX_TOKEN_RESPONSE_BYTES:
        raise SsoTokenExchangeFailed("token response exceeds the maximum accepted size")
    try:
        document = json.loads(response.content)
    except ValueError as exc:
        raise SsoTokenExchangeFailed("token response is not valid JSON") from exc
    return parse_token_response(document)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


@trust_boundary(
    tier=3,
    source="userinfo endpoint response JSON from the IdP, fetched with the just-redeemed access token",
    source_param="document",
    suppresses=("R1",),
    invariant="raises SsoUserinfoInvalid unless document is a JSON object whose sub is a string equal to expected_subject; never coerces",
    test_ref="tests/unit/web/auth/test_sso_callback.py::TestUserinfoBoundary::test_parse_userinfo_non_dict_raises",
    test_fingerprint="ede8a61e1355122e70018faa82b2941179b4e5e3427298d79a4502ea9b223bc5",
)
def parse_userinfo(document: object, *, expected_subject: str) -> Mapping[str, Any]:
    """Bind a userinfo body to the ID token it was fetched for.

    The ONE check that is this function's own: userinfo ``sub`` must equal
    the ID token's ``sub``, in constant time. Without it, a provider (or a
    proxy in front of one) that answers userinfo with a different person's
    claims would have those claims attached to the verified identity.

    The remaining claims are not read here. The profile's ``map_identity``
    reads exactly the keys it declares and constructs the owned
    ``IdentityClaims``; this function's job is to establish that the body is
    an object about the right subject, and to hand over nothing else.
    """
    if not isinstance(document, dict):
        raise SsoUserinfoInvalid(f"userinfo response is not a JSON object (got {type(document).__name__})")
    mapping = cast("Mapping[str, Any]", document)
    subject = mapping["sub"] if "sub" in mapping else None
    if type(subject) is not str or not hmac.compare_digest(subject.encode("utf-8"), expected_subject.encode("utf-8")):
        raise SsoUserinfoInvalid("userinfo sub does not match the ID token")
    return mapping


async def fetch_userinfo(
    *,
    userinfo_endpoint: str,
    access_token: str,
    expected_subject: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Mapping[str, Any]:
    """Fetch userinfo for the subject just verified, or refuse.

    Spec §Userinfo: 200, ``application/json``, at most 64 KiB, an object
    whose ``sub`` matches. Anything else — including a transport failure —
    is ``sso_userinfo_invalid``: the profile declared it cannot build an
    identity without this call, so there is no login to fall back to.
    """
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_USERINFO_TIMEOUT, follow_redirects=False, transport=transport) as client:
            response = await client.get(userinfo_endpoint, headers=headers)
    except httpx.HTTPError as exc:
        raise SsoUserinfoInvalid(f"userinfo request failed ({type(exc).__name__})") from exc

    if response.status_code != 200:
        raise SsoUserinfoInvalid(f"userinfo endpoint returned HTTP {response.status_code}")
    content_type = response.headers["content-type"] if "content-type" in response.headers else ""
    if _media_type(content_type) != "application/json":
        raise SsoUserinfoInvalid("userinfo response is not application/json")
    if len(response.content) > _MAX_USERINFO_BYTES:
        raise SsoUserinfoInvalid("userinfo response exceeds the maximum accepted size")
    try:
        document = json.loads(response.content)
    except ValueError as exc:
        raise SsoUserinfoInvalid("userinfo response is not valid JSON") from exc
    return parse_userinfo(document, expected_subject=expected_subject)


@dataclass(frozen=True, slots=True)
class SsoClient:
    """What this deployment is to its IdP, bound once at startup.

    Everything the callback needs that does not vary per request. The
    endpoints are the RESOLVED ones — already put through the origin policy
    by discovery or by the break-glass validation — so nothing here is
    re-checked per login.

    ``userinfo`` and ``endpoints.userinfo_endpoint`` are made to agree at
    construction: a profile that needs userinfo against a provider that
    published no endpoint is a deployment that cannot log anyone in, and it
    should say so at startup rather than at the first user's callback.
    """

    provider: AuthProviderType
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    transaction_secret: str = field(repr=False)
    public_base_url: str
    endpoints: DiscoveredEndpoints
    id_token_algorithms: tuple[str, ...]
    userinfo: bool

    def __post_init__(self) -> None:
        if self.userinfo and self.endpoints.userinfo_endpoint is None:
            raise ValueError(f"the {self.provider!r} profile requires userinfo but the provider published no userinfo_endpoint")


class AdmittedIdentity(Protocol):
    """The three facts about an identity row the callback acts on.

    ``web.auth`` does not import ``web.sessions``, so the row type is not
    named here; this is what the injected upsert must return, and
    ``IdentityRecord`` satisfies it. Typing only — nothing dispatches on it.
    """

    @property
    def identity_id(self) -> str: ...
    @property
    def username(self) -> str: ...
    @property
    def access_state(self) -> str: ...


def admit(identity: AdmittedIdentity) -> None:
    """Refuse a verified login the container has not admitted.

    Three states, three outcomes, and an unknown state is a refusal rather
    than a pass: the column carries a CHECK constraint, so a fourth value
    means the store is not the store this code was written against.
    """
    state = identity.access_state
    if state == "active":
        return
    if state == "disabled":
        raise SsoIdentityDisabled
    if state == "pending":
        raise SsoAccessPending
    raise AuthenticationError(f"identity {identity.identity_id} has an unrecognised access_state")


def _spa_location(public_base_url: str, params: Mapping[str, str]) -> str:
    # The FRAGMENT, not the query: browsers do not send it, so neither the
    # load balancer nor uvicorn logs it. That is the whole reason the
    # handoff exists (module docstring). The failure category rides the same
    # way so the SPA has one route with one parser.
    return f"{public_base_url.rstrip('/')}/#/auth/callback?{urlencode(params)}"


def handoff_location(public_base_url: str, code: str) -> str:
    """Where to send the browser after a successful callback."""
    return _spa_location(public_base_url, {"code": code})


def failure_location(public_base_url: str, category: str) -> str:
    """Where to send the browser after a refused callback. Category only."""
    if category not in SSO_FAILURE_CATEGORIES:
        # Derived from the classes, so the only way here is a caller passing
        # something that is not a category — never let it into a URL.
        raise ValueError(f"{category!r} is not an SSO failure category")
    return _spa_location(public_base_url, {"error": category})


async def login_callback(
    query: CallbackQuery,
    cookie_value: str | None,
    *,
    client: SsoClient,
    validator: JWKSTokenValidator,
    claim_checks: Callable[[Mapping[str, Any]], None],
    map_identity: Callable[[Mapping[str, Any], Mapping[str, Any] | None], IdentityClaims],
    upsert_identity: Callable[[IdentityClaims], AdmittedIdentity],
    record_login: Callable[[AdmittedIdentity], None],
    handoffs: HandoffStore,
    request_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
    now: int | None = None,
) -> str:
    """The whole callback, from cookie to redirect location.

    Injected rather than imported: the identity upsert and the audit write
    live in ``web.sessions`` and the Landscape, which ``web.auth`` does not
    depend on, and the route is where a ``Request`` exists to derive the
    audit row's client host and request id from. Taking them as callables
    lets THIS function own the order — which is the property worth a test:

    1. cookie → state → IdP error → code, none of it remote;
    2. token exchange;
    3. ID-token verification, with the nonce from the cookie;
    4. the profile's own claim checks;
    5. userinfo, only if the profile needs it, bound to the ID token's sub;
    6. ``map_identity`` — the Tier-3 boundary that yields the owned claims;
    7. the IdP's tokens are dropped: nothing after this line can see them;
    8. upsert, admit, audit ``login``, THEN issue the handoff.

    The login row is written before the handoff exists, so a handoff can
    never be redeemed for a login the trail does not record.

    ``AuthProviderUnavailable`` is re-raised as itself: it is a 503 and the
    browser's remedy is to wait, so it must not be reclassified as an
    ID-token failure, which says "start again".
    """
    code, transaction = open_callback(
        query,
        cookie_value,
        transaction_secret=client.transaction_secret,
        provider=client.provider,
        redirect_uri=client.redirect_uri,
        now=now,
    )
    tokens = await redeem_authorization_code(
        code,
        verifier=transaction.verifier,
        token_endpoint=client.endpoints.token_endpoint,
        client_id=client.client_id,
        client_secret=client.client_secret,
        redirect_uri=client.redirect_uri,
        transport=transport,
    )

    try:
        id_claims = await validator.decode_id_token_with_refresh(
            tokens.id_token,
            algorithms=client.id_token_algorithms,
            audience=client.client_id,
            nonce=transaction.nonce,
            client_id=client.client_id,
        )
    except AuthProviderUnavailable:
        raise
    except AuthenticationError as exc:
        raise SsoIdTokenInvalid from exc

    try:
        claim_checks(id_claims)
    except AuthenticationError as exc:
        raise SsoClaimCheckFailed from exc

    userinfo: Mapping[str, Any] | None = None
    if client.userinfo:
        # ``sub`` is in the decoder's required-claims list, so it is present
        # and a string here; the membership form is kept because this is
        # still foreign data and the accessor should look like one.
        subject = id_claims["sub"] if "sub" in id_claims else None
        if type(subject) is not str:
            raise SsoIdTokenInvalid
        userinfo = await fetch_userinfo(
            # ``__post_init__`` made this non-None whenever ``client.userinfo``.
            userinfo_endpoint=cast("str", client.endpoints.userinfo_endpoint),
            access_token=tokens.access_token,
            expected_subject=subject,
            transport=transport,
        )

    try:
        claims = map_identity(id_claims, userinfo)
    except AuthenticationError as exc:
        raise (SsoUserinfoInvalid if userinfo is not None else SsoIdTokenInvalid) from exc

    # ELSPETH never stores IdP tokens. From here on nothing can read them.
    del tokens

    identity = upsert_identity(claims)
    admit(identity)
    record_login(identity)

    handoff = new_handoff_code()
    handoffs.issue(code_hash=handoff_code_hash(handoff), identity_id=identity.identity_id, request_id=request_id)
    return handoff_location(client.public_base_url, handoff)


# ── complete ─────────────────────────────────────────────────────────────
#
# The browser is back with the handoff code from the fragment. This is the
# only step that mints a session token, and it does so only after the code
# has been consumed — so a token never exists before someone has proven they
# hold the code, and the code cannot be used again once one does.

# token_urlsafe(32) is 43 characters. The bound is generous, but it is a
# bound: the code is hashed as-is, and a hash of a megabyte is work an
# unauthenticated caller should not be able to demand.
_MAX_HANDOFF_CODE_LENGTH: Final = 128


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """What ``complete`` returns to the SPA: the token, and what kind it is."""

    access_token: str = field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"


def complete_login(
    code: str,
    *,
    handoffs: HandoffStore,
    read_identity: Callable[[str], AdmittedIdentity | None],
    issuer: SessionTokenIssuer,
    record_token_issued: Callable[[AdmittedIdentity, str, str], None],
) -> IssuedSession:
    """Trade a handoff code for a session token, or refuse.

    CONSUME FIRST, READ SECOND. The code is claimed by one conditional
    UPDATE before anything else is looked at. Two things follow from that
    order, and both are the point:

    * a code that does not claim never touches the identity table, so the
      response time of an invalid code carries no fact about any identity —
      an attacker guessing codes learns only that the guess failed;
    * a code that DOES claim is spent even if the login is then refused —
      an identity disabled between callback and complete gets
      ``sso_identity_disabled``, and its handoff is gone, not left live for
      a retry after the administrator's decision.

    The admission check runs again here, not only at callback. The spec
    calls this "re-check disabled_at" (§Disable reach [rev2]): the window
    between the two steps is short, but a disable that lands inside it must
    hold, and the only way it holds is to read the row now.

    ``record_token_issued`` runs BEFORE the token is returned, and it is
    given the login's request id from the handoff row. A token whose audit
    row failed to write is a token nobody gets: the handoff is spent, the
    caller sees the failure, and the person logs in again.
    """
    if not code or len(code) > _MAX_HANDOFF_CODE_LENGTH or not code.isprintable():
        raise SsoHandoffInvalid
    consumed = handoffs.consume(code_hash=handoff_code_hash(code))
    if consumed is None:
        raise SsoHandoffInvalid

    identity = read_identity(consumed.identity_id)
    if identity is None:
        # The row is a foreign key of the handoff's, so this is a delete that
        # landed between the claim and this read. The handoff is spent; the
        # caller sees the same refusal as for an unknown code.
        raise SsoHandoffInvalid(detail="This sign-in could not be completed — start the login again")
    admit(identity)

    token = issuer.mint(identity_id=identity.identity_id, username=identity.username)
    record_token_issued(identity, token, consumed.login_request_id)
    return IssuedSession(access_token=token)
