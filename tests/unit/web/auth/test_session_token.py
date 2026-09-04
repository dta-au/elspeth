"""Session-token minting, verification, and the refresh chain.

Written adversarially: each test names a forgery or an escape and asserts it
is refused. A suite of refusals proves nothing without a positive control, so
:func:`test_a_well_formed_token_round_trips` is the first test in the file --
if it ever fails, every refusal below is passing for the wrong reason.
"""

from __future__ import annotations

import time

import jwt
import pytest

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.session_token import LOCAL_AUDIENCE, SessionTokenIssuer

_KEY = b"a" * 32
_OTHER_KEY = b"b" * 32
_IDENTITY = "3f2a6c1e-0000-4000-8000-000000000001"


def _issuer(
    *,
    key: bytes = _KEY,
    provider: str = "local",
    audience: str = LOCAL_AUDIENCE,
    expiry_hours: int = 24,
    chain_hours: int = 168,
    active: bool = True,
) -> SessionTokenIssuer:
    return SessionTokenIssuer(
        signing_key=key,
        provider=provider,  # type: ignore[arg-type]
        audience=audience,
        token_expiry_hours=expiry_hours,
        max_refresh_chain_hours=chain_hours,
        principal_is_active=lambda _identity_id: active,
    )


# --------------------------------------------------------------------------
# Positive control.
# --------------------------------------------------------------------------


def test_a_well_formed_token_round_trips() -> None:
    """The control. Everything below asserts a refusal; this asserts success."""
    issuer = _issuer()
    token = issuer.mint(identity_id=_IDENTITY, username="ada")
    claims = issuer.authenticate(token)

    assert claims.identity_id == _IDENTITY
    assert claims.username == "ada"
    assert claims.provider == "local"
    assert claims.expires_at > claims.issued_at


def test_sub_carries_the_identity_id_not_the_username() -> None:
    """``sub`` is the ownership key every row points at."""
    issuer = _issuer()
    token = issuer.mint(identity_id=_IDENTITY, username="ada")
    raw = jwt.decode(token, _KEY, algorithms=["HS256"], audience=LOCAL_AUDIENCE, issuer="elspeth")

    assert raw["sub"] == _IDENTITY
    assert raw["username"] == "ada"


def test_each_token_gets_a_distinct_jti() -> None:
    """A denylist needs something to name; a constant jti would name every token."""
    issuer = _issuer()
    first = issuer.authenticate(issuer.mint(identity_id=_IDENTITY, username="ada"))
    second = issuer.authenticate(issuer.mint(identity_id=_IDENTITY, username="ada"))

    assert first.token_id != second.token_id


# --------------------------------------------------------------------------
# Forgery and cross-deployment escapes.
# --------------------------------------------------------------------------


def test_a_token_signed_with_another_key_is_refused() -> None:
    forged = _issuer(key=_OTHER_KEY).mint(identity_id=_IDENTITY, username="ada")
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(forged)


