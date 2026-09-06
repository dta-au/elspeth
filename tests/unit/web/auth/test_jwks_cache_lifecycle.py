"""JWKS cache lifecycle: refresh, throttling, staleness bounds and redaction.

These behaviours belong to ``JWKSTokenValidator`` and are live: the SSO walk
holds one validator per deployment and every login decodes through it. They
were written against ``OIDCAuthProvider.authenticate`` only because that was
the entry point that existed at the time, and they moved here when identity
sprint step E deleted that provider. Nothing about the SUBJECT changed --
these are the same guarantees, driven through the live entry points
(``ensure_jwks`` and ``decode_id_token_with_refresh``).

What they protect, and why each is worth a test:

* **Throttling.** An IdP outage must not turn every in-flight login into its
  own request to a dead IdP (elspeth-32982f17cf, elspeth-7f262cf7e1). Serving
  a stale cache without advancing the refresh horizon is the shape that
  produced the partial DoS, so the tests count network attempts, not results.
* **A bound on stale authority.** Failure retries move the next-attempt time;
  they must never renew how long keys stay usable. Otherwise a long outage
  silently converts "cached keys" into "keys with no expiry".
* **Cache integrity.** A malformed document must be refused BEFORE it reaches
  the cache: a poisoned cache outlives the response that poisoned it.
* **Honest failures.** A programmer bug inside the fetch block (an
  ``AttributeError``, a ``TypeError``, a ``KeyError``) must propagate rather
  than be laundered into "the IdP is down, serve stale" -- that reading hides
  our own defects behind someone else's outage.
* **Redaction.** A JWKS failure log records the exception CLASS, never its
  message, which can carry IdP-side detail.

Failure is injected through the validator's ``transport`` seam rather than by
patching ``httpx.AsyncClient``: the seam is what production leaves open for
exactly this, and a test that patches the client tests the patch as much as
the code.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from structlog.testing import capture_logs

from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthenticationError, AuthProviderUnavailable
from tests.unit.web.auth.conftest import build_rsa_jwk, make_rs256_token

pytestmark = pytest.mark.asyncio

ISSUER = "https://login.example.com"
AUDIENCE = "my-app-client-id"
JWKS_URI = f"{ISSUER}/keys"
NONCE = "nonce-from-the-sealed-cookie"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-123",
        "preferred_username": "alice",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "nonce": NONCE,
    }
    claims.update(overrides)
    return claims


def _jwks_with_kid(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, Any]:
    """A one-key JWKS carrying an explicit rotation identifier."""
    jwks = build_rsa_jwk(public_key)
    keys = jwks["keys"]
    assert isinstance(keys, list)
    key = keys[0]
    assert isinstance(key, dict)
    key["kid"] = kid
    return jwks


def _token_with_kid(private_key: rsa.RSAPrivateKey, kid: str) -> str:
    return jwt.encode(_claims(), private_key, algorithm="RS256", headers={"kid": kid})


class _Idp:
    """A JWKS endpoint under the test's control, counting every request.

    ``document`` is re-read on each request so a test can rotate keys, and
    ``failure`` short-circuits the response so an outage needs no separate
    transport.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.failure: BaseException | None = None
        self.body: Any = None
        self.status = 200
        self.requests = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests += 1
            if self.failure is not None:
                raise self.failure
            if self.status != 200:
                return httpx.Response(self.status, json={})
            payload = self.document if self.body is None else self.body
            if isinstance(payload, str):
                return httpx.Response(200, content=payload.encode("utf-8"))
            return httpx.Response(200, json=payload)

        return httpx.MockTransport(handler)


def _validator(idp: _Idp, **kwargs: Any) -> JWKSTokenValidator:
    return JWKSTokenValidator(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        jwks_uri=JWKS_URI,
        transport=idp.transport(),
        **kwargs,
    )


