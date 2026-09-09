"""Adversarial tests for the SSO ID-token decode path.

Spec: docs/specs/2026-09-02-pluggable-sso-design.md §ID-token validation.

Everything here is an attack the decode path must refuse. A valid signature
proves the token was minted by a key in the IdP's JWKS; it proves nothing
about WHICH algorithm was intended, which relying party it was for, or which
login attempt asked for it. Those are the gaps below.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest

from elspeth.web.auth.id_token import JwkSet, JWKSTokenValidator, parse_id_token_claims
from elspeth.web.auth.models import AuthenticationError
from tests.unit.web.auth.conftest import build_rsa_jwk, make_rsa_token

ISSUER = "https://issuer.example.gov.au"
AUDIENCE = "elspeth-client"
NONCE = "nonce-from-the-sealed-cookie"
# Required since step E: the validator no longer discovers a key URL for
# itself, so every one is built with the jwks_uri sso_wiring already resolved
# and origin-checked. Nothing in this file fetches it — each test hands
# ``decode_id_token`` a JwkSet it built through the JWKS boundary directly —
# but the constructor will not produce a validator that has not been told
# where its keys come from, and that refusal is the point.
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"


def _validator(algorithms: tuple[str, ...] = ("RS256",)) -> JWKSTokenValidator:
    return JWKSTokenValidator(issuer=ISSUER, audience=AUDIENCE, algorithms=algorithms, jwks_uri=JWKS_URI)


@pytest.fixture
def validator() -> JWKSTokenValidator:
    return _validator()


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


def _jwks(document: dict[str, Any]) -> JwkSet:
    """Through the ONE JWKS boundary, as production does."""
    return JWKSTokenValidator._validate_jwks_document(document)


def _beside_a_usable_key(jwks: dict[str, Any]) -> dict[str, Any]:
    """Append a second, unrelated RSA key so the set parses and the FIRST entry is what the token's kid selects."""
    from cryptography.hazmat.primitives.asymmetric import rsa as rsa_mod

    other = build_rsa_jwk(rsa_mod.generate_private_key(public_exponent=65537, key_size=2048).public_key())["keys"][0]
    other["kid"] = "an-unrelated-key"
    jwks["keys"].append(other)
    return jwks


def _decode(validator: JWKSTokenValidator, token: str, jwks: dict[str, Any], **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "audience": AUDIENCE,
        "nonce": NONCE,
        "client_id": AUDIENCE,
    }
    kwargs.update(overrides)
    return validator.decode_id_token(token, _jwks(jwks), **kwargs)


class TestTheHappyPathIsActuallyReachable:
    """A positive control. A refusal test proves nothing if nothing passes."""

    def test_a_well_formed_token_decodes(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims())
        payload = _decode(validator, token, build_rsa_jwk(public_key))
        assert payload.subject == "subject-1"


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

    def test_pinning_is_per_profile_not_global(self, rsa_keypair) -> None:
        """A validator BUILT for a PS256 profile accepts the same token.

        The pin is the tuple the validator was constructed with -- there is
        no per-call override to reach for, so a caller cannot widen it.
        """
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(), algorithm="PS256")
        payload = _decode(_validator(("PS256",)), token, build_rsa_jwk(public_key, alg="PS256"))
        assert payload.subject == "subject-1"

    @pytest.mark.parametrize("algorithms", [(), ("RS256", "HS256"), ("none",), ("HS256",), ("RS256", "")])
    def test_a_profile_cannot_declare_a_symmetric_or_absent_signature(self, algorithms: tuple[str, ...]) -> None:
        """A JWKS is public. An HMAC or ``none`` entry would make it the secret."""
        with pytest.raises(ValueError, match="algorithm"):
            _validator(algorithms)

    def test_the_pin_must_be_a_tuple(self) -> None:
        not_a_tuple: Any = ["RS256"]
        with pytest.raises(ValueError, match="tuple"):
            JWKSTokenValidator(issuer=ISSUER, audience=AUDIENCE, algorithms=not_a_tuple, jwks_uri=JWKS_URI)


