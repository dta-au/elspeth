"""ID-token validation: JWKS discovery, caching, and JWT decode.

Everything here answers ONE question — is this token genuinely from the
configured issuer, for us, and still valid — and nothing here knows what a
session is. It is shared by every OIDC-family provider and by the SSO login
walk in ``auth/sso.py``.

It lives in its own module because ``auth/oidc.py`` is scheduled for deletion
with the legacy provider. The validation is not legacy; only its old housing
was.

ONE DECODE PATH. The accepted signature algorithms are fixed when the
validator is built, from the IdP profile, and every token -- an SSO ID token
or a legacy bearer token -- is checked against that same list. There is no
path that reads the algorithm out of the token's own header: that shape let
the attacker nominate which check their forgery faced (elspeth-e8a9973c37),
and it survived here as the bearer path for a while after the pinned path was
written beside it. Now there is nothing to survive as.
"""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, NoReturn, final

import httpx
import jwt
import structlog
from jwt.exceptions import PyJWTError

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.auth.claims import (
    IdTokenClaims,
    claim_is_exactly_true,
    optional_string_claim,
    required_string_claim,
    string_list_claim,
)
from elspeth.web.auth.models import AuthenticationError, AuthProviderUnavailable
from elspeth.web.auth.urls import validate_oidc_issuer

slog = structlog.get_logger()


# Bounded clock skew between this container and the IdP. Wide enough for
# ordinary NTP drift, narrow enough that a stolen token's usable lifetime is
# not meaningfully extended.
_ID_TOKEN_LEEWAY_SECONDS = 60

# Only asymmetric signatures can be verified against a PUBLISHED key set. An
# HMAC entry would make the JWKS the signing secret, and ``none`` is no
# signature at all; neither is something a profile may declare, so the
# validator refuses the list at construction rather than trusting PyJWT to
# refuse the token later.
_PERMITTED_SIGNATURE_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"})


def _pinned_algorithms(algorithms: tuple[str, ...]) -> tuple[str, ...]:
    """Validate a profile's algorithm list before it becomes the pin."""
    if type(algorithms) is not tuple or not algorithms:
        raise ValueError("algorithms must be a non-empty tuple of signature algorithm names")
    for algorithm in algorithms:
        if algorithm not in _PERMITTED_SIGNATURE_ALGORITHMS:
            raise ValueError(f"algorithm {algorithm!r} is not a permitted asymmetric signature algorithm")
    return algorithms


# A JWK announcing any other key type is refused BEFORE its key material is
# used. "oct" is the dangerous one: a symmetric key published in a JWKS, paired
# with an HMAC algorithm, turns a public document into a signing secret.
_PERMITTED_JWK_KEY_TYPES = frozenset({"RSA", "EC"})


@final
@dataclass(frozen=True, slots=True)
class SigningKeyEntry:
    """One JWKS entry's identity, read at the boundary before its material is parsed."""

    key_id: str | None
    key_type: str | None


@final
@dataclass(frozen=True, slots=True)
class JwkSet:
    """The IdP's published key set, parsed ONCE at the JWKS boundary.

    ``entries`` is what the key-type gate reads and what equality means.
    ``key_set`` is PyJWT's parsed key material and is excluded from
    equality: two fetches of the same document are equal ``JwkSet`` values
    but distinct instances, and the refresh path in :meth:`ensure_jwks`
    deliberately compares by INSTANCE (``is``) -- "the cache is still the
    one I found insufficient" is a question about identity, not content.
    """

    entries: tuple[SigningKeyEntry, ...]
    key_set: jwt.PyJWKSet = field(repr=False, compare=False)


def _numeric_date(value: object, *, name: str) -> int:
    """A NumericDate (RFC 7519 §2): a JSON number, possibly fractional.

    PyJWT compared the whole-second value against the clock; that is what
    is kept. ``bool`` is a JSON boolean, not a number, and is refused.
    """
    if type(value) is int:
        return value
    if type(value) is float:
        return int(value)
    raise AuthenticationError(f"Invalid token: {name} claim is not a number")


def _compared_string(value: object) -> str | None:
    """A claim that is compared, never displayed.

    Anything that is not a string cannot equal the string it is checked
    against, so it is read as absent and the comparison -- the ONE authority
    for that refusal -- says so. No message here, no second check.
    """
    return value if type(value) is str else None