async def _decode(validator: JWKSTokenValidator, token: str) -> Any:
    return await validator.decode_id_token_with_refresh(token, audience=AUDIENCE, nonce=NONCE, client_id=AUDIENCE)


class TestFetchFailures:
    """What the validator does when the key endpoint does not answer."""

    async def test_a_cold_start_outage_is_provider_unavailable_not_an_auth_failure(self, jwks_response) -> None:
        """No cached keys and no IdP is OUR unavailability, not a bad token.

        The distinction reaches the caller as 503 rather than 401, so a
        monitored outage does not read as a wave of rejected users.
        """
        idp = _Idp(jwks_response)
        idp.failure = httpx.ConnectError("Connection refused")
        with pytest.raises(AuthProviderUnavailable, match="JWKS unavailable"):
            await _validator(idp).ensure_jwks()

    async def test_an_http_error_status_is_provider_unavailable(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        idp.status = 500
        with pytest.raises(AuthProviderUnavailable, match="JWKS unavailable"):
            await _validator(idp).ensure_jwks()

    async def test_an_expired_cache_is_refetched(self, jwks_response) -> None:
        """TTL=0 makes every call past due, so each one goes to the IdP."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        await validator.ensure_jwks()
        await validator.ensure_jwks()
        assert idp.requests == 2

    async def test_a_failed_refresh_serves_the_keys_already_held(self, jwks_response) -> None:
        """An outage after a good fetch must not deny logins that could work."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        first = await validator.ensure_jwks()
        idp.failure = httpx.ConnectError("IdP is down")
        assert await validator.ensure_jwks() == first


class TestDocumentShape:
    """A malformed document is refused before it can reach the cache."""

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ([{"kty": "RSA"}], "JWKS"),
            ({"no_keys": []}, "JWKS"),
            ({"keys": "not-a-list"}, "JWKS"),
            ({"keys": [{"kty": "RSA", "n": None}]}, "JWKS"),
            ({"keys": [{}]}, "JWKS"),
        ],
        ids=["json-array", "missing-keys", "keys-not-a-list", "malformed-inner-key", "empty-inner-key"],
    )
    async def test_a_malformed_document_is_refused(self, jwks_response, body: Any, match: str) -> None:
        idp = _Idp(jwks_response)
        idp.body = body
        with pytest.raises(AuthenticationError, match=match):
            await _validator(idp).ensure_jwks()

    async def test_a_malformed_refresh_does_not_poison_a_good_cache(self, jwks_response) -> None:
        """The cache outlives the response, so it must never accept one blind."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        good = await validator.ensure_jwks()
        idp.body = {"keys": "not-a-list"}
        with pytest.raises(AuthenticationError):
            await validator.ensure_jwks()
        idp.body = None
        idp.failure = httpx.ConnectError("IdP is down")
        assert await validator.ensure_jwks() == good


class TestRefetchThrottling:
    """An outage costs ONE request per retry window, not one per login."""

    async def test_a_served_stale_cache_throttles_the_next_attempt(self, jwks_response) -> None:
        """Regression: elspeth-7f262cf7e1. Count attempts, not outcomes."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=60)
        await validator.ensure_jwks()
        idp.failure = httpx.ConnectError("IdP is down")
        idp.requests = 0

        for _ in range(3):
            await validator.ensure_jwks()

        assert idp.requests == 1, (
            f"expected 1 IdP fetch inside the backoff window, got {idp.requests}: "
            "serving a stale cache is not advancing the refresh horizon"
        )

    async def test_the_window_elapsing_allows_a_fresh_attempt(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=60)
        await validator.ensure_jwks()
        idp.failure = httpx.ConnectError("IdP is down")
        idp.requests = 0

        await validator.ensure_jwks()
        validator._next_refresh_at = time.monotonic() - 1
        await validator.ensure_jwks()

        assert idp.requests == 2

    async def test_a_shape_failure_throttles_like_a_network_failure(self, jwks_response) -> None:
        """A reachable IdP serving nonsense is the same thundering herd."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=60)
        await validator.ensure_jwks()
        idp.body = {"keys": "not-a-list"}
        idp.requests = 0

        with pytest.raises(AuthenticationError):
            await validator.ensure_jwks()
        await validator.ensure_jwks()

        assert idp.requests == 1

    async def test_a_cold_start_outage_throttles_too(self, jwks_response) -> None:
        """With no cache to serve, the throttle is all that bounds the load."""
        idp = _Idp(jwks_response)
        idp.failure = httpx.ConnectError("IdP is down")
        validator = _validator(idp, jwks_failure_retry_seconds=60)

        for _ in range(3):
            with pytest.raises(AuthProviderUnavailable):
                await validator.ensure_jwks()

        assert idp.requests == 1

    async def test_a_cold_start_window_elapsing_allows_a_fresh_attempt(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        idp.failure = httpx.ConnectError("IdP is down")
        validator = _validator(idp, jwks_failure_retry_seconds=60)

        with pytest.raises(AuthProviderUnavailable):
            await validator.ensure_jwks()
        validator._next_refresh_at = time.monotonic() - 1
        with pytest.raises(AuthProviderUnavailable):
            await validator.ensure_jwks()

        assert idp.requests == 2

    async def test_concurrent_cold_start_requests_share_one_attempt(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        idp.failure = httpx.ConnectError("IdP is down")
        validator = _validator(idp, jwks_failure_retry_seconds=60)

        results = await asyncio.gather(
            *(validator.ensure_jwks() for _ in range(5)),
            return_exceptions=True,
        )

        assert all(isinstance(result, AuthProviderUnavailable) for result in results)
        assert idp.requests == 1


class TestStaleAuthorityIsBounded:
    """Cached keys have a lifetime that a failing refresh cannot renew."""

    async def test_the_retry_horizon_cannot_extend_the_absolute_age(self, jwks_response) -> None:
        """The bug this forbids: an outage making stale keys permanent."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=1, jwks_max_stale_seconds=60)
        await validator.ensure_jwks()
        idp.failure = httpx.ConnectError("IdP is down")

        validator._jwks_last_success_at = time.monotonic() - 61
        with pytest.raises(AuthProviderUnavailable, match="maximum stale age"):
            await validator.ensure_jwks()

    async def test_only_a_validated_success_starts_a_new_lifetime(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_max_stale_seconds=60)
        await validator.ensure_jwks()
        validator._jwks_last_success_at = time.monotonic() - 59

        await validator.ensure_jwks()

        validator._jwks_last_success_at = time.monotonic() - 59
        idp.failure = httpx.ConnectError("IdP is down")
        assert await validator.ensure_jwks() is not None

    async def test_a_shape_failure_past_the_hard_age_is_unavailable_not_unauthorized(self, jwks_response) -> None:
        """Hard-expired keys cannot be left usable, and this is not a 401."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_max_stale_seconds=60)
        await validator.ensure_jwks()
        idp.body = {"keys": "not-a-list"}
        validator._jwks_last_success_at = time.monotonic() - 61

        with pytest.raises(AuthProviderUnavailable):
            await validator.ensure_jwks()

    async def test_a_healthy_idp_is_refreshed_at_the_hard_age_even_under_a_longer_ttl(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=3600, jwks_max_stale_seconds=10)
        await validator.ensure_jwks()
        idp.requests = 0

        validator._jwks_last_success_at = time.monotonic() - 11
        await validator.ensure_jwks()

        assert idp.requests == 1

    async def test_a_hard_expired_follower_does_not_short_circuit_past_the_ceiling(self, jwks_response) -> None:
        """The stale-serve shortcut is bounded by the ceiling, not just the lock.

        A follower that arrives while someone else holds the refresh lock is
        allowed to skip the queue and use cached keys -- but only inside the
        hard limit. Without this, the very outage that makes followers
        short-circuit would also be what lets them use keys past their
        lifetime.
        """
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=60, jwks_max_stale_seconds=30)
        await validator.ensure_jwks()
        validator._jwks_last_success_at = time.monotonic() - 31

        await validator._jwks_lock.acquire()
        try:
            with pytest.raises(AuthProviderUnavailable, match="maximum stale age"):
                await validator.ensure_jwks()
        finally:
            validator._jwks_lock.release()

    async def test_followers_serve_stale_without_queueing_behind_the_refresh(self, jwks_response) -> None:
        """The partial DoS this forbids: every login queued behind a dead IdP.

        One caller wins the refresh lock and blocks inside a request that
        never answers. Every other caller must return the keys already held
        rather than wait for it -- and must not open its own request.
        """
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0, jwks_failure_retry_seconds=300)
        await validator.ensure_jwks()

        release = asyncio.Event()
        reached = asyncio.Event()

        class _HangingTransport(httpx.AsyncBaseTransport):
            """Answers no request until the test releases it."""

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                idp.requests += 1
                reached.set()
                await release.wait()
                raise httpx.ConnectError("IdP is down")

        validator._transport = _HangingTransport()
        idp.requests = 0

        winner = asyncio.create_task(validator.ensure_jwks())
        await asyncio.wait_for(reached.wait(), timeout=2.0)

        followers = [asyncio.create_task(validator.ensure_jwks()) for _ in range(5)]
        done, pending = await asyncio.wait(followers, timeout=1.0)
        try:
            assert not winner.done(), "the winner should still be blocked in its request"
            assert len(done) == 5, (
                f"{len(pending)} of 5 followers are still queued on the refresh lock during an IdP "
                "outage; serving the held keys must not be gated by that lock"
            )
            assert idp.requests == 1, f"followers opened their own requests (total {idp.requests})"
        finally:
            release.set()
            await asyncio.gather(winner, *followers, return_exceptions=True)


class TestProgrammerBugsAreNotLaunderedAsOutages:
    """Our defects must not hide inside someone else's outage.

    The fetch block's ``except`` narrowly names IdP failures. A bug raised
    inside it propagates; only a genuine transport or payload failure falls
    back to the cache. Without this, a ``KeyError`` in our own code reads as
    "the IdP is down" and is served stale keys forever.
    """

    @pytest.mark.parametrize(
        "bug",
        [AttributeError("bug"), TypeError("bug"), KeyError("bug")],
        ids=["attribute-error", "type-error", "key-error"],
    )
    async def test_a_bug_inside_the_fetch_propagates(self, jwks_response, bug: BaseException) -> None:
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        await validator.ensure_jwks()
        idp.failure = bug

        with pytest.raises(type(bug)):
            await validator.ensure_jwks()

    @pytest.mark.parametrize(
        "outage",
        [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.InvalidURL("bad url")],
        ids=["connect-error", "read-timeout", "invalid-url"],
    )
    async def test_a_genuine_idp_failure_still_serves_stale(self, jwks_response, outage: BaseException) -> None:
        """``InvalidURL`` is NOT an ``HTTPError`` subclass; it is still theirs."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        good = await validator.ensure_jwks()
        idp.failure = outage

        assert await validator.ensure_jwks() == good

    async def test_a_malformed_body_still_serves_stale(self, jwks_response) -> None:
        """A ``JSONDecodeError`` is a payload failure, not a bug of ours."""
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        good = await validator.ensure_jwks()
        idp.body = "{not json"

        assert await validator.ensure_jwks() == good