class TestKeyTypeConfusion:
    def test_a_symmetric_jwk_entry_is_refused_before_its_key_is_used(self, validator, rsa_keypair) -> None:
        """A JWKS is public. An `oct` entry in one is a published secret.

        Refused on the key type itself, so the refusal does not depend on
        also having pinned the algorithm list correctly.
        """
        private_key, public_key = rsa_keypair
        jwks = _beside_a_usable_key(build_rsa_jwk(public_key))
        jwks["keys"][0]["kty"] = "oct"
        token = make_rsa_token(private_key, _claims())
        with pytest.raises(AuthenticationError, match="key type is not permitted"):
            _decode(validator, token, jwks)

    def test_a_key_with_no_type_at_all_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        jwks = _beside_a_usable_key(build_rsa_jwk(public_key))
        del jwks["keys"][0]["kty"]
        token = make_rsa_token(private_key, _claims())
        with pytest.raises(AuthenticationError, match="key type is not permitted"):
            _decode(validator, token, jwks)

    def test_a_document_with_no_usable_key_is_refused_at_the_boundary(self, validator, rsa_keypair) -> None:
        """Alone, the same malformed entry fails the DOCUMENT: PyJWT parses the set once, at the boundary."""
        private_key, public_key = rsa_keypair
        jwks = build_rsa_jwk(public_key)
        jwks["keys"][0]["kty"] = "oct"
        with pytest.raises(AuthenticationError, match="unusable key entries"):
            _decode(validator, make_rsa_token(private_key, _claims()), jwks)


class TestNonceBinding:
    """The nonce is the only thing tying a token to THIS login attempt."""

    def test_a_token_carrying_another_sessions_nonce_is_refused(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(nonce="a-different-login"))
        with pytest.raises(AuthenticationError, match="nonce check failed"):
            _decode(validator, token, build_rsa_jwk(public_key))

    def test_the_sso_path_refuses_to_run_without_a_nonce_to_compare(self, validator, rsa_keypair) -> None:
        """An empty expected nonce is a caller defect, refused before the token is read."""
        private_key, public_key = rsa_keypair
        token = make_rsa_token(private_key, _claims(nonce=""))
        with pytest.raises(ValueError, match="nonce"):
            _decode(validator, token, build_rsa_jwk(public_key), nonce="")

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
        assert _decode(validator, token, build_rsa_jwk(public_key)).subject == "subject-1"

    def test_a_single_audience_token_needs_no_azp(self, validator, rsa_keypair) -> None:
        private_key, public_key = rsa_keypair
        assert _decode(validator, make_rsa_token(private_key, _claims()), build_rsa_jwk(public_key)).subject


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
        assert _decode(validator, token, build_rsa_jwk(public_key)).subject == "subject-1"


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


# --------------------------------------------------------------------------
# Moved here with JWKSTokenValidator itself. The tests below are the
# CONTRACTUAL anchors of the @trust_boundary decorators in
# ``elspeth/web/auth/id_token.py`` — the trust_boundary.tests rule resolves
# each ``test_ref`` nodeid and fails when it does not exist. Left in
# test_oidc_provider.py they would have been deleted along with the legacy
# provider, silently breaking an enforced gate on code that is staying.
# --------------------------------------------------------------------------


