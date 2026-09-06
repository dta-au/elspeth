"""The sealed SSO transaction cookie.

This cookie is the whole of the login walk's server-side memory: the state
the callback compares, the nonce that binds the ID token, and the PKCE
verifier. It lives in a browser, so it is attacker-reachable by definition,
and everything below is written from that assumption.

The positive control comes first. A file of refusals proves nothing if
sealing and opening never worked — every "rejected" would be trivially true.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from elspeth.web.auth.sso import (
    COOKIE_MAX_AGE_SECONDS,
    COOKIE_NAME,
    SSO_FAILURE_CATEGORIES,
    SsoCookieInvalid,
    SsoHandoffInvalid,
    SsoLoginError,
    SsoStateMismatch,
    cookie_attributes,
    new_transaction,
    open_transaction,
    pkce_challenge,
    seal_transaction,
)

_SECRET = "an-operator-transaction-secret-of-adequate-length-0123456789"
_OTHER_SECRET = _SECRET + "-rotated"
_PROVIDER = "vanguard"
_REDIRECT = "https://elspeth.example.gov.au/api/auth/sso/callback"


def _sealed(txn=None, *, secret=_SECRET, provider=_PROVIDER, redirect=_REDIRECT) -> str:
    return seal_transaction(txn or new_transaction(), secret=secret, provider=provider, redirect_uri=redirect)


def _open(value: str, *, secret=_SECRET, provider=_PROVIDER, redirect=_REDIRECT, now=None):
    return open_transaction(value, secret=secret, provider=provider, redirect_uri=redirect, now=now)


# --------------------------------------------------------------------------
# Positive control.
# --------------------------------------------------------------------------


def test_a_sealed_transaction_opens_to_exactly_what_was_sealed() -> None:
    original = new_transaction()
    reopened = _open(_sealed(original))

    assert reopened.state == original.state
    assert reopened.nonce == original.nonce
    assert reopened.verifier == original.verifier
    assert reopened.issued_at == original.issued_at


def test_each_transaction_is_independent() -> None:
    """Reused state or nonce across logins would defeat both of their jobs."""
    first, second = new_transaction(), new_transaction()
    assert {first.state, first.nonce, first.verifier} & {second.state, second.nonce, second.verifier} == set()


def test_the_sealed_value_reveals_nothing_in_plaintext() -> None:
    """It is encrypted, not merely signed — the verifier must not be readable."""
    txn = new_transaction()
    sealed = _sealed(txn)
    assert txn.verifier not in sealed
    assert txn.state not in sealed
    raw = base64.urlsafe_b64decode(sealed)
    assert txn.verifier.encode() not in raw
    assert b"verifier" not in raw


# --------------------------------------------------------------------------
# Tampering. Every one of these must be SsoCookieInvalid and nothing else.
# --------------------------------------------------------------------------


def test_a_flipped_ciphertext_byte_is_refused() -> None:
    raw = bytearray(base64.urlsafe_b64decode(_sealed()))
    raw[-1] ^= 0x01
    with pytest.raises(SsoCookieInvalid):
        _open(base64.urlsafe_b64encode(bytes(raw)).decode())


def test_a_flipped_nonce_byte_is_refused() -> None:
    raw = bytearray(base64.urlsafe_b64decode(_sealed()))
    raw[0] ^= 0x01
    with pytest.raises(SsoCookieInvalid):
        _open(base64.urlsafe_b64encode(bytes(raw)).decode())


def test_a_truncated_cookie_is_refused() -> None:
    raw = base64.urlsafe_b64decode(_sealed())
    with pytest.raises(SsoCookieInvalid):
        _open(base64.urlsafe_b64encode(raw[:-4]).decode())


def test_a_cookie_shorter_than_its_nonce_is_refused() -> None:
    """The length guard: without it this indexes into nothing and raises oddly."""
    with pytest.raises(SsoCookieInvalid):
        _open(base64.urlsafe_b64encode(b"tiny").decode())


def test_a_non_base64_cookie_is_refused() -> None:
    with pytest.raises(SsoCookieInvalid):
        _open("this is not base64 at all !!!")


def test_an_empty_cookie_is_refused() -> None:
    with pytest.raises(SsoCookieInvalid):
        _open("")


def test_a_cookie_sealed_with_another_key_is_refused() -> None:
    """Rotating sso_transaction_secret must invalidate outstanding cookies."""
    with pytest.raises(SsoCookieInvalid):
        _open(_sealed(secret=_OTHER_SECRET))


def test_a_forged_plaintext_cannot_be_substituted() -> None:
    """The attack the AEAD exists to stop: attacker-chosen state and nonce.

    If this were signed-but-not-authenticated, or authenticated with a key an
    attacker could reach, they would choose the state the callback compares
    against — and a stateless transaction has nothing else to fall back on.
    """
    forged = json.dumps({"v": 1, "state": "chosen", "nonce": "chosen", "verifier": "chosen", "iat": int(time.time())})
    with pytest.raises(SsoCookieInvalid):
        _open(base64.urlsafe_b64encode(b"\x00" * 12 + forged.encode()).decode())


# --------------------------------------------------------------------------
# Binding: a cookie is for ONE provider and ONE redirect URI.
# --------------------------------------------------------------------------


def test_a_cookie_sealed_for_another_provider_is_refused() -> None:
    with pytest.raises(SsoCookieInvalid):
        _open(_sealed(provider="google"), provider="vanguard")


def test_a_cookie_sealed_for_another_redirect_uri_is_refused() -> None:
    """A staging cookie must not open against production, even sharing a secret."""
    with pytest.raises(SsoCookieInvalid):
        _open(_sealed(redirect="https://staging.example.gov.au/api/auth/sso/callback"))


# --------------------------------------------------------------------------
# Time bounds, both directions.
# --------------------------------------------------------------------------


def test_a_cookie_older_than_its_lifetime_is_refused() -> None:
    now = int(time.time())
    sealed = _sealed(new_transaction(now=now - COOKIE_MAX_AGE_SECONDS - 1))
    with pytest.raises(SsoCookieInvalid):
        _open(sealed, now=now)


def test_a_cookie_inside_its_lifetime_is_accepted() -> None:
    """The boundary's other side — an expiry that refused everything would pass."""
    now = int(time.time())
    sealed = _sealed(new_transaction(now=now - COOKIE_MAX_AGE_SECONDS + 5))
    assert _open(sealed, now=now).issued_at == now - COOKIE_MAX_AGE_SECONDS + 5