class TestFailureLogsAreRedacted:
    """A JWKS failure log names the exception class, never its message."""

    async def test_a_stale_fallback_logs_the_class_without_the_message(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        validator = _validator(idp, jwks_cache_ttl_seconds=0)
        await validator.ensure_jwks()
        idp.failure = httpx.ConnectError("connection refused to secret-idp.internal:8443")

        with capture_logs() as logs:
            await validator.ensure_jwks()

        rendered = repr(logs)
        assert "ConnectError" in rendered
        assert "secret-idp.internal" not in rendered

    async def test_a_cold_start_failure_logs_the_class_without_the_message(self, jwks_response) -> None:
        idp = _Idp(jwks_response)
        idp.failure = httpx.ConnectError("connection refused to secret-idp.internal:8443")
        validator = _validator(idp)

        with capture_logs() as logs, pytest.raises(AuthProviderUnavailable):
            await validator.ensure_jwks()

        rendered = repr(logs)
        assert "ConnectError" in rendered
        assert "secret-idp.internal" not in rendered


class TestDefaultsAreSafe:
    """Defaults decide the behaviour of every deployment that says nothing."""

    async def test_the_failure_retry_default_is_five_minutes(self, jwks_response) -> None:
        """Lower values amplify the per-retry partial DoS (elspeth-32982f17cf)."""
        assert _validator(_Idp(jwks_response))._jwks_failure_retry_seconds == 300

    async def test_the_stale_ceiling_default_is_finite(self, jwks_response) -> None:
        """A default of "forever" would make cached keys unrevokable."""
        ceiling = _validator(_Idp(jwks_response))._jwks_max_stale_seconds
        assert 0 < ceiling < float("inf")


class TestSigningKeyRotation:
    """An unknown key id triggers ONE shared, bounded refresh."""

    async def test_an_unknown_key_id_refreshes_once_and_then_fails_closed(self, rsa_keypair) -> None:
        """A token no key can verify must not refresh the cache repeatedly."""
        old_private_key, old_public_key = rsa_keypair
        stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        idp = _Idp(_jwks_with_kid(old_public_key, "old-key"))
        validator = _validator(idp)

        await _decode(validator, _token_with_kid(old_private_key, "old-key"))
        idp.requests = 0

        with pytest.raises(AuthenticationError):
            await _decode(validator, _token_with_kid(stranger, "never-issued"))

        assert idp.requests == 1, "an unverifiable token must cost at most one refresh"

    async def test_a_rotated_key_is_picked_up_by_the_refresh(self, rsa_keypair) -> None:
        old_private_key, old_public_key = rsa_keypair
        new_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        idp = _Idp(_jwks_with_kid(old_public_key, "old-key"))
        validator = _validator(idp)

        await _decode(validator, _token_with_kid(old_private_key, "old-key"))
        idp.document = _jwks_with_kid(new_private_key.public_key(), "rotated-key")

        claims = await _decode(validator, _token_with_kid(new_private_key, "rotated-key"))
        assert claims.subject == "user-123"

    async def test_concurrent_misses_share_one_refresh(self, rsa_keypair) -> None:
        """Six logins arriving mid-rotation cost ONE fetch between them."""
        old_private_key, old_public_key = rsa_keypair
        new_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        idp = _Idp(_jwks_with_kid(old_public_key, "old-key"))
        validator = _validator(idp)

        await _decode(validator, _token_with_kid(old_private_key, "old-key"))
        idp.document = _jwks_with_kid(new_private_key.public_key(), "rotated-key")
        idp.requests = 0

        rotated_token = _token_with_kid(new_private_key, "rotated-key")
        results = await asyncio.gather(*(_decode(validator, rotated_token) for _ in range(6)))

        assert [claims.subject for claims in results] == ["user-123"] * 6
        assert idp.requests == 1, f"six concurrent key-miss refreshes cost {idp.requests} fetches; they must share one"


class TestTheHappyPathIsReachable:
    """A positive control: refusal tests prove nothing if nothing passes."""

    async def test_a_well_formed_token_decodes_against_a_fetched_jwks(self, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        idp = _Idp(build_rsa_jwk(public_key))
        claims = await _decode(_validator(idp), make_rs256_token(private_key, _claims()))
        assert claims.subject == "user-123"
        assert idp.requests == 1