class TestJWKSValidatorBoundaryRaises:
    """Direct-call boundary test for the @trust_boundary-decorated JWKS validator.

    It invokes the validator with the malformed external value passed DIRECTLY
    as the decorator's ``source_param`` (no httpx mock indirection) so the
    trust_boundary.tests honesty gate can prove the raising invariant against
    the named parameter. The same shape failure is exercised end-to-end
    against a real provider in ``test_fake_idp.py``; this direct call pins the
    boundary contract at the function granularity the decorator attests.

    The discovery document is no longer one of these. Step E deleted
    ``_validate_discovery_document`` along with the validator's self-discovery
    path: sso_wiring resolves and origin-checks the jwks_uri, and the
    validator never reads a discovery document at all. That it does not is
    pinned in ``test_id_token_jwks_source.py``.
    """

    def test_validate_jwks_document_missing_keys_raises(self) -> None:
        """A JWKS document without a 'keys' list is rejected at the boundary."""
        with pytest.raises(AuthenticationError, match="missing 'keys' list"):
            JWKSTokenValidator._validate_jwks_document(jwks={"not_keys": []})


# --------------------------------------------------------------------------
# The two boundaries that turn PyJWT's dicts into owned values.
# --------------------------------------------------------------------------


class TestIdTokenClaimsBoundary:
    """``parse_id_token_claims`` is the ONE place a verified payload becomes owned claims.

    PyJWT has verified the signature, issuer, audience and clock by the time
    it runs; these tests are about the types of what it verified, and about
    the profile claims nothing before this point has looked at.
    """

    def test_a_blank_subject_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match="non-blank"):
            parse_id_token_claims(payload=_claims(sub="   "))

    def test_a_missing_subject_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match="missing the required claim 'sub'"):
            parse_id_token_claims(payload=_claims(sub=_ABSENT))

    def test_a_non_string_issuer_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match="'iss'"):
            parse_id_token_claims(payload=_claims(iss=["https://issuer.example.gov.au"]))

    @pytest.mark.parametrize(
        "aud",
        [None, "", 42, [], ["ok", 7], {"aud": "x"}],
        ids=["absent", "empty", "number", "empty-list", "mixed-list", "object"],
    )
    def test_an_audience_that_is_not_a_string_or_a_string_list_is_refused(self, aud: Any) -> None:
        with pytest.raises(AuthenticationError, match="aud"):
            parse_id_token_claims(payload=_claims(aud=aud))

    def test_the_audience_keeps_its_wire_shape(self) -> None:
        """A string stays a string and a list becomes a tuple: the azp rule turns on exactly that."""
        assert parse_id_token_claims(payload=_claims()).audience == AUDIENCE
        assert parse_id_token_claims(payload=_claims(aud=[AUDIENCE, "other"])).audience == (AUDIENCE, "other")

    @pytest.mark.parametrize("claim", ["exp", "iat"])
    @pytest.mark.parametrize("value", ["1700000000", True, None], ids=["string", "boolean", "absent"])
    def test_a_numeric_date_that_is_not_a_number_is_refused(self, claim: str, value: Any) -> None:
        with pytest.raises(AuthenticationError, match=claim):
            parse_id_token_claims(payload=_claims(**{claim: value}))

    def test_a_fractional_numeric_date_keeps_its_whole_seconds(self) -> None:
        claims = parse_id_token_claims(payload=_claims(iat=1_700_000_000.75))
        assert claims.issued_at == 1_700_000_000

    def test_a_compared_claim_that_is_not_a_string_reads_as_absent(self) -> None:
        """The comparison is the ONE authority for these two: a non-string cannot match, so it
        arrives there as absent and is refused there, with that check's message (``TestNonceBinding``)."""
        assert parse_id_token_claims(payload=_claims(nonce=12345)).nonce is None
        assert parse_id_token_claims(payload=_claims(nonce=_ABSENT)).nonce is None
        assert parse_id_token_claims(payload=_claims(nonce="value")).nonce == "value"
        assert parse_id_token_claims(payload=_claims(azp=12345)).authorized_party is None
        assert parse_id_token_claims(payload=_claims(azp=_ABSENT)).authorized_party is None
        assert parse_id_token_claims(payload=_claims(azp="value")).authorized_party == "value"

    def test_a_non_string_cosmetic_claim_reads_as_absent(self) -> None:
        """An IdP sending the wrong type must not deny access over a display name."""
        claims = parse_id_token_claims(payload=_claims(name=42, email=["a@b"], preferred_username="\u200b", tid=7))
        assert (claims.name, claims.email, claims.preferred_username, claims.tenant_id) == (None, None, None, None)

    @pytest.mark.parametrize("value", ["true", 1, "false", None], ids=["string-true", "one", "string-false", "absent"])
    def test_email_verified_is_true_only_for_the_json_boolean(self, value: Any) -> None:
        assert parse_id_token_claims(payload=_claims(email_verified=value)).email_verified is False
        assert parse_id_token_claims(payload=_claims(email_verified=True)).email_verified is True

    def test_the_closed_set_reads_every_profile_claim(self) -> None:
        payload = _claims(tid="tenant", hd="example.gov.au", given_name="Ada", family_name="Lovelace", abn="51 824 753 556")
        payload["cognito:username"] = "cog"
        claims = parse_id_token_claims(payload=payload)
        assert (claims.tenant_id, claims.hosted_domain, claims.cognito_username) == ("tenant", "example.gov.au", "cog")
        assert (claims.given_name, claims.family_name, claims.abn) == ("Ada", "Lovelace", "51 824 753 556")

    def test_a_group_claim_is_read_past_rather_than_carried(self) -> None:
        """The closed set has no group field, so a token carrying one still parses.

        Step E deleted ``groups``/``roles``/``groups_overage`` with the bearer
        path that consumed them (D17: IdP groups are organisation facts, not
        compartment facts). The risk in removing fields from a closed set is
        the opposite of the one they were added for: a token that carries the
        claim must still be a VALID token, or every Entra deployment whose
        app registration emits groups would stop being able to log in. So
        this pins that the claim is ignored, not refused — including the
        overage forms, which arrive as an ordinary unread claim now.
        """
        claims = parse_id_token_claims(payload=_claims(groups=["g1", 2], roles=["admin"], hasgroups=True))
        assert claims.subject == "subject-1"
        assert "groups" not in {declared.name for declared in dataclasses.fields(claims)}
        assert parse_id_token_claims(payload=_claims(groups="not-a-list")).subject == "subject-1"