@trust_boundary(
    tier=3,
    source="ID-token payload returned by jwt.decode after signature, issuer, audience and clock verification",
    source_param="payload",
    suppresses=("R1",),
    invariant=(
        "raises AuthenticationError unless iss and sub are non-blank strings, aud is a non-empty string or list of strings, "
        "exp and iat are numbers, and groups/roles are lists when present; nonce and azp are carried only as strings "
        "(anything else reads as absent for the comparison that is their one authority); optional profile claims that "
        "are not visible strings read as None; never coerces a malformed document"
    ),
    test_ref="tests/unit/web/auth/test_id_token_decode.py::TestIdTokenClaimsBoundary::test_a_blank_subject_is_refused",
    test_fingerprint="16f289fa46b57bafff5243d391f56745a817e924b780b616e0aa329cd80e1746",
)
def parse_id_token_claims(payload: dict[str, Any]) -> IdTokenClaims:
    """THE boundary between a verified token and the claims ELSPETH reads.

    PyJWT has verified the signature, issuer, audience and clock by the time
    this runs, and its result is typed ``dict[str, Any]``: every value is
    still whatever the IdP put there. This is the one place that turns that
    into :class:`IdTokenClaims`; nothing downstream sees the dict.

    Membership-then-subscript rather than ``.get()``: this is IdP data, and
    the absent case is a decision worth seeing.
    """

    def claim(name: str) -> object:
        return payload[name] if name in payload else None

    raw_audience = claim("aud")
    audience: str | tuple[str, ...]
    if type(raw_audience) is str and raw_audience:
        audience = raw_audience
    elif type(raw_audience) is list and raw_audience and all(type(entry) is str for entry in raw_audience):
        audience = tuple(raw_audience)
    else:
        raise AuthenticationError("Invalid token: aud claim is not a string or a list of strings")

    claim_names = claim("_claim_names")
    return IdTokenClaims(
        issuer=required_string_claim(claim("iss"), name="iss", document="ID token"),
        subject=required_string_claim(claim("sub"), name="sub", document="ID token"),
        audience=audience,
        issued_at=_numeric_date(claim("iat"), name="iat"),
        expires_at=_numeric_date(claim("exp"), name="exp"),
        nonce=_compared_string(claim("nonce")),
        authorized_party=_compared_string(claim("azp")),
        preferred_username=optional_string_claim(claim("preferred_username")),
        name=optional_string_claim(claim("name")),
        email=optional_string_claim(claim("email")),
        email_verified=claim_is_exactly_true(claim("email_verified")),
        tenant_id=optional_string_claim(claim("tid")),
        hosted_domain=optional_string_claim(claim("hd")),
        cognito_username=optional_string_claim(claim("cognito:username")),
        given_name=optional_string_claim(claim("given_name")),
        family_name=optional_string_claim(claim("family_name")),
        abn=optional_string_claim(claim("abn")),
        groups=string_list_claim(claim("groups"), name="groups"),
        roles=string_list_claim(claim("roles"), name="roles"),
        groups_overage=claim_is_exactly_true(claim("hasgroups")) or (type(claim_names) is dict and "groups" in claim_names),
    )


class _UnknownSigningKeyError(AuthenticationError):
    """Internal signal that cached JWKS did not contain the token's kid."""


