"""Adversarial tests for the SSO ID-token decode path.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §ID-token validation.

Everything here is an attack the decode path must refuse. A valid signature
proves the token was minted by a key in the IdP's JWKS; it proves nothing
about WHICH algorithm was intended, which relying party it was for, or which
login attempt asked for it. Those are the gaps below.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from elspeth.web.auth.models import AuthenticationError
from elspeth.web.auth.oidc import JWKSTokenValidator
from tests.unit.web.auth.conftest import build_rsa_jwk, make_rsa_token

ISSUER = "https://issuer.example.gov.au"
AUDIENCE = "elspeth-client"
NONCE = "nonce-from-the-sealed-cookie"


@pytest.fixture
def validator() -> JWKSTokenValidator:
    return JWKSTokenValidator(issuer=ISSUER, audience=AUDIENCE)


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "subject-1",
        "exp": now + 300,
        "iat": now,
        "nonce": NONCE,
    }
    claims.update(overrides)
    return {key: value for key, value in claims.items() if value is not _ABSENT}


_ABSENT = object()


def _decode(validator: JWKSTokenValidator, token: str, jwks: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "algorithms": ("RS256",),
        "audience": AUDIENCE,
        "nonce": NONCE,
        "client_id": AUDIENCE,
    }
    kwargs.update(overrides)
    return validator.decode_id_token(token, jwks, **kwargs)


class TestTheHappyPathIsActuallyReachable:
    """A positive control. A refusal test proves nothing if nothing passes."""

    def test_a_well_formed_token_decodes(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims())
        payload = _decode(validator, token, build_rsa_jwk(public_key))
        assert payload["sub"] == "subject-1"


class TestAlgorithmConfusion:
    """The algorithm comes from the PROFILE, never from the token header."""

    def test_a_token_signed_with_an_unlisted_algorithm_is_refused(self, validator, rsa_keypair) -> None:
        """The defect the old path had: `algorithms=[token_alg]`.

        Reading the accepted algorithm out of the token means the attacker
        chooses which check their forgery faces.
        """
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(), algorithm="PS256")
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key, alg="PS256"))

    def test_pinning_is_per_profile_not_global(self, validator, rsa_keypair) -> None:
        """A profile that pinned PS256 would accept the same token."""
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(), algorithm="PS256")
        payload = _decode(validator, token, build_rsa_jwk(public_key, alg="PS256"), algorithms=("PS256",))
        assert payload["sub"] == "subject-1"


class TestKeyTypeConfusion:
    def test_a_symmetric_jwk_entry_is_refused_before_its_key_is_used(self, validator, rsa_keypair) -> None:
        """A JWKS is public. An `oct` entry in one is a published secret.

        Refused on the key type itself, so the refusal does not depend on
        also having pinned the algorithm list correctly.
        """
        private_key, public_key = rsa_keypair
        jwks = build_rsa_jwk(public_key)
        jwks["keys"][0]["kty"] = "oct"
        token = make_rsa_token(private_key, _claims())
        with pytest.raises(AuthenticationError, match="key type is not permitted"):
            _decode(validator, token, jwks)

    def test_a_key_with_no_type_at_all_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        jwks = build_rsa_jwk(public_key)
        del jwks["keys"][0]["kty"]
        token = make_rsa_token(private_key, _claims())
        with pytest.raises(AuthenticationError, match="key type is not permitted"):
            _decode(validator, token, jwks)


class TestNonceBinding:
    """The nonce is the only thing tying a token to THIS login attempt."""

    def test_a_token_carrying_another_sessions_nonce_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(nonce="a-different-login"))
        with pytest.raises(AuthenticationError, match="nonce check failed"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_token_with_no_nonce_is_refused(self, validator, rsa_keypair) -> None:
        """Required, not optional: absence must not skip the comparison."""
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(nonce=_ABSENT))
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_non_string_nonce_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(nonce=12345))
        with pytest.raises(AuthenticationError, match="nonce check failed"):
            _decode(validator, token, build_rsa_jwk(public_key))


class TestAudienceAndAuthorizedParty:
    def test_a_token_for_another_client_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(aud="some-other-app"))
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_multi_audience_token_needs_azp_naming_us(self, validator, rsa_keypair) -> None:
        """Our client id appearing in a list is not the same as being the party.

        Without this, a token minted for a different relying party that
        happens to list us as an audience would authenticate here.
        """
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(aud=[AUDIENCE, "some-other-app"], azp="some-other-app"))
        with pytest.raises(AuthenticationError, match="authorized party check failed"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_multi_audience_token_with_no_azp_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(aud=[AUDIENCE, "some-other-app"]))
        with pytest.raises(AuthenticationError, match="authorized party check failed"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_multi_audience_token_naming_us_passes(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(aud=[AUDIENCE, "some-other-app"], azp=AUDIENCE))
        assert _decode(validator, token, build_rsa_jwk(public_key))["sub"] == "subject-1"

    def test_a_single_audience_token_needs_no_azp(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        assert _decode(validator, make_rsa_token(private_key, _claims()), build_rsa_jwk(public_key))["sub"]


class TestRequiredClaimsAndClock:
    @pytest.mark.parametrize("claim", ["exp", "iat", "iss", "sub", "aud"])
    def test_every_required_claim_is_required(self, validator, rsa_keypair, claim: str) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(**{claim: _ABSENT}))
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_a_token_from_another_issuer_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(iss="https://evil.example"))
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_an_expired_token_is_refused_beyond_the_leeway(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        now = int(time.time())
        token = make_rsa_token(private_key, _claims(exp=now - 120, iat=now - 400))
        with pytest.raises(AuthenticationError, match="Invalid token"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_bounded_skew_is_tolerated(self, validator, rsa_keypair) -> None:
        """60s, so ordinary NTP drift does not deny a correct login.

        Bounded on purpose: a wider window extends a stolen token's usable
        life for no operational gain.
        """
        private_key, public_key = rsa_keypair
        now = int(time.time())
        token = make_rsa_token(private_key, _claims(exp=now - 30, iat=now - 300))
        assert _decode(validator, token, build_rsa_jwk(public_key))["sub"] == "subject-1"


class TestSignature:
    def test_a_token_signed_by_a_key_not_in_the_jwks_is_refused(self, validator, rsa_keypair) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

        _private_key, public_key = rsa_keypair
        attacker_key = rsa_mod.generate_private_key(public_exponent=65537, key_size=2048)
        token = make_rsa_token(attacker_key, _claims())
        with pytest.raises(AuthenticationError):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_an_error_never_echoes_the_token_or_a_claim_value(self, validator, rsa_keypair) -> None:
        """401 bodies must not become an oracle.

        PyJWT's own messages quote expected and received audiences; surfacing
        them tells an attacker exactly what to send next.
        """
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(aud="some-other-app"))
        with pytest.raises(AuthenticationError) as excinfo:
            _decode(validator, token, build_rsa_jwk(public_key))
        message = str(excinfo.value)
        assert "some-other-app" not in message
        assert token.split(".")[1] not in message