def test_a_cookie_from_the_future_is_refused() -> None:
    """A backwards clock, or a cookie sealed by a host running ahead.

    Without this bound, a far-future ``iat`` would never age out and the
    five-minute window would be indefinite.
    """
    now = int(time.time())
    with pytest.raises(SsoCookieInvalid):
        _open(_sealed(new_transaction(now=now + 3600)), now=now)


def test_small_forward_skew_is_tolerated() -> None:
    """Real clocks disagree by a little; refusing that would fail real logins."""
    now = int(time.time())
    assert _open(_sealed(new_transaction(now=now + 5)), now=now) is not None


# --------------------------------------------------------------------------
# Cookie attributes — each one load-bearing.
# --------------------------------------------------------------------------


def test_the_cookie_name_carries_the_host_prefix() -> None:
    """__Host- is browser-ENFORCED: a sibling subdomain cannot overwrite it."""
    assert COOKIE_NAME.startswith("__Host-")


def test_secure_is_unconditional() -> None:
    """uvicorn sees the INTERNAL hop, so request scheme is not trustworthy."""
    assert cookie_attributes("v")["secure"] is True
    assert cookie_attributes(None)["secure"] is True


def test_the_cookie_is_httponly_and_lax() -> None:
    attributes = cookie_attributes("v")
    assert attributes["httponly"] is True
    # Lax, not Strict: the IdP returns via a top-level GET and Strict would
    # withhold the cookie on exactly that navigation, breaking every login.
    assert attributes["samesite"] == "lax"


def test_host_prefix_requirements_are_actually_met() -> None:
    """__Host- is void unless Secure, Path=/ and NO Domain all hold."""
    attributes = cookie_attributes("v")
    assert attributes["secure"] is True
    assert attributes["path"] == "/"
    assert "domain" not in attributes


def test_clearing_repeats_every_matching_attribute() -> None:
    """A clear that omits path silently leaves the cookie in place."""
    setting, clearing = cookie_attributes("v"), cookie_attributes(None)
    assert clearing["max_age"] == 0
    assert clearing["value"] == ""
    for attribute in ("key", "path", "secure", "httponly", "samesite"):
        assert clearing[attribute] == setting[attribute], attribute


# --------------------------------------------------------------------------
# PKCE.
# --------------------------------------------------------------------------


def test_the_challenge_is_s256_and_not_the_verifier() -> None:
    """With ``plain`` the verifier would travel in the URL the browser follows."""
    import base64 as b64
    import hashlib

    verifier = new_transaction().verifier
    challenge = pkce_challenge(verifier)

    assert challenge != verifier
    expected = b64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge, "base64url in a URL must be unpadded"


# --------------------------------------------------------------------------
# The failure taxonomy.
# --------------------------------------------------------------------------


def test_every_failure_category_is_distinct() -> None:
    """Two categories collapsing would merge two different diagnoses.

    Twelve is the spec's count (§Failure categories [rev2]): eleven ``sso_*``
    refusals plus ``provider_unavailable``.
    """
    assert len(SSO_FAILURE_CATEGORIES) == 12


def test_categories_are_carried_by_TYPE_not_message_prefix() -> None:
    """The rule 08e51563b established: rewording a message must not reclassify."""
    assert SsoStateMismatch("a completely different sentence").category == "sso_state_mismatch"
    assert SsoHandoffInvalid("reworded for the user").category == "sso_handoff_invalid"


def test_every_sso_failure_is_an_authentication_error() -> None:
    """The middleware maps AuthenticationError to 401; a stray type would 500."""
    from elspeth.web.auth.models import AuthenticationError

    assert issubclass(SsoLoginError, AuthenticationError)
    for error in (SsoStateMismatch, SsoHandoffInvalid, SsoCookieInvalid):
        assert issubclass(error, AuthenticationError)


def test_no_category_leaks_idp_supplied_text() -> None:
    """error_description is attacker-influenced and must never be a category.

    Every refusal is ``sso_``-prefixed. The single admitted exception is the
    spec-named ``provider_unavailable``, which is not a refusal at all — it is
    the 503 path — and is a literal in this module, not anything an IdP sent.
    """
    assert SSO_FAILURE_CATEGORIES - {"provider_unavailable"} == {c for c in SSO_FAILURE_CATEGORIES if c.startswith("sso_")}
    assert "provider_unavailable" in SSO_FAILURE_CATEGORIES