class JWKSTokenValidator:
    """JWKS discovery, caching, and JWT decode -- shared by OIDC and Entra."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_cache_ttl_seconds: int = 3600,
        jwks_failure_retry_seconds: int = 300,
        jwks_max_stale_seconds: int = 86_400,
        *,
        algorithms: tuple[str, ...],
        jwks_uri: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer = validate_oidc_issuer(issuer)
        # THE PIN. ``algorithms`` is the profile's declared list
        # (``IdPProfile.id_token_algorithms``) and is the only algorithm
        # list any decode on this validator will ever pass to PyJWT. It is
        # required, not defaulted: a validator built without saying what it
        # accepts is the shape this module exists to refuse.
        self._algorithms = _pinned_algorithms(algorithms)
        # WHERE THE KEYS COME FROM. With ``jwks_uri`` given, every fetch and
        # every key-miss refresh goes to exactly that URL — the one the
        # generalised endpoint policy already validated against the profile's
        # expected origins when the endpoints were resolved. That is what
        # makes "applied on every JWKS refresh" (spec §Endpoint policy) true:
        # the refresh cannot fetch a URL the check never saw.
        #
        # ``None`` selects the legacy self-discovery path, which re-reads the
        # discovery document under its own same-origin rule. It exists only
        # for ``OIDCAuthProvider`` and ``EntraAuthProvider`` and is deleted
        # with them (identity sprint step E); no new caller may rely on it.
        #
        # ``transport`` lets a test stand a fake provider in front of the
        # validator without patching ``httpx``. Production passes nothing.
        self._jwks_uri = jwks_uri
        self._transport = transport
        self._audience = audience
        self._jwks_cache_ttl_seconds = jwks_cache_ttl_seconds
        # 300s default (5 min): JWKS keys rotate on the order of hours
        # to days, so serving stale keys for up to 5 minutes is safer
        # than forcing concurrent auth requests through a blocked
        # httpx.get to a dead IdP. Lower values amplify the per-retry
        # partial DoS described in elspeth-32982f17cf.
        self._jwks_failure_retry_seconds = jwks_failure_retry_seconds
        # Absolute upper bound on cached-key authority. Failure retries move
        # ``_next_refresh_at`` but must never renew this lifetime; only a
        # fully validated successful fetch resets ``_jwks_last_success_at``.
        self._jwks_max_stale_seconds = jwks_max_stale_seconds
        self._jwks: JwkSet | None = None
        self._jwks_last_success_at: float | None = None
        self._jwks_refresh_failed = False
        # Unknown token key IDs may force one refresh before the normal TTL,
        # but cannot turn repeated invalid tokens into unbounded IdP traffic.
        self._next_key_miss_refresh_at: float = 0.0
        # Separate "when should we try to refresh next" from "when did we
        # last succeed." A successful fetch sets this to now+ttl; a failure
        # that serves stale cache sets this to now+failure_retry so concurrent
        # auth requests during an IdP outage don't all queue behind the lock
        # re-hitting a dead IdP.
        self._next_refresh_at: float = 0.0
        self._jwks_lock = asyncio.Lock()

    def _cached_jwks_within_max_stale_age(self, now: float) -> bool:
        """Return whether cached keys still have authority at ``now``."""
        if self._jwks is None or self._jwks_last_success_at is None:
            return False
        age = now - self._jwks_last_success_at
        return 0 <= age < self._jwks_max_stale_seconds

    @staticmethod
    def _raise_max_stale_age_exceeded() -> NoReturn:
        """Fail closed without exposing IdP payloads or cache timestamps."""
        raise AuthProviderUnavailable("JWKS unavailable (cached keys exceeded maximum stale age)")

    @trust_boundary(
        tier=3,
        source="OIDC discovery document JSON fetched from the IdP's .well-known/openid-configuration endpoint",
        source_param="discovery",
        suppresses=("R1",),
        invariant="raises AuthenticationError on non-dict or missing/blank 'jwks_uri'; never coerces a malformed document",
        test_ref="tests/unit/web/auth/test_id_token_decode.py::TestJWKSValidatorBoundaryRaises::test_validate_discovery_document_non_dict_raises",
        test_fingerprint="4f5119780912c2aede7e67abeea5e6401c5b76d3eccfcd68798ea773105c812e",
    )
    def _validate_discovery_document(self, discovery: Any) -> str:
        """Shape-validate the OIDC discovery document and return jwks_uri.

        Tier 3 boundary: an IdP (or a misbehaving proxy in front of one)
        can return JSON-valid payloads with the wrong top-level shape.
        Reject them at the boundary as ``AuthenticationError`` rather
        than letting ``TypeError``/``KeyError`` escape as HTTP 500.
        """
        if type(discovery) is not dict:
            raise AuthenticationError(f"OIDC discovery document is not a JSON object (got {type(discovery).__name__})")
        discovery_issuer = discovery["issuer"] if "issuer" in discovery else None
        if type(discovery_issuer) is not str or discovery_issuer != self._issuer:
            raise AuthenticationError("OIDC discovery document failed exact issuer check")
        jwks_uri = discovery["jwks_uri"] if "jwks_uri" in discovery else None
        if type(jwks_uri) is not str or not jwks_uri.strip():
            raise AuthenticationError("OIDC discovery document missing non-empty string 'jwks_uri'")
        return self._validate_jwks_uri_policy(jwks_uri)

    def _validate_jwks_uri_policy(self, jwks_uri: str) -> str:
        """Validate discovery-provided JWKS URL before fetching it."""
        try:
            issuer_url = httpx.URL(self._issuer)
            jwks_url = httpx.URL(jwks_uri)
        except httpx.InvalidURL as exc:
            raise AuthenticationError("OIDC discovery document 'jwks_uri' must be a valid URL") from exc

        if jwks_url.scheme != "https":
            raise AuthenticationError("OIDC discovery document 'jwks_uri' must be an HTTPS URL")
        if jwks_url.userinfo:
            raise AuthenticationError("OIDC discovery document 'jwks_uri' must not include embedded credentials")

        issuer_origin = (issuer_url.scheme, issuer_url.host, issuer_url.port)
        jwks_origin = (jwks_url.scheme, jwks_url.host, jwks_url.port)
        if jwks_origin != issuer_origin:
            raise AuthenticationError("OIDC discovery document 'jwks_uri' must use the same origin as issuer")

        return jwks_uri

    @staticmethod
    @trust_boundary(
        tier=3,
        source="JWKS document JSON fetched from the IdP's jwks_uri endpoint",
        source_param="jwks",
        suppresses=("R1",),
        invariant=(
            "raises AuthenticationError on non-dict, missing 'keys' list, or key entries PyJWT cannot parse; "
            "returns an owned JwkSet whose entries carry each key's kid and kty; never coerces a malformed document"
        ),
        test_ref="tests/unit/web/auth/test_id_token_decode.py::TestJWKSValidatorBoundaryRaises::test_validate_jwks_document_missing_keys_raises",
        test_fingerprint="c06b1f0b8c04a6b33dd5e1b3bec1da3752bc6081c170ae076aa175f20893e09d",
    )
    def _validate_jwks_document(jwks: Any) -> JwkSet:
        """THE JWKS boundary: the IdP's document in, an owned :class:`JwkSet` out.

        Called BEFORE caching so a malformed response cannot poison
        ``self._jwks`` for the TTL window. Each entry's ``kid`` and ``kty``
        are read here, once, so the key-type gate in ``_decode`` reads a
        typed entry rather than the document. An entry whose ``kid`` is
        present but not a string can never be named by a token header and
        is not carried; PyJWT skips it for the same reason.
        """
        if type(jwks) is not dict:
            raise AuthenticationError(f"JWKS document is not a JSON object (got {type(jwks).__name__})")
        keys = jwks["keys"] if "keys" in jwks else None
        if type(keys) is not list:
            raise AuthenticationError("JWKS document missing 'keys' list")
        entries: list[SigningKeyEntry] = []
        for raw_key in keys:
            if type(raw_key) is not dict:
                continue
            raw_kid = raw_key["kid"] if "kid" in raw_key else None
            if raw_kid is not None and type(raw_kid) is not str:
                continue
            raw_kty = raw_key["kty"] if "kty" in raw_key else None
            entries.append(SigningKeyEntry(key_id=raw_kid, key_type=raw_kty if type(raw_kty) is str else None))
        try:
            key_set = jwt.PyJWKSet.from_dict(jwks)
        except (PyJWTError, AttributeError, TypeError, ValueError) as exc:
            raise AuthenticationError(f"JWKS document contains unusable key entries: {type(exc).__name__}") from exc
        return JwkSet(entries=tuple(entries), key_set=key_set)

    async def ensure_jwks(
        self,
        *,
        refresh_if_unchanged: JwkSet | None = None,
    ) -> JwkSet:
        """Fetch and cache JWKS keys from the OIDC discovery endpoint.

        Uses double-checked locking to prevent thundering herd at TTL
        boundary. On fetch failure, serves stale cache only within
        ``jwks_max_stale_seconds`` of the last successful fetch and advances
        the refresh horizon by ``jwks_failure_retry_seconds``
        so concurrent auth requests during an IdP outage don't all queue
        behind the lock re-hitting a dead IdP. (JWKS keys are long-lived;
        stale keys during a transient IdP blip are safer than a hard
        auth outage.)

        Followers short-circuit when a refresh is already in flight:
        if stale cache is populated and the refresh lock is held, return
        stale immediately rather than queue behind a blocked ``httpx.get``.
        Only the single lock-holder pays the network cost per retry
        window — see elspeth-32982f17cf for the partial-DoS this
        prevents.

        **Cold-start throttle:** with no cache, the stale-serve bypasses
        cannot fire (they all gate on ``self._jwks is not None``). If the
        IdP is down at cold start, every concurrent auth request would
        otherwise serialize on the refresh lock and hit the httpx timeout
        in turn. The cold-start throttle — advancing ``_next_refresh_at``
        unconditionally on fetch failure and short-circuiting requests
        while ``self._jwks is None and now < self._next_refresh_at`` —
        means only the first request per retry window pays the network
        cost, and the rest fail fast with 503 until the horizon passes.

        ``refresh_if_unchanged`` requests one forced refresh after a token
        references an unknown signing key. These callers wait on the normal
        refresh lock because the cached keys are known to be insufficient.
        After acquiring the lock, a caller reuses any cache replacement made
        by the winner instead of fetching again. Successful key-miss refreshes
        also use the failure-retry interval as a short cooldown, preventing
        repeated invalid key IDs from amplifying IdP traffic.
        """
        now = time.monotonic()
        force_refresh = refresh_if_unchanged is not None
        if not force_refresh and self._jwks is not None and now < self._next_refresh_at:
            if self._cached_jwks_within_max_stale_age(now):
                return self._jwks
            # A failure retry window remains load-bearing even after cached
            # keys lose authority: fail closed until the retry horizon rather
            # than re-hitting a dead IdP on every request. If this is merely a
            # cache TTL longer than the configured hard age, fall through and
            # refresh now.
            if self._jwks_refresh_failed:
                self._raise_max_stale_age_exceeded()

        # Cold-start throttle fast-path: a prior fetch failed within the
        # current retry window AND we have no cache to serve. Fail fast
        # BEFORE touching the lock so cold-start traffic during an IdP
        # outage is shed without queueing. The ``_next_refresh_at``
        # timestamp is the single source of truth for "are we in a
        # throttle window" — see the failure branches below for where
        # it is advanced on both network and shape failures.
        if not force_refresh and self._jwks is None and now < self._next_refresh_at:
            raise AuthProviderUnavailable("JWKS unavailable (cold-start fetch failed, retry throttled)")

        # Lock-decoupled stale-serve: if another coroutine is already
        # attempting a refresh and we have a cached (possibly stale) JWKS,
        # return it without waiting on the lock. This prevents concurrent
        # auth requests from serializing behind a dead IdP fetch (up to
        # the httpx 15s timeout worst case). The ``locked()`` check is
        # best-effort: if the lock is released between the check and our
        # acquire call, we fall through to the normal double-checked
        # locking path and the re-check inside the lock is authoritative.
        if not force_refresh and self._jwks is not None and self._jwks_lock.locked():
            if self._cached_jwks_within_max_stale_age(now):
                return self._jwks
            self._raise_max_stale_age_exceeded()

        async with self._jwks_lock:
            # Re-check inside lock (another coroutine may have refreshed)
            now = time.monotonic()
            if refresh_if_unchanged is not None:
                current_jwks = self._jwks
                if current_jwks is None:
                    raise AuthProviderUnavailable("JWKS unavailable (cache cleared during refresh)")
                if current_jwks is not refresh_if_unchanged:
                    return current_jwks
                # A failed key-miss refresh uses the existing retry horizon.
                # Followers share the failed attempt and retry decode with the
                # same stale cache rather than serially re-hitting the IdP.
                if self._jwks_refresh_failed and now < self._next_refresh_at:
                    if self._cached_jwks_within_max_stale_age(now):
                        return current_jwks
                    self._raise_max_stale_age_exceeded()
                if now < self._next_key_miss_refresh_at and self._cached_jwks_within_max_stale_age(now):
                    return current_jwks
            elif self._jwks is not None and now < self._next_refresh_at:
                if self._cached_jwks_within_max_stale_age(now):
                    return self._jwks
                if self._jwks_refresh_failed:
                    self._raise_max_stale_age_exceeded()

            # Cold-start throttle inside lock: another coroutine's fetch
            # may have failed while we were queued on the lock. Repeat
            # the fail-fast check here so lock-queued cold-start requests
            # don't re-hit the dead IdP when the first coroutine releases
            # the lock after raising.
            if not force_refresh and self._jwks is None and now < self._next_refresh_at:
                raise AuthProviderUnavailable("JWKS unavailable (cold-start fetch failed, retry throttled)")

            stale_jwks = self._jwks
            try:
                # REDIRECTS ARE DISABLED, explicitly. A JWKS fetch that
                # follows one verifies every signature against whoever the
                # redirect names. httpx does not follow by default, and the
                # flag is passed so that enabling it is a visible edit.
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, connect=5.0),
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    jwks_uri = self._jwks_uri
                    if jwks_uri is None:
                        # Legacy self-discovery; see __init__. Dies in step E.
                        discovery_url = f"{self._issuer}/.well-known/openid-configuration"
                        discovery_resp = await client.get(discovery_url)
                        discovery_resp.raise_for_status()
                        jwks_uri = self._validate_discovery_document(discovery_resp.json())

                    jwks_resp = await client.get(jwks_uri)
                    jwks_resp.raise_for_status()
                    # Shape-validate BEFORE assigning to cache: a wrong-shaped
                    # response must not poison self._jwks.
                    validated = self._validate_jwks_document(jwks_resp.json())
                    success_at = time.monotonic()
                    self._jwks = validated
                    self._jwks_last_success_at = success_at
                    self._jwks_refresh_failed = False
                    self._next_refresh_at = success_at + self._jwks_cache_ttl_seconds
                    if refresh_if_unchanged is not None:
                        self._next_key_miss_refresh_at = success_at + self._jwks_failure_retry_seconds
            except AuthenticationError:
                # Shape-validation failure — advance the refresh horizon
                # by ``_jwks_failure_retry_seconds`` (the same throttle the
                # network-failure branch below applies) BEFORE re-raising.
                #
                # Why throttle here too: without the horizon advance, a
                # malformed-JSON outage at the IdP causes every concurrent
                # auth request in the critical section to re-hit the IdP —
                # the partial-DoS vector elspeth-32982f17cf closed for
                # network errors.  Shape-failure is functionally
                # indistinguishable at this layer (reachable IdP, bad
                # payload); the thundering herd is identical.
                #
                # Why we still re-raise (no stale-cache return on this
                # path): the CURRENT caller — who triggered the validator
                # — gets a clean 401 so an unrecoverable misconfiguration
                # (IdP rotated its document schema, corrupt reverse proxy,
                # etc.) surfaces as an auth failure rather than a silent
                # fallback.  Subsequent callers within the throttle window
                # short-circuit at the top of ``ensure_jwks`` via the
                # ``self._jwks is not None and now < self._next_refresh_at``
                # gate and receive the previously-validated cached keys —
                # symmetric with the network-failure branch's stale-serve
                # semantics, where only the first caller per window pays
                # the cost of discovering the outage.  If cache is empty
                # (shape failure during bootstrap), the top-of-function
                # gate does not trigger and the window only throttles the
                # single-caller path; every caller still gets 401 until
                # the IdP returns valid JSON.
                # Advance the horizon UNCONDITIONALLY (both warm and
                # cold-start paths). Without this, a cold-start shape
                # failure leaves ``_next_refresh_at`` at 0 and every
                # queued coroutine re-hits the malformed IdP in
                # succession. With the cold-start throttle fast-paths
                # above, setting the horizon here lets all subsequent
                # callers in the retry window fail fast at the top of
                # ``ensure_jwks`` with a clean 401.
                failure_at = time.monotonic()
                self._jwks_refresh_failed = True
                self._next_refresh_at = failure_at + self._jwks_failure_retry_seconds
                slog.debug(
                    "JWKS shape validation failed; throttling refresh",
                    issuer=self._issuer,
                    has_stale_cache=stale_jwks is not None,
                    next_refresh_in_seconds=self._jwks_failure_retry_seconds,
                )
                if stale_jwks is not None and not self._cached_jwks_within_max_stale_age(failure_at):
                    self._raise_max_stale_age_exceeded()
                raise
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
                # Narrowed from the historical (HTTPError, KeyError, ValueError,
                # TypeError, AttributeError) catch so that programmer-bug
                # exceptions no longer launder into a stale-cache fallback.
                #
                # After the shape validators (_validate_discovery_document,
                # _validate_jwks_document) were added, IdP payload access at
                # this Tier 3 boundary cannot produce KeyError / TypeError /
                # AttributeError on the happy path — those shapes are
                # rejected upstream as AuthenticationError. Anything in
                # those classes reaching this catch would therefore be a
                # bug in the surrounding try block, and suppressing it to
                # serve stale keys would produce a confident-but-wrong
                # auth decision (CLAUDE.md's "silent wrong result is worse
                # than a crash" rule).
                #
                # The remaining catches preserve the legitimate Tier 3
                # failure modes that must serve stale cache:
                #   - httpx.HTTPError: connect/read timeouts, HTTP 5xx from
                #     the IdP, transport errors. Base class of
                #     RequestError / TransportError / ConnectError /
                #     TimeoutException / HTTPStatusError (raised by
                #     response.raise_for_status()).
                #   - httpx.InvalidURL: explicitly named because it sits
                #     OUTSIDE the HTTPError hierarchy (direct Exception
                #     subclass). Fires when jwks_uri is a non-empty string
                #     but not a parseable URL — the shape validator only
                #     checks the string-ness, not URL syntax, so the IdP
                #     can still feed us junk here.
                #   - ValueError: covers json.JSONDecodeError and
                #     UnicodeDecodeError from response.json() when the
                #     IdP returns non-JSON or mis-encoded bytes.
                # Advance the horizon UNCONDITIONALLY (both stale-serve
                # and cold-start paths). The original code only advanced
                # when ``stale_jwks is not None`` — cold-start outages
                # therefore left ``_next_refresh_at`` at 0 and every
                # concurrent auth request serialized on ``self._jwks_lock``
                # through a full httpx timeout apiece, which is the
                # documented-but-live DoS vector the cold-start throttle
                # (above) was added to close. Writing the horizon here is
                # the same source-of-truth update that makes the
                # fast-paths at the top of ``ensure_jwks`` fire.
                failure_at = time.monotonic()
                self._jwks_refresh_failed = True
                self._next_refresh_at = failure_at + self._jwks_failure_retry_seconds
                if stale_jwks is not None and self._cached_jwks_within_max_stale_age(failure_at):
                    # Serve stale cache -- JWKS keys are long-lived
                    slog.debug(
                        "JWKS fetch failed, serving stale cache",
                        issuer=self._issuer,
                        exc_class=type(exc).__name__,
                        next_refresh_in_seconds=self._jwks_failure_retry_seconds,
                    )
                    return stale_jwks
                if stale_jwks is not None:
                    slog.debug(
                        "JWKS fetch failed after cached keys exceeded maximum stale age",
                        issuer=self._issuer,
                        exc_class=type(exc).__name__,
                        max_stale_seconds=self._jwks_max_stale_seconds,
                        next_refresh_in_seconds=self._jwks_failure_retry_seconds,
                    )
                    self._raise_max_stale_age_exceeded()
                slog.debug(
                    "JWKS cold-start fetch failed; throttling retry",
                    issuer=self._issuer,
                    exc_class=type(exc).__name__,
                    next_refresh_in_seconds=self._jwks_failure_retry_seconds,
                )
                # Class name only. ``str(exc)`` on httpx.InvalidURL carries
                # the raw jwks_uri (Tier-3 IdP-provided string), and
                # httpx.ConnectError can include the resolved IP of the IdP.
                # ``AuthProviderUnavailable.detail`` flows verbatim into the 503
                # response body via auth middleware, so payload-free text is
                # the only safe channel here. Symmetric with the Tier-1
                # redaction discipline applied to _handle_plugin_crash
                # (routes.py) and the blob/plugin SQLAlchemyError sites.
                raise AuthProviderUnavailable(f"JWKS unavailable: {type(exc).__name__}") from exc

        return self._jwks

    def decode_token(self, token: str, jwks: JwkSet) -> IdTokenClaims:
        """Decode a bearer token presented on an API call (legacy providers).

        Same core as :meth:`decode_id_token`; the difference is the caller's
        situation, not the checks. A bearer token arrives on a request the
        server did not start: there was no authorization redirect from here,
        so there is no server-held nonce for the token to be bound to.
        ``nonce=None`` states that fact at the call site rather than hiding
        it in a default -- the core has no default, so every caller says
        which case it is in. Audience and authorized party are the
        configured audience: for these providers the audience IS our client
        id, which is what ``azp`` must name on a multi-audience token.
        """
        return self._decode(token, jwks, audience=self._audience, nonce=None, client_id=self._audience)

    @staticmethod
    @trust_boundary(
        tier=3,
        source="Unverified JWT header decoded from the externally-supplied token (its 'kid' field)",
        source_param="header",
        suppresses=("R1",),
        invariant="returns None when 'kid' is absent and the value when it is a string; raises AuthenticationError on a present non-string 'kid'; never coerces",
        test_ref="tests/unit/web/auth/test_id_token_decode.py::TestHeaderKeyIdBoundary::test_a_non_string_kid_is_refused",
        test_fingerprint="94ee285aeb60b09fdb1ba641aee6d0adb046294d8d45147676366cd07e5aa231",
    )
    def _header_key_id(header: dict[str, Any]) -> str | None:
        """The key id the token names, read before anything about it is trusted.

        ``kid`` is optional per RFC 7515 and, when present, a string. Absent
        matches only a JWK without one. Anything else names no key and is
        refused here rather than compared against whatever type the JWKS
        happened to carry in the same field.
        """
        kid = header["kid"] if "kid" in header else None
        if kid is None:
            return None
        if type(kid) is not str:
            raise AuthenticationError("Invalid token: header key id is not a string")
        return kid

    @staticmethod
    def _assert_supported_key_type(jwks: JwkSet, *, kid: str | None) -> None:
        """Refuse a matched JWK whose key type is not RSA or EC.

        Checked BEFORE the key material is used. A JWKS is a public
        document, so an ``oct`` entry paired with an HMAC algorithm would let
        anyone who can read it forge a token; pinning the algorithm list
        already blocks that, and this closes the same door from the other
        side.
        """
        for entry in jwks.entries:
            if entry.key_id != kid:
                continue
            if entry.key_type not in _PERMITTED_JWK_KEY_TYPES:
                raise AuthenticationError("Invalid token: JWKS key type is not permitted")
            return

    @staticmethod
    def _verify_nonce(claims: IdTokenClaims, *, expected: str) -> None:
        """Bind the token to THIS login attempt, in constant time.

        The nonce is the only thing tying an ID token to the browser
        transaction that asked for it; without this check a token replayed
        from another session validates perfectly. Compared as bytes:
        ``compare_digest`` on ``str`` raises for non-ASCII, and a token is
        the wrong place to let an attacker choose between refusal and crash.
        """
        actual = claims.nonce
        if actual is None or not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
            raise AuthenticationError("Invalid token: nonce check failed")

    @staticmethod
    def _verify_authorized_party(claims: IdTokenClaims, *, client_id: str) -> None:
        """When ``aud`` is a list, ``azp`` must name us.

        A multi-audience token is addressed to several relying parties at
        once. Accepting one because our client id appears somewhere in the
        list would let a token minted for a different application be
        presented here; ``azp`` is the claim that says which party it was
        actually issued to.
        """
        if type(claims.audience) is str:
            return
        authorized_party = claims.authorized_party
        if authorized_party is None or not hmac.compare_digest(authorized_party.encode("utf-8"), client_id.encode("utf-8")):
            raise AuthenticationError("Invalid token: authorized party check failed")

    def decode_id_token(
        self,
        token: str,
        jwks: JwkSet,
        *,
        audience: str,
        nonce: str,
        client_id: str,
    ) -> IdTokenClaims:
        """Decode an ID token from an SSO login.

        The nonce is REQUIRED here and is the transaction's: the sealed
        cookie carried it out to the IdP and back, and a token without it,
        or with any other value, was not minted for this login attempt.
        An empty nonce is a caller defect, not a token defect, and is
        refused before any token is looked at.
        """
        if not nonce:
            raise ValueError("decode_id_token requires the login transaction's nonce")
        return self._decode(token, jwks, audience=audience, nonce=nonce, client_id=client_id)

    def _decode(
        self,
        token: str,
        jwks: JwkSet,
        *,
        audience: str,
        nonce: str | None,
        client_id: str,
    ) -> IdTokenClaims:
        """The one decode. Profile-pinned algorithms; no header-driven branch.

        ``exp iat iss sub aud`` are always required -- a token missing any
        of them is refused rather than defaulted. The checks PyJWT cannot
        express run after decode, in constant time: ``azp`` for a list
        audience always, and the nonce whenever the caller holds one.
        :meth:`_verify_nonce` is the ONE authority for the nonce -- absent,
        non-string and mismatched are all its refusal -- so it is not also
        listed as a required claim; a second check that the first makes
        unreachable is a check nobody can see fail.

        ``nonce`` has no default on purpose: ``None`` is a fact only the
        caller knows (see :meth:`decode_token`), and a core that defaulted
        it would let a new SSO caller forget the binding silently.
        """
        try:
            header = jwt.get_unverified_header(token)
            kid = self._header_key_id(header)
            self._assert_supported_key_type(jwks, kid=kid)
            matched_jwk = None
            for key in jwks.key_set.keys:
                if key.key_id == kid:
                    matched_jwk = key
                    break
            if matched_jwk is None:
                raise _UnknownSigningKeyError("Invalid token: signing key check failed")
            payload = jwt.decode(
                token,
                matched_jwk.key,
                algorithms=list(self._algorithms),
                audience=audience,
                issuer=self._issuer,
                leeway=_ID_TOKEN_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except PyJWTError as exc:
            # Class name only: PyJWT messages echo claim values and token
            # segments, which AuthenticationError would surface into a 401 body.
            raise AuthenticationError(f"Invalid token: {type(exc).__name__}") from exc
        claims = parse_id_token_claims(payload)
        if nonce is not None:
            self._verify_nonce(claims, expected=nonce)
        self._verify_authorized_party(claims, client_id=client_id)
        return claims

    async def decode_id_token_with_refresh(
        self,
        token: str,
        *,
        audience: str,
        nonce: str,
        client_id: str,
    ) -> IdTokenClaims:
        """``decode_id_token``, refreshing JWKS once on an unknown key."""
        jwks = await self.ensure_jwks()
        try:
            return self.decode_id_token(token, jwks, audience=audience, nonce=nonce, client_id=client_id)
        except _UnknownSigningKeyError:
            refreshed_jwks = await self.ensure_jwks(refresh_if_unchanged=jwks)
            return self.decode_id_token(token, refreshed_jwks, audience=audience, nonce=nonce, client_id=client_id)

    async def decode_token_with_refresh(self, token: str) -> IdTokenClaims:
        """Decode a token, refreshing JWKS once if its signing key is unknown."""
        jwks = await self.ensure_jwks()
        try:
            return self.decode_token(token, jwks)
        except _UnknownSigningKeyError:
            refreshed_jwks = await self.ensure_jwks(refresh_if_unchanged=jwks)
            return self.decode_token(token, refreshed_jwks)