class TestHeaderKeyIdBoundary:
    """``kid`` is read from the UNVERIFIED header, so its type is asserted before it selects a key."""

    def test_a_non_string_kid_is_refused(self) -> None:
        with pytest.raises(AuthenticationError, match="key id"):
            JWKSTokenValidator._header_key_id(header={"alg": "RS256", "kid": 7})

    def test_an_absent_kid_is_none_and_a_string_kid_is_itself(self) -> None:
        assert JWKSTokenValidator._header_key_id(header={"alg": "RS256"}) is None
        assert JWKSTokenValidator._header_key_id(header={"alg": "RS256", "kid": "k1"}) == "k1"


class TestJwkSetIsAnOwnedType:
    def test_the_boundary_reads_each_keys_kid_and_kty_once(self, rsa_keypair) -> None:
        _private_key, public_key = rsa_keypair
        jwks = _jwks(build_rsa_jwk(public_key))
        (entry,) = jwks.entries
        assert entry.key_type == "RSA" and entry.key_id == build_rsa_jwk(public_key)["keys"][0]["kid"]

    def test_equal_documents_give_equal_but_distinct_sets(self, rsa_keypair) -> None:
        """Value equality for tests; identity for the refresh path, which is why both are pinned."""
        _private_key, public_key = rsa_keypair
        first, second = _jwks(build_rsa_jwk(public_key)), _jwks(build_rsa_jwk(public_key))
        assert first == second and first is not second

    def test_an_entry_with_a_non_string_kid_is_not_carried(self, rsa_keypair) -> None:
        """No header can name it (RFC 7515: kid is a string), so it can never be selected."""
        _private_key, public_key = rsa_keypair
        document = build_rsa_jwk(public_key)
        document["keys"][0]["kid"] = 7
        assert _jwks(document).entries == ()