def test_an_unsigned_none_algorithm_token_is_refused() -> None:
    """The classic JWT forgery: claim the token needs no signature."""
    forged = jwt.encode(
        {
            "sub": _IDENTITY,
            "username": "ada",
            "provider": "local",
            "iss": "elspeth",
            "aud": LOCAL_AUDIENCE,
            "jti": "forged",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(forged)


def test_a_token_for_another_deployment_is_refused() -> None:
    """Two deployments sharing a secret must not accept each other's tokens."""
    other_deployment = _issuer(audience="https://elsewhere.example.com")
    token = other_deployment.mint(identity_id=_IDENTITY, username="ada")
    with pytest.raises(AuthenticationError):
        _issuer(audience="https://here.example.com").authenticate(token)


def test_a_token_from_a_different_provider_is_refused() -> None:
    """Subjects from two IdPs are unrelated namespaces.

    Honouring this would let a token minted while the deployment ran one IdP
    bind to a same-named identity under another.
    """
    entra_token = _issuer(provider="entra").mint(identity_id=_IDENTITY, username="ada")
    with pytest.raises(AuthenticationError, match="different authentication provider"):
        _issuer(provider="local").authenticate(entra_token)


def test_a_token_with_a_foreign_issuer_is_refused() -> None:
    forged = jwt.encode(
        {
            "sub": _IDENTITY,
            "username": "ada",
            "provider": "local",
            "iss": "not-elspeth",
            "aud": LOCAL_AUDIENCE,
            "jti": "forged",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        },
        _KEY,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(forged)


@pytest.mark.parametrize("missing", ["sub", "jti", "iat", "exp"])
def test_a_token_missing_a_required_claim_is_refused(missing: str) -> None:
    payload = {
        "sub": _IDENTITY,
        "username": "ada",
        "provider": "local",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "present",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    del payload[missing]
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(jwt.encode(payload, _KEY, algorithm="HS256"))


def test_a_token_with_no_provider_claim_is_refused() -> None:
    """A pre-extraction token has no ``provider``; it must not authenticate."""
    payload = {
        "sub": _IDENTITY,
        "username": "ada",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "legacy",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(jwt.encode(payload, _KEY, algorithm="HS256"))


def test_an_expired_token_is_refused() -> None:
    past = int(time.time()) - 7200
    payload = {
        "sub": _IDENTITY,
        "username": "ada",
        "provider": "local",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "stale",
        "iat": past,
        "exp": past + 60,
    }
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(jwt.encode(payload, _KEY, algorithm="HS256"))


@pytest.mark.parametrize("blank", ["", "   ", "​"])
def test_a_blank_subject_is_refused(blank: str) -> None:
    """A blank ``sub`` would authenticate as no identity at all."""
    payload = {
        "sub": blank,
        "username": "ada",
        "provider": "local",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "blank-sub",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(jwt.encode(payload, _KEY, algorithm="HS256"))


def test_a_boolean_iat_is_refused() -> None:
    """``bool`` is an ``int`` subclass; ``True`` must not read as epoch 1."""
    payload = {
        "sub": _IDENTITY,
        "username": "ada",
        "provider": "local",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "bool-iat",
        "iat": True,
        "exp": int(time.time()) + 3600,
    }
    with pytest.raises(AuthenticationError):
        _issuer().authenticate(jwt.encode(payload, _KEY, algorithm="HS256"))


# --------------------------------------------------------------------------
# Disable reach.
# --------------------------------------------------------------------------


def test_a_disabled_identity_cannot_authenticate() -> None:
    minted = _issuer(active=True).mint(identity_id=_IDENTITY, username="ada")
    with pytest.raises(AuthenticationError):
        _issuer(active=False).authenticate(minted)


def test_a_disabled_identity_cannot_refresh() -> None:
    """The revocation hole this module exists to close.

    Refresh carries the original ``iat`` forward, so without this check a
    token issued before a disable stays renewable for the whole chain window.
    """
    minted = _issuer(active=True).mint(identity_id=_IDENTITY, username="ada")
    with pytest.raises(AuthenticationError):
        _issuer(active=False).refresh(minted)


def test_decode_does_not_consult_the_identity_store() -> None:
    """``decode`` answers "what does this say", not "may they act"."""

    def _explode(_identity_id: str) -> bool:
        raise AssertionError("decode must not read the identity store")

    issuer = SessionTokenIssuer(
        signing_key=_KEY,
        provider="local",
        audience=LOCAL_AUDIENCE,
        token_expiry_hours=24,
        max_refresh_chain_hours=168,
        principal_is_active=_explode,
    )
    token = issuer.mint(identity_id=_IDENTITY, username="ada")
    assert issuer.decode(token).identity_id == _IDENTITY


# --------------------------------------------------------------------------
# Refresh chain.
# --------------------------------------------------------------------------


def test_refresh_carries_the_original_issue_time_forward() -> None:
    """Otherwise the chain never ages and a stolen token renews forever."""
    issuer = _issuer()
    original = issuer.mint(identity_id=_IDENTITY, username="ada")
    original_iat = issuer.decode(original).issued_at

    renewed = issuer.refresh(original)
    assert issuer.decode(renewed).issued_at == original_iat


def test_expiry_tracks_the_refresh_while_iat_tracks_the_chain() -> None:
    """The two clocks in a refresh chain move independently.

    ``iat`` is the chain's age and must NOT advance, or the chain never
    expires. ``exp`` is this token's own life and must be measured from the
    refresh, or a renewed token would arrive already stale. Asserting
    ``renewed.exp > original.exp`` would be wrong: both are ``now + expiry``,
    so within one second they are equal by design.
    """
    issuer = _issuer(expiry_hours=24)
    chain_started = int(time.time()) - 20 * 3600
    original = issuer.mint(identity_id=_IDENTITY, username="ada", issued_at=chain_started)

    renewed = issuer.decode(issuer.refresh(original))

    assert renewed.issued_at == chain_started
    # A full expiry window ahead of the REFRESH, not of the chain start.
    assert renewed.expires_at - int(time.time()) > 23 * 3600
    assert renewed.expires_at > chain_started + 24 * 3600


def test_a_chain_older_than_the_bound_is_refused() -> None:
    issuer = _issuer(chain_hours=1)
    aged = issuer.mint(identity_id=_IDENTITY, username="ada", issued_at=int(time.time()) - 7200)
    with pytest.raises(AuthenticationError, match="refresh chain expired"):
        issuer.refresh(aged)


def test_a_chain_inside_the_bound_is_renewed() -> None:
    """The boundary's other side — a bound that refused everything would pass."""
    issuer = _issuer(chain_hours=4)
    recent = issuer.mint(identity_id=_IDENTITY, username="ada", issued_at=int(time.time()) - 3600)
    assert issuer.decode(issuer.refresh(recent)).identity_id == _IDENTITY


# --------------------------------------------------------------------------
# Construction.
# --------------------------------------------------------------------------


def test_a_short_signing_key_is_refused() -> None:
    """HS256 with a weak key is forgeable; refuse at construction, not use."""
    with pytest.raises(ValueError, match="at least 32 bytes"):
        _issuer(key=b"tooshort")


def test_a_blank_audience_is_refused() -> None:
    with pytest.raises(ValueError, match="non-blank"):
        _issuer(audience="   ")


@pytest.mark.parametrize("hours", [0, -1])
def test_a_nonpositive_expiry_is_refused(hours: int) -> None:
    """A zero expiry mints tokens that are already expired."""
    with pytest.raises(ValueError, match="token_expiry_hours"):
        _issuer(expiry_hours=hours)


@pytest.mark.parametrize("hours", [0, -1])
def test_a_nonpositive_chain_bound_is_refused(hours: int) -> None:
    with pytest.raises(ValueError, match="max_refresh_chain_hours"):
        _issuer(chain_hours=hours)


@pytest.mark.parametrize("blank", ["", "   "])
def test_minting_without_an_identity_is_refused(blank: str) -> None:
    with pytest.raises(AuthenticationError, match="without an identity"):
        _issuer().mint(identity_id=blank, username="ada")


@pytest.mark.parametrize("blank", ["", "   "])
def test_minting_without_a_username_is_refused(blank: str) -> None:
    with pytest.raises(AuthenticationError, match="without a username"):
        _issuer().mint(identity_id=_IDENTITY, username=blank)
